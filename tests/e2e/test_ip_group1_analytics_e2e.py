"""E2E test Nhóm 1 (CRY-76, CRY-36, CRY-53, CRY-80): Admin bot detail + Telegram H18–H20."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.adapters.telegram.card_delivery import send_signal_card
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.app import create_app
from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    TelegramSignalFilters,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.strategy.signal_card import build_signal_card


def _install_fakes(app: FastAPI, bot_id: UUID, decision_id: UUID) -> None:
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=uuid4(),
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )

    class _FakeAuth:
        async def issue_csrf_token(self, _cookie: str | None) -> str:
            return "csrf-test"

        async def authenticate(self, _cookie: str | None) -> SimpleNamespace:
            return SimpleNamespace(
                principal=principal,
                email="owner@crypidian.local",
                display_name="Owner",
                session_id=uuid4(),
            )

    class _FakeAdmin:
        async def get_bot(self, _p: Principal, *, bot_id: UUID) -> dict:
            return {
                "id": str(bot_id),
                "name": "T01 H18 5m",
                "status": "RUNNING",
                "symbol": "BTCUSDT",
                "versions": [
                    {
                        "id": str(uuid4()),
                        "status": "PUBLISHED",
                        "version_number": 1,
                        "overlay": {"strategy": {"release_id": "h18-v1"}},
                    }
                ],
            }

    class _FakeOps:
        def for_principal(self, _p: Principal) -> _FakeOps:
            return self

        async def bot_status(self, _bot_id: UUID) -> dict:
            return {
                "bot_id": str(bot_id),
                "name": "T01 H18 5m",
                "status": "RUNNING",
                "entry_state": "ACTIVE",
                "workspace_kill_state": "ACTIVE",
                "readiness": "READY",
                "entry_allowed": True,
                "revision": 1,
            }

    class _FakeReporting:
        async def get_bot_report(self, _query: object, _p: Principal) -> SimpleNamespace:
            return SimpleNamespace(
                as_dict=lambda: {
                    "totals": {"USDT": {}},
                    "trades": [],
                    "open_trades": [],
                }
            )

        async def get_latest_signal(self, _p: Principal, *, bot_id: UUID) -> dict:
            now = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)
            return {
                "latest_decision": {
                    "decision_id": str(decision_id),
                    "status": "ABSTAIN",
                    "reason_code": "H18_NO_SWEEP",
                    "detail_vi": "Nến chưa quét mức phiên.",
                    "evaluated_at": now.isoformat(),
                    "strategy_release_id": "h18-v1",
                    "checklist": [
                        {
                            "key": "sweep_reclaim",
                            "label_vi": "Giá đóng quay lại trên mức",
                            "actual": "68.123",
                            "operator": ">",
                            "threshold": "68.450",
                            "passed": False,
                        }
                    ],
                },
                "card": None,
            }

    app.state.auth_service = _FakeAuth()
    app.state.bot_admin_service = _FakeAdmin()
    app.state.bot_operations_service = _FakeOps()
    app.state.bot_reporting_service = _FakeReporting()
    app.dependency_overrides[current_identity] = lambda: SimpleNamespace(
        principal=principal,
        email="owner@crypidian.local",
        display_name="Owner",
        session_id=uuid4(),
    )


def test_e2e_bot_detail_renders_mi2_counterfactual_and_mi6_narrative() -> None:
    bot_id = uuid4()
    decision_id = uuid4()
    app = create_app(AppSettings(app_env="test"))
    with TestClient(app) as client:
        _install_fakes(app, bot_id, decision_id)
        client.cookies.set("crypidian_session", "valid-session")
        resp = client.get(f"/admin/bots/{bot_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Tường thuật quyết định" in body
    assert "data-setup-counterfactual" in body
    assert "+0,48%" in body
    assert "ĐỨNG NGOÀI" in body
    assert str(decision_id)[:8] in body


async def test_e2e_h18_to_h20_signal_filter_card_and_reply_traceability() -> None:
    now = datetime(2026, 11, 18, 4, 30, tzinfo=UTC)
    decision_id = uuid4()
    snapshot_id = "snap-e2e-h20-42"
    card = build_signal_card(
        {
            "symbol": "ETHUSDT",
            "direction": "short",
            "strategy_id": "H20",
            "strategy_release_id": "h20-v1",
            "reason_code": "H20_SHORT_ENTRY",
            "feature_snapshot_id": snapshot_id,
            "signal_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "reference_price": "3200",
            "atr_value": "20",
            "recipe": {"stop_atr_multiple": "1.5", "take_profit_atr_multiple": "3.0"},
        },
        fee_rate=Decimal("0.0005"),
        account_risk_pct=Decimal("0.5"),
        confidence={"tier": "B", "label": "Hạng B"},
    )
    candidate = TelegramSignalCandidate(
        decision_id=decision_id,
        symbol="ETHUSDT",
        direction="SHORT",
        timeframe="5m",
        strategy_id="H20",
        strategy_release_id="h20-v1",
        reference_price=Decimal("3200"),
        signal_at=now,
        expires_at=now + timedelta(minutes=15),
        reason_code="H20_SHORT_ENTRY",
        detail_vi="BOS giảm M5 có retest hợp lệ.",
        card=card,
    )
    filters = TelegramSignalFilters(
        strategy_ids=("H18", "H19", "H20"),
        setup_codes=("H20_SHORT_ENTRY",),
    )
    assert filters.matches(candidate) is True

    sender = RecordingTelegramSender()
    orig_msg_id = await send_signal_card(sender, 987654, candidate)
    assert orig_msg_id is not None
    assert len(sender.photos) == 1
    caption = sender.photos[0][2]
    assert str(decision_id) in caption
    assert snapshot_id in caption
    assert "ròng" in caption
    assert "0,5%" in caption

    # Reply cập nhật vào đúng tin gốc.
    reply_msg_id = await sender.send_message(
        987654,
        f"Cập nhật tín hiệu {decision_id}: đã chạm TP1.",
        reply_to_message_id=orig_msg_id,
    )
    assert reply_msg_id is not None
    assert sender.replies[-1] == ("message", 987654, reply_msg_id, orig_msg_id)
