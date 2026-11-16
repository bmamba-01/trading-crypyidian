"""Unit test H03 v3: mỗi release một khung vào lệnh (15m/1h/4h), tham số chỉnh theo bot."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.unit.test_strategy_hypotheses import _bar, _feature, _settings, _snapshot

from trading_bot.application.strategy_market_requirements import (
    entry_timeframe_for_release,
    feature_requirements_for_release,
)
from trading_bot.domain.strategy import rules as rules_module
from trading_bot.domain.strategy import rules_v3
from trading_bot.domain.strategy.checklists import strategy_checklist
from trading_bot.domain.strategy.contracts import MarketSeries, NoTrade, StrategyCandidate
from trading_bot.domain.strategy.registry import StrategyRegistry

V3 = {"h03-v3-15m": "15m", "h03-v3-1h": "1h", "h03-v3-4h": "4h"}


def _snapshot_for(timeframe: str, *, lookback: int = 55, close: float = 101.5, volume: float = 6.0):
    bars = [_bar(index, timeframe, 96.0, high=100.0, low=95.0) for index in range(lookback)]
    bars.append(_bar(lookback, timeframe, close, high=102.0, low=94.0))
    volumes = (5.0,) * lookback + (volume,)
    return _snapshot(
        series=(MarketSeries(timeframe, tuple(bars), quote_volumes=volumes),),
        features=(_feature("atr_14", timeframe, 2.0, available_at=bars[-1].available_at),),
    )


@pytest.mark.parametrize(("release_id", "timeframe"), V3.items())
def test_release_breaks_out_on_its_own_timeframe(release_id: str, timeframe: str) -> None:
    rule = {item.release_id: item for item in _rules()}[release_id]
    result = rule.evaluate(_snapshot_for(timeframe), _settings(release_id))
    assert isinstance(result, StrategyCandidate), result
    assert result.direction == "long"
    assert result.strategy_release_id == release_id
    assert result.recipe.recipe_id == f"h03-exit-v3-{timeframe}"
    assert entry_timeframe_for_release(release_id) == timeframe
    assert dict(feature_requirements_for_release(release_id)) == {timeframe: ("atr_14",)}


def _rules():
    return (
        rules_v3.H03DonchianBreakoutRuleV3M15(),
        rules_v3.H03DonchianBreakoutRuleV3H1(),
        rules_v3.H03DonchianBreakoutRuleV3H4(),
    )


def test_release_ignores_series_of_other_timeframes() -> None:
    rule = rules_v3.H03DonchianBreakoutRuleV3H4()
    result = rule.evaluate(_snapshot_for("1h"), _settings("h03-v3-4h"))
    assert isinstance(result, NoTrade)
    assert result.code == "H03_INSUFFICIENT_BARS"


def test_bot_parameters_change_lookback_and_are_accepted() -> None:
    rule = rules_v3.H03DonchianBreakoutRuleV3M15()
    settings = _settings("h03-v3-15m", donchian_lookback=30, volume_median_lookback=10)
    result = rule.evaluate(_snapshot_for("15m", lookback=30), settings)
    assert isinstance(result, StrategyCandidate), result
    # Với lookback mặc định 55 thì chuỗi 31 nến chưa đủ.
    short = rule.evaluate(_snapshot_for("15m", lookback=30), _settings("h03-v3-15m"))
    assert isinstance(short, NoTrade)
    assert short.code == "H03_INSUFFICIENT_BARS"


def test_no_breakout_and_low_volume_are_rejected() -> None:
    rule = rules_v3.H03DonchianBreakoutRuleV3H1()
    no_break = rule.evaluate(_snapshot_for("1h", close=99.0), _settings("h03-v3-1h"))
    assert isinstance(no_break, NoTrade)
    assert no_break.code == "H03_NO_BREAKOUT"
    low_volume = rule.evaluate(_snapshot_for("1h", volume=5.0), _settings("h03-v3-1h"))
    assert isinstance(low_volume, NoTrade)
    assert low_volume.code == "H03_VOLUME_NOT_ABOVE_MEDIAN"


@pytest.mark.parametrize(("release_id", "timeframe"), V3.items())
def test_checklist_reads_release_timeframe_and_matches_rule(
    release_id: str, timeframe: str
) -> None:
    items = strategy_checklist(release_id, _snapshot_for(timeframe), _settings(release_id))
    assert items
    assert all(item.passed is True for item in items)
    assert f"({timeframe})" in items[0].label_vi


def test_hashes_differ_from_v1_v2_and_exit_evidence_uses_same_timeframe() -> None:
    releases = {item.release_id: item for item in StrategyRegistry().list_releases()}
    hashes = {releases[key].content_hash for key in ("h03-v1", "h03-v2", *V3)}
    assert len(hashes) == 5
    for timeframe in V3.values():
        spec = rules_v3.exit_evidence_spec(f"h03-exit-v3-{timeframe}")
        assert spec is not None
        assert (spec.timeframe, spec.channel_period) == (timeframe, 20)
    # Recipe v2 vẫn tra được qua hàm v3; v1 không có bằng chứng thoát.
    assert rules_v3.exit_evidence_spec("h03-exit-v2") is not None
    assert rules_v3.exit_evidence_spec("h03-exit-v1") is None
    source = Path(rules_module.__file__).read_bytes()
    assert hashlib.sha256(source).hexdigest() == rules_v3.BASE_RULES_SOURCE_SHA256


def test_bot_config_allows_parameters_for_v3_releases() -> None:
    from trading_bot.application import bot_config_resolver, paper_decision_cycle

    assert set(V3) <= bot_config_resolver._PARAMETERIZED_RELEASES
    assert (
        paper_decision_cycle.PARAMETERIZED_RELEASES == bot_config_resolver._PARAMETERIZED_RELEASES
    )


def test_v3_rejects_selection_of_other_release() -> None:
    rule = rules_v3.H03DonchianBreakoutRuleV3H1()
    result = rule.evaluate(_snapshot_for("1h"), _settings("h03-v1"))
    assert isinstance(result, NoTrade)
    assert result.code == "H03_NOT_SELECTED"
