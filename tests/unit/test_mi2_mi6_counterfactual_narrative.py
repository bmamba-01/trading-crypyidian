"""Unit test MI-2 (counterfactual theo ngưỡng) & MI-6 (tường thuật quyết định theo mẫu)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.application.market_narrative import (
    DECISION_NARRATIVE_VERSION,
    decision_narrative,
    format_decision_narrative_text,
)
from trading_bot.domain.strategy.setup_progress import (
    counterfactual_gap,
    format_setup_missing_vi,
    missing_counterfactual,
    setup_progress,
)
from trading_bot.telegram.bot_commands import BotCommand, BotCommandHandler, telegram_principal

NOW = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)


def test_counterfactual_gap_numeric_and_vietnamese_format() -> None:
    item = {
        "key": "band_breakout",
        "label_vi": "Giá đóng phá ngưỡng",
        "actual": "68.123",
        "operator": ">",
        "threshold": "68.450",
        "passed": False,
    }
    gap = counterfactual_gap(item)
    assert gap is not None
    assert gap["target_value"] == 68450.0
    assert gap["delta_abs"] == 327.0
    assert gap["delta_pct"] == pytest.approx(0.48, abs=0.01)
    assert "cần > 68.450" in gap["gap_text_vi"]
    assert "+0,48%" in gap["gap_text_vi"]


def test_counterfactual_gap_duration_and_closest_selection() -> None:
    items = [
        {
            "key": "adx",
            "label_vi": "ADX14 1h",
            "actual": "10",
            "operator": ">=",
            "threshold": "20",
            "passed": False,
        },
        {
            "key": "funding_clear",
            "label_vi": "Cách mốc funding hơn 10 phút",
            "actual": "5 phút",
            "operator": "≥",
            "threshold": "10 phút",
            "passed": False,
        },
        {
            "key": "ema_gap",
            "label_vi": "Khoảng cách EMA",
            "actual": "99",
            "operator": ">=",
            "threshold": "100",
            "passed": False,
        },
    ]
    cf = missing_counterfactual(items)
    assert cf is not None
    assert cf["key"] == "adx"
    assert cf["closest"]["key"] == "ema_gap"

    dur = counterfactual_gap(items[1])
    assert dur is not None
    assert dur["delta_abs"] == 5.0
    assert "5 phút" in dur["gap_text_vi"]


def test_setup_progress_exposes_counterfactual_and_shared_formatter() -> None:
    decisions = [
        {
            "evaluated_at": NOW.isoformat(),
            "status": "ABSTAIN",
            "reason_code": "H01_ADX_BELOW_20",
            "detail_vi": "ADX14 1h phải đạt tối thiểu 20.",
            "checklist": [
                {
                    "key": "ema_cross",
                    "label_vi": "EMA20 cắt EMA80",
                    "actual": "đạt",
                    "operator": "=",
                    "threshold": "đạt",
                    "passed": True,
                },
                {
                    "key": "htf_trend",
                    "label_vi": "Xu hướng 4h",
                    "actual": "đạt",
                    "operator": "=",
                    "threshold": "đạt",
                    "passed": True,
                },
                {
                    "key": "adx",
                    "label_vi": "ADX14 1h",
                    "actual": "15",
                    "operator": ">=",
                    "threshold": "20",
                    "passed": False,
                },
            ],
            "candidate": None,
        }
    ]
    progress = setup_progress(strategy_release_id="h01-v1", decisions=decisions, bonus=[], now=NOW)
    assert progress["counterfactual"] is not None
    assert progress["counterfactual"]["delta_abs"] == 5.0
    text = format_setup_missing_vi(progress)
    assert text is not None
    assert "ADX14 1h" in text
    assert "+33,33%" in text


def test_decision_narrative_abstain_and_candidate_trace_to_source() -> None:
    assert DECISION_NARRATIVE_VERSION == "mi6-decision-narrative-v1"
    dec_id = str(uuid4())
    abstain_dec = {
        "decision_id": dec_id,
        "evaluated_at": NOW.isoformat(),
        "status": "ABSTAIN",
        "reason_code": "H01_ADX_BELOW_20",
        "detail_vi": "ADX14 1h dưới ngưỡng 20.",
        "strategy_release_id": "h01-v1",
    }
    setup = {
        "required": {"passed": 2, "total": 3},
        "counterfactual": {
            "key": "adx",
            "label_vi": "ADX14 1h",
            "summary_vi": "Còn thiếu: ADX14 1h — hiện 15, cần >= 20, còn +33,33%",
        },
    }
    sentences = decision_narrative(abstain_dec, setup=setup)
    assert len(sentences) == 2
    assert "ĐỨNG NGOÀI" in sentences[0]["text"]
    assert sentences[0]["source"]["decision_id"] == dec_id
    assert sentences[1]["source"]["kind"] == "setup_counterfactual"
    assert sentences[1]["source"]["condition_key"] == "adx"
    assert "Tiến độ điều kiện 2/3" in format_decision_narrative_text(sentences)

    cand_dec = {
        "decision_id": dec_id,
        "evaluated_at": NOW.isoformat(),
        "status": "CANDIDATE",
        "reason_code": "H18_LONG_ENTRY",
        "detail_vi": "Quét đáy phiên và lấy lại mức.",
        "strategy_release_id": "h18-v1",
        "candidate": {"direction": "long"},
    }
    card = {
        "entry_zone": {"low": 68000.0, "high": 68050.0},
        "stop": 67500.0,
        "confidence": {"tier": "A", "label": "Hạng A"},
    }
    cand_sentences = decision_narrative(cand_dec, card=card)
    assert any("ĐỦ ĐIỀU KIỆN MUA (LONG)" in s["text"] for s in cand_sentences)
    assert any(s["source"]["kind"] == "signal_card" for s in cand_sentences)


async def test_telegram_bot_status_includes_mi2_mi6_insights() -> None:
    bot_id = uuid4()
    ws_id = uuid4()
    principal = telegram_principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=ws_id,
        roles=frozenset(),
    )

    class _FakeOps:
        def for_principal(self, _p: object) -> _FakeOps:
            return self

        async def bot_status(self, _bot_id: object) -> SimpleNamespace:
            return SimpleNamespace(
                bot_id=bot_id,
                name="T01 H01 1h",
                status="RUNNING",
                entry_state=SimpleNamespace(value="ACTIVE"),
                workspace_kill_state=SimpleNamespace(value="ACTIVE"),
                readiness="READY",
            )

    class _FakeInsights:
        async def get_latest_signal(self, _p: object, *, bot_id: object) -> dict:
            return {
                "latest_decision": {
                    "decision_id": str(uuid4()),
                    "evaluated_at": (NOW - timedelta(minutes=5)).isoformat(),
                    "status": "ABSTAIN",
                    "reason_code": "H01_ADX_BELOW_20",
                    "detail_vi": "ADX14 1h dưới 20.",
                    "strategy_release_id": "h01-v1",
                    "checklist": [
                        {
                            "key": "adx",
                            "label_vi": "ADX14 1h",
                            "actual": "16",
                            "operator": ">=",
                            "threshold": "20",
                            "passed": False,
                        }
                    ],
                },
                "card": None,
            }

    handler = BotCommandHandler(_FakeOps(), decision_insights=_FakeInsights())  # type: ignore[arg-type]
    result = await handler.handle_bot_command(principal, BotCommand.STATUS, bot_id)
    assert result.ok is True
    assert "ĐỨNG NGOÀI" in result.message
    assert "+25%" in result.message
