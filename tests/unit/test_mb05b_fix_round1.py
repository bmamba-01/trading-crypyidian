"""MB-05 Part B fix round 1: timeout phiên DB (I2), fence trigger (gap a), readiness DB (gap b),
audit mất lease qua command (M1), guard disable lấy L1 (M7), gate/recorder fan-out bắt buộc (I3).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.runtime_fakes import RecordingSession

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "trading_bot"


# --- I2: timeout phiên DB của runtime -------------------------------------------------------
def test_runtime_session_timeouts_are_bounded_by_lease_ttl() -> None:
    from trading_bot.adapters.database.engine import runtime_session_timeouts

    for ttl in (timedelta(seconds=6), timedelta(seconds=30), timedelta(seconds=300)):
        timeouts = runtime_session_timeouts(ttl)
        ttl_ms = int(ttl.total_seconds() * 1000)
        # Chờ khóa < transaction idle bị cắt < TTL: worker treo không giữ L1/lease lâu hơn TTL,
        # lệnh chờ khóa thất bại có giới hạn rồi được thử lại.
        assert 0 < timeouts.lock_timeout_ms < timeouts.idle_in_transaction_timeout_ms < ttl_ms
        assert 0 < timeouts.statement_timeout_ms <= ttl_ms
    assert runtime_session_timeouts(timedelta(seconds=30)).as_options() == (
        "-c lock_timeout=5000 -c idle_in_transaction_session_timeout=10000 "
        "-c statement_timeout=15000"
    )


def test_create_database_engine_passes_session_timeouts_as_connection_options(monkeypatch) -> None:
    from trading_bot.adapters.database import engine as engine_module

    captured: dict[str, Any] = {}

    def fake_create(url: object, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(engine_module, "create_async_engine", fake_create)
    timeouts = engine_module.runtime_session_timeouts(timedelta(seconds=30))

    engine_module.create_database_engine("postgresql+psycopg://x", session_timeouts=timeouts)  # type: ignore[arg-type]

    assert captured["connect_args"] == {"options": timeouts.as_options()}
    assert captured["pool_pre_ping"] is True


def test_runtime_app_builds_engine_with_ttl_derived_timeouts(monkeypatch, tmp_path) -> None:
    from trading_bot import app as app_module
    from trading_bot.adapters.database.engine import runtime_session_timeouts
    from trading_bot.config.settings import AppSettings

    password = tmp_path / "password"
    password.write_text("secret")
    captured: dict[str, Any] = {}

    class _Engine:
        async def dispose(self) -> None:
            return None

    def fake_engine(url: object, **kwargs: Any) -> _Engine:
        captured.update(kwargs)
        return _Engine()

    class _Bootstrap:
        async def bootstrap(self) -> None:
            return None

    monkeypatch.setattr(app_module, "create_database_engine", fake_engine)
    settings = AppSettings(
        process_role="trading-runtime",
        runtime_workspace_id=uuid4(),
        database_user="crypidian_trading_runtime",
        database_password_file=password,
        runtime_lease_ttl_seconds=30,
    )
    app = app_module.create_app(settings, paper_runtime_bootstrap_factory=lambda **_: _Bootstrap())
    with TestClient(app):
        pass

    assert captured["session_timeouts"] == runtime_session_timeouts(timedelta(seconds=30))


# --- gap (a): trigger fence dùng chung không tham chiếu NEW.revision cho bảng không có cột ----
def _load_migration() -> Any:
    path = next((ROOT / "migrations" / "versions").glob("e1f3a5c7b9d2_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_fence_function_only_reads_revision_inside_genesis_branch() -> None:
    """`bot_runtime_state`/`account_activity_incident` không có cột `revision`: biểu thức đọc
    `NEW.revision` phải nằm trong nhánh IF riêng của `genesis_null`, không chung điều kiện với
    `TG_ARGV` (PL/pgSQL không bảo đảm đánh giá ngắn mạch -> lỗi 42703)."""
    module = _load_migration()
    body = module._FUNCTION.format(null_condition=module._GENESIS)
    lines = [line.strip() for line in body.splitlines()]
    assert not any("TG_ARGV" in line and "NEW.revision" in line for line in lines), body
    genesis = next(index for index, line in enumerate(lines) if "'genesis_null'" in line)
    revision = next(index for index, line in enumerate(lines) if "NEW.revision" in line)
    assert lines[genesis].endswith("THEN")
    assert revision > genesis


# --- gap (b): readiness phản ánh mất DB sau khi khởi động ---------------------------------
def test_runtime_readiness_turns_503_when_database_becomes_unreachable() -> None:
    from trading_bot.app import create_app
    from trading_bot.config.settings import AppSettings

    workspace_id = uuid4()
    account_id = uuid4()
    healthy = {"value": True}
    probes: list[bool] = []

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return SimpleNamespace(
                workspace_id=workspace_id,
                ready=True,
                database_ready=True,
                error_code=None,
                account_states={account_id: SimpleNamespace(status="READY", reason_code=None)},
                active_accounts={account_id: SimpleNamespace(account_id=account_id)},
            )

    async def probe() -> bool:
        probes.append(healthy["value"])
        return healthy["value"]

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
        database_probe=probe,
    )
    with TestClient(app) as client:
        first = client.get("/health/ready")
        healthy["value"] = False
        down = client.get("/health/ready")
        healthy["value"] = True
        back = client.get("/health/ready")

    assert first.status_code == 200
    assert down.status_code == 503
    assert down.json()["pending"] == ["database"]
    assert down.json()["paper_runtime_error_code"] == "DATABASE_UNAVAILABLE"
    assert back.status_code == 200
    assert probes == [True, False, True]


def test_default_database_probe_is_bounded_and_fails_closed() -> None:
    from trading_bot.app import probe_database

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, _statement: object) -> None:
            await asyncio.sleep(10)

    assert asyncio.run(probe_database(lambda: _Session(), timeout=0.05)) is False  # type: ignore[arg-type]

    class _Broken(_Session):
        async def execute(self, _statement: object) -> None:
            raise OSError("connection refused")

    assert asyncio.run(probe_database(lambda: _Broken(), timeout=1)) is False  # type: ignore[arg-type]


# --- M1: mất lease phát hiện qua command được audit, lỗi tick không bị kẹt ------------------
def test_lease_loss_detected_by_command_is_audited_on_next_tick_and_error_cleared() -> None:
    from tests.unit.runtime_registry_fakes import World
    from tests.unit.test_bot_runtime_supervisor import _Application, _registry, _supervisor

    world = World(uuid4(), account_count=1)
    registry = _registry(world)
    account_id = world.accounts[0].id
    registry.record_error(account_id, "SUPERVISOR_TICK_FAILED")
    supervisor, bootstrap = _supervisor(world, registry, _Application())

    async def standby(_account: UUID) -> SimpleNamespace:
        return SimpleNamespace(status="STANDBY", reason_code="LEASE_HELD_ELSEWHERE", account=None)

    bootstrap.bootstrap_bot = standby  # type: ignore[method-assign]
    # Application hạ bot qua callback (fence của một command bị từ chối).
    registry.mark_lease_lost(account_id, "STALE_FENCING_TOKEN")

    asyncio.run(supervisor.tick_once())
    asyncio.run(supervisor.tick_once())

    lost = [audit for audit in world.audits if audit["action"] == "bot.runtime.lease_lost"]
    assert [audit["reason"] for audit in lost] == ["STALE_FENCING_TOKEN"]
    assert registry.entry(account_id).last_error_code is None
    assert registry.entry(account_id).readiness.value == "STANDBY"


def test_runtime_app_wires_lease_lost_callback_to_audited_registry_path() -> None:
    source = (SRC / "app.py").read_text(encoding="utf-8")
    assert "lease_lost_callback=registry.mark_lease_lost" in source


# --- M7: guard disable lấy L1 trước khi đếm ---------------------------------------------
def test_flat_guard_takes_workspace_l1_before_counting() -> None:
    from trading_bot.application.bot_lifecycle_guard import BotLifecycleGuard

    session = RecordingSession(scalar_results=[object(), 0, 0, 0])

    asyncio.run(BotLifecycleGuard(session).ensure_flat_for_disable(uuid4(), uuid4()))  # type: ignore[arg-type]

    assert "pg_advisory_xact_lock" in session.statements[0]
    assert session.events[0] == "execute"


# --- I3: gate + recorder fan-out bắt buộc ---------------------------------------------------
def test_context_provider_requires_runtime_gate_and_exclusion_recorder() -> None:
    from trading_bot.application.bot_decision_contexts import EffectiveBotDecisionContextProvider

    parameters = inspect.signature(EffectiveBotDecisionContextProvider).parameters
    for name in ("runtime_gate", "exclusion_recorder"):
        assert parameters[name].default is inspect.Parameter.empty, name
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_production_code_never_builds_provider_without_gate_and_recorder() -> None:
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", ""))
                == "EffectiveBotDecisionContextProvider"
            ):
                keywords = {keyword.arg for keyword in node.keywords}
                assert {"runtime_gate", "exclusion_recorder"} <= keywords, path


def test_eligibility_persists_exclusions_through_recorder() -> None:
    from trading_bot.application.bot_decision_contexts import (
        EffectiveBotDecisionContextProvider,
        RuntimeBotRef,
    )

    recorded: list[Any] = []

    class _Uow:
        def __init__(self, *_: object, **kwargs: Any) -> None:
            self.workspace_id = kwargs["workspace_id"]
            self.session = object()

        async def __aenter__(self) -> _Uow:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class _Recorder:
        async def record(self, eligibility: Any) -> None:
            recorded.append(eligibility)

    async def list_bots(_uow: Any) -> tuple[RuntimeBotRef, ...]:
        return (RuntimeBotRef(bot_id=uuid4(), account_id=uuid4(), enabled=False),)

    async def gate(_uow: Any, _bot: Any) -> None:
        return None

    provider = EffectiveBotDecisionContextProvider(
        object(),  # type: ignore[arg-type]
        unit_of_work_factory=_Uow,  # type: ignore[arg-type]
        list_bots=list_bots,  # type: ignore[arg-type]
        runtime_gate=gate,
        exclusion_recorder=_Recorder(),
    )
    from datetime import UTC, datetime

    asyncio.run(provider.eligibility(workspace_id=uuid4(), as_of=datetime.now(UTC)))

    assert len(recorded) == 1
    assert [item.reason_code for item in recorded[0].exclusions] == ["BOT_DISABLED"]


@pytest.mark.parametrize("placeholder", [None])
def test_wbs_records_mb05b_wiring_item(placeholder: object) -> None:
    del placeholder
    wbs = (ROOT / "docs/superpowers/plans/2026-10-26-mb05-runtime-registry-wbs.md").read_text(
        encoding="utf-8"
    )
    assert "RuntimeFanOutGate" in wbs.split("## MB-05b")[1]
    assert "job_processor" in wbs.split("## MB-05b")[1]
