"""PAPER runtime dùng cấu hình hiệu lực của bot cho entry mới và ghim lineage cho vị thế."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_bot_config_resolver import NOW, World
from tests.unit.test_paper_runtime_application import _application_with_fakes

from trading_bot.adapters.database.repositories.workspace_settings import settings_hash
from trading_bot.application import paper_decision_cycle
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    EffectiveBotConfig,
    resolve_effective_bot_config,
)
from trading_bot.application.paper_decision_cycle import DecisionCycleOutcome, PaperDecisionCycle
from trading_bot.application.paper_runtime_application import (
    PaperRuntimeApplicationError,
    _decision_idempotency_key,
)
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings
from trading_bot.domain.risk.planner import ExchangeRules, PlannerSettings
from trading_bot.domain.strategy.registry import StrategyRegistry


def effective_for(
    published: PublishedWorkspaceSettings,
    account_id: Any,
    bot_id: Any,
    overlay: dict[str, Any],
) -> EffectiveBotConfig:
    """Resolve thật trên root của fixture runtime để có hash hợp lệ."""
    world = World()
    world.workspace_id = published.workspace_id
    world.account_id = account_id
    world.bot_id = bot_id
    world.root = published
    world.profile_id = published.settings.risk.profile_version_id
    world.risk_profile = replace(
        world.risk_profile,
        profile_id=world.profile_id,
        workspace_id=published.workspace_id,
    )
    world.account = SimpleNamespace(
        **(
            vars(world.account)
            | {
                "id": account_id,
                "workspace_id": published.workspace_id,
                "max_notional": Decimal("10000"),
            }
        )
    )
    world.releases = [
        world.release(published.settings.strategy.default_release),
        world.release("h03-v1"),
    ]
    version = world.version(overlay, effective_at=published.effective_at)
    return resolve_effective_bot_config(
        published,
        version,  # type: ignore[arg-type]
        world.account,  # type: ignore[arg-type]
        published.effective_at + timedelta(seconds=1),  # type: ignore[operator]
        root_risk_profile=world.risk_profile,
        strategy_releases=world.releases,  # type: ignore[arg-type]
    )


def application(loader: Any) -> tuple[Any, ...]:
    overrides = {} if loader is None else {"bot_config_loader": loader}
    return _application_with_fakes(fail_submission=False, **overrides)


def test_entry_decision_uses_bot_config_and_records_bot_lineage() -> None:
    holder: dict[str, EffectiveBotConfig] = {}

    async def loader(*, uow: Any, account: Any, published: Any, as_of: Any) -> EffectiveBotConfig:
        config = effective_for(
            published,
            account.account_id,
            account.bot_id,
            {"schema_version": 1, "risk": {"per_trade_risk_fraction": "0.005"}},
        )
        holder["config"] = config
        return config

    service, command, callback_state, keys, counters, _, _, _ = application(loader)
    asyncio.run(service.evaluate_and_submit(command))
    # Helper fake dùng state lớp chung nên dựng service đối chứng sau khi chạy service đầu.
    legacy, legacy_command, _, legacy_keys, legacy_counters, _, _, _ = application(None)
    asyncio.run(legacy.evaluate_and_submit(legacy_command))

    config = holder["config"]
    assert counters.cycle_kwargs["bot_config"] is config
    assert legacy_counters.cycle_kwargs["bot_config"] is None
    evidence = callback_state.submitted_evidence
    assert evidence["bot_config_version_id"] == str(config.bot_config_version_id)
    assert evidence["bot_config_hash"] == config.bot_config_hash
    assert evidence["effective_config_hash"] == config.effective_hash
    assert keys[0].startswith("paper-decision-v1:")
    assert legacy_keys[0].startswith("paper-decision-v1:")


def test_bot_config_version_changes_decision_idempotency_key_but_legacy_key_is_stable() -> None:
    account_id, settings_id = uuid4(), uuid4()
    arguments = {
        "account_id": account_id,
        "settings_version_id": settings_id,
        "strategy_release": "h03-v1",
        "feature_snapshot_id": "feature-1",
    }

    legacy = _decision_idempotency_key(**arguments)
    legacy_explicit = _decision_idempotency_key(**arguments, bot_config_version_id=None)
    first = _decision_idempotency_key(**arguments, bot_config_version_id=uuid4())
    second = _decision_idempotency_key(**arguments, bot_config_version_id=uuid4())

    # Key cũ (MB-01) không đổi để retry intent lịch sử vẫn khớp.
    assert legacy == legacy_explicit
    assert len({legacy, first, second}) == 3


def test_bot_config_resolution_failure_blocks_entry_fail_closed() -> None:
    async def loader(**_: Any) -> None:
        raise BotConfigResolutionError("BOT_CONFIG_ROOT_MISMATCH")

    service, command, _, _, counters, _, _, _ = application(loader)

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_BOT_CONFIG_NOT_EFFECTIVE"):
        asyncio.run(service.evaluate_and_submit(command))

    assert counters.cycles == 0
    assert counters.reserves == 0
    assert counters.submissions == 0


def test_bot_strategy_release_other_than_root_requires_fanout() -> None:
    async def loader(**kwargs: Any) -> EffectiveBotConfig:
        published = kwargs["published"]
        other = "h03-v1" if published.settings.strategy.default_release != "h03-v1" else "h01-v1"
        return effective_for(
            published,
            kwargs["account"].account_id,
            kwargs["account"].bot_id,
            {"schema_version": 1, "strategy": {"release_id": other}},
        )

    service, command, _, _, counters, _, _, _ = application(loader)

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_BOT_STRATEGY_FANOUT_REQUIRED"):
        asyncio.run(service.evaluate_and_submit(command))
    assert counters.cycles == 0


@pytest.mark.parametrize("tamper", ["account", "hash", "root"])
def test_bot_config_scope_or_hash_mismatch_is_rejected(tamper: str) -> None:
    async def loader(**kwargs: Any) -> EffectiveBotConfig:
        config = effective_for(
            kwargs["published"],
            kwargs["account"].account_id,
            kwargs["account"].bot_id,
            {"schema_version": 1},
        )
        if tamper == "account":
            return replace(config, account_id=uuid4())
        if tamper == "hash":
            return replace(config, symbols=("BTCUSDT",))
        return replace(config, settings_version_id=uuid4())

    service, command, _, _, counters, _, _, _ = application(loader)

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_BOT_CONFIG_SCOPE_MISMATCH"):
        asyncio.run(service.evaluate_and_submit(command))
    assert counters.cycles == 0


def test_duplicate_intent_from_other_bot_version_conflicts() -> None:
    async def loader(**kwargs: Any) -> EffectiveBotConfig:
        return effective_for(
            kwargs["published"],
            kwargs["account"].account_id,
            kwargs["account"].bot_id,
            {"schema_version": 1},
        )

    service, command, _, _, _, published, profile, _ = application(loader)
    from tests.unit.test_paper_runtime_application import _FakeUnitOfWork

    _FakeUnitOfWork.scalar_results = [
        SimpleNamespace(
            id=uuid4(),
            risk_decision_id=uuid4(),
            risk_reservation_id=uuid4(),
            settings_version_id=published.settings_version_id,
            feature_snapshot_id=command.feature_snapshot_id,
            evidence={
                "settings_hash": published.settings_hash,
                "paper_execution_profile_hash": profile.profile_hash,
                "evaluated_at": command.evaluated_at.isoformat(),
                "strategy_release_hash": StrategyRegistry()
                .get_release(published.settings.strategy.default_release)
                .content_hash,
                "bot_config_version_id": str(uuid4()),
            },
        )
    ]

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_IDEMPOTENCY_CONFLICT"):
        asyncio.run(service.evaluate_and_submit(command))


def test_position_pin_uses_bot_overlay_parameters_recorded_in_intent() -> None:
    service, _, _, _, _, published, _, _ = application(None)
    release = StrategyRegistry().get_release(published.settings.strategy.default_release)
    release_id = uuid4()
    bot_id = uuid4()
    overlay = BotConfigOverlay.model_validate(
        {
            "schema_version": 1,
            "strategy": {"release_id": published.settings.strategy.default_release},
        }
    )
    payload = canonical_overlay(overlay)
    bot_version = SimpleNamespace(
        id=uuid4(),
        workspace_id=published.workspace_id,
        bot_id=bot_id,
        status="PUBLISHED",
        overlay_json=payload,
        overlay_hash=overlay_hash(payload),
    )
    settings_json = published.settings.model_dump(mode="json")
    # Root ghim tham số khác; overlay bot thay nguyên khối strategy nên pin phải rỗng.
    settings_json["strategy"]["parameters"] = {"donchian_lookback": 56}
    intent = SimpleNamespace(
        bot_id=bot_id,
        strategy_release_id=release_id,
        strategy_release_hash=release.content_hash,
        settings_version_id=published.settings_version_id,
        evidence={
            "bot_config_version_id": str(bot_version.id),
            "bot_config_hash": bot_version.overlay_hash,
        },
    )
    trade = SimpleNamespace(
        strategy_release_id=release_id,
        lineage={
            "strategy_release_id": str(release_id),
            "strategy_release_hash": release.content_hash,
            "settings_version_id": str(published.settings_version_id),
        },
    )

    class _Session:
        def __init__(self, bot_row: Any) -> None:
            self.bot_row = bot_row

        async def scalar(self, statement: object) -> Any:
            text = str(statement)
            if "bot_config_version" in text:
                return self.bot_row
            if "strategy_release" in text:
                return SimpleNamespace(
                    id=release_id,
                    workspace_id=published.workspace_id,
                    name=published.settings.strategy.default_release,
                    content_hash=release.content_hash,
                )
            return SimpleNamespace(
                id=published.settings_version_id,
                workspace_id=published.workspace_id,
                status="PUBLISHED",
                settings_json=settings_json,
                settings_hash=settings_hash(settings_json),
            )

    pinned = asyncio.run(
        service._load_position_release_pin(
            SimpleNamespace(session=_Session(bot_version), workspace_id=published.workspace_id),
            intent=intent,
            trade=trade,
        )
    )
    assert pinned.release_name == published.settings.strategy.default_release
    assert pinned.parameters == ()

    tampered = SimpleNamespace(**(vars(bot_version) | {"overlay_hash": "0" * 64}))
    invalid = asyncio.run(
        service._load_position_release_pin(
            SimpleNamespace(session=_Session(tampered), workspace_id=published.workspace_id),
            intent=intent,
            trade=trade,
        )
    )
    assert invalid.release_name is None


def test_decision_cycle_applies_bot_universe_before_strategy() -> None:
    world = World()
    config = world.resolve(
        world.version({"schema_version": 1, "universe": {"symbols": ["ETHUSDT"]}})
    )
    cycle = PaperDecisionCycle(StrategyRegistry(), None, None, None)  # type: ignore[arg-type]
    published = world.root
    snapshot = SimpleNamespace(
        symbol="BTCUSDT",
        series=(SimpleNamespace(timeframe="1h"),),
    )
    planner = SimpleNamespace(
        workspace_id=str(published.workspace_id),
        settings_version_id=str(published.settings_version_id),
        settings_hash=published.settings_hash,
    )

    result = cycle.prepare_published(
        published_settings=published,
        snapshot=snapshot,  # type: ignore[arg-type]
        evaluated_at=NOW,
        planner_settings=planner,  # type: ignore[arg-type]
        account=None,  # type: ignore[arg-type]
        risk_state=None,  # type: ignore[arg-type]
        open_exposures=(),
        cost_model=None,  # type: ignore[arg-type]
        selection_override=config.strategy_selection(),
        allowed_symbols=config.symbols,
        allowed_timeframes=config.timeframes,
    )

    assert result.outcome is DecisionCycleOutcome.NO_TRADE
    assert result.strategy_result.code == "BOT_MARKET_SCOPE_MISMATCH"


async def test_risk_profile_cycle_replaces_limits_and_selection_with_bot_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    config = world.resolve(
        world.version(
            {
                "schema_version": 1,
                "strategy": {"release_id": "h03-v1", "parameters": {"donchian_lookback": 60}},
                "risk": {"per_trade_risk_fraction": "0.005"},
            }
        )
    )
    captured: dict[str, Any] = {}

    async def fake_profile(_session: Any, **_: Any) -> Any:
        return world.risk_profile

    monkeypatch.setattr(paper_decision_cycle, "load_risk_profile", fake_profile)
    cycle = PaperDecisionCycle(StrategyRegistry(), None, None, None)  # type: ignore[arg-type]

    def capture(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "prepared"

    monkeypatch.setattr(cycle, "prepare_published", capture)
    planner = PlannerSettings(
        workspace_id=str(world.workspace_id),
        settings_version_id=str(world.root.settings_version_id),
        risk_profile_version_id=str(world.profile_id),
        published=True,
        active_release_ids=("h03-v1",),
        leverage=Decimal("1"),
        exchange=ExchangeRules(
            Decimal("0.1"),
            Decimal("0.001"),
            Decimal("1"),
            Decimal("5"),
            Decimal("10000"),
            Decimal("100"),
        ),
        limits=world.risk_profile.limits,
        release_content_hashes=(("h03-v1", StrategyRegistry().get_release("h03-v1").content_hash),),
        settings_hash=world.root.settings_hash,
    )

    result = await cycle.prepare_published_with_risk_profile(
        session=None,  # type: ignore[arg-type]
        published_settings=world.root,
        snapshot=None,  # type: ignore[arg-type]
        evaluated_at=NOW,
        planner_settings=planner,  # type: ignore[arg-type]
        account=None,  # type: ignore[arg-type]
        risk_state=None,  # type: ignore[arg-type]
        open_exposures=(),
        cost_model=None,  # type: ignore[arg-type]
        bot_config=config,
    )

    assert result == "prepared"
    assert captured["planner_settings"].limits == config.risk_limits
    assert captured["planner_settings"].limits.per_trade_risk_fraction == Decimal("0.005")
    assert captured["selection_override"].parameters == (("donchian_lookback", 60),)
    assert captured["allowed_symbols"] == config.symbols

    mismatched = replace(config, settings_version_id=uuid4())
    rejected = await cycle.prepare_published_with_risk_profile(
        session=None,  # type: ignore[arg-type]
        published_settings=world.root,
        snapshot=None,  # type: ignore[arg-type]
        evaluated_at=NOW,
        planner_settings=planner,  # type: ignore[arg-type]
        account=None,  # type: ignore[arg-type]
        risk_state=None,  # type: ignore[arg-type]
        open_exposures=(),
        cost_model=None,  # type: ignore[arg-type]
        bot_config=mismatched,
    )
    assert rejected.outcome is DecisionCycleOutcome.NO_TRADE
    assert rejected.strategy_result.code == "BOT_CONFIG_SCOPE_MISMATCH"
