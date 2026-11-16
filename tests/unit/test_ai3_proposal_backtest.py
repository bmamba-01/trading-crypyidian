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


def test_arms_apply_changes_and_flag_unsupported_fields() -> None:
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
    assert unsupported == ("risk.max_positions",)

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


def test_unsupported_only_proposal_is_not_replayed() -> None:
    result = backtest_proposal(
        bot_name="T06",
        bot_config=H03_CONFIG,
        changes=[{"field": "risk.max_positions", "to": "2"}],
        bundles={"AAAUSDT": "không được dùng"},
    )
    assert result.verdict == "UNSUPPORTED"
    assert result.skipped_symbols == ()
    assert "risk.max_positions" in result.notes[0]
