"""Unit test SIG-8: thẻ tín hiệu đầy đủ (net R:R, rủi ro % vốn, hạng tin cậy, reply, H18–H20)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx2

from trading_bot.adapters.telegram.bot_api_sender import TelegramBotApiSender
from trading_bot.adapters.telegram.card_delivery import send_signal_card
from trading_bot.adapters.telegram.recording_sender import RecordingTelegramSender
from trading_bot.adapters.telegram.topic_sender import TopicBoundSender
from trading_bot.admin.form_choices import setup_code_choices, strategy_id_choices
from trading_bot.application.telegram_cards import signal_card_caption, signal_card_view_from
from trading_bot.application.telegram_signals import (
    SUPPRESSED_COOLDOWN,
    RecentSignalDelivery,
    SignalThrottlePolicy,
    TelegramSignalCandidate,
    build_signal_message,
    candidate_dedup_key,
    throttle_reason,
)
from trading_bot.domain.strategy.signal_card import build_signal_card

NOW = datetime(2026, 11, 18, 4, 30, tzinfo=UTC)


def _sample_candidate(
    *, strategy_id: str = "H18", timeframe: str = "5m", reason_code: str = "H18_LONG_ENTRY"
) -> TelegramSignalCandidate:
    card = build_signal_card(
        {
            "symbol": "BTCUSDT",
            "direction": "long",
            "strategy_id": strategy_id,
            "strategy_release_id": f"{strategy_id.lower()}-v1",
            "reason_code": reason_code,
            "feature_snapshot_id": "snap-h18-001",
            "signal_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
            "reference_price": "68000",
            "atr_value": "200",
            "recipe": {"stop_atr_multiple": "1.5", "take_profit_atr_multiple": "3.0"},
        },
        fee_rate=Decimal("0.0005"),
        account_risk_pct=Decimal("0.5"),
        confidence={"tier": "A", "label": "Hạng A"},
    )
    return TelegramSignalCandidate(
        decision_id=uuid4(),
        symbol="BTCUSDT",
        direction="LONG",
        timeframe=timeframe,
        strategy_id=strategy_id,
        strategy_release_id=f"{strategy_id.lower()}-v1",
        reference_price=Decimal("68000"),
        signal_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        reason_code=reason_code,
        detail_vi="Quét thanh khoản đáy phiên và đóng nến quay lại.",
        card=card,
    )


def test_signal_card_includes_net_rr_risk_pct_confidence_and_snapshot() -> None:
    cand = _sample_candidate()
    assert cand.card is not None
    assert cand.card["account_risk_pct"] == 0.5
    assert cand.card["feature_snapshot_id"] == "snap-h18-001"
    assert cand.card["confidence"] == {"tier": "A", "label": "Hạng A"}
    targets = cand.card["targets"]
    assert isinstance(targets, list)
    assert all(t["net_r_multiple"] is not None for t in targets)
    assert all(t["net_r_multiple"] < t["r_multiple"] for t in targets)

    text = build_signal_message(cand)
    assert "Rủi ro % vốn: 0,50%" in text
    assert "ròng" in text
    assert "Hạng A [A]" in text
    assert "Feature snapshot: snap-h18-001" in text
    assert "Tường thuật:" in text

    view = signal_card_view_from(cand)
    assert view.account_risk_pct == Decimal("0.5")
    assert view.feature_snapshot_id == "snap-h18-001"
    assert view.narrative is not None
    caption = signal_card_caption(view)
    assert "Rủi ro vốn: 0,5%" in caption
    assert "Tường thuật:" in caption
    assert "Snapshot: snap-h18-001" in caption


def test_scalping_cooldown_is_per_symbol_and_setup() -> None:
    policy = SignalThrottlePolicy(
        cooldown=timedelta(minutes=90), scalping_cooldown=timedelta(minutes=15)
    )
    h18 = _sample_candidate(strategy_id="H18", timeframe="5m", reason_code="H18_LONG_ENTRY")
    h19 = _sample_candidate(strategy_id="H18", timeframe="5m", reason_code="H18_RECLAIM_ALT")

    recent = [
        RecentSignalDelivery(dedup_key=candidate_dedup_key(h18), at=NOW - timedelta(minutes=10))
    ]
    assert throttle_reason(h18, recent, now=NOW, policy=policy) == SUPPRESSED_COOLDOWN
    # Setup khác trên cùng symbol không bị chặn bởi cooldown của setup kia.
    assert throttle_reason(h19, recent, now=NOW, policy=policy) is None
    # Sau 16 phút (> scalping_cooldown 15p) thì gửi lại được dù < 90p.
    assert throttle_reason(h18, recent, now=NOW + timedelta(minutes=16), policy=policy) is None


async def test_telegram_reply_to_original_message_and_bot_api_message_id() -> None:
    cand = _sample_candidate()
    sender = RecordingTelegramSender()
    orig_id = await send_signal_card(sender, 123456, cand)
    assert orig_id == 1

    topic_sender = TopicBoundSender(sender, thread_id=42)
    reply_id = await topic_sender.send_message(
        123456, "Cập nhật: đã chạm TP1.", reply_to_message_id=orig_id
    )
    assert reply_id == 2
    assert sender.replies[-1] == ("message", 123456, 2, 1)

    captured: list[dict] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 999}})

    api = TelegramBotApiSender(
        "123456:ABCDEF_valid_token", transport=httpx2.MockTransport(_handler)
    )
    msg_id = await api.send_message(123456, "Cập nhật TP1", reply_to_message_id=500)
    await api.aclose()
    assert msg_id == 999
    assert captured[0]["reply_parameters"] == {"message_id": 500}


def test_h18_to_h20_available_in_admin_filter_choices() -> None:
    strategies = {c.value for c in strategy_id_choices()}
    setups = {c.value for c in setup_code_choices()}
    for sid in ("H18", "H19", "H20"):
        assert sid in strategies
        assert f"{sid}_LONG_ENTRY" in setups
        assert f"{sid}_SHORT_ENTRY" in setups
