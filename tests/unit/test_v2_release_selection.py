"""Unit test sửa 16/11/2026: H03-v2/H06-v2 ra tín hiệu khi được chọn đúng như runtime dựng."""

from __future__ import annotations

import pytest
from tests.unit.test_strategy_hypotheses import _h03_snapshot, _h06_snapshot, _settings

from trading_bot.domain.strategy.contracts import NoTrade, StrategyCandidate, StrategySelection
from trading_bot.domain.strategy.registry import StrategyRegistry
from trading_bot.domain.strategy.rules_v2 import (
    H03DonchianBreakoutRuleV2,
    H06RangeMeanReversionRuleV2,
)

CASES = [
    (H03DonchianBreakoutRuleV2(), _h03_snapshot, "h03-v2", "h03-v1", "H03"),
    (H06RangeMeanReversionRuleV2(), _h06_snapshot, "h06-v2", "h06-v1", "H06"),
]


@pytest.mark.parametrize(("rule", "snapshot", "release", "v1", "strategy"), CASES)
def test_v2_selected_alone_produces_v2_candidate(rule, snapshot, release, v1, strategy) -> None:
    result = rule.evaluate(snapshot(), _settings(release))
    assert isinstance(result, StrategyCandidate), result
    assert result.strategy_release_id == release
    assert result.release_content_hash == StrategyRegistry().get_release(release).content_hash


@pytest.mark.parametrize(("rule", "snapshot", "release", "v1", "strategy"), CASES)
def test_v2_not_selected_when_only_v1_is_selected(rule, snapshot, release, v1, strategy) -> None:
    result = rule.evaluate(snapshot(), _settings(v1))
    assert isinstance(result, NoTrade)
    assert result.code == f"{strategy}_NOT_SELECTED"


@pytest.mark.parametrize(("rule", "snapshot", "release", "v1", "strategy"), CASES)
def test_v2_unpublished_settings_still_rejected(rule, snapshot, release, v1, strategy) -> None:
    result = rule.evaluate(snapshot(), StrategySelection("s", (release,), published=False))
    assert isinstance(result, NoTrade)
    assert result.code == f"{strategy}_SETTINGS_UNPUBLISHED"


def test_registry_resolve_v2_then_evaluate_like_runtime() -> None:
    settings = _settings("h03-v2")
    rule = StrategyRegistry().resolve("h03-v2", settings)
    assert isinstance(rule.evaluate(_h03_snapshot(), settings), StrategyCandidate)
