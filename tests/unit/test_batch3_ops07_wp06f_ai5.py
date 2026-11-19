"""Unit test tổng hợp Đợt 3: CRY-263, CRY-266, CRY-439, CRY-440 và CRY-438."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trading_bot.admin.learning_paper_entry_block_router import (
    router as paper_entry_block_router,
)
from trading_bot.admin.web_router import _build_operations_health_data
from trading_bot.admin.web_router import router as admin_router
from trading_bot.application.learning_signal_dataset import (
    SignalOutcomeSampleInput,
    build_signal_outcome_dataset_manifest,
)
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.execution.runtime_control import (
    BotEntryState,
    BotReadiness,
    EntryBlockReason,
    EntryGate,
    EntryGateInput,
    RuntimeActivityStatus,
    RuntimeOperation,
    WorkspaceKillState,
)
from trading_bot.domain.learning.dataset import DatasetSourceReference, DatasetSplit
from trading_bot.domain.research.source_policy import SourceUsePurpose
from trading_bot.domain.risk.circuit_breaker import (
    BreakerState,
    BreakerTriggerKind,
    CircuitBreakerEngine,
    CircuitBreakerPolicy,
    CircuitBreakerSnapshot,
    OperationalTelemetry,
)
from trading_bot.domain.strategy.signal_outcome import (
    ForwardBar,
    SignalLevels,
    score_signal,
)

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


# ---------------------------------------------------------------------------
# CRY-263 & CRY-266: Real telemetry & Circuit Breaker runtime gate
# ---------------------------------------------------------------------------


def test_operational_telemetry_marks_missing_fields_as_unknown_not_zero() -> None:
    """Trường telemetry chưa đo được phải ghi rõ 'unknown' trong evidence, không mặc định 0."""
    telemetry = OperationalTelemetry(
        evaluated_at=NOW,
        feed_age_seconds=None,
        daily_loss=None,
        drawdown_pct=None,
        consecutive_api_errors=0,
        outbox_lag_seconds=None,
        clock_skew_ms=None,
        unprotected_exposure_count=None,
        reconciliation_discrepancy_count=None,
        unknown_order_count=None,
    )
    evidence = telemetry.to_evidence()
    assert evidence["feed_age_seconds"] == "unknown"
    assert evidence["daily_loss"] == "unknown"
    assert evidence["drawdown_pct"] == "unknown"
    assert evidence["unknown_order_count"] == "unknown"
    assert "feed_age_seconds" in evidence["unknown_fields"]
    snap = CircuitBreakerEngine().evaluate(telemetry)
    assert snap.state is BreakerState.CLOSED
    assert snap.evidence["feed_age_seconds"] == "unknown"


def test_circuit_breaker_from_snapshot_hydrates_open_state_and_entry_gate_blocks() -> None:
    """Trạng thái OPEN bền vững khôi phục vào engine và EntryGate chỉ chặn NEW_ENTRY."""
    persisted = CircuitBreakerSnapshot(
        state=BreakerState.OPEN,
        entry_allowed=False,
        protective_exit_allowed=True,
        tripped_reasons=(BreakerTriggerKind.MAX_DRAWDOWN_BREACH,),
        detail_vi="Vượt ngưỡng drawdown",
        tripped_at=NOW - timedelta(minutes=2),
        cooldown_until=NOW + timedelta(minutes=13),
        revision=4,
        evidence={"drawdown_pct": "12.5"},
    )
    engine = CircuitBreakerEngine.from_snapshot(
        persisted,
        policy=CircuitBreakerPolicy(cooldown_period=timedelta(minutes=15)),
    )
    # Ping sạch trong cooldown không được clear breaker
    clean = OperationalTelemetry(
        evaluated_at=NOW,
        feed_age_seconds=1,
        daily_loss=Decimal("0"),
        drawdown_pct=Decimal("1.0"),
        consecutive_api_errors=0,
        outbox_lag_seconds=0,
        clock_skew_ms=5,
        unprotected_exposure_count=0,
        reconciliation_discrepancy_count=0,
        unknown_order_count=0,
    )
    after_ping = engine.evaluate(clean)
    assert after_ping.state is BreakerState.OPEN
    assert after_ping.entry_allowed is False
    assert after_ping.revision == 4

    gate_input = EntryGateInput(
        account_enabled=True,
        bot_enabled=True,
        workspace_kill_state=WorkspaceKillState.ACTIVE,
        bot_entry_state=BotEntryState.ENABLED,
        readiness=BotReadiness.READY,
        lease_held=True,
        open_incident=False,
        activity_status=RuntimeActivityStatus.VERIFIED,
        control_available=True,
        learning_incident_hold_active=False,
        circuit_breaker_open=True,
    )
    entry_decision = EntryGate.evaluate(gate_input, RuntimeOperation.NEW_ENTRY)
    assert entry_decision.allowed is False
    assert entry_decision.reason is EntryBlockReason.CIRCUIT_BREAKER_OPEN

    exit_decision = EntryGate.evaluate(gate_input, RuntimeOperation.PROTECTIVE_EXIT)
    assert exit_decision.allowed is True
    monitor_decision = EntryGate.evaluate(gate_input, RuntimeOperation.PROTECTIVE_MONITORING)
    assert monitor_decision.allowed is True


@pytest.mark.asyncio
async def test_build_operations_health_data_uses_real_service_or_unknown() -> None:
    """Khi không có DB trả 'unknown'; khi có service thật thì ủy quyền đọc telemetry thật."""
    ws_id = uuid4()
    no_db = await _build_operations_health_data(
        workspace_id=str(ws_id),
        runtime=[{"error_count": 0}],
        workspace={"kill_state": "ACTIVE"},
        correlation_id="corr-test-1",
        now_utc=NOW,
    )
    assert no_db["metadata"]["telemetry"]["feed_age_seconds"] == "unknown"

    fake_service = SimpleNamespace(
        load_health_snapshot=AsyncMock(
            return_value={"status": "HALTED", "breaker": {"state": "OPEN"}}
        )
    )
    fake_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(operational_health_service=fake_service))
    )
    with_db = await _build_operations_health_data(
        workspace_id=str(ws_id),
        runtime=[],
        workspace={"kill_state": "ACTIVE"},
        correlation_id="corr-test-2",
        now_utc=NOW,
        request=fake_request,  # type: ignore[arg-type]
    )
    assert with_db["status"] == "HALTED"
    fake_service.load_health_snapshot.assert_awaited_once()


def test_operations_breaker_resume_endpoint_requires_permission_and_csrf() -> None:
    """POST /api/operations/breaker/resume yêu cầu RUNTIME_RESUME và CSRF hợp lệ."""
    ws_id = uuid4()
    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = AppSettings()
    app.state.auth_service = SimpleNamespace(verify_csrf=AsyncMock(return_value=None))
    app.state.operational_health_service = SimpleNamespace(
        resume_breaker=AsyncMock(
            return_value=CircuitBreakerSnapshot(
                state=BreakerState.CLOSED,
                entry_allowed=True,
                protective_exit_allowed=True,
                tripped_reasons=(),
                detail_vi="Đã mở lại",
                tripped_at=None,
                cooldown_until=None,
                revision=5,
                evidence={},
            )
        )
    )

    viewer = AuthIdentity(
        email="viewer@crypidian.io",
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=ws_id,
            roles=frozenset({Role.STRATEGY_ADMIN}),
            mfa_method=MfaMethod.TOTP,
            mfa_verified_at=NOW,
        ),
    )
    app.dependency_overrides[current_identity] = lambda: viewer
    client = TestClient(app)
    forbidden = client.post("/admin/api/operations/breaker/resume?reason=ok&csrf_token=tok")
    assert forbidden.status_code == 403

    owner = AuthIdentity(
        email="owner@crypidian.io",
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=ws_id,
            roles=frozenset({Role.OWNER}),
            mfa_method=MfaMethod.TOTP,
            mfa_verified_at=NOW,
        ),
    )
    app.dependency_overrides[current_identity] = lambda: owner
    ok = client.post("/admin/api/operations/breaker/resume?reason=Da+kiem+tra&csrf_token=tok")
    assert ok.status_code == 200
    assert ok.json()["state"] == "CLOSED"


# ---------------------------------------------------------------------------
# CRY-439 & CRY-440: Scorer History DB Role & Operator PAPER Entry Block
# ---------------------------------------------------------------------------


def test_settings_validate_scorer_history_database_identity() -> None:
    """Scorer-history credential chỉ chấp nhận admin-web và đúng role crypidian_scorer_history."""
    ok = AppSettings(
        process_role="admin-web",
        scorer_history_database_user="crypidian_scorer_history",
        scorer_history_database_password_file=Path("/run/secrets/database_scorer_history_password"),
    )
    assert ok.scorer_history_database_user == "crypidian_scorer_history"

    with pytest.raises(ValidationError, match="crypidian_scorer_history"):
        AppSettings(
            process_role="admin-web",
            scorer_history_database_user="crypidian_admin_web",
            scorer_history_database_password_file=Path("/run/secrets/x"),
        )

    with pytest.raises(ValidationError, match="admin-web"):
        AppSettings(
            process_role="execution-gateway",
            database_user="crypidian_execution_gateway",
            database_password_file=Path("/run/secrets/db_gw"),
            scorer_history_database_user="crypidian_scorer_history",
            scorer_history_database_password_file=Path("/run/secrets/x"),
        )


def test_create_app_scorer_history_read_model_is_fail_closed_without_credential() -> None:
    """Thiếu scorer_history credential/session factory thì read model phải None (fail-closed)."""
    from trading_bot.app import create_app

    fake_sessions = MagicMock()
    app_without_scorer = create_app(
        AppSettings(process_role="admin-web"),
        session_factory=fake_sessions,
    )
    with TestClient(app_without_scorer):
        assert app_without_scorer.state.learning_scoring_score_event_read_model is None
        assert app_without_scorer.state.learning_scoring_report_read_model is not None

    fake_scorer_sessions = MagicMock()
    app_with_scorer = create_app(
        AppSettings(process_role="admin-web"),
        session_factory=fake_sessions,
        scorer_history_session_factory=fake_scorer_sessions,
    )
    with TestClient(app_with_scorer):
        assert app_with_scorer.state.learning_scoring_score_event_read_model is not None


def test_legacy_block_paper_entry_endpoint_is_removed() -> None:
    """API cũ theo BotDecision không được giữ song song với route scorer event mới."""
    app = FastAPI()
    app.include_router(paper_entry_block_router)
    client = TestClient(app)
    legacy_path = f"/admin/learning/scoring/decisions/{uuid4()}/block-paper-entry"
    current_path = "/admin/learning/scoring/events/{score_event_id}/paper-block"
    assert any(route.path == current_path for route in paper_entry_block_router.routes)
    response = client.post(
        legacy_path,
        json={"reason": "Không dùng API cũ", "confirm": True},
        headers={"X-CSRF-Token": "csrf", "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CRY-438: Signal outcome as separate AI-5 dataset label
# ---------------------------------------------------------------------------


def test_score_signal_captures_bar_sources() -> None:
    """Chấm tín hiệu lưu danh sách nguồn nến đã dùng trong horizon."""
    levels = SignalLevels(
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("98"),
        tp1=Decimal("102"),
        tp2=Decimal("104"),
    )
    bars = [
        ForwardBar(
            open_time=NOW + timedelta(hours=i),
            close_time=NOW + timedelta(hours=i + 1),
            open=Decimal("100"),
            high=Decimal("103"),
            low=Decimal("99.5"),
            close=Decimal("102"),
            ref=f"b{i}",
            source="BINANCE_USDM_REST" if i == 0 else "BINANCE_USDM",
        )
        for i in range(4)
    ]
    res = score_signal(levels, signal_at=NOW, window=bars)
    assert res.bar_sources == ("BINANCE_USDM", "BINANCE_USDM_REST")


def test_signal_outcome_dataset_manifest_enforces_training_rights_and_isolation() -> None:
    """Dataset tín hiệu chỉ nhận mẫu mọi nến có quyền TRAINING, tách biệt với nhãn lệnh."""
    ws_id = uuid4()
    rest_ref = DatasetSourceReference(
        source_id="BINANCE_USDM_REST",
        item_id="BINANCE_USDM_REST",
        revision_id="1",
        policy_id="pol-rest",
        policy_sequence=1,
        purpose=SourceUsePurpose.TRAINING,
        rights_hash=SHA_A,
    )
    holdout_start = NOW + timedelta(days=5)
    eval_cutoff = NOW + timedelta(days=10)

    rows = [
        # Hợp lệ -> DEVELOPMENT
        SignalOutcomeSampleInput(
            outcome_id=uuid4(),
            decision_id=uuid4(),
            symbol="BTCUSDT",
            timeframe="1h",
            signal_at=NOW + timedelta(days=1),
            feature_available_at=NOW + timedelta(days=1),
            feature_snapshot_hash=SHA_A,
            horizon_bars=4,
            horizon_end_at=NOW + timedelta(days=1, hours=4),
            status="SCORED",
            label=1,
            label_version="signal-gross-r-band-v1",
            outcome_hash=SHA_B,
            bar_sources=("BINANCE_USDM_REST",),
        ),
        # Hợp lệ -> HOLDOUT
        SignalOutcomeSampleInput(
            outcome_id=uuid4(),
            decision_id=uuid4(),
            symbol="BTCUSDT",
            timeframe="1h",
            signal_at=holdout_start + timedelta(hours=1),
            feature_available_at=holdout_start + timedelta(hours=1),
            feature_snapshot_hash=SHA_A,
            horizon_bars=4,
            horizon_end_at=holdout_start + timedelta(hours=5),
            status="SCORED",
            label=-1,
            label_version="signal-gross-r-band-v1",
            outcome_hash=SHA_B,
            bar_sources=("BINANCE_USDM_REST",),
        ),
        # Nguồn live chỉ có paper_demo -> loại
        SignalOutcomeSampleInput(
            outcome_id=uuid4(),
            decision_id=uuid4(),
            symbol="BTCUSDT",
            timeframe="1h",
            signal_at=NOW + timedelta(days=2),
            feature_available_at=NOW + timedelta(days=2),
            feature_snapshot_hash=SHA_A,
            horizon_bars=4,
            horizon_end_at=NOW + timedelta(days=2, hours=4),
            status="SCORED",
            label=1,
            label_version="signal-gross-r-band-v1",
            outcome_hash=SHA_B,
            bar_sources=("BINANCE_USDM",),
        ),
        # Cắt ranh giới holdout -> loại
        SignalOutcomeSampleInput(
            outcome_id=uuid4(),
            decision_id=uuid4(),
            symbol="BTCUSDT",
            timeframe="1h",
            signal_at=holdout_start - timedelta(hours=1),
            feature_available_at=holdout_start - timedelta(hours=1),
            feature_snapshot_hash=SHA_A,
            horizon_bars=4,
            horizon_end_at=holdout_start + timedelta(hours=3),
            status="SCORED",
            label=0,
            label_version="signal-gross-r-band-v1",
            outcome_hash=SHA_B,
            bar_sources=("BINANCE_USDM_REST",),
        ),
    ]

    manifest = build_signal_outcome_dataset_manifest(
        workspace_id=ws_id,
        rows=rows,
        training_licensed_sources={"BINANCE_USDM_REST": rest_ref},
        holdout_start=holdout_start,
        evaluation_cutoff=eval_cutoff,
        as_of=eval_cutoff,
    )
    assert manifest.label_version == "signal-gross-r-band-v1"
    assert len(manifest.samples) == 2
    assert {s.split for s in manifest.samples} == {DatasetSplit.DEVELOPMENT, DatasetSplit.HOLDOUT}
    assert len(manifest.exclusions) == 2
    reasons = {r for ex in manifest.exclusions for r in ex.reasons}
    assert "SIGNAL_BAR_SOURCE_NOT_TRAINING_LICENSED" in reasons
    assert "LABEL_WINDOW_CROSSES_HOLDOUT_BOUNDARY" in reasons

    with pytest.raises(ValueError, match="không trộn"):
        build_signal_outcome_dataset_manifest(
            workspace_id=ws_id,
            rows=rows,
            training_licensed_sources={"BINANCE_USDM_REST": rest_ref},
            holdout_start=holdout_start,
            evaluation_cutoff=eval_cutoff,
            as_of=eval_cutoff,
            label_version="net-r-band-v1",
        )
