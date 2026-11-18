"""Unit test cho đồng bộ StrategyRelease khi hash release nâng cấp (CRY-437)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.adapters.database.models.entities import StrategyRelease
from trading_bot.admin.strategy_release_sync import (
    ExistingReleaseRow,
    plan_strategy_release_sync,
    sync_workspace_strategy_releases,
)
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    resolve_effective_bot_config,
)
from trading_bot.application.runtime_risk_profile import risk_limits_from_profile
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.cli.strategy_releases import _parser as cli_parser
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings

CARD_RELEASE_ID = "sc-binance-usdm-vol-breakout-001-v1.0.0"
OLD_CARD_HASH = "9055c001" + "0" * 56
NOW = datetime(2026, 11, 18, 2, 0, tzinfo=UTC)


def _bot_world(
    releases: list[SimpleNamespace],
) -> tuple[
    PublishedWorkspaceSettings,
    Any,
    SimpleNamespace,
    SimpleNamespace,
]:
    workspace_id = uuid4()
    bot_id = uuid4()
    account_id = uuid4()
    profile_id = uuid4()
    payload = valid_payload()
    payload["strategy"] = {
        "default_release": "h03-v1",
        "parameters": {"donchian_lookback": 55},
        "entry_timeframe": "1h",
        "confirmation_timeframe": "4h",
    }
    payload["risk"] = {"profile_version_id": str(profile_id)}
    root = PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=uuid4(),
        settings_hash="a" * 64,
        settings=WorkspaceSettings.model_validate(payload),
        effective_at=NOW - timedelta(hours=2),
    )
    risk_profile = risk_limits_from_profile(
        SimpleNamespace(
            id=profile_id,
            workspace_id=workspace_id,
            name="Conservative",
            version=1,
            max_risk_fraction=Decimal("0.01"),
            settings={
                "schema_version": 1,
                "hard_caps": {
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
                },
            },
        )
    )
    account = SimpleNamespace(
        id=account_id,
        workspace_id=workspace_id,
        mode="PAPER",
        venue="binance_usdm",
        status="ENABLED",
        max_notional=Decimal("8000"),
    )
    for item in releases:
        item.workspace_id = workspace_id
    overlay = BotConfigOverlay.model_validate(
        {"schema_version": 1, "strategy": {"release_id": CARD_RELEASE_ID, "parameters": {}}}
    )
    overlay_payload = canonical_overlay(overlay)
    bot_version = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        version=1,
        schema_version=1,
        status="PUBLISHED",
        overlay_json=overlay_payload,
        overlay_hash=overlay_hash(overlay_payload),
        effective_at=NOW - timedelta(hours=1),
    )
    return root, risk_profile, account, bot_version


def test_stale_card_hash_fails_closed_then_resolves_after_sync_plan() -> None:
    registry = strategy_registry()
    current_hash = registry.get_release(CARD_RELEASE_ID).content_hash
    assert current_hash != OLD_CARD_HASH

    stale_id = uuid4()
    stale_release = SimpleNamespace(
        id=stale_id,
        workspace_id=uuid4(),
        name=CARD_RELEASE_ID,
        version=1,
        status="PUBLISHED",
        content_hash=OLD_CARD_HASH,
    )
    root, risk_profile, account, bot_version = _bot_world([stale_release])

    # Trước đồng bộ: resolver chặn fail-closed với BOT_CONFIG_ROOT_MISMATCH.
    with pytest.raises(BotConfigResolutionError) as error:
        resolve_effective_bot_config(
            root,
            bot_version,
            account,
            NOW,
            root_risk_profile=risk_profile,
            strategy_releases=[stale_release],
            registry=registry,
        )
    assert error.value.code == "BOT_CONFIG_ROOT_MISMATCH"

    # Lập kế hoạch đồng bộ: supersede bản v1 cũ, publish v2 với hash mới.
    plan = plan_strategy_release_sync(
        release_id=CARD_RELEASE_ID,
        existing_rows=[
            ExistingReleaseRow(
                id=stale_id,
                name=CARD_RELEASE_ID,
                version=1,
                status="PUBLISHED",
                content_hash=OLD_CARD_HASH,
            )
        ],
        registry=registry,
    )
    assert plan.action == "published_new_version"
    assert plan.supersede_ids == (stale_id,)
    assert plan.new_version == 2
    assert plan.target_hash == current_hash

    stale_release.status = "SUPERSEDED"
    new_release_id = uuid4()
    new_release = SimpleNamespace(
        id=new_release_id,
        workspace_id=root.workspace_id,
        name=CARD_RELEASE_ID,
        version=2,
        status="PUBLISHED",
        content_hash=current_hash,
    )

    # Sau đồng bộ: cùng BotConfigVersion cũ của bot tự resolve thành công.
    effective = resolve_effective_bot_config(
        root,
        bot_version,
        account,
        NOW,
        root_risk_profile=risk_profile,
        strategy_releases=[stale_release, new_release],
        registry=registry,
    )
    assert effective.strategy.release_id == CARD_RELEASE_ID
    assert effective.strategy.release_content_hash == current_hash
    assert effective.strategy.workspace_release_id == new_release_id


def test_plan_strategy_release_sync_is_idempotent_when_matched() -> None:
    registry = strategy_registry()
    current_hash = registry.get_release(CARD_RELEASE_ID).content_hash
    plan = plan_strategy_release_sync(
        release_id=CARD_RELEASE_ID,
        existing_rows=[
            ExistingReleaseRow(
                id=uuid4(),
                name=CARD_RELEASE_ID,
                version=2,
                status="PUBLISHED",
                content_hash=current_hash,
            ),
            ExistingReleaseRow(
                id=uuid4(),
                name=CARD_RELEASE_ID,
                version=1,
                status="SUPERSEDED",
                content_hash=OLD_CARD_HASH,
            ),
        ],
        registry=registry,
    )
    assert plan.action == "unchanged"
    assert plan.supersede_ids == ()
    assert plan.new_version is None


def test_plan_strategy_release_sync_republishes_existing_matching_row() -> None:
    registry = strategy_registry()
    current_hash = registry.get_release(CARD_RELEASE_ID).content_hash
    stale_id = uuid4()
    keeper_id = uuid4()
    plan = plan_strategy_release_sync(
        release_id=CARD_RELEASE_ID,
        existing_rows=[
            ExistingReleaseRow(
                id=stale_id,
                name=CARD_RELEASE_ID,
                version=1,
                status="PUBLISHED",
                content_hash=OLD_CARD_HASH,
            ),
            ExistingReleaseRow(
                id=keeper_id,
                name=CARD_RELEASE_ID,
                version=2,
                status="DRAFT",
                content_hash=current_hash,
            ),
        ],
        registry=registry,
    )
    assert plan.action == "republished_existing"
    assert plan.supersede_ids == (stale_id,)
    assert plan.republish_id == keeper_id
    assert plan.new_version == 2


def test_plan_strategy_release_sync_handles_unknown_release() -> None:
    plan = plan_strategy_release_sync(
        release_id="h99-does-not-exist",
        existing_rows=[],
        registry=strategy_registry(),
    )
    assert plan.action == "unknown_release"
    assert plan.target_hash is None


@pytest.mark.asyncio
async def test_sync_workspace_strategy_releases_updates_stale_row() -> None:
    workspace_id = uuid4()
    registry = strategy_registry()
    current_hash = registry.get_release(CARD_RELEASE_ID).content_hash
    stale_row = StrategyRelease(
        id=uuid4(),
        workspace_id=workspace_id,
        name=CARD_RELEASE_ID,
        version=1,
        status="PUBLISHED",
        recipe={},
        content_hash=OLD_CARD_HASH,
    )

    added: list[object] = []

    class _Scalars:
        def __init__(self, rows: list[StrategyRelease]) -> None:
            self._rows = rows

        def all(self) -> list[StrategyRelease]:
            return self._rows

    session = SimpleNamespace(
        execute=AsyncMock(),
        scalars=AsyncMock(return_value=_Scalars([stale_row])),
        add=added.append,
        flush=AsyncMock(),
    )

    report = await sync_workspace_strategy_releases(
        session,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        registry=registry,
    )
    assert stale_row.status == "SUPERSEDED"
    assert len(added) == 1
    created = added[0]
    assert isinstance(created, StrategyRelease)
    assert created.workspace_id == workspace_id
    assert created.name == CARD_RELEASE_ID
    assert created.version == 2
    assert created.status == "PUBLISHED"
    assert created.content_hash == current_hash
    assert report.plans[0].action == "published_new_version"
    session.flush.assert_awaited_once()


def test_cli_parser_supports_workspace_slug_and_dry_run() -> None:
    args = cli_parser().parse_args(["sync", "--workspace-slug", "crypidian-cards-v1", "--dry-run"])
    assert args.command == "sync"
    assert args.workspace_slug == "crypidian-cards-v1"
    assert args.dry_run is True
    assert isinstance(UUID(str(uuid4())), UUID)
