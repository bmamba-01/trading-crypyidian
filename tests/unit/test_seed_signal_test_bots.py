"""Unit test script đợt thử 10 bot (chỉ phần dựng kế hoạch, không chạm DB/mạng)."""

from __future__ import annotations

from decimal import Decimal

from scripts import seed_signal_test_bots as script

from trading_bot.application.market_bar_collector import parse_collector_streams
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.config.bot_settings import BotConfigOverlay
from trading_bot.domain.strategy.contracts import StrategySelection


def _symbols(count: int = 60) -> list[str]:
    return [f"C{index:02d}USDT" for index in range(count)]


def test_eligible_symbols_filters_perpetual_usdt_trading_and_volume() -> None:
    info = {
        "symbols": [
            {
                "symbol": "AAAUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "BBBUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "SETTLING",
            },
            {
                "symbol": "CCCUSDC",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDC",
                "status": "TRADING",
            },
            {
                "symbol": "DDDUSDT_260327",
                "contractType": "CURRENT_QUARTER",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
            {
                "symbol": "EEEUSDT",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "status": "TRADING",
            },
        ]
    }
    tickers = [
        {"symbol": "AAAUSDT", "quoteVolume": "25000000"},
        {"symbol": "EEEUSDT", "quoteVolume": "1000"},
        {"symbol": "牛来USDT", "quoteVolume": "90000000"},
    ]
    info["symbols"].append(
        {
            "symbol": "牛来USDT",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "status": "TRADING",
        }
    )
    assert script.eligible_symbols(info, tickers, min_quote_volume=Decimal("20000000")) == [
        "AAAUSDT"
    ]


def test_plan_is_deterministic_and_respects_limits() -> None:
    plan = script.build_plan(_symbols(), seed=7)
    assert plan == script.build_plan(_symbols(), seed=7)
    assert plan != script.build_plan(_symbols(), seed=8)
    assert len(plan["coins"]) == 20 == len(set(plan["coins"]))
    assert len(plan["bots"]) == 10
    # 20 coin × 4 khung (gồm 1m execution) = 80 luồng, dưới trần collector 100.
    streams = parse_collector_streams(script.collector_streams(plan))
    assert len(streams) == 80
    assert {stream.interval for stream in streams} == {"1m", "15m", "1h", "4h"}
    registry = strategy_registry()
    for bot in plan["bots"]:
        assert set(bot["symbols"]) <= set(plan["coins"])
        assert 3 <= len(bot["symbols"]) <= 20
        registry.get_release(bot["release_id"])
        overlay = BotConfigOverlay.model_validate(
            {
                "schema_version": 1,
                "strategy": {"release_id": bot["release_id"], "parameters": bot["parameters"]},
                "universe": {"symbols": bot["symbols"]},
                "risk": bot["risk"],
            }
        )
        assert overlay.risk is not None
        assert Decimal(bot["risk"]["per_trade_risk_fraction"]) <= Decimal("0.01")
        assert bot["risk"]["max_positions"] <= 6
        StrategySelection("s", (bot["release_id"],), True, tuple(sorted(bot["parameters"].items())))
    timeframes = {bot["timeframe"] for bot in plan["bots"]}
    assert plan["timeframes"][0] == "1m"
    assert timeframes == {"15m", "1h", "4h"}


def test_plan_rejects_too_few_symbols() -> None:
    import pytest

    with pytest.raises(SystemExit, match="cần 20"):
        script.build_plan(_symbols(5), seed=1)
