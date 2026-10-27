"""MB-05 Part B: migration siết fence DB (M3) và các ghim chính sách RLS (M2, M4)."""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from tests.unit.runtime_fakes import FakeUow, RecordingSession

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "migrations" / "versions"


def _load(name: str) -> ModuleType:
    path = next(VERSIONS.glob(f"{name}_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strict_fence_migration_chains_after_part_a_head() -> None:
    module = _load("e1f3a5c7b9d2")

    assert module.revision == "e1f3a5c7b9d2"
    assert module.down_revision == "e4f6a8c0d2b5"  # sau MB-04 equity aggregate
    parents = [_load(path.stem.split("_")[0]).down_revision for path in VERSIONS.glob("*.py")]
    # MB-07a nối đúng một revision sau (`f7b9d1e3a5c8`); chuỗi vẫn một head.
    assert parents.count("e1f3a5c7b9d2") == 1


def _executed(module: ModuleType, function: str) -> list[str]:
    statements: list[str] = []

    class Op:
        def execute(self, sql: str) -> None:
            statements.append(" ".join(str(sql).split()))

    original = module.op
    module.op = Op()
    try:
        getattr(module, function)()
    finally:
        module.op = original
    return statements


def test_checkpoint_null_token_only_allowed_for_genesis_revision_one() -> None:
    sql = " ".join(_executed(_load("e1f3a5c7b9d2"), "upgrade"))

    assert "genesis_null" in sql
    assert "NEW.revision = 1" in sql
    assert "transitional_null" not in sql.split("CREATE TRIGGER")[-1]
    assert "SECURITY INVOKER" in sql
    assert "search_path = pg_catalog, pg_temp" in sql
    assert 'CREATE TRIGGER "paper_execution_checkpoint_fence"' in sql
    assert "mb05_require_runtime_fence('genesis_null')" in sql
    # State/incident vẫn strict (không nhận NULL token).
    assert "MB05_FENCING_TOKEN_REQUIRED" in sql
    assert "REVOKE ALL ON FUNCTION public.mb05_require_runtime_fence() FROM PUBLIC" in sql


def test_downgrade_restores_part_a_transitional_trigger() -> None:
    sql = " ".join(_executed(_load("e1f3a5c7b9d2"), "downgrade"))

    assert "mb05_require_runtime_fence('transitional_null')" in sql


def _normalize(policy: str, table: str) -> str:
    return re.sub(r"\s+", " ", policy.replace(table, "<table>")).strip()


def test_checkpoint_write_policies_match_lease_read_policies() -> None:
    """M4: ai ghi được checkpoint của bot X cũng thấy hàng lease của bot X.

    Trigger fence là SECURITY INVOKER và đọc `bot_runtime_lease` dưới RLS của chính writer; nếu
    policy đọc lease hẹp hơn policy ghi checkpoint, writer có thể thấy "chưa từng có lease" và
    lọt nhánh NULL. Ghim: predicate runtime và admin của hai bảng phải trùng nhau (sau khi thay tên
    bảng). Thêm vào đó `genesis_null` chỉ nhận revision 1 nên kể cả lệch policy cũng chỉ ảnh hưởng
    genesis (revision 1 là unique theo account).
    """
    mb01 = _load("c7a90d3f8e21")
    mb05 = _load("d9b2f4a6c8e1")

    checkpoint_admin = _normalize(
        mb01._member_policy("paper_execution_checkpoint"), "paper_execution_checkpoint"
    )
    lease_admin = _normalize(mb05._member_policy("bot_runtime_lease"), "bot_runtime_lease")
    assert checkpoint_admin == lease_admin
    checkpoint_runtime = f"({mb01._WORKSPACE}) AND bot_id = {mb01._BOT}"
    lease_runtime = f"({mb05._WORKSPACE}) AND bot_id = {mb05._BOT}"
    assert checkpoint_runtime == lease_runtime


def test_duplicate_checkpoint_replay_asserts_db_fence() -> None:
    """M2: nhánh replay checkpoint trùng không INSERT nên trigger không chạy; repository phải
    tự kiểm fence ở DB (FOR SHARE hàng lease, token hiện hành, chưa hết hạn)."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from trading_bot.adapters.database.models import BotRuntimeLeaseRecord
    from trading_bot.adapters.database.repositories.paper_checkpoints import (
        PaperCheckpointConflict,
        PaperCheckpointRepository,
    )
    from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
    from trading_bot.domain.execution.runtime_control import FencingToken

    workspace, bot, account = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 10, 26, tzinfo=UTC)
    snapshot = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="p",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            fee_rate=Decimal("0"),
        )
    ).export_state()
    event_hash = "a" * 64
    existing = type("Row", (), {"event_hash": event_hash, "event_scope": None, "event_at": now})()

    def lease(token: int, expires: datetime) -> BotRuntimeLeaseRecord:
        return BotRuntimeLeaseRecord(
            workspace_id=workspace,
            bot_id=bot,
            account_id=account,
            owner_id="o",
            fencing_token=token,
            expires_at=expires,
        )

    def replay(results: list[object], token: FencingToken | None) -> RecordingSession:
        session = RecordingSession(scalar_results=results)
        session.info["bot_id"] = bot
        uow = FakeUow(workspace, session)
        asyncio.run(
            PaperCheckpointRepository().append(
                uow,  # type: ignore[arg-type]
                account_id=account,
                event_key="k",
                event_hash=event_hash,
                snapshot=snapshot,
                occurred_at=now,
                fencing_token=token,
            )
        )
        return session

    account_row = type("A", (), {"mode": "PAPER", "status": "ENABLED"})()
    bot_row = type("B", (), {"id": bot})()
    latest = type(
        "L",
        (),
        {
            "revision": 4,
            "schema_version": snapshot.schema_version,
            "policy_hash": snapshot.policy_hash,
            "snapshot_payload": snapshot.payload,
            "snapshot_hash": snapshot.content_hash,
        },
    )()
    base = [account_row, bot_row, existing]

    stale = base + [lease(5, now + timedelta(minutes=1)), now]
    with pytest.raises(PaperCheckpointConflict, match="STALE_FENCING_TOKEN"):
        replay(stale, FencingToken(4))
    expired = base + [lease(4, now - timedelta(seconds=1)), now]
    with pytest.raises(PaperCheckpointConflict, match="LEASE_EXPIRED"):
        replay(expired, FencingToken(4))
    ok = replay(base + [lease(4, now + timedelta(minutes=1)), now, latest], FencingToken(4))
    assert any("FOR SHARE" in statement for statement in ok.statements)
