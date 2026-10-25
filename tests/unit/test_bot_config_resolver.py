"""Resolver root + overlay bot: kế thừa, chỉ siết, release đã duyệt, scope và thời điểm."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    EffectiveBotConfig,
    compute_effective_hash,
    resolve_effective_bot_config,
    validate_bot_overlay,
)
from trading_bot.application.runtime_risk_profile import risk_limits_from_profile
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.strategy.contracts import StrategySelection
from trading_bot.domain.strategy.registry import StrategyRegistry

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
HARD_CAPS = {
    "max_aggregate_open_risk": "250",
    "max_notional": "10000",
    "max_leverage": "5",
    "max_positions": 2,
    "max_symbol_notional": "2000",
    "max_concentration_fraction": "0.2",
    "max_daily_loss": "200",
    "max_rolling_loss": "500",
    "max_account_age_seconds": 30,
    "max_data_age_seconds": 300,
}


class World:
    """Dựng một workspace PAPER hợp lệ cùng các bản ghi tối thiểu cho resolver."""

    def __init__(self) -> None:
        self.workspace_id = uuid4()
        self.bot_id = uuid4()
        self.account_id = uuid4()
        self.profile_id = uuid4()
        payload = valid_payload()
        payload["strategy"] = {
            "default_release": "h03-v1",
            "parameters": {"donchian_lookback": 55},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        }
        payload["risk"] = {"profile_version_id": str(self.profile_id)}
        self.root = PublishedWorkspaceSettings(
            workspace_id=self.workspace_id,
            settings_version_id=uuid4(),
            settings_hash="a" * 64,
            settings=WorkspaceSettings.model_validate(payload),
            effective_at=NOW - timedelta(hours=2),
        )
        self.risk_profile = risk_limits_from_profile(
            SimpleNamespace(
                id=self.profile_id,
                workspace_id=self.workspace_id,
                name="Conservative",
                version=1,
                max_risk_fraction=Decimal("0.01"),
                settings={"schema_version": 1, "hard_caps": dict(HARD_CAPS)},
            )
        )
        self.account = SimpleNamespace(
            id=self.account_id,
            workspace_id=self.workspace_id,
            mode="PAPER",
            venue="binance_usdm",
            status="ENABLED",
            max_notional=Decimal("8000"),
        )
        self.releases = [self.release("h03-v1"), self.release("h01-v1")]

    def release(
        self, name: str, *, status: str = "PUBLISHED", workspace_id: UUID | None = None
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace_id or self.workspace_id,
            name=name,
            status=status,
            content_hash=StrategyRegistry().get_release(name).content_hash,
        )

    def version(self, overlay: dict[str, Any], **changes: Any) -> SimpleNamespace:
        parsed = BotConfigOverlay.model_validate(overlay)
        payload = canonical_overlay(parsed)
        values: dict[str, Any] = {
            "id": uuid4(),
            "workspace_id": self.workspace_id,
            "bot_id": self.bot_id,
            "account_id": self.account_id,
            "version": 1,
            "status": "PUBLISHED",
            "schema_version": 1,
            "overlay_json": payload,
            "overlay_hash": overlay_hash(payload),
            "base_settings_version_id": self.root.settings_version_id,
            "base_settings_hash": self.root.settings_hash,
            "published_at": NOW - timedelta(hours=1),
            "effective_at": NOW - timedelta(hours=1),
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def resolve(self, version: SimpleNamespace, **overrides: Any) -> EffectiveBotConfig:
        arguments: dict[str, Any] = {
            "root_risk_profile": self.risk_profile,
            "strategy_releases": self.releases,
        }
        arguments.update(overrides)
        return resolve_effective_bot_config(
            overrides.pop("workspace_settings", self.root),
            version,  # type: ignore[arg-type]
            overrides.pop("account", self.account),  # type: ignore[arg-type]
            overrides.pop("as_of", NOW),
            root_risk_profile=arguments["root_risk_profile"],
            strategy_releases=arguments["strategy_releases"],
        )


def codes(error: pytest.ExceptionInfo[BotConfigResolutionError]) -> set[str]:
    return {issue.field for issue in error.value.issues} | {error.value.code}


def test_empty_overlay_inherits_root_strategy_universe_and_hard_caps() -> None:
    world = World()

    effective = world.resolve(world.version({"schema_version": 1}))

    assert effective.workspace_id == world.workspace_id
    assert effective.bot_id == world.bot_id
    assert effective.account_id == world.account_id
    assert effective.settings_version_id == world.root.settings_version_id
    assert effective.settings_hash == world.root.settings_hash
    assert effective.strategy.release_id == "h03-v1"
    assert effective.strategy.parameters == (("donchian_lookback", 55),)
    assert effective.symbols == ("BTCUSDT", "ETHUSDT")
    assert effective.timeframes == ("5m", "1h", "4h")
    limits = effective.risk_limits
    assert limits.per_trade_risk_fraction == Decimal("0.01")
    assert limits.max_positions == 2
    # Cap account (8000) chặt hơn root (10000) nên effective lấy giá trị nhỏ hơn.
    assert limits.max_notional == Decimal("8000")
    assert effective.risk_profile_version_id == world.profile_id


def test_effective_strategy_is_consumable_as_strategy_selection() -> None:
    world = World()
    effective = world.resolve(
        world.version(
            {
                "schema_version": 1,
                "strategy": {"release_id": "h03-v1", "parameters": {"donchian_lookback": 60}},
            }
        )
    )

    selection = effective.strategy_selection()

    assert isinstance(selection, StrategySelection)
    assert selection.settings_version_id == str(world.root.settings_version_id)
    assert selection.active_release_ids == ("h03-v1",)
    assert selection.parameters == (("donchian_lookback", 60),)
    assert StrategyRegistry().resolve("h03-v1", selection).release_id == "h03-v1"
    assert effective.strategy.release_content_hash == (
        StrategyRegistry().get_release("h03-v1").content_hash
    )
    assert effective.strategy.workspace_release_id == world.releases[0].id


def test_strategy_block_replaces_root_parameters_as_a_unit() -> None:
    world = World()

    effective = world.resolve(
        world.version({"schema_version": 1, "strategy": {"release_id": "h03-v1"}})
    )

    assert effective.strategy.parameters == ()


def test_overlay_tightens_risk_and_universe_with_boundary_equality() -> None:
    world = World()
    effective = world.resolve(
        world.version(
            {
                "schema_version": 1,
                "universe": {"symbols": ["BTCUSDT"], "timeframes": ["1h", "4h"]},
                "risk": {
                    "per_trade_risk_fraction": "0.0100",
                    "max_positions": 2,
                    "max_notional": "8000.000",
                    "max_leverage": "3",
                    "max_concentration_fraction": "0.2",
                    "max_daily_loss": "199.999999999999999999",
                },
            }
        )
    )

    assert effective.symbols == ("BTCUSDT",)
    assert effective.timeframes == ("1h", "4h")
    assert effective.risk_limits.per_trade_risk_fraction == Decimal("0.01")
    assert effective.risk_limits.max_notional == Decimal("8000")
    assert effective.risk_limits.max_leverage == Decimal("3")
    assert effective.risk_limits.max_daily_loss == Decimal("199.999999999999999999")
    assert effective.risk_limits.max_rolling_loss == Decimal("500")


@pytest.mark.parametrize(
    ("risk", "field"),
    [
        ({"per_trade_risk_fraction": "0.0100000000000000000001"}, "risk.per_trade_risk_fraction"),
        ({"max_positions": 3}, "risk.max_positions"),
        ({"max_leverage": "5.01"}, "risk.max_leverage"),
        ({"max_aggregate_open_risk": "251"}, "risk.max_aggregate_open_risk"),
        ({"max_symbol_notional": "2000.0001"}, "risk.max_symbol_notional"),
        ({"max_concentration_fraction": "0.21"}, "risk.max_concentration_fraction"),
        ({"max_daily_loss": "201"}, "risk.max_daily_loss"),
        ({"max_rolling_loss": "500.5"}, "risk.max_rolling_loss"),
        # Root cho 10000 nhưng account chỉ 8000: không được nới cap account.
        ({"max_notional": "8000.01"}, "risk.max_notional"),
    ],
)
def test_overlay_cannot_raise_root_or_account_caps(risk: dict[str, object], field: str) -> None:
    world = World()

    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1, "risk": risk}))

    assert error.value.code == "BOT_CONFIG_ROOT_MISMATCH"
    assert field in codes(error)


@pytest.mark.parametrize(
    ("universe", "field"),
    [
        ({"symbols": ["SOLUSDT"]}, "universe.symbols"),
        ({"timeframes": ["15m", "1h", "4h"]}, "universe.timeframes"),
        ({"timeframes": ["5m", "4h"]}, "universe.timeframes"),
    ],
)
def test_universe_must_stay_inside_root_and_keep_strategy_timeframes(
    universe: dict[str, object], field: str
) -> None:
    world = World()

    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1, "universe": universe}))

    assert field in codes(error)


def test_unknown_inactive_and_foreign_workspace_release_are_rejected() -> None:
    world = World()
    unknown = world.version({"schema_version": 1, "strategy": {"release_id": "h99-v1"}})
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(unknown)
    assert "strategy.release_id" in codes(error)

    inactive_world = World()
    inactive_world.releases = [
        inactive_world.release("h03-v1"),
        inactive_world.release("h01-v1", status="RETIRED"),
    ]
    with pytest.raises(BotConfigResolutionError) as error:
        inactive_world.resolve(
            inactive_world.version({"schema_version": 1, "strategy": {"release_id": "h01-v1"}})
        )
    assert "strategy.release_id" in codes(error)

    foreign_world = World()
    foreign_world.releases = [
        foreign_world.release("h03-v1"),
        foreign_world.release("h01-v1", workspace_id=uuid4()),
    ]
    with pytest.raises(BotConfigResolutionError) as error:
        foreign_world.resolve(
            foreign_world.version({"schema_version": 1, "strategy": {"release_id": "h01-v1"}})
        )
    assert "strategy.release_id" in codes(error)


def test_release_with_mismatched_content_hash_is_not_active() -> None:
    world = World()
    world.releases = [replace_release(world.releases[0], content_hash="b" * 64)]

    with pytest.raises(BotConfigResolutionError):
        world.resolve(world.version({"schema_version": 1}))


def replace_release(release: SimpleNamespace, **changes: object) -> SimpleNamespace:
    return SimpleNamespace(**(vars(release) | changes))


def test_parameters_follow_strategy_selection_contract() -> None:
    world = World()
    for strategy in (
        {"release_id": "h03-v1", "parameters": {"donchian_lookback": 19}},
        {"release_id": "h03-v1", "parameters": {"unknown": 1}},
        {"release_id": "h01-v1", "parameters": {"donchian_lookback": 60}},
    ):
        with pytest.raises(BotConfigResolutionError) as error:
            world.resolve(world.version({"schema_version": 1, "strategy": strategy}))
        assert "strategy.parameters" in codes(error)


def test_draft_and_not_yet_effective_versions_never_resolve() -> None:
    world = World()
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(
            world.version(
                {"schema_version": 1}, status="DRAFT", published_at=None, effective_at=None
            )
        )
    assert error.value.code == "BOT_CONFIG_NOT_PUBLISHED"

    future = world.version({"schema_version": 1}, effective_at=NOW + timedelta(seconds=1))
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(future)
    assert error.value.code == "BOT_CONFIG_NOT_EFFECTIVE"

    late_root = PublishedWorkspaceSettings(
        workspace_id=world.workspace_id,
        settings_version_id=world.root.settings_version_id,
        settings_hash=world.root.settings_hash,
        settings=world.root.settings,
        effective_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1}), workspace_settings=late_root)
    assert error.value.code == "ROOT_SETTINGS_NOT_EFFECTIVE"

    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1}), as_of=datetime(2026, 10, 24))  # noqa: DTZ001
    assert error.value.code == "AS_OF_NAIVE"


def test_effective_at_is_latest_of_root_and_bot_and_resolve_time_is_kept_separately() -> None:
    world = World()
    version = world.version({"schema_version": 1})

    effective = world.resolve(version)

    assert effective.effective_at == version.effective_at
    assert effective.resolved_at == NOW


def test_effective_hash_is_deterministic_and_verifiable() -> None:
    world = World()
    version = world.version({"schema_version": 1, "risk": {"max_positions": 1}})

    first = world.resolve(version)
    later = world.resolve(version, as_of=NOW + timedelta(minutes=5))

    assert first.effective_hash == later.effective_hash
    assert compute_effective_hash(first) == first.effective_hash
    assert first.verify_hash() is True
    tampered = replace(first, symbols=("BTCUSDT",))
    assert tampered.verify_hash() is False
    changed = world.resolve(world.version({"schema_version": 1, "risk": {"max_positions": 2}}))
    assert changed.effective_hash != first.effective_hash


def test_overlay_hash_mismatch_and_invalid_payload_fail_closed() -> None:
    world = World()
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1}, overlay_hash="0" * 64))
    assert error.value.code == "BOT_CONFIG_HASH_MISMATCH"

    corrupt_payload = {"schema_version": 1, "venue": "BINANCE_USDM"}
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(
            world.version(
                {"schema_version": 1},
                overlay_json=corrupt_payload,
                overlay_hash=overlay_hash(corrupt_payload),
            )
        )
    assert error.value.code == "BOT_CONFIG_INVALID"


@pytest.mark.parametrize(
    "mutation",
    [
        {"workspace_id": uuid4()},
        {"account_id": uuid4()},
    ],
)
def test_version_scope_must_match_root_and_account(mutation: dict[str, object]) -> None:
    world = World()

    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1}, **mutation))

    assert error.value.code == "BOT_CONFIG_SCOPE_MISMATCH"


def test_account_from_other_workspace_or_non_paper_or_other_venue_is_rejected() -> None:
    world = World()
    for changes in (
        {"workspace_id": uuid4()},
        {"mode": "LIVE"},
        {"venue": "okx_swap"},
    ):
        account = SimpleNamespace(**(vars(world.account) | changes))
        with pytest.raises(BotConfigResolutionError) as error:
            world.resolve(world.version({"schema_version": 1}), account=account)
        assert error.value.code == "BOT_CONFIG_SCOPE_MISMATCH"


def test_risk_profile_must_be_the_one_pinned_by_root() -> None:
    world = World()
    other_profile = replace(world.risk_profile, profile_id=uuid4())

    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(world.version({"schema_version": 1}), root_risk_profile=other_profile)

    assert error.value.code == "BOT_CONFIG_SCOPE_MISMATCH"


def test_root_tightening_after_bot_publish_is_a_mismatch_not_a_silent_clamp() -> None:
    world = World()
    version = world.version({"schema_version": 1, "risk": {"max_positions": 2}})
    tighter_caps = dict(HARD_CAPS) | {"max_positions": 1}
    tighter = risk_limits_from_profile(
        SimpleNamespace(
            id=world.profile_id,
            workspace_id=world.workspace_id,
            name="Conservative",
            version=2,
            max_risk_fraction=Decimal("0.01"),
            settings={"schema_version": 1, "hard_caps": tighter_caps},
        )
    )

    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(version, root_risk_profile=tighter)

    assert "risk.max_positions" in codes(error)


def test_validate_bot_overlay_reports_all_issues_for_admin_api() -> None:
    world = World()
    overlay = BotConfigOverlay.model_validate(
        {
            "schema_version": 1,
            "strategy": {"release_id": "h99-v1"},
            "universe": {"symbols": ["SOLUSDT"]},
            "risk": {"max_positions": 5},
        }
    )

    issues = validate_bot_overlay(
        overlay,
        workspace_settings=world.root,
        account=world.account,  # type: ignore[arg-type]
        root_risk_profile=world.risk_profile,
        strategy_releases=world.releases,  # type: ignore[arg-type]
    )

    assert {issue.field for issue in issues} == {
        "strategy.release_id",
        "universe.symbols",
        "risk.max_positions",
    }
    assert all(issue.message for issue in issues)
