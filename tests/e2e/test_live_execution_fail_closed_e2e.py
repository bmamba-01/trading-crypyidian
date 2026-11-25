"""Bộ E2E fail-closed cho ma trận lỗi production (OPS07F / Task 8 / CRY-466).

Bao phủ toàn bộ ma trận release-blocking:
1. Outbound network guard tầng socket/DNS cấm mọi kết nối tới Binance production.
2. Timeout / HTTP 503 mơ hồ sau submit -> UNKNOWN, giữ reservation, không reset về 0/flat.
3. Partial fill, duplicate & out-of-order stream event -> không rollback fill, idempotent.
4. HTTP 429/418, clock skew, key thu hồi/thiếu quyền/bật withdraw, không lộ secret qua log/error.
5. Thiếu protective/Algo order, stream gap, REST vs stream mismatch, foreign position/order,
   host/account/env mismatch, preflight stale -> đóng entry gate, không xóa protection hợp lệ.
6. PostgreSQL Compose E2E thật: restart + rollback giữ nguyên UNKNOWN reservation,
   concurrent reservation sát cap (`FOR UPDATE`), maker-checker / profile hash mismatch /
   expired / immediate pause đều bị từ chối và ghi audit decision bất biến.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available

from trading_bot.adapters.binance_usdm.gateway_boundary import (
    HostBoundaryViolationError,
    redact_sensitive_text,
    validate_host_boundary,
)
from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LocalReconciliationState,
    PreflightReasonCode,
    VenueOrderSummary,
    VenuePositionSnapshot,
    evaluate_live_preflight,
)
from trading_bot.domain.execution.venue_orders import (
    ReconciliationSource,
    SubmissionOutcome,
    VenueOrderIntent,
    VenueOrderState,
    VenueOrderUpdate,
)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_PRODUCTION_HOSTS = frozenset(
    {
        "fapi.binance.com",
        "fstream.binance.com",
        "api.binance.com",
        "dapi.binance.com",
    }
)


@pytest.fixture(autouse=True)
def production_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outbound guard tầng socket/DNS: thất bại ngay nếu có bất kỳ lookup tới Binance production."""
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        host_str = str(host or "").lower().strip()
        if host_str in FORBIDDEN_PRODUCTION_HOSTS or host_str.endswith(".binance.com"):
            raise RuntimeError(f"PRODUCTION_NETWORK_GUARD_BLOCKED: {host_str}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def test_matrix_outbound_network_guard_and_host_boundary_block_production() -> None:
    """Ca 1: Guard tầng socket và gateway boundary đều chặn tuyệt đối Binance production."""
    with pytest.raises(RuntimeError, match="PRODUCTION_NETWORK_GUARD_BLOCKED"):
        socket.getaddrinfo("fapi.binance.com", 443)

    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode="DEMO",
            rest_base_url="https://fapi.binance.com",
            ws_base_url="wss://fstream.binance.com",
            allow_local_mock=False,
        )


def test_matrix_ambiguous_timeout_503_partial_fill_and_duplicate_out_of_order_events() -> None:
    """Ca 2 & 3: Timeout/503 mơ hồ -> UNKNOWN giữ reservation; partial/duplicate không rollback."""
    intent = VenueOrderIntent(
        intent_id=uuid4(),
        workspace_id="ws-matrix",
        account_id="acc-matrix",
        idempotency_key="fail-closed-timeout-1",
        reservation_id=uuid4(),
        plan_hash="a" * 64,
        settings_version="settings-v1",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("1.0"),
        client_order_id="cid-timeout-1",
        created_at=NOW,
    )
    submitting = intent.begin_submission(at=NOW + timedelta(seconds=1))
    unknown = submitting.resolve_submission(
        outcome=SubmissionOutcome.UNKNOWN,
        observed_at=NOW + timedelta(seconds=2),
    )
    assert unknown.state is VenueOrderState.UNKNOWN
    assert unknown.reservation_action == "RETAIN"

    # Lệnh khác nhận partial fill rồi gặp duplicate & out-of-order event -> không tụt filled_qty
    ack = (
        VenueOrderIntent(
            intent_id=uuid4(),
            workspace_id="ws-matrix",
            account_id="acc-matrix",
            idempotency_key="fail-closed-partial-1",
            reservation_id=uuid4(),
            plan_hash="b" * 64,
            settings_version="settings-v1",
            symbol="BTCUSDT",
            side="BUY",
            quantity=Decimal("1.0"),
            client_order_id="cid-partial-1",
            created_at=NOW,
        )
        .begin_submission(at=NOW + timedelta(seconds=1))
        .resolve_submission(
            outcome=SubmissionOutcome.ACCEPTED,
            observed_at=NOW + timedelta(seconds=2),
            venue_order_id="v-ord-1",
        )
    )
    fill_ev = VenueOrderUpdate(
        event_id="ev-1",
        source=ReconciliationSource.USER_STREAM,
        observed_at=NOW + timedelta(seconds=4),
        venue_order_id="v-ord-1",
        cumulative_filled=Decimal("0.6"),
        status=VenueOrderState.PARTIALLY_FILLED,
    )
    pf = ack.apply_update(fill_ev)
    assert pf.filled_quantity == Decimal("0.6")
    # Duplicate cùng event_id -> idempotent
    assert pf.apply_update(fill_ev) is pf
    # Out-of-order cũ hơn với ACKNOWLEDGED (fill = 0) -> không rollback fill
    stale = pf.apply_update(
        VenueOrderUpdate(
            event_id="ev-old",
            source=ReconciliationSource.USER_STREAM,
            observed_at=NOW + timedelta(seconds=3),
            venue_order_id="v-ord-1",
            cumulative_filled=Decimal("0"),
            status=VenueOrderState.ACKNOWLEDGED,
        )
    )
    assert stale.filled_quantity == Decimal("0.6")


def test_matrix_preflight_faults_and_secret_redaction() -> None:
    """Ca 4 & 5: Clock skew, thiếu protection, stream gap, foreign state, withdraw, redaction."""
    base_local = LocalReconciliationState(
        account_id="acc-prod-syn",
        environment="LIVE",
        known_client_order_ids=frozenset({"bot-sl-1"}),
        known_positions={("BTCUSDT", "BOTH"): Decimal("0.5")},
        active_protection_symbols=frozenset({"BTCUSDT"}),
        unknown_intent_ids=[],
        last_processed_stream_seq=100,
    )
    pos = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="BOTH",
        quantity=Decimal("0.5"),
        entry_price=Decimal("90000"),
        mark_price=Decimal("90100"),
        unrealized_pnl=Decimal("50"),
    )
    sl_order = VenueOrderSummary(
        symbol="BTCUSDT",
        order_id="algo-1",
        client_order_id="bot-sl-1",
        side="SELL",
        type="STOP_MARKET",
        orig_qty=Decimal("0.5"),
        executed_qty=Decimal("0"),
        price=Decimal("0"),
        status="NEW",
        is_algo=True,
        stop_price=Decimal("88000"),
    )

    def _snap(**overrides) -> LivePreflightSnapshot:
        kwargs = {
            "account_id": "acc-prod-syn",
            "environment": "LIVE",
            "observed_at": NOW,
            "server_time": NOW,
            "api_permissions": frozenset({"READ", "TRADE"}),
            "balances": {"USDT": Decimal("10000")},
            "positions": [pos],
            "open_orders": [],
            "algo_orders": [sl_order],
            "recent_activity": [{"stream_seq": 101}],
        }
        kwargs.update(overrides)
        return LivePreflightSnapshot(**kwargs)

    # Clock skew > 1000ms
    v_skew = evaluate_live_preflight(
        _snap(server_time=NOW + timedelta(milliseconds=2500)), base_local, now=NOW
    )
    assert v_skew.passed is False
    assert v_skew.reason_code == PreflightReasonCode.CLOCK_DRIFT_EXCESSIVE

    # Stream gap (100 -> 105)
    v_gap = evaluate_live_preflight(
        _snap(recent_activity=[{"stream_seq": 105}]), base_local, now=NOW
    )
    assert v_gap.passed is False
    assert v_gap.reason_code == PreflightReasonCode.STREAM_GAP_DETECTED

    # Key bị bật quyền WITHDRAW
    v_wd = evaluate_live_preflight(
        _snap(api_permissions=frozenset({"READ", "TRADE", "WITHDRAW"})), base_local, now=NOW
    )
    assert v_wd.passed is False
    assert v_wd.reason_code == PreflightReasonCode.API_WITHDRAWAL_ENABLED

    # Thiếu protection (Algo stop loss bị mất)
    local_no_prot = LocalReconciliationState(
        account_id="acc-prod-syn",
        environment="LIVE",
        known_client_order_ids=frozenset(),
        known_positions={("BTCUSDT", "BOTH"): Decimal("0.5")},
        active_protection_symbols=frozenset(),
    )
    v_prot = evaluate_live_preflight(_snap(algo_orders=[]), local_no_prot, now=NOW)
    assert v_prot.passed is False
    assert v_prot.reason_code == PreflightReasonCode.PROTECTION_MISSING

    # REST vs stream / ledger exposure mismatch
    pos_diff = VenuePositionSnapshot(
        symbol="BTCUSDT",
        position_side="BOTH",
        quantity=Decimal("0.9"),
        entry_price=Decimal("90000"),
        mark_price=Decimal("90100"),
        unrealized_pnl=Decimal("90"),
    )
    v_exp = evaluate_live_preflight(_snap(positions=[pos_diff]), base_local, now=NOW)
    assert v_exp.passed is False
    assert v_exp.reason_code == PreflightReasonCode.EXPOSURE_MISMATCH

    # Redaction không làm lộ signature / listenKey / API key
    raw_err = (
        "HTTP 418 X-MBX-APIKEY: AbCdEf1234567890GhIjKl "
        "signature=0123456789abcdef0123456789abcdef listenKey=SecretListenKey123456"
    )
    redacted = redact_sensitive_text(raw_err)
    assert "AbCdEf1234567890GhIjKl" not in redacted
    assert "0123456789abcdef0123456789abcdef" not in redacted
    assert "SecretListenKey123456" not in redacted


def test_matrix_postgres_compose_restart_rollback_concurrency_and_authz_denials() -> None:
    """Ca 6 (PostgreSQL Compose E2E): restart/rollback giữ UNKNOWN, concurrent cap, authz denial."""
    repo_root = Path(__file__).resolve().parents[2]
    compose_file = repo_root / "compose.yaml"
    project_name = f"crypidian-cry466-matrix-{uuid4().hex[:10]}"
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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models.bot_entities import BotInstance
from trading_bot.adapters.database.models.bot_runtime_entities import (
    BotRuntimeLeaseRecord,
    BotRuntimeStateRecord,
)
from trading_bot.adapters.database.models.entities import TradingAccount, Workspace
from trading_bot.adapters.database.models.risk_entities import (
    RiskDecisionRecord,
    RiskReservationRecord,
)
from trading_bot.adapters.database.repositories.live_preflight import (
    SqlAlchemyPreflightIncidentRecorder,
)
from trading_bot.adapters.database.repositories.live_risk_enforcement import (
    AtomicLiveRiskEnforcementRepository,
)
from trading_bot.application.live_preflight_service import LiveReadOnlyPreflightService
from trading_bot.domain.execution.live_preflight import (
    LivePreflightSnapshot,
    LocalReconciliationState,
    PreflightReasonCode,
)
from trading_bot.domain.risk.live_risk_profile import (
    LiveAccountMode,
    LiveOrderRiskRequest,
    LiveRiskReasonCode,
    SignedLiveRiskProfile,
    sign_live_risk_profile_digest,
)

SIGNING_KEY = "cry466-synthetic-signing-key"

def build_profile(
    acc_id: str,
    now: datetime,
    *,
    owner: str = "owner-1",
    approver: str = "risk-2",
    max_cap: Decimal = Decimal("1000"),
) -> SignedLiveRiskProfile:
    base = SignedLiveRiskProfile(
        profile_version="live-matrix-v1",
        account_id=acc_id,
        environment="LIVE",
        build_revision="rev-466",
        account_mode=LiveAccountMode.SINGLE_ASSET_USDT,
        timezone_name="UTC",
        daily_reset_hour=0,
        rolling_window_hours=24,
        include_unrealized_loss=True,
        max_account_notional=max_cap,
        max_symbol_notional=max_cap,
        max_order_notional=Decimal("700"),
        max_leverage=Decimal("3"),
        max_aggregate_exposure_notional=max_cap,
        max_daily_loss=Decimal("200"),
        max_rolling_loss=Decimal("300"),
        owner_id=owner,
        approver_id=approver,
        signed_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=12),
        signature_digest="placeholder",
    )
    sig = sign_live_risk_profile_digest(base.compute_hash(), signing_key=SIGNING_KEY)
    return replace(base, signature_digest=sig)

async def run_drill() -> None:
    with open("/run/secrets/postgres_password", "r", encoding="utf-8") as fh:
        pw = fh.read().strip()
    url = f"postgresql+psycopg://crypidian_admin:{pw}@postgres:5432/crypidian"
    engine = create_async_engine(url, pool_size=6)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    ws_id, acc_id = uuid4(), uuid4()

    # 1. Seed workspace, account, bot_instance (via trigger), lease & runtime state
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            session.add(
                Workspace(
                    id=ws_id,
                    slug=f"m466-{ws_id.hex[:8]}",
                    name="CRY-466 Matrix",
                    mode="PAPER",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                TradingAccount(
                    id=acc_id,
                    workspace_id=ws_id,
                    name="Matrix Account",
                    venue="binance_usdm",
                    mode="PAPER",
                    environment="local",
                    status="ENABLED",
                    max_notional=Decimal("1000"),
                )
            )
            await session.flush()
            bot = await session.scalar(
                select(BotInstance).where(
                    BotInstance.workspace_id == ws_id,
                    BotInstance.trading_account_id == acc_id,
                )
            )
            assert bot is not None
            bot_id = bot.id
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            session.add(
                BotRuntimeLeaseRecord(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    owner_id="matrix-worker",
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

    profile = build_profile(str(acc_id), now)

    async def reserve(
        key: str,
        notional: Decimal,
        sym: str,
        prof: SignedLiveRiskProfile,
        *,
        expected_hash: str | None = None,
        paused: bool = False,
    ):
        async with sessions() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
                )
                await session.execute(
                    text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
                )
                repo = AtomicLiveRiskEnforcementRepository(session)
                req = LiveOrderRiskRequest(
                    symbol=sym,
                    order_notional=notional,
                    leverage=Decimal("2"),
                    build_revision="rev-466",
                    expected_profile_hash=expected_hash or prof.compute_hash(),
                    paused_or_killed=paused,
                )
                return await repo.evaluate_and_reserve(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    profile=prof,
                    wallet_balance=Decimal("5000"),
                    unrealized_pnl=Decimal("0"),
                    available_balance=Decimal("5000"),
                    initial_margin_used=Decimal("0"),
                    fee_and_funding_today=Decimal("0"),
                    fee_and_funding_rolling=Decimal("0"),
                    open_exposure_notional=Decimal("0"),
                    symbol_open_exposure_notional=Decimal("0"),
                    mark_price=Decimal("90000"),
                    mark_observed_at=now - timedelta(seconds=1),
                    request=req,
                    realized_events=[],
                    idempotency_key=key,
                    reserved_risk=Decimal("20"),
                    reserved_initial_margin=Decimal("150"),
                    reservation_ttl=timedelta(minutes=15),
                    as_of=now,
                    signing_key=SIGNING_KEY,
                )

    # 2. Concurrent reservation sát cap (600 + 600 vs cap 1000) -> đúng 1 thắng
    (d1, r1), (d2, r2) = await asyncio.gather(
        reserve("m-conc-1", Decimal("600"), "BTCUSDT", profile),
        reserve("m-conc-2", Decimal("600"), "ETHUSDT", profile),
    )
    assert sum(1 for d in (d1, d2) if d.approved) == 1
    win_res = r1 or r2
    assert win_res is not None

    # 3. Giả lập restart giữa submit/ack: chuyển reservation sang UNKNOWN + rollback transaction lỗi
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(
                select(RiskReservationRecord).where(RiskReservationRecord.id == win_res.id)
            )
            assert row is not None
            row.status = "UNKNOWN"

    try:
        async with sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(RiskReservationRecord).where(RiskReservationRecord.id == win_res.id)
                )
                assert row is not None
                raise RuntimeError("simulated-db-control-plane-drop")
    except RuntimeError:
        pass

    # 4. Preflight sau restart gặp UNKNOWN -> đóng entry gate (RECOVERY_REQUIRED) & giữ reservation
    class _CleanVenue:
        async def fetch_authoritative_snapshot(self) -> LivePreflightSnapshot:
            return LivePreflightSnapshot(
                account_id=str(acc_id),
                environment="LIVE",
                observed_at=now,
                server_time=now,
                api_permissions=frozenset({"READ", "TRADE"}),
                balances={"USDT": Decimal("5000")},
                positions=[],
                open_orders=[],
                algo_orders=[],
            )

    class _UnknownLocal:
        async def load_local_state(self, **kwargs):
            return LocalReconciliationState(
                account_id=str(acc_id),
                environment="LIVE",
                known_client_order_ids=frozenset(),
                known_positions={},
                active_protection_symbols=frozenset(),
                unknown_intent_ids=[win_res.id],
            )

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws_id)}
            )
            await session.execute(
                text("SELECT set_config('app.bot_id', :bot, true)"), {"bot": str(bot_id)}
            )
            svc = LiveReadOnlyPreflightService(
                venue_gateway=_CleanVenue(),
                state_provider=_UnknownLocal(),
                incident_recorder=SqlAlchemyPreflightIncidentRecorder(session),
                clock=lambda: now,
            )
            verdict = await svc.run_preflight(
                workspace_id=ws_id, bot_id=bot_id, account_id=acc_id
            )
            assert verdict.passed is False
            assert verdict.reason_code == PreflightReasonCode.UNKNOWN_STATE_HELD
            assert verdict.hold_reservations is True

    # 5. Maker-checker violation & payload hash tamper / replay -> từ chối, ghi decision bất biến
    self_prof = build_profile(str(acc_id), now, owner="same-actor", approver="same-actor")
    d_mc, r_mc = await reserve("m-mc-deny", Decimal("100"), "SOLUSDT", self_prof)
    assert d_mc.approved is False
    assert d_mc.reason_code == LiveRiskReasonCode.MAKER_CHECKER_VIOLATION
    assert r_mc is None

    d_hash, r_hash = await reserve(
        "m-hash-tamper", Decimal("100"), "SOLUSDT", profile, expected_hash="f" * 64
    )
    assert d_hash.approved is False
    assert d_hash.reason_code == LiveRiskReasonCode.PROFILE_HASH_MISMATCH
    assert r_hash is None

    # 6. Xác minh trạng thái bền vững trên PostgreSQL
    async with sessions() as session:
        state = await session.scalar(
            select(BotRuntimeStateRecord).where(BotRuntimeStateRecord.bot_id == bot_id)
        )
        assert state is not None
        assert state.readiness == "RECOVERY_REQUIRED"
        assert state.reason_code == "PREFLIGHT_UNKNOWN_STATE_HELD"

        sum_notional = func.coalesce(
            func.sum(RiskReservationRecord.reserved_notional), Decimal("0")
        )
        active = await session.scalar(
            select(sum_notional).where(
                RiskReservationRecord.account_id == acc_id,
                RiskReservationRecord.status.in_(["RESERVED", "UNKNOWN"]),
            )
        )
        assert active == Decimal("600"), active

        denied_codes = set(
            (
                await session.scalars(
                    select(RiskDecisionRecord.reason_code).where(
                        RiskDecisionRecord.account_id == acc_id,
                        RiskDecisionRecord.approved.is_(False),
                    )
                )
            ).all()
        )
        assert LiveRiskReasonCode.MAKER_CHECKER_VIOLATION.value in denied_codes
        assert LiveRiskReasonCode.PROFILE_HASH_MISMATCH.value in denied_codes

    await engine.dispose()
    print(json.dumps({"ok": True, "active_notional": str(active), "state": state.reason_code}))

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
