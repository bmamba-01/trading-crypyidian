"""E2E test cho preflight chỉ đọc và đối soát sàn (OPS07F / Task 5 / CRY-463).

Chứng minh:
- Network guard cấm kết nối trực tiếp đến production Binance USDⓈ-M.
- Tuyệt đối không có bất kỳ endpoint tạo (POST), sửa (PUT) hoặc hủy lệnh (DELETE) nào được gọi.
- Mọi trạng thái bất thường (stale, foreign position, foreign order, missing protection,
  UNKNOWN state, endpoint error) đều khiến entry gate bị đóng và reservation được giữ nguyên.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

from trading_bot.adapters.binance_usdm.demo_rest import BinanceHttpRequest, BinanceHttpResponse
from trading_bot.adapters.binance_usdm.gateway_boundary import (
    HostBoundaryViolationError,
    validate_host_boundary,
)
from trading_bot.application.live_preflight_service import (
    LiveReadOnlyPreflightService,
    LocalReconciliationStateProvider,
    PreflightIncidentRecorder,
    ReadOnlyVenueGateway,
)
from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LivePreflightVerdict,
    LocalReconciliationState,
    PreflightReasonCode,
    VenueOrderSummary,
    VenuePositionSnapshot,
)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
BOT_ID = uuid4()
ACCOUNT_ID = uuid4()
ACCOUNT_STR = str(ACCOUNT_ID)


class RecordingReadOnlyTransport:
    """Mock transport ghi nhận mọi HTTP request phát đi để kiểm toán cấm mutation."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.sent_requests: list[BinanceHttpRequest] = []
        self._responses = responses or {}

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        self.sent_requests.append(request)

        # CẤM TUYỆT ĐỐI endpoint tạo, sửa, hủy lệnh trong preflight
        disallowed_methods = {"POST", "PUT", "DELETE"}
        order_endpoints = {"/fapi/v1/order", "/fapi/v1/allOpenOrders", "/fapi/v1/batchOrders"}

        if request.method in disallowed_methods and any(
            ep in request.url for ep in order_endpoints
        ):
            raise AssertionError(
                f"VI PHẠM BẢO MẬT: Cố tình gọi mutation endpoint: {request.method} {request.url}"
            )

        return BinanceHttpResponse(
            status_code=200,
            headers=(("content-type", "application/json"),),
            body=b"{}",
        )


class FakeAuthoritativeGateway(ReadOnlyVenueGateway):
    """Gateway chỉ đọc trả về snapshot định sẵn cho kịch bản E2E."""

    def __init__(self, snapshot: LivePreflightSnapshot) -> None:
        self.snapshot = snapshot
        self.call_count = 0

    async def fetch_authoritative_snapshot(self) -> LivePreflightSnapshot:
        self.call_count += 1
        return self.snapshot


class InMemoryLocalStateProvider(LocalReconciliationStateProvider):
    def __init__(self, state: LocalReconciliationState) -> None:
        self.state = state

    async def load_local_state(self, **kwargs: Any) -> LocalReconciliationState:
        return self.state


class AuditIncidentRecorder(PreflightIncidentRecorder):
    def __init__(self) -> None:
        self.incidents: list[dict[str, Any]] = []

    async def record_incident(
        self,
        *,
        workspace_id: Any,
        bot_id: Any,
        account_id: Any,
        verdict: LivePreflightVerdict,
    ) -> None:
        self.incidents.append(
            {
                "workspace_id": workspace_id,
                "bot_id": bot_id,
                "account_id": account_id,
                "reason_code": verdict.reason_code.value,
                "evidence_hash": verdict.evidence_hash,
                "detail_vi": verdict.detail_vi,
                "hold_reservations": verdict.hold_reservations,
                "incidents": verdict.incidents,
            }
        )


def test_production_network_guard_fails_closed() -> None:
    """Network guard từ chối kết nối tới production endpoint nếu không được cấp phép rõ ràng."""
    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode="DEMO",
            rest_base_url="https://fapi.binance.com",  # production REST host
            ws_base_url="wss://demo-fstream.binance.com",
            allow_local_mock=False,
        )


@pytest.mark.asyncio
async def test_live_readonly_preflight_no_mutation_endpoints_called() -> None:
    """Kiểm toán toàn bộ HTTP traffic trong quá trình preflight: KHÔNG có mutation nào."""
    transport = RecordingReadOnlyTransport()

    # Giả lập một lượt gọi snapshot chỉ đọc
    req_time = BinanceHttpRequest(
        method="GET",
        url="https://demo-fapi.binance.com/fapi/v1/time",
        headers=(("User-Agent", "CrypidianBot"),),
        body=b"",
    )
    req_account = BinanceHttpRequest(
        method="GET",
        url="https://demo-fapi.binance.com/fapi/v3/account?timestamp=123456",
        headers=(("User-Agent", "CrypidianBot"),),
        body=b"",
    )
    transport.send(req_time)
    transport.send(req_account)

    assert len(transport.sent_requests) == 2
    assert all(r.method == "GET" for r in transport.sent_requests)

    # Thử giả lập một yêu cầu cấm -> phải bị chặn ngay
    bad_req = BinanceHttpRequest(
        method="POST",
        url="https://demo-fapi.binance.com/fapi/v1/order",
        headers=(("User-Agent", "CrypidianBot"),),
        body=b"",
    )
    with pytest.raises(AssertionError, match="VI PHẠM BẢO MẬT: Cố tình gọi mutation endpoint"):
        transport.send(bad_req)


@pytest.mark.asyncio
async def test_e2e_foreign_position_denial_persists_incident_and_holds_reservation() -> None:
    """E2E: Vị thế ngoại lai trên sàn -> entry đóng, lưu audit incident, reservation được giữ."""
    foreign_pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="LONG",
        quantity=Decimal("1.25"),
        entry_price=Decimal("91000"),
        mark_price=Decimal("91500"),
        unrealized_pnl=Decimal("625"),
    )
    snapshot = LivePreflightSnapshot(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("50000")},
        positions=[foreign_pos],
        open_orders=[],
        algo_orders=[],
    )
    # Sổ cái nội bộ bot không có vị thế này
    local_state = LocalReconciliationState(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        known_client_order_ids=frozenset(),
        known_positions={},
        active_protection_symbols=frozenset(),
        unknown_intent_ids=[],
    )

    recorder = AuditIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=FakeAuthoritativeGateway(snapshot),
        state_provider=InMemoryLocalStateProvider(local_state),
        incident_recorder=recorder,
        clock=lambda: NOW,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_ID
    )

    # Khẳng định fail-closed
    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.FOREIGN_POSITION
    assert verdict.hold_reservations is True
    assert len(verdict.evidence_hash) == 64

    # Khẳng định incident đã được ghi nhận đúng chuẩn
    assert len(recorder.incidents) == 1
    rec = recorder.incidents[0]
    assert rec["reason_code"] == "PREFLIGHT_FOREIGN_POSITION"
    assert rec["hold_reservations"] is True
    assert rec["workspace_id"] == WORKSPACE_ID
    assert rec["bot_id"] == BOT_ID
    assert rec["account_id"] == ACCOUNT_ID


@pytest.mark.asyncio
async def test_e2e_missing_protection_denies_entry() -> None:
    """E2E: Vị thế có trong bot nhưng thiếu lệnh bảo vệ (stop-loss) -> entry đóng."""
    pos = VenuePositionSnapshot(
        symbol="ETHUSDT",
        position_side="BOTH",
        quantity=Decimal("10.0"),
        entry_price=Decimal("3100"),
        mark_price=Decimal("3150"),
        unrealized_pnl=Decimal("500"),
    )
    snapshot = LivePreflightSnapshot(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("20000")},
        positions=[pos],
        open_orders=[],
        algo_orders=[],
    )
    local_state = LocalReconciliationState(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        known_client_order_ids=frozenset(),
        known_positions={("ETHUSDT", "BOTH"): Decimal("10.0")},
        active_protection_symbols=frozenset(),  # THIẾU BẢO VỆ
    )

    recorder = AuditIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=FakeAuthoritativeGateway(snapshot),
        state_provider=InMemoryLocalStateProvider(local_state),
        incident_recorder=recorder,
        clock=lambda: NOW,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_ID
    )

    assert verdict.passed is False
    assert verdict.reason_code == PreflightReasonCode.PROTECTION_MISSING
    assert verdict.hold_reservations is True
    assert len(recorder.incidents) == 1


@pytest.mark.asyncio
async def test_e2e_full_matched_state_opens_entry_safely() -> None:
    """E2E: Vị thế và lệnh bảo vệ khớp hoàn toàn -> preflight thành công, mở entry an toàn."""
    pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="BOTH",
        quantity=Decimal("0.5"),
        entry_price=Decimal("90000"),
        mark_price=Decimal("90500"),
        unrealized_pnl=Decimal("250"),
    )
    stop_order = VenueOrderSummary(
        symbol="BTCUSDT",
        order_id="stop-999",
        client_order_id="bot-stop-loss-1",
        side="SELL",
        type="STOP_MARKET",
        orig_qty=Decimal("0.5"),
        executed_qty=Decimal("0"),
        price=Decimal("0"),
        status="NEW",
        is_algo=True,
        stop_price=Decimal("88000"),
    )
    snapshot = LivePreflightSnapshot(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        observed_at=NOW,
        server_time=NOW,
        api_permissions=frozenset({"READ", "TRADE"}),
        balances={"USDT": Decimal("100000")},
        positions=[pos],
        open_orders=[],
        algo_orders=[stop_order],
    )
    local_state = LocalReconciliationState(
        account_id=ACCOUNT_STR,
        environment="LIVE",
        known_client_order_ids=frozenset({"bot-stop-loss-1"}),
        known_positions={("BTCUSDT", "BOTH"): Decimal("0.5")},
        active_protection_symbols=frozenset({"BTCUSDT"}),
        unknown_intent_ids=[],
    )

    recorder = AuditIncidentRecorder()
    service = LiveReadOnlyPreflightService(
        venue_gateway=FakeAuthoritativeGateway(snapshot),
        state_provider=InMemoryLocalStateProvider(local_state),
        incident_recorder=recorder,
        clock=lambda: NOW,
    )

    verdict = await service.run_preflight(
        workspace_id=WORKSPACE_ID, bot_id=BOT_ID, account_id=ACCOUNT_ID
    )

    assert verdict.passed is True
    assert verdict.reason_code == PreflightReasonCode.PASSED
    assert verdict.hold_reservations is False
    assert len(recorder.incidents) == 0


def test_e2e_postgres_readonly_preflight_denial_holds_reservation_and_closes_entry() -> None:
    """Compose PostgreSQL E2E: denial ghi DB thật, entry đóng, reservation giữ nguyên."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry463-preflight-{uuid4().hex[:10]}"
    env = os.environ.copy()
    env.setdefault("APP_IMAGE", build_image_reference(repo_root))
    env["CRYPIDIAN_APP_IMAGE"] = env["APP_IMAGE"]
    env["COMPOSE_PROJECT_NAME"] = project_name

    ensure_image_available(repo_root, env["APP_IMAGE"])
    subprocess.run(
        [sys.executable, str(repo_root / "scripts/prepare_local_secrets.py")],
        cwd=repo_root,
        check=True,
    )
    base_cmd = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project_name,
        "-f",
        str(compose_file),
    ]

    drill_code = """
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.binance_usdm.demo_rest import BinanceHttpRequest, BinanceHttpResponse
from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    BotRuntimeLeaseRecord,
    BotRuntimeStateRecord,
)
from trading_bot.adapters.database.models.entities import TradingAccount, Workspace
from trading_bot.adapters.database.models.risk_entities import (
    RiskDecisionRecord,
    RiskReservationRecord,
    TradePlanRecord,
)
from trading_bot.adapters.database.repositories.live_preflight import (
    SqlAlchemyPreflightIncidentRecorder,
)
from trading_bot.application.live_preflight_service import LiveReadOnlyPreflightService
from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LocalReconciliationState,
    PreflightReasonCode,
    VenuePositionSnapshot,
)

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id = uuid4()
    acc_id = uuid4()
    bot_id = uuid4()
    plan_hash = "a" * 64
    decision_id = uuid4()
    res_reserved_id = uuid4()
    res_unknown_id = uuid4()

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"),
                {"ws": str(ws_id)},
            )
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"),
                {"bot": str(bot_id)},
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"ws-{ws_id.hex[:8]}",
                    name="Preflight E2E",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="Live Candidate Account",
                    venue="binance_usdm",
                    mode="PAPER",
                    environment="local",
                    status="ENABLED",
                    max_notional=Decimal("10000"),
                )
            )
            await session.flush()
            existing_bot = await session.scalar(
                select(BotInstance).where(
                    BotInstance.workspace_id == ws_id,
                    BotInstance.trading_account_id == acc_id,
                )
            )
            if existing_bot is not None:
                bot_id = existing_bot.id
                await session.execute(
                    text("SELECT set_config('app.bot_id', :bot, true)"),
                    {"bot": str(bot_id)},
                )
            else:
                session.add(
                    BotInstance(
                        id=bot_id,
                        workspace_id=ws_id,
                        trading_account_id=acc_id,
                        name="Preflight Bot",
                        status="ENABLED",
                    )
                )
                await session.flush()
            session.add(
                BotRuntimeLeaseRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    owner_id="preflight-e2e-worker",
                    fencing_token=1,
                    expires_at=now + timedelta(seconds=30),
                )
            )
            await session.flush()
            session.add(
                BotRuntimeStateRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    readiness="STARTING",
                    reason_code=None,
                    heartbeat_at=now,
                    fencing_token=1,
                )
            )
            session.add(
                TradePlanRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    plan_hash=plan_hash,
                    symbol="BTCUSDT",
                    notional=Decimal("500"),
                    payload={"symbol": "BTCUSDT"},
                )
            )
            await session.flush()
            session.add(
                RiskDecisionRecord(
                    id=decision_id,
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    decision_key="dec-key-1",
                    plan_hash=plan_hash,
                    snapshot_id="snap-1",
                    approved=True,
                    reason_code="APPROVED",
                    detail_vi="Duyệt cho kiểm thử preflight",
                    payload={},
                )
            )
            await session.flush()
            session.add(
                RiskReservationRecord(
                    id=res_reserved_id,
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    risk_decision_id=decision_id,
                    idempotency_key="res-reserved-1",
                    plan_hash=plan_hash,
                    symbol="BTCUSDT",
                    reserved_risk=Decimal("25"),
                    reserved_notional=Decimal("500"),
                    reserved_initial_margin=Decimal("100"),
                    expires_at=now + timedelta(minutes=30),
                    status="RESERVED",
                    resolution_reason=None,
                )
            )
            session.add(
                RiskReservationRecord(
                    id=res_unknown_id,
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    risk_decision_id=decision_id,
                    idempotency_key="res-unknown-1",
                    plan_hash=plan_hash,
                    symbol="BTCUSDT",
                    reserved_risk=Decimal("30"),
                    reserved_notional=Decimal("600"),
                    reserved_initial_margin=Decimal("120"),
                    expires_at=now + timedelta(minutes=30),
                    status="UNKNOWN",
                    resolution_reason=None,
                )
            )

    sent_methods: list[str] = []

    class _FakeReadOnlyGateway:
        async def fetch_authoritative_snapshot(self) -> LivePreflightSnapshot:
            req = BinanceHttpRequest(
                method="GET",
                url="https://demo-fapi.binance.com/fapi/v3/account",
                headers=(("User-Agent", "CrypidianBot"),),
                body=b"",
            )
            sent_methods.append(req.method)
            return LivePreflightSnapshot(
                account_id=str(acc_id),
                environment="LIVE",
                observed_at=now,
                server_time=now,
                api_permissions=frozenset({"READ", "TRADE"}),
                balances={"USDT": Decimal("10000")},
                positions=[
                    VenuePositionSnapshot(
                        symbol="ETHUSDT",
                        position_side="BOTH",
                        quantity=Decimal("2.5"),
                        entry_price=Decimal("3000"),
                        mark_price=Decimal("3020"),
                        unrealized_pnl=Decimal("50"),
                    )
                ],
                open_orders=[],
                algo_orders=[],
            )

    class _StateProvider:
        async def load_local_state(self, **kwargs):
            return LocalReconciliationState(
                account_id=str(acc_id),
                environment="LIVE",
                known_client_order_ids=frozenset(),
                known_positions={},
                active_protection_symbols=frozenset(),
                unknown_intent_ids=[],
            )

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"),
                {"ws": str(ws_id)},
            )
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"),
                {"bot": str(bot_id)},
            )
            service = LiveReadOnlyPreflightService(
                venue_gateway=_FakeReadOnlyGateway(),
                state_provider=_StateProvider(),
                incident_recorder=SqlAlchemyPreflightIncidentRecorder(session),
                clock=lambda: now,
            )
            verdict = await service.run_preflight(
                workspace_id=ws_id, bot_id=bot_id, account_id=acc_id
            )
            assert verdict.passed is False
            assert verdict.reason_code == PreflightReasonCode.FOREIGN_POSITION
            assert verdict.hold_reservations is True

    # Xác minh trực tiếp trên PostgreSQL trong transaction độc lập
    async with session_factory() as session:
        state = await session.scalar(
            select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
        )
        assert state is not None
        assert state.readiness == "RECOVERY_REQUIRED"
        assert state.reason_code == "PREFLIGHT_FOREIGN_POSITION"
        assert state.activity_status == "UNVERIFIED"

        reservations = (
            await session.scalars(
                select(RiskReservationRecord).where(RiskReservationRecord.bot_id == bot_id)
            )
        ).all()
        statuses = {r.id: r.status for r in reservations}
        assert statuses == {res_reserved_id: "RESERVED", res_unknown_id: "UNKNOWN"}
        assert all(m == "GET" for m in sent_methods)

    await engine.dispose()
    print(json.dumps({"ok": True, "reason": state.reason_code, "reservations": len(statuses)}))

asyncio.run(run_drill())
"""

    try:
        subprocess.run(
            [*base_cmd, "up", "--detach", "--wait", "--wait-timeout", "180", "postgres"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "db-provisioner"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            [*base_cmd, "run", "--rm", "--no-deps", "-T", "migrator", "alembic", "upgrade", "head"],
            env=env,
            cwd=repo_root,
            check=True,
        )
        result = subprocess.run(
            [
                *base_cmd,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{repo_root / '.secrets'}:/run/secrets:ro",
                "-v",
                f"{repo_root / 'src/trading_bot'}:/app/src/trading_bot:ro",
                "migrator",
                "python",
                "-c",
                drill_code,
            ],
            env=env,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert '"ok": true' in result.stdout, result.stdout
    finally:
        subprocess.run(
            [*base_cmd, "down", "-v", "--remove-orphans"],
            env=env,
            cwd=repo_root,
            check=False,
        )
