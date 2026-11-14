import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from trading_bot.domain.strategy import rules as rules_module
from trading_bot.domain.strategy import rules_v2
from trading_bot.domain.strategy.contracts import DecisionSnapshot, StrategySelection
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.registry import (
    StrategyNotSelectedError,
    StrategyRegistry,
    UnknownStrategyReleaseError,
    UnpublishedStrategySettingsError,
)
from trading_bot.domain.strategy.rules import H01EmaTrendRule, release_content_hash


def test_registry_exposes_stable_experimental_versions_and_hashes() -> None:
    first = StrategyRegistry().list_releases()
    second = StrategyRegistry().list_releases()

    assert [item.release_id for item in first] == [
        "h01-v1",
        "h03-v1",
        "h03-v2",
        "h06-v1",
        "h06-v2",
    ]
    assert [item.content_hash for item in first] == [item.content_hash for item in second]
    assert all(item.status == "EXPERIMENTAL_UNVALIDATED" for item in first)
    assert all(len(item.content_hash) == 64 for item in first)
    with pytest.raises(FrozenInstanceError):
        first[0].status = "VALIDATED"  # type: ignore[misc]


def test_registry_resolves_only_known_selected_releases_from_published_settings() -> None:
    registry = StrategyRegistry()
    settings = StrategySelection("settings-1", ("h01-v1",), published=True)

    assert isinstance(registry.resolve("h01-v1", settings), H01EmaTrendRule)
    with pytest.raises(StrategyNotSelectedError):
        registry.resolve("h03-v1", settings)
    with pytest.raises(UnknownStrategyReleaseError):
        registry.resolve("missing-v1", StrategySelection("settings-1", ("missing-v1",), True))
    with pytest.raises(UnpublishedStrategySettingsError):
        registry.resolve("h01-v1", StrategySelection("draft-1", ("h01-v1",), False))
    with pytest.raises(TypeError):
        registry._rules["h99-v1"] = H01EmaTrendRule()  # type: ignore[index]


def test_unknown_release_metadata_is_an_explicit_lookup_error() -> None:
    with pytest.raises(UnknownStrategyReleaseError):
        StrategyRegistry().get_release("unknown-v9")


def test_release_hash_changes_when_executable_rule_or_recipe_changes() -> None:
    original = release_content_hash(H01EmaTrendRule())

    class ChangedH01Rule(H01EmaTrendRule):
        __slots__ = ()

        def evaluate(self, snapshot: DecisionSnapshot, settings: StrategySelection):
            return super().evaluate(snapshot, settings)

    changed_rule_hash = release_content_hash(ChangedH01Rule())

    class ChangedH01Recipe(H01EmaTrendRule):
        __slots__ = ()
        recipe = ExitRecipe(
            "h01-exit-v1",
            2.5,
            trailing_atr_multiple=3.0,
            opposite_signal_exit=True,
            max_hold_bars=120,
        )

    assert changed_rule_hash != original
    assert release_content_hash(ChangedH01Recipe()) != original


def test_v2_releases_keep_v1_hashes_and_bind_to_the_inherited_entry_rules() -> None:
    releases = {item.release_id: item for item in StrategyRegistry().list_releases()}

    # v1 vẫn tồn tại để vị thế đang mở khôi phục được; v2 là release nội dung khác.
    assert releases["h03-v1"].content_hash != releases["h03-v2"].content_hash
    assert releases["h06-v1"].content_hash != releases["h06-v2"].content_hash
    assert releases["h03-v2"].recipe_version == "h03-exit-v2"
    assert releases["h06-v2"].recipe_version == "h06-exit-v2"
    # Rule vào lệnh của v2 kế thừa từ rules.py: sửa file đó phải buộc phát hành lại v2.
    source = Path(rules_module.__file__).read_bytes()
    assert hashlib.sha256(source).hexdigest() == rules_v2.BASE_RULES_SOURCE_SHA256


def test_only_v2_recipes_request_exit_evidence() -> None:
    assert rules_v2.exit_evidence_spec("h03-exit-v1") is None
    assert rules_v2.exit_evidence_spec("h06-exit-v1") is None
    h03 = rules_v2.exit_evidence_spec("h03-exit-v2")
    h06 = rules_v2.exit_evidence_spec("h06-exit-v2")
    assert h03 is not None
    assert (h03.timeframe, h03.channel_period) == ("1h", 20)
    assert h06 is not None
    assert (h06.timeframe, h06.feature_name) == ("15m", "zscore_20")
    assert rules_v2.H03DonchianBreakoutRuleV2.recipe.exit_channel_period == h03.channel_period
