"""AI-4: digest lịch vĩ mô cho AI Analyst, validator ngày giờ và khối lịch trên thẻ Telegram."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.application.ai_analyst import build_prompt, topic_report, validate_analysis
from trading_bot.domain.research.macro_calendar import (
    BEA_SOURCE_ID,
    FOMC_SOURCE_ID,
    PFEI_SOURCE_ID,
    MacroCalendarFetch,
    MacroEvent,
    macro_digest,
)

NOW = datetime(2026, 11, 16, 7, tzinfo=UTC)
RETAIL = MacroEvent(
    PFEI_SOURCE_ID, "r", "Retail Sales", datetime(2026, 11, 17, 13, 30, tzinfo=UTC), True
)
GDP = MacroEvent(
    BEA_SOURCE_ID, "g", "Gross Domestic Product", datetime(2026, 11, 25, 13, 30, tzinfo=UTC), True
)
MINOR = MacroEvent(BEA_SOURCE_ID, "m", "Travel", datetime(2026, 11, 18, 15, tzinfo=UTC), False)
FAR = datetime(2027, 3, 1, tzinfo=UTC)
FETCHES = [
    MacroCalendarFetch("b", BEA_SOURCE_ID, NOW - timedelta(hours=1), (GDP, MINOR,
        MacroEvent(BEA_SOURCE_ID, "f", "Gross Domestic Product", FAR, True))),
    MacroCalendarFetch("f", FOMC_SOURCE_ID, NOW - timedelta(hours=1),
        (MacroEvent(FOMC_SOURCE_ID, "x", "FOMC rate decision", FAR, True),)),
    MacroCalendarFetch("p", PFEI_SOURCE_ID, NOW - timedelta(hours=1), (RETAIL,
        MacroEvent(PFEI_SOURCE_ID, "y", "Retail Sales", FAR, True))),
]  # fmt: skip


def test_digest_lists_high_impact_events_in_vietnam_time() -> None:
    digest = macro_digest(FETCHES, NOW)
    assert digest["state"] == {"blackout": False, "is_fresh": True, "blocking_event": None}
    assert digest["missing_sources"] == []
    # GDP 25/11 nằm ngoài 7 ngày; sự kiện tác động thấp bị bỏ.
    assert [(e["label_vi"], e["vn_date"], e["vn_time"]) for e in digest["upcoming"]] == [
        ("Doanh số bán lẻ", "17/11", "20:30")
    ]
    assert digest["horizon_days"] == 7
    # Chưa có lần tải nào trước NOW → nguồn thiếu, trạng thái chưa biết.
    empty = macro_digest(FETCHES, NOW - timedelta(hours=2))
    assert empty["state"]["blackout"] is None
    assert len(empty["missing_sources"]) == 3


REPORT: dict[str, Any] = {
    "report_hash": "a" * 64,
    "window": {"start": "2026-11-15T07:00:00+00:00", "end": "2026-11-16T07:00:00+00:00"},
    "bots": [
        {
            "bot_name": "T01 H03 1h · 20 coin",
            "config": {"strategy_family": "H03", "timeframe": "1h", "release_id": "h03-v3-1h"},
            "funnel": {"candidates": 5},
            "performance": {"net_pnl_usdt": "-12.5", "closed_trades": 2},
        }
    ],
    "macro_calendar": macro_digest(FETCHES, NOW),
}


def _out(statement: str, refs: list[str], summary: str = "Tóm tắt.") -> dict[str, Any]:
    return {
        "summary": summary,
        "findings": [{"statement": statement, "evidence_refs": refs}],
        "hypotheses": [],
        "proposals": [],
        "confidence": "low",
    }


def test_validator_accepts_report_dates_and_rejects_invented_ones() -> None:
    ref = ["/macro_calendar/upcoming/0"]
    ok = validate_analysis(_out("Doanh số bán lẻ công bố 17/11 lúc 20:30 giờ VN.", ref), REPORT)
    assert ok.ok, ok.errors
    # Giờ ET/UTC của cùng mốc, ngày ISO của cửa sổ cũng hợp lệ.
    assert validate_analysis(_out("Mốc 13:30 UTC, tức 08:30 ET.", ref), REPORT).ok
    assert validate_analysis(
        _out(
            "PnL -12.5 USDT.",
            ["/bots/0/performance/net_pnl_usdt"],
            "Cửa sổ 2026-11-15 → 2026-11-16.",
        ),
        REPORT,
    ).ok
    # "7 ngày tới" là tầm lịch, không phải số bịa.
    assert validate_analysis(_out("Trong 7 ngày tới có một sự kiện.", ref), REPORT).ok
    bad_date = validate_analysis(_out("CPI công bố 26/11 lúc 20:30.", ref), REPORT)
    assert any("26/11" in e for e in bad_date.errors)
    bad_time = validate_analysis(_out("Công bố 17/11 lúc 21h00.", ref), REPORT)
    assert any("21h00" in e for e in bad_time.errors)
    bad_summary = validate_analysis(_out("x", ref, summary="Fed họp 2026-12-09."), REPORT)
    assert any("2026-12-09" in e for e in bad_summary.errors)
    # Mảnh ngày giờ của lịch không thành "số hợp lệ" cho tóm tắt.
    assert not validate_analysis(_out("x", ref, summary="Lãi 30 USDT."), REPORT).ok


def test_topic_report_and_prompt_carry_macro_calendar() -> None:
    topic = topic_report(REPORT, "H03")
    assert topic["macro_calendar"] == REPORT["macro_calendar"]
    prompt = build_prompt(topic)
    assert "macro_calendar" in prompt
    assert "vn_date" in prompt
    without = copy.deepcopy(REPORT)
    del without["macro_calendar"]
    assert "macro_calendar" not in topic_report(without, "H03")


def test_analysis_card_shows_macro_block_from_calendar_not_ai() -> None:
    from trading_bot.adapters.telegram.card_renderer import render_analysis_card
    from trading_bot.application.telegram_cards import (
        analysis_card_caption,
        analysis_card_view_from,
    )

    output = _out("Bán lẻ 17/11 20:30.", ["/macro_calendar/upcoming/0"])
    view = analysis_card_view_from("family:H03", REPORT, output)
    assert view.macro_events == ("17/11 20:30 · Doanh số bán lẻ",)
    assert view.macro_horizon_days == 7
    assert view.macro_blackout is False
    assert "Vĩ mô kế tiếp (giờ VN): 17/11 20:30 · Doanh số bán lẻ" in analysis_card_caption(view)
    assert render_analysis_card(view).startswith(b"\x89PNG")
    blocked = copy.deepcopy(REPORT)
    blocked["macro_calendar"]["state"]["blackout"] = True
    caption = analysis_card_caption(analysis_card_view_from("family:H03", blocked, output))
    assert "khoảng cấm" in caption
    legacy = {k: v for k, v in REPORT.items() if k != "macro_calendar"}
    assert analysis_card_view_from("family:H03", legacy, output).macro_events == ()


def test_number_tokenizer_does_not_backtrack_into_units() -> None:
    from trading_bot.application.ai_analyst import _NUMBER

    # 16/11: "15m" từng bị đọc thành "1", "1.2568R" thành "1.256" → kết quả AI bị loại oan.
    assert _NUMBER.findall("tất cả ở khung 15m, nến 4h") == []
    assert _NUMBER.findall("MFE 1.2568R, win rate 50%, PnL -72.5969.") == [
        "1.2568R",
        "50%",
        "-72.5969",
    ]
    report = {
        "bots": [{"bot_name": "T09", "config": {}, "performance": {"mfe_r": {"max": "1.2568"}}}]
    }
    output = _out("MFE lớn nhất 1.2568R ở khung 15m.", ["/bots/0/performance/mfe_r/max"])
    assert validate_analysis(output, report).ok
    wrong = _out("MFE lớn nhất 1.4R.", ["/bots/0/performance/mfe_r/max"])
    assert not validate_analysis(wrong, report).ok
