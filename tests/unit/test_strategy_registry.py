from dataclasses import FrozenInstanceError

import pytest

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

    assert [item.release_id for item in first] == ["h01-v1", "h03-v1", "h06-v1"]
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
