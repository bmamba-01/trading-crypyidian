"""Unit test SIG-0: lưu số giao dịch và taker buy của kline, không ước lượng khi thiếu."""

from __future__ import annotations

import ast
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from tests.unit.test_binance_market_data_adapter import RecordingLimiter, build_adapter
from tests.unit.test_market_repository import FakeSession, bar_and_version

from trading_bot.adapters.binance_usdm.market_data import MarketDataContractError
from trading_bot.adapters.database.models.market_entities import MarketBar
from trading_bot.adapters.database.repositories.market_data import (
    MarketDataRepository,
    _to_domain_bar,
)
from trading_bot.application.binance_rest_backfill_ingestion import _bar_content_checksum

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations" / "versions"
MIGRATION = MIGRATIONS / "e3a5c7e9f1b4_sig0_market_bar_taker_flow.py"
FIXTURE = ROOT / "tests" / "fixtures" / "binance_usdm" / "klines.json"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sig0_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(step: str) -> list[str]:
    statements: list[str] = []
    module = _migration()
    module.op = SimpleNamespace(execute=lambda sql: statements.append(" ".join(str(sql).split())))
    getattr(module, step)()
    return statements


def _revision_graph() -> dict[str, Any]:
    graph: dict[str, Any] = {}
    for path in MIGRATIONS.glob("*.py"):
        values: dict[str, Any] = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.AnnAssign | ast.Assign):
                target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(node.value) if node.value else None
        if "revision" in values:
            graph[values["revision"]] = values.get("down_revision")
    return graph


def test_migration_is_the_single_head_after_previous_head() -> None:
    graph = _revision_graph()
    parents: set[str] = set()
    for parent in graph.values():
        if isinstance(parent, str):
            parents.add(parent)
        elif isinstance(parent, tuple | list):
            parents.update(parent)
    heads = [revision for revision in graph if revision not in parents]
    # RT-1 lát 2 nối migration mark fallback ngay sau SIG-0. Sau đó hai nhánh tách ra: dev (CK-1
    # bảng giá trực tiếp, CK-10 lịch kinh tế) và main (AI-3/AI-4); revision gộp CRY-88 nối lại
    # thành một head; CRY-384/CRY-380 nối analyst_run.context và run_report sau 7707d82de246.
    # WP06 nối migration caller EVALUATION của scorer TEST proof, rồi lát B (manifest v5).
    assert heads == ["d8e2f4a6b8c0"]
    assert graph["d8e2f4a6b8c0"] == "d4f6a8b0c2e1"
    assert graph["d4f6a8b0c2e1"] == "c9e1a3b5d7f9"
    assert graph["c9e1a3b5d7f9"] == "a5c7e9b1d3f5"
    assert graph["a5c7e9b1d3f5"] == "e5b7d9f1a3c6"  # AI-5 cohort sau CRY-428 lát 2
    assert graph["e5b7d9f1a3c6"] == "8f8ec8240b11"  # CRY-428 nguồn backtest đề xuất
    assert graph["8f8ec8240b11"] == "d4f6a8b0c2e4"  # CRY-430 signal_outcome
    assert graph["d4f6a8b0c2e4"] == "c2e4a6b8d0f1"
    assert graph["c2e4a6b8d0f1"] == "d8f1b3a5c7e9"  # FX-3 bundle replay funding (CRY-198)
    assert graph["d8f1b3a5c7e9"] == "f1b8e93fa47f"  # FX-3 funding/mark price (CRY-198)
    assert graph["f1b8e93fa47f"] == "60a62c705a89"  # TG-4 topic Telegram (CRY-424)
    assert graph["60a62c705a89"] == "ad99c4f520d0"
    assert graph["ad99c4f520d0"] == "a42edbd211e5"
    assert graph["a42edbd211e5"] == "7707d82de246"
    assert graph["7707d82de246"] == "c1fec3af85ea"  # bù bảng live market b4d6 (CRY-88)
    assert graph["c1fec3af85ea"] == ("645ed2998ab1", "b8d0f2a4c6e8")
    assert graph["b8d0f2a4c6e8"] == "a7c9e1b3d5f6"
    assert graph["a7c9e1b3d5f6"] == "f1b3d5e7a9c2"
    assert graph["f1b3d5e7a9c2"] == "e3a5c7e9f1b4"
    assert graph["e3a5c7e9f1b4"] == "d2e4f6a8b0c1"


def test_upgrade_adds_nullable_columns_with_bounds_and_downgrade_is_guarded() -> None:
    upgrade = _sql("upgrade")
    assert "ADD COLUMN trade_count BIGINT" in upgrade[0]
    assert "ADD COLUMN taker_buy_base_volume NUMERIC(38, 18)" in upgrade[0]
    assert "ADD COLUMN taker_buy_quote_volume NUMERIC(38, 18)" in upgrade[0]
    assert "NOT NULL" not in upgrade[0]
    assert "DEFAULT" not in upgrade[0]
    joined = " ".join(upgrade[1:])
    assert "taker_buy_base_volume <= volume" in joined
    assert "taker_buy_quote_volume <= quote_volume" in joined
    assert "trade_count >= 0" in joined

    downgrade = _sql("downgrade")
    assert (
        "RAISE EXCEPTION 'SIG0_MARKET_BAR_TAKER_FLOW_DOWNGRADE_REQUIRES_EMPTY_COLUMNS'"
        in (downgrade[0])
    )
    assert downgrade[-1].startswith("ALTER TABLE public.market_bar DROP COLUMN")


def test_orm_constraints_match_migration() -> None:
    # ORM dùng naming convention `ck_<bảng>_<tên>`; migration SQL thô dùng tên gốc như tiền lệ
    # `market_bar_quote_volume_nonnegative` (c9d7e5f3a1b2).
    names = {constraint.name for constraint in MarketBar.__table__.constraints}
    for name, _ in _migration().CONSTRAINTS:
        assert f"ck_market_bar_{name}" in names
    for column in ("trade_count", "taker_buy_base_volume", "taker_buy_quote_volume"):
        assert MarketBar.__table__.columns[column].nullable is True


@pytest.mark.asyncio
async def test_rest_kline_parser_keeps_trade_count_and_taker_buy_decimals() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=FIXTURE.read_bytes())

    async with build_adapter(handler, limiter=RecordingLimiter()) as adapter:
        page = await adapter.get_klines(
            symbol="BTCUSDT",
            interval="1m",
            start_time=datetime(2026, 10, 22, 12, 29, tzinfo=UTC),
            end_time=datetime(2026, 10, 22, 12, 30, tzinfo=UTC),
            limit=1000,
        )
    first = page.records[0]
    assert first.trade_count == 90
    assert first.taker_buy_base_volume == Decimal("5.12000000")
    assert first.taker_buy_quote_volume == Decimal("332800.00000000")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index", "value"),
    [(8, -1), (8, "1.5"), (8, True), (9, "99.0"), (10, "-1")],
)
async def test_rest_kline_parser_rejects_invalid_taker_fields(index: int, value: object) -> None:
    rows = json.loads(FIXTURE.read_text())
    rows[0][index] = value

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=rows)

    async with build_adapter(handler, limiter=RecordingLimiter()) as adapter:
        with pytest.raises(MarketDataContractError):
            await adapter.get_klines(
                symbol="BTCUSDT",
                interval="1m",
                start_time=datetime(2026, 10, 22, 12, 29, tzinfo=UTC),
                end_time=datetime(2026, 10, 22, 12, 30, tzinfo=UTC),
                limit=1000,
            )


def test_domain_bar_rejects_taker_volume_above_total() -> None:
    bar, _ = bar_and_version(quote_volume="1000")
    with pytest.raises(ValueError, match=r"taker_buy_base_volume phải nằm trong"):
        replace(bar, taker_buy_base_volume=Decimal("10.26"))
    with pytest.raises(ValueError, match=r"taker_buy_quote_volume phải nằm trong"):
        replace(bar, taker_buy_quote_volume=Decimal("1000.01"))
    with pytest.raises(ValueError, match="trade_count"):
        replace(bar, trade_count=-1)
    ok = replace(
        bar,
        trade_count=7,
        taker_buy_base_volume=Decimal("10.25"),
        taker_buy_quote_volume=Decimal("0"),
    )
    assert ok.taker_buy_base_volume == Decimal("10.25")


@pytest.mark.asyncio
async def test_repository_persists_and_reads_back_taker_fields_or_null() -> None:
    bar, version = bar_and_version(quote_volume="1025.125")
    bar = replace(
        bar,
        trade_count=42,
        taker_buy_base_volume=Decimal("6.5"),
        taker_buy_quote_volume=Decimal("650.1"),
    )
    session = FakeSession([None, None])
    repository = MarketDataRepository(session)  # type: ignore[arg-type]
    await repository.append_bar(
        workspace_id=uuid4(),
        instrument_id=uuid4(),
        bar=bar,
        version=version,
    )
    stored = session.added[-1]
    assert stored.trade_count == 42
    assert stored.taker_buy_base_volume == Decimal("6.5")
    assert stored.taker_buy_quote_volume == Decimal("650.1")
    restored = _to_domain_bar(stored)
    assert restored.taker_buy_base_volume == Decimal("6.5")

    legacy, legacy_version = bar_and_version()
    session = FakeSession([None, None])
    await MarketDataRepository(session).append_bar(  # type: ignore[arg-type]
        workspace_id=uuid4(),
        instrument_id=uuid4(),
        bar=legacy,
        version=legacy_version,
    )
    assert "taker_buy_base_volume" not in session.added[-1].__dict__
    assert _to_domain_bar(session.added[-1]).taker_buy_base_volume is None


def test_backfill_checksum_unchanged_for_bars_without_taker_fields() -> None:
    bar, _ = bar_and_version(quote_volume="1000")
    legacy = _bar_content_checksum(bar)
    # Không có trường taker → checksum giống hệt trước SIG-0 (khóa không được thêm vào payload).
    assert _bar_content_checksum(replace(bar)) == legacy
    with_taker = _bar_content_checksum(replace(bar, taker_buy_base_volume=Decimal("1")))
    assert with_taker != legacy
    assert _bar_content_checksum(replace(bar, trade_count=3)) != legacy


def test_wp07c_replay_job_downgrade_guard_still_present() -> None:
    """Guard WP07C không còn chạm tới được trong E2E backfill (guard license WP06 chặn trước)."""
    source = (MIGRATIONS / "f7a9c1e3d5b7_wp07c_backtest_replay_jobs.py").read_text()
    downgrade = source.split("def downgrade", 1)[1]
    assert "WP07C_REPLAY_JOB_DOWNGRADE_REQUIRES_EMPTY_TABLE" in downgrade
    assert downgrade.index("RAISE EXCEPTION") < downgrade.index("drop_table")


def test_incident_definer_owner_migration_moves_three_functions_off_admin() -> None:
    """Hàm SECURITY DEFINER WP06 incident không còn do user đăng nhập `crypidian_admin` sở hữu."""
    path = MIGRATIONS / "b2d4f6a8c0e3_wp06_incident_definer_owner.py"
    spec = importlib.util.spec_from_file_location("incident_owner", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(" ".join(str(sql).split())))
    module.upgrade()
    sql = "\n".join(statements)
    assert "CREATE ROLE crypidian_learning_incident_guard NOLOGIN NOSUPERUSER NOBYPASSRLS" in sql
    for function in module.OWNED_FUNCTIONS:
        assert f"ALTER FUNCTION public.{function} OWNER TO crypidian_learning_incident_guard" in sql
    for table in module.TABLES:
        assert f'ON public."{table}" FOR SELECT TO crypidian_learning_incident_guard' in sql
    assert "GRANT EXECUTE ON FUNCTION public.wp06_learning_incident_guard()" not in sql
