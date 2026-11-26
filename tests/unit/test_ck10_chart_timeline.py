"""CK-10 (CRY-228): dòng thời gian tín hiệu/khớp PAPER/lịch vĩ mô và xác minh nguồn H06."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from trading_bot.domain.market.chart_timeline import (
    TIMELINE_VERSION,
    build_chart_timeline,
    macro_h06_status_view,
)
from trading_bot.domain.market.economic_calendar import news_condition
from trading_bot.domain.research.macro_calendar import (
    BEA_SOURCE_ID,
    FOMC_SOURCE_ID,
    PFEI_SOURCE_ID,
    MacroCalendarFetch,
    MacroEvent,
    macro_state,
)
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    StrategySelection,
)
from trading_bot.domain.strategy.rules import H06RangeMeanReversionRule, NoTrade

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)


def _official_fetches(*, with_blackout: bool) -> list[MacroCalendarFetch]:
    fetched_at = NOW - timedelta(hours=2)
    future_cover = NOW + timedelta(days=14)
    fomc_time = NOW + timedelta(minutes=15) if with_blackout else NOW + timedelta(hours=5)
    return [
        MacroCalendarFetch(
            fetch_id="macro:bea-1",
            source_id=BEA_SOURCE_ID,
            fetched_at=fetched_at,
            events=(
                MacroEvent(
                    BEA_SOURCE_ID,
                    "BEA:GDP:cover",
                    "Gross Domestic Product",
                    future_cover,
                    True,
                ),
            ),
        ),
        MacroCalendarFetch(
            fetch_id="macro:fomc-1",
            source_id=FOMC_SOURCE_ID,
            fetched_at=fetched_at,
            events=(
                MacroEvent(
                    FOMC_SOURCE_ID,
                    "FOMC:2026-11-20",
                    "FOMC rate decision",
                    fomc_time,
                    True,
                ),
                MacroEvent(
                    FOMC_SOURCE_ID,
                    "FOMC:cover",
                    "FOMC rate decision",
                    future_cover,
                    True,
                ),
            ),
        ),
        MacroCalendarFetch(
            fetch_id="macro:pfei-1",
            source_id=PFEI_SOURCE_ID,
            fetched_at=fetched_at,
            events=(
                MacroEvent(
                    PFEI_SOURCE_ID,
                    "PFEI:CPI:cover",
                    "Consumer Price Index",
                    future_cover,
                    True,
                ),
            ),
        ),
    ]


def test_build_chart_timeline_merges_and_sorts_utc_point_in_time() -> None:
    signals = [
        {
            "evaluated_at": "2026-11-20T12:15:00+00:00",
            "status": "CANDIDATE",
            "direction": "long",
            "symbol": "BTCUSDT",
            "strategy_release_id": "h01-v1",
            "reference_price": "91250.5",
            "reason_code": "H01_TREND_SIGNAL",
        },
        {
            # Không phải CANDIDATE → không đưa vào dòng tín hiệu
            "evaluated_at": "2026-11-20T12:20:00+00:00",
            "status": "ABSTAIN",
            "direction": None,
            "symbol": "BTCUSDT",
            "strategy_release_id": "h01-v1",
            "reason_code": "H01_ADX_BELOW_20",
        },
        {
            # Khác mã → bỏ qua
            "evaluated_at": "2026-11-20T12:30:00+00:00",
            "status": "CANDIDATE",
            "direction": "short",
            "symbol": "ETHUSDT",
            "strategy_release_id": "h01-v1",
        },
    ]
    trades = [
        {
            "trade_id": "t1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quote_asset": "USDT",
            "opened_at": "2026-11-20T12:16:00+00:00",
            "closed_at": "2026-11-20T13:10:00+00:00",
            "net_pnl": "18.5",
            "exit_reason": "TAKE_PROFIT",
            "fills": [
                {
                    "role": "ENTRY",
                    "price": "91260",
                    "quantity": "0.02",
                    "occurred_at": "2026-11-20T12:16:00+00:00",
                    "exit_reason": None,
                },
                {
                    "role": "EXIT",
                    "price": "92185",
                    "quantity": "0.02",
                    "occurred_at": "2026-11-20T13:10:00+00:00",
                    "exit_reason": "TAKE_PROFIT",
                },
            ],
        },
        {
            # Lệnh không có danh sách fills chi tiết → fallback opened_at / closed_at
            "trade_id": "t2",
            "symbol": "BTCUSDT",
            "side": "SHORT",
            "quote_asset": "USDT",
            "opened_at": "2026-11-20T10:00:00+00:00",
            "closed_at": "2026-11-20T10:45:00+00:00",
            "net_pnl": "-5.0",
            "exit_reason": "STOP_LOSS",
        },
    ]
    official_macro = [
        {
            "title": "FOMC rate decision",
            "label_vi": "Fed quyết định lãi suất (FOMC)",
            "source_id": FOMC_SOURCE_ID,
            "scheduled_at_utc": "2026-11-20T14:15:00+00:00",
        }
    ]
    internal_calendar = [
        {
            "title": "Họp nội bộ kiểm tra",
            "region": "US",
            "impact": "HIGH",
            "event_at": "2026-11-20T13:30:00+00:00",
        }
    ]

    timeline = build_chart_timeline(
        symbol="BTCUSDT",
        signals=signals,
        trades=trades,
        official_macro_events=official_macro,
        internal_calendar_events=internal_calendar,
    )

    assert TIMELINE_VERSION == "ck10-timeline-v1"
    # Sắp giảm dần theo UTC (mới nhất trước)
    timestamps = [item["timestamp"] for item in timeline]
    assert timestamps == sorted(timestamps, reverse=True)
    kinds = [item["kind"] for item in timeline]
    assert kinds == [
        "macro_calendar",  # 14:15 AI-4 official
        "macro_calendar",  # 13:30 internal calendar
        "paper_fill",  # 13:10 EXIT t1
        "paper_fill",  # 12:16 ENTRY t1
        "signal",  # 12:15 CANDIDATE BTCUSDT
        "paper_fill",  # 10:45 EXIT t2 fallback
        "paper_fill",  # 10:00 ENTRY t2 fallback
    ]
    assert timeline[0]["source_badge"] == "AI-4 official calendar"
    assert timeline[1]["source_badge"] == "Lịch nội bộ"
    assert timeline[2]["kind_label_vi"] == "Khớp PAPER"
    assert timeline[2]["exit_reason"] == "TAKE_PROFIT"
    assert "92.185" in timeline[2]["summary_vi"] or "92185" in timeline[2]["summary_vi"]
    assert timeline[4]["kind_label_vi"] == "Tín hiệu"
    assert timeline[4]["side"] == "LONG"
    assert timeline[4]["symbol"] == "BTCUSDT"


def test_h06_macro_blackout_uses_only_ai4_official_calendar_not_manual_events() -> None:
    """Lịch tự nhập nội bộ (economic_event) KHÔNG được kích hoạt macro_blackout của H06."""
    # Có sự kiện nội bộ HIGH cách 5 phút → news_condition (CK-6) báo không đạt,
    # nhưng H06 chỉ nhận trạng thái từ AI-4 official calendar (đang thông thoáng).
    manual_events = [
        {
            "id": str(uuid4()),
            "title": "Sự kiện tự nhập chưa thẩm định",
            "region": "US",
            "impact": "HIGH",
            "event_at": (NOW + timedelta(minutes=5)).isoformat(),
        }
    ]
    assert news_condition(manual_events, now=NOW)["passed"] is False

    clear_fetches = _official_fetches(with_blackout=False)
    clear_state = macro_state(clear_fetches, NOW)
    assert clear_state.blackout is False
    assert clear_state.is_fresh is True

    # Xây snapshot H06 với trạng thái từ AI-4 (clear) → H06 KHÔNG bị H06_MACRO_BLACKOUT.
    snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=NOW,
        snapshot_id="snap-clear",
        series=(),
        features=(),
        position_is_flat=True,
        macro_blackout=clear_state.blackout,
        macro_evidence_ids=clear_state.evidence_ids,
        macro_evidence_available_at=clear_state.evidence_available_at,
        macro_is_fresh=clear_state.is_fresh,
    )
    selection = StrategySelection(
        settings_version_id="sv-1", active_release_ids=("h06-v1",), published=True
    )
    decision = H06RangeMeanReversionRule().evaluate(snapshot, selection)
    assert isinstance(decision, NoTrade)
    assert decision.code != "H06_MACRO_BLACKOUT"

    # Khi AI-4 official calendar có sự kiện FOMC cách 15 phút → H06 kích hoạt H06_MACRO_BLACKOUT.
    blocked_fetches = _official_fetches(with_blackout=True)
    blocked_state = macro_state(blocked_fetches, NOW)
    assert blocked_state.blackout is True
    blocked_snapshot = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=NOW,
        snapshot_id="snap-blocked",
        series=(),
        features=(),
        position_is_flat=True,
        macro_blackout=blocked_state.blackout,
        macro_evidence_ids=blocked_state.evidence_ids,
        macro_evidence_available_at=blocked_state.evidence_available_at,
        macro_is_fresh=blocked_state.is_fresh,
    )
    blocked_decision = H06RangeMeanReversionRule().evaluate(blocked_snapshot, selection)
    assert isinstance(blocked_decision, NoTrade)
    assert blocked_decision.code == "H06_MACRO_BLACKOUT"


def test_macro_h06_status_view_exposes_transparent_source_and_states() -> None:
    blocked_view = macro_h06_status_view(_official_fetches(with_blackout=True), now=NOW)
    assert blocked_view["source"] == "AI-4 official calendar"
    assert blocked_view["status"] == "blackout"
    assert blocked_view["blackout"] is True
    assert blocked_view["manual_calendar_isolated"] is True
    assert blocked_view["blocking_event"] == "FOMC:2026-11-20"
    assert len(blocked_view["upcoming"]) >= 1

    clear_view = macro_h06_status_view(_official_fetches(with_blackout=False), now=NOW)
    assert clear_view["status"] == "clear"
    assert clear_view["blackout"] is False

    unknown_view = macro_h06_status_view([], now=NOW)
    assert unknown_view["status"] == "unknown"
    assert unknown_view["blackout"] is None
    assert len(unknown_view["missing_sources"]) == 3
