"""AI-3: backtest đề xuất AI (baseline vs variant) trên bundle dựng từ nến đã lưu."""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.proposal_backtest import (
    ArmMetrics,
    SymbolResult,
    aggregate,
    arms_for_proposal,
    backtest_proposal,
    build_stored_bundle,
    compare,
    required_timeframes,
)
from trading_bot.domain.market.models import OHLCVBar

WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 11, 16, 3, 30, tzinfo=UTC)
FINGERPRINT = hashlib.sha256(b"ai3-policy").hexdigest()
H03_CONFIG = {
    "release_id": "h03-v3-1h",
    "parameters": {"donchian_lookback": 20, "volume_median_lookback": 20},
    "risk": {"per_trade_risk_fraction": "0.005", "max_positions": 2},
}


def _bars(symbol: str, timeframe: str, count: int, *, gap_at: int | None = None) -> list[OHLCVBar]:
    step = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    end = datetime(2026, 11, 16, 0, tzinfo=UTC)
    bars = []
    for index in range(count):
        if index == gap_at:
            continue
        open_time = end - step * (count - index)
        # Chuỗi dao động + xu hướng để kênh Donchian bị phá vỡ cả hai chiều.
        price = Decimal(str(round(100 + 15 * math.sin(index / 9) + index * 0.03, 4)))
        close = open_time + step - timedelta(milliseconds=1)
        bars.append(
            OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close,
                event_at=close,
                received_at=close,
                available_at=close,
                open=price,
                high=price + Decimal("1.5"),
                low=price - Decimal("1.5"),
                close=price + Decimal("0.5"),
                volume=Decimal(str(100 + (index % 7) * 40)),
                quote_volume=Decimal(str(10000 + (index % 7) * 4000)),
                is_closed=True,
                source="BINANCE_USDM_REST_BACKFILL",
                source_cursor=f"{timeframe}:{open_time.isoformat()}",
                checksum=hashlib.sha256(f"{symbol}{timeframe}{index}".encode()).hexdigest(),
            )
        )
    return bars


class _Market:
    def __init__(self, series: dict[tuple[str, str], list[OHLCVBar]]) -> None:
        self.series = series

    async def list_bars(
        self, *, symbol: str, timeframe: str, source: str | None = None, **_: object
    ):
        assert source == "BINANCE_USDM_REST_BACKFILL"
        return list(self.series.get((symbol, timeframe), []))


class _Gate:
    def __init__(self) -> None:
        self.requests: list[RestBackfillRequest] = []

    async def authorize(self, *, request: RestBackfillRequest, now: datetime):
        assert request.purpose is BackfillRightsPurpose.BACKTEST_RESEARCH
        self.requests.append(request)
        return BackfillRightsEvidence(
            workspace_id=WORKSPACE,
            provider="BINANCE_USDM_REST",
            purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
            terms_url="https://example.invalid/terms",
            policy_fingerprint=FINGERPRINT,
            verified_at=now - timedelta(days=1),
            checked_at=now,
            valid_until=now + timedelta(minutes=10),
        )


def _bundle(symbol: str, series, timeframes=("1h",)):
    return asyncio.run(
        build_stored_bundle(
            workspace_id=WORKSPACE,
            symbol=symbol,
            timeframes=timeframes,
            market_data=_Market(series),
            rights_gate=_Gate(),
            now=NOW,
        )
    )


def test_arms_apply_changes_portfolio_fields_now_supported() -> None:
    changes = [
        {"field": "strategy.parameters.donchian_lookback", "to": "40"},
        {"field": "risk.per_trade_risk_fraction", "to": "0.003"},
        {"field": "risk.max_positions", "to": "3"},
    ]
    baseline, variant, unsupported = arms_for_proposal(H03_CONFIG, changes)

    assert baseline.parameters == (("donchian_lookback", 20), ("volume_median_lookback", 20))
    assert variant.parameters == (("donchian_lookback", 40), ("volume_median_lookback", 20))
    assert baseline.per_trade_risk_fraction == Decimal("0.005")
    assert variant.per_trade_risk_fraction == Decimal("0.003")
    # CRY-386: max_positions kiểm được bằng danh mục, không còn "unsupported".
    assert unsupported == ()

    _, switched, _ = arms_for_proposal(
        H03_CONFIG, [{"field": "strategy.release_id", "to": "h01-v1"}]
    )
    assert switched.release_id == "h01-v1"
    assert switched.parameters == ()  # H01 không nhận tham số H03


def test_required_timeframes_by_release() -> None:
    assert required_timeframes("h03-v3-15m") == ("15m",)
    assert required_timeframes("h01-v1") == ("1h", "4h")
    with pytest.raises(ValueError, match="chưa"):
        required_timeframes("card-v1")


def test_aggregate_and_conservative_verdict() -> None:
    def result(trades, wins, profit, loss, dd):
        return SymbolResult(
            "X",
            trades,
            wins,
            Decimal(profit),
            Decimal(loss),
            Decimal(profit) - Decimal(loss),
            Decimal(dd),
        )

    metrics = aggregate([result(6, 3, "30", "10", "8"), result(6, 2, "5", "15", "12")])
    assert metrics.trades == 12
    assert metrics.win_rate == Decimal("0.4167")
    assert metrics.profit_factor == Decimal("1.40")
    assert metrics.net_pnl == Decimal("10.00")
    assert metrics.max_drawdown == Decimal("12.00")
    assert aggregate([]).win_rate is None

    small = ArmMetrics(1, 4, 2, None, None, Decimal("5"), Decimal("1"))
    assert compare(small, replace(small, net_pnl=Decimal("50")))[0] == "INCONCLUSIVE"
    base = replace(small, trades=20)
    assert compare(base, replace(base, net_pnl=Decimal("20")))[0] == "BETTER"
    assert compare(base, replace(base, net_pnl=Decimal("-3")))[0] == "WORSE"
    verdict, notes = compare(base, replace(base, net_pnl=Decimal("20"), max_drawdown=Decimal("50")))
    assert verdict == "INCONCLUSIVE"
    assert "drawdown" in notes[0]


def test_stored_bundle_uses_backtest_rights_and_rejects_gaps() -> None:
    gate = _Gate()
    series = {("AAAUSDT", "1h"): _bars("AAAUSDT", "1h", 320)}
    bundle = asyncio.run(
        build_stored_bundle(
            workspace_id=WORKSPACE,
            symbol="AAAUSDT",
            timeframes=("1h",),
            market_data=_Market(series),
            rights_gate=gate,
            now=NOW,
        )
    )
    assert not isinstance(bundle, str)
    assert bundle.rights_purpose == "BACKTEST_RESEARCH"
    assert bundle.range_end == datetime(2026, 11, 16, 0, tzinfo=UTC)
    assert [r.interval for r in gate.requests] == ["1h"]

    gapped = {("BBBUSDT", "1h"): _bars("BBBUSDT", "1h", 320, gap_at=300)}
    reason = _bundle("BBBUSDT", gapped)
    assert isinstance(reason, str)
    assert "thiếu nến" in reason
    assert isinstance(_bundle("CCCUSDT", {("CCCUSDT", "1h"): _bars("CCCUSDT", "1h", 100)}), str)


def test_backtest_proposal_runs_both_arms_on_same_bundle_deterministically() -> None:
    series = {("AAAUSDT", "1h"): _bars("AAAUSDT", "1h", 320)}
    bundles = {"AAAUSDT": _bundle("AAAUSDT", series), "ZZZUSDT": "Thiếu nến 1h"}
    changes = [{"field": "strategy.parameters.donchian_lookback", "to": "60"}]

    first = backtest_proposal(
        bot_name="T01 H03 1h", bot_config=H03_CONFIG, changes=changes, bundles=bundles
    )
    second = backtest_proposal(
        bot_name="T01 H03 1h", bot_config=H03_CONFIG, changes=changes, bundles=bundles
    )

    assert first.payload() == second.payload()
    payload = first.payload()
    assert payload["baseline"]["symbols"] == payload["variant"]["symbols"] == 1
    assert payload["skipped_symbols"] == [{"symbol": "ZZZUSDT", "reason": "Thiếu nến 1h"}]
    assert payload["baseline_arm"]["parameters"]["donchian_lookback"] == 20
    assert payload["variant_arm"]["parameters"]["donchian_lookback"] == 60
    assert payload["verdict"] in {"BETTER", "WORSE", "INCONCLUSIVE"}
    assert payload["window"] is not None
    # Kênh 20 nến bị phá nhiều lần hơn kênh 60 nến trên chuỗi dao động.
    assert first.baseline.trades > 0
    assert first.baseline.trades >= first.variant.trades


def test_backtest_card_uses_only_replay_numbers() -> None:
    from trading_bot.adapters.telegram.card_renderer import render_backtest_card
    from trading_bot.application.telegram_cards import (
        backtest_card_caption,
        backtest_card_view_from,
    )

    series = {("AAAUSDT", "1h"): _bars("AAAUSDT", "1h", 320)}
    changes = [{"field": "strategy.parameters.donchian_lookback", "to": "60"}]
    result = backtest_proposal(
        bot_name="T01 H03 1h",
        bot_config=H03_CONFIG,
        changes=changes,
        bundles={"AAAUSDT": _bundle("AAAUSDT", series)},
    ).payload()

    view = backtest_card_view_from({"changes": changes}, result)
    rows = {label: (base, var) for label, base, var in view.rows()}
    assert rows["Số lệnh"] == (
        str(result["baseline"]["trades"]),
        str(result["variant"]["trades"]),
    )
    caption = backtest_card_caption(view)
    assert "KHÔNG TỰ ÁP" in caption
    assert "donchian_lookback → 60" in caption
    assert render_backtest_card(view).startswith(b"\x89PNG")

    empty = backtest_card_view_from({}, {"bot_name": "X", "verdict": "UNSUPPORTED"})
    assert empty.window_start is None
    assert empty.verdict_vi == "Không backtest được"
    assert render_backtest_card(empty).startswith(b"\x89PNG")


def test_no_change_proposal_is_not_replayed_and_portfolio_change_is() -> None:
    result = backtest_proposal(
        bot_name="T06",
        bot_config=H03_CONFIG,
        changes=[{"field": "risk.per_trade_risk_fraction", "to": "0.005"}],
        bundles={"AAAUSDT": "không được dùng"},
    )
    assert result.verdict == "UNSUPPORTED"
    assert result.skipped_symbols == ()
    portfolio_only = backtest_proposal(
        bot_name="T06",
        bot_config=H03_CONFIG,
        changes=[{"field": "risk.max_positions", "to": "3"}],
        bundles={"AAAUSDT": "thiếu nến"},
    )
    # CRY-386: đổi trần danh mục giờ được chạy (ở đây không coin nào có dữ liệu).
    assert portfolio_only.comparison_basis == "portfolio"
    assert portfolio_only.verdict == "INCONCLUSIVE"
    assert portfolio_only.skipped_symbols == (("AAAUSDT", "thiếu nến"),)


def test_portfolio_change_runs_replay_once_and_compares_on_portfolio(monkeypatch) -> None:
    from trading_bot.application import proposal_backtest as module

    series = {
        ("AAAUSDT", "1h"): _bars("AAAUSDT", "1h", 320),
        ("BBBUSDT", "1h"): _bars("BBBUSDT", "1h", 320),
    }
    bundles = {s: _bundle(s, series) for s in ("AAAUSDT", "BBBUSDT")}
    calls: list[str] = []
    real = module.run_arm

    def counting(bundle, arm, **kwargs):
        calls.append(bundle.symbol)
        return real(bundle, arm, **kwargs)

    monkeypatch.setattr(module, "run_arm", counting)
    config = {**H03_CONFIG, "risk": {"per_trade_risk_fraction": "0.005", "max_positions": 1}}
    result = backtest_proposal(
        bot_name="T06",
        bot_config=config,
        changes=[{"field": "risk.max_positions", "to": "2"}],
        bundles=bundles,
    )
    assert calls == ["AAAUSDT", "BBBUSDT"]  # mỗi coin replay đúng một lần
    payload = result.payload()
    assert payload["comparison_basis"] == "portfolio"
    assert payload["version"] == "ai3-proposal-backtest-v3"
    assert payload["verdict_rule"] == "compare-v2"
    base, variant = payload["portfolio"]["baseline"], payload["portfolio"]["variant"]
    assert (base["max_positions"], variant["max_positions"]) == (1, 2)
    # Hai coin cùng chuỗi giá → tín hiệu trùng giờ: trần 1 vị thế phải bỏ lệnh, trần 2 thì không.
    assert base["skipped_max_positions"] > 0
    assert variant["skipped_max_positions"] < base["skipped_max_positions"]
    assert result.variant.trades > result.baseline.trades
    assert any("danh mục chung vốn" in note for note in result.notes)


def test_compare_v2_does_not_reward_scale_only_changes() -> None:
    # Ca thật 17/11: T02 giảm rủi ro 0,005→0,002 — cùng 107 lệnh, 26 thắng, PF 0,52.
    base = ArmMetrics(10, 107, 26, Decimal("0.2430"), Decimal("0.52"),
                      Decimal("-952.45"), Decimal("205.58"))  # fmt: skip
    smaller = replace(base, net_pnl=Decimal("-635.44"), max_drawdown=Decimal("137.37"))
    verdict, notes = compare(base, smaller)
    assert verdict == "INCONCLUSIVE"
    assert "Chỉ đổi quy mô lệnh" in notes[0]
    assert "đang lỗ" in notes[1]
    # Chiều ngược lại (tăng quy mô chiến lược lời) cũng không phải "tốt hơn" hay "kém hơn".
    winning = replace(base, profit_factor=Decimal("1.60"), net_pnl=Decimal("500"))
    assert compare(winning, replace(winning, net_pnl=Decimal("800")))[0] == "INCONCLUSIVE"
    assert compare(winning, replace(winning, net_pnl=Decimal("300")))[0] == "INCONCLUSIVE"


def test_compare_v2_requires_profit_factor_not_worse_and_flags_still_losing() -> None:
    base = ArmMetrics(10, 107, 26, None, Decimal("0.52"), Decimal("-952.45"), Decimal("205.58"))
    # T02 donchian 80: ít lệnh hơn, chất lượng tốt hơn, vẫn lỗ → BETTER kèm ghi chú.
    better = replace(base, trades=79, wins=22, profit_factor=Decimal("0.71"),
                     net_pnl=Decimal("-309.71"), max_drawdown=Decimal("146.45"))  # fmt: skip
    verdict, notes = compare(base, better)
    assert verdict == "BETTER"
    assert "vẫn lỗ" in notes[0]
    # PnL tốt hơn nhưng PF giảm (vd lệnh lớn hơn trên tập lệnh khác) → không BETTER.
    bigger = replace(base, trades=90, wins=20, profit_factor=Decimal("0.40"),
                     net_pnl=Decimal("-900"), max_drawdown=Decimal("200"))  # fmt: skip
    verdict, notes = compare(base, bigger)
    assert verdict == "INCONCLUSIVE"
    assert "profit factor giảm" in notes[0]
    # PF vô cùng (không lệnh lỗ) vẫn so sánh được.
    perfect = replace(base, trades=12, wins=12, profit_factor=None, net_pnl=Decimal("50"),
                      max_drawdown=Decimal("0"))  # fmt: skip
    assert compare(base, perfect)[0] == "BETTER"


def test_parallel_replay_matches_sequential_exactly() -> None:
    # CRY-423: process pool theo coin; payload phải trùng khớp chạy tuần tự (tất định).
    import time

    series = {(s, "1h"): _bars(s, "1h", 320) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")}
    bundles: dict = {s: _bundle(s, series) for s in ("CCCUSDT", "AAAUSDT", "BBBUSDT")}
    bundles["ZZZUSDT"] = "Thiếu nến 1h"
    changes = [{"field": "strategy.parameters.donchian_lookback", "to": "60"}]
    started = time.perf_counter()
    sequential = backtest_proposal(
        bot_name="T01", bot_config=H03_CONFIG, changes=changes, bundles=bundles, workers=1
    )
    middle = time.perf_counter()
    parallel = backtest_proposal(
        bot_name="T01", bot_config=H03_CONFIG, changes=changes, bundles=bundles, workers=3
    )
    assert parallel.payload() == sequential.payload()
    assert [b.symbol for b, _ in parallel.per_symbol] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert parallel.skipped_symbols == (("ZZZUSDT", "Thiếu nến 1h"),)
    assert middle > started  # thời gian chỉ để tham khảo; không khẳng định tốc độ trong unit
