"""Unit test AI-1: Run Report tất định từ báo cáo bot, quyết định và cực trị giá."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.portfolio.run_report import (
    RUN_REPORT_VERSION,
    BotRunInput,
    DecisionCount,
    TradeExtremes,
    build_run_report,
    strategy_family,
)

START = datetime(2026, 11, 15, 16, 0, tzinfo=UTC)
END = START + timedelta(hours=4)


def _trade(trade_id: str, side: str, entry: str, pnl: str, symbol: str = "BTWUSDT") -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "net_pnl": pnl,
        "exit_reason": "STOP_LOSS" if pnl.startswith("-") else "TAKE_PROFIT",
        "opened_at": START.isoformat(),
        "closed_at": (START + timedelta(hours=1)).isoformat(),
        "fills": [{"role": "ENTRY", "quantity": "10", "price": entry}],
    }


def _bot(name: str, release: str, trades: list[dict], **kwargs) -> BotRunInput:
    report = {
        "report_hash": "h" * 64,
        "trades": trades,
        "open_trades": [],
        "totals": {"USDT": {"net_pnl": str(sum(Decimal(t["net_pnl"]) for t in trades))}},
    }
    return BotRunInput(
        bot_id=name,
        bot_name=name,
        release_id=release,
        timeframe=kwargs.pop("timeframe", "1h"),
        parameters=kwargs.pop("parameters", {}),
        risk={"max_positions": 3},
        universe=("BTWUSDT", "XMRUSDT"),
        bot_report=report,
        **kwargs,
    )


def test_r_multiple_mae_mfe_for_long_and_short() -> None:
    long_trade = _trade("a", "long", "100", "20")  # rủi ro 10 → +2R
    short_trade = _trade("b", "short", "100", "-10")  # -1R
    bot = _bot(
        "T01",
        "h03-v3-1h",
        [long_trade, short_trade],
        extremes=(
            TradeExtremes("a", Decimal("10"), Decimal("99"), Decimal("103")),
            TradeExtremes("b", Decimal("10"), Decimal("98"), Decimal("101")),
        ),
    )
    report = build_run_report(
        workspace_id="w", run_label="t", window_start=START, window_end=END, bots=[bot]
    )
    perf = report.payload["bots"][0]["performance"]
    assert perf["r_multiple"] == {"mean": "0.5", "median": "0.5", "min": "-1", "max": "2"}
    # long: bất lợi (100-99)*10/10 = 1R, thuận lợi (103-100)*10/10 = 3R
    # short: bất lợi (101-100)*10/10 = 1R, thuận lợi (100-98)*10/10 = 2R
    assert perf["mae_r"]["max"] == "1"
    assert perf["mfe_r"] == {"mean": "2.5", "median": "2.5", "min": "2", "max": "3"}
    assert perf["exit_reasons"] == {"STOP_LOSS": 1, "TAKE_PROFIT": 1}
    assert report.payload["bots"][0]["by_symbol"]["BTWUSDT"] == {
        "closed": 2,
        "wins": 1,
        "net_pnl_usdt": "10",
    }


def test_funnel_separates_out_of_universe_and_flags_unconverted_candidates() -> None:
    decisions = (
        DecisionCount("CANDIDATE", "H03_SHORT_ENTRY", 4),
        DecisionCount("ABSTAIN", "H03_NO_BREAKOUT", 30),
        DecisionCount("ABSTAIN", "SYMBOL_OUT_OF_BOT_UNIVERSE", 120),
    )
    bot = _bot("T04", "h03-v3-1h", [], decisions=decisions, candidates_with_intent=1)
    report = build_run_report(
        workspace_id="w", run_label="t", window_start=START, window_end=END, bots=[bot]
    )
    funnel = report.payload["bots"][0]["funnel"]
    assert funnel["decisions_total"] == 34
    assert funnel["out_of_universe_evaluations"] == 120
    assert funnel["top_skip_reasons"] == {"H03_NO_BREAKOUT": 30}
    assert funnel["candidate_to_order_rate"] == "0.25"
    assert any("3 tín hiệu chưa thành lệnh" in w for w in report.warnings)
    explained = _bot(
        "T04",
        "h03-v3-1h",
        [],
        decisions=decisions,
        candidates_with_intent=1,
        rejections=(DecisionCount("PLAN", "MARKET_DATA_STALE", 2),),
        candidates_with_recorded_rejection=2,
    )
    with_reasons = build_run_report(
        workspace_id="w", run_label="t", window_start=START, window_end=END, bots=[explained]
    )
    assert with_reasons.payload["bots"][0]["funnel"]["rejection_reasons"] == {
        "PLAN:MARKET_DATA_STALE": 2
    }
    assert any("1 tín hiệu chưa thành lệnh mà chưa có lý do" in w for w in with_reasons.warnings)
    assert any("chưa đủ để kết luận" in w for w in report.warnings)


def test_groups_ranking_and_hash_are_deterministic() -> None:
    bots = [
        _bot("T02", "h03-v3-15m", [_trade("x", "long", "1", "5")], timeframe="15m"),
        _bot("T07", "h01-v1", [_trade("y", "long", "1", "-3")]),
        _bot("T03", "sc-binance-usdm-vol-breakout-001-v1.0.0", []),
    ]
    first = build_run_report(
        workspace_id="w", run_label="t", window_start=START, window_end=END, bots=bots
    )
    second = build_run_report(
        workspace_id="w",
        run_label="t",
        window_start=START,
        window_end=END,
        bots=list(reversed(bots)),
    )
    assert first.report_hash == second.report_hash
    assert first.payload["report_version"] == RUN_REPORT_VERSION
    assert [row["bot_name"] for row in first.payload["ranking_by_net_pnl"]] == ["T02", "T03", "T07"]
    families = first.payload["groups"]["by_strategy_family"]
    assert set(families) == {"CARD", "H01", "H03"}
    assert first.payload["groups"]["by_timeframe"]["15m"]["net_pnl_usdt"] == "5"


def test_missing_risk_or_bars_leaves_r_and_excursions_empty() -> None:
    bot = _bot(
        "T05",
        "h03-v3-4h",
        [_trade("z", "long", "1", "2")],
        extremes=(TradeExtremes("z", None, None, None),),
    )
    perf = build_run_report(
        workspace_id="w", run_label="t", window_start=START, window_end=END, bots=[bot]
    ).payload["bots"][0]["performance"]
    assert perf["r_multiple"]["mean"] is None
    assert perf["mae_r"]["mean"] is None


def test_invalid_window_and_family_names() -> None:
    with pytest.raises(ValueError, match="Cửa sổ"):
        build_run_report(
            workspace_id="w", run_label="t", window_start=END, window_end=START, bots=[]
        )
    assert strategy_family("h03-v3-15m") == "H03"
    assert strategy_family("sc-binance-usdm-vol-breakout-001-v1.0.0") == "CARD"


def test_signal_outcomes_block_and_ai_validator_refs() -> None:
    """CRY-430: khối signal_outcomes theo bot, cảnh báo ít mẫu, AI trích được số qua pointer."""
    from trading_bot.application.ai_analyst import topic_report, validate_analysis
    from trading_bot.domain.portfolio.run_report import SignalOutcomeInput
    from trading_bot.domain.strategy.signal_outcome_summary import OutcomeRow

    def outcome(horizon: int, symbol: str, label: int, r: str, *, sent: bool) -> SignalOutcomeInput:
        return SignalOutcomeInput(
            horizon_bars=horizon,
            symbol=symbol,
            direction="long",
            delivered=sent,
            row=OutcomeRow(
                status="SCORED",
                label=label,
                outcome_r=Decimal(r),
                mfe_r=Decimal("1.5"),
                mae_r=Decimal("-0.5"),
                first_touch="SL" if label < 0 else "TP1",
                same_bar_conflict=False,
                seconds_to_first_touch=900,
            ),
        )

    outcomes = (
        outcome(4, "XMRUSDT", -1, "-1", sent=True),
        outcome(4, "XMRUSDT", -1, "-1", sent=False),
        outcome(4, "XMRUSDT", -1, "-1", sent=False),
        outcome(4, "BTWUSDT", 1, "1", sent=True),
        outcome(1, "BTWUSDT", 0, "0.05", sent=True),
        outcome(24, "XMRUSDT", 1, "1", sent=True),
    )
    report = build_run_report(
        workspace_id="w",
        run_label="t",
        window_start=START,
        window_end=END,
        bots=[_bot("T09", "h03-v3-15m", [], signal_outcomes=outcomes)],
    ).payload
    block = report["bots"][0]["signal_outcomes"]
    assert block["method_version"] == "sig-outcome-v2"
    assert block["primary_horizon_bars"] == 4
    primary = block["by_horizon_bars"]["4"]
    assert (primary["scored"], primary["correct"], primary["wrong"]) == (4, 1, 3)
    assert primary["hit_rate_percent"] == "25.0"
    assert "r_distribution" in primary
    assert "r_distribution" not in block["by_horizon_bars"]["1"]
    assert block["repeated_wrong"][0]["key"] == "XMRUSDT long"
    assert block["repeated_wrong"][0]["wrong"] == 3
    assert {row["key"] for row in block["by_delivery"]} == {"sent", "not_sent"}
    assert any("tín hiệu đã chấm" in warning for warning in report["data_quality"]["warnings"])
    family = topic_report(report, "H03")
    assert family["bots"][0]["signal_outcomes"] == block
    output = {
        "summary": "Tín hiệu XMRUSDT sai lặp lại.",
        "confidence": "low",
        "findings": [
            {
                "statement": "Ở horizon 4 nến, tỷ lệ đúng 25.0%, XMRUSDT long sai 3 lần; "
                "24 nến có 1 tín hiệu đúng.",
                "evidence_refs": [
                    "/bots/0/signal_outcomes/by_horizon_bars/4/hit_rate_percent",
                    "/bots/0/signal_outcomes/repeated_wrong/0/wrong",
                    "/bots/0/signal_outcomes/by_horizon_bars/24/correct",
                ],
            }
        ],
        "hypotheses": [],
        "proposals": [],
    }
    assert validate_analysis(output, family).ok, validate_analysis(output, family).errors
    output["findings"][0]["statement"] = "Tỷ lệ đúng 30.0%."
    assert not validate_analysis(output, family).ok
