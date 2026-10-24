from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.application.paper_execution_profile import (
    ExecutionProfileResolutionError,
    FeeScheduleDefinition,
    FillModelDefinition,
    PaperExecutionProfileCatalog,
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.risk.planner import AccountSnapshot


def _published(*, fee_schedule: str = "paper-fees-v1") -> PublishedWorkspaceSettings:
    workspace_id = uuid4()
    settings_version_id = uuid4()
    payload = valid_payload()
    paper = payload["paper_execution"]
    assert isinstance(paper, dict)
    paper["fee_schedule_version"] = fee_schedule
    return PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=settings_version_id,
        settings_hash="a" * 64,
        settings=WorkspaceSettings.model_validate(payload),
    )


def _catalog(
    *, quote_asset: str = "USDT", entry_fee: str = "0.001", exit_fee: str = "0.001"
) -> PaperExecutionProfileCatalog:
    return PaperExecutionProfileCatalog(
        fee_schedules=(
            FeeScheduleDefinition(
                version_id="paper-fees-v1",
                quote_asset=quote_asset,
                entry_fee_rate=Decimal(entry_fee),
                exit_fee_rate=Decimal(exit_fee),
            ),
        ),
        fill_models=(
            FillModelDefinition(
                reference="next_bar_with_adverse_costs",
                version_id="paper-fill-v3",
                latency=timedelta(seconds=2),
                spread_rate=Decimal("0.0002"),
                slippage_rate=Decimal("0.0003"),
                adverse_funding_per_unit=Decimal("0.05"),
                max_fill_quantity_per_event=Decimal("0.2"),
            ),
        ),
    )


def test_resolves_immutable_versioned_profile_from_published_root_settings() -> None:
    published = _published()

    profile = resolve_paper_execution_profile(published, _catalog())

    assert profile.workspace_id == published.workspace_id
    assert profile.settings_version_id == published.settings_version_id
    assert profile.settings_hash == published.settings_hash
    assert profile.quote_asset == "USDT"
    assert profile.new_account_initial_equity == Decimal("10000")
    assert profile.fee_schedule_version == "paper-fees-v1"
    assert profile.fill_model_reference == "next_bar_with_adverse_costs"
    assert profile.fee_schedule_hash
    assert profile.fill_model_hash
    assert profile.profile_hash == profile.fill_policy.version_id
    assert profile.profile_hash == profile.cost_model.version_id
    assert profile.fill_policy.latency == timedelta(seconds=2)
    assert profile.fill_policy.spread_rate == Decimal("0.0002")
    assert profile.fill_policy.slippage_rate == Decimal("0.0003")
    assert profile.fill_policy.fee_rate == Decimal("0.001")
    assert profile.cost_model.entry_fee_rate == Decimal("0.001")
    assert profile.cost_model.exit_fee_rate == Decimal("0.001")
    assert profile.cost_model.entry_slippage_rate == Decimal("0.0005")
    assert profile.cost_model.adverse_funding_per_unit == Decimal("0.05")


def test_strategy_only_settings_publish_keeps_execution_identity_but_lineage_changes() -> None:
    original = _published()
    changed_payload = original.settings.model_dump(mode="python")
    changed_payload["strategy"]["default_release"] = "h02-v1"
    next_settings = WorkspaceSettings.model_validate(changed_payload)
    changed = PublishedWorkspaceSettings(
        workspace_id=original.workspace_id,
        settings_version_id=uuid4(),
        settings_hash="b" * 64,
        settings=next_settings,
    )

    old_profile = resolve_paper_execution_profile(original, _catalog())
    new_profile = resolve_paper_execution_profile(changed, _catalog())

    assert new_profile.profile_hash == old_profile.profile_hash
    assert new_profile.settings_version_id != old_profile.settings_version_id
    assert new_profile.settings_hash != old_profile.settings_hash


def test_execution_identity_changes_when_bootstrap_or_fill_policy_changes() -> None:
    original = _published()
    base = resolve_paper_execution_profile(original, _catalog())
    payload = original.settings.model_dump(mode="python")
    payload["paper_execution"]["starting_equity"] = "12000"
    changed = PublishedWorkspaceSettings(
        workspace_id=original.workspace_id,
        settings_version_id=uuid4(),
        settings_hash="c" * 64,
        settings=WorkspaceSettings.model_validate(payload),
    )

    assert resolve_paper_execution_profile(changed, _catalog()).profile_hash != base.profile_hash
    catalog = _catalog()
    changed_fill = replace(catalog.fill_models[0], slippage_rate=Decimal("0.0004"))
    changed_catalog = PaperExecutionProfileCatalog(catalog.fee_schedules, (changed_fill,))
    assert (
        resolve_paper_execution_profile(original, changed_catalog).profile_hash != base.profile_hash
    )


def test_default_catalog_has_explicit_stress_versions_and_values() -> None:
    first = default_paper_execution_profile_catalog()
    second = default_paper_execution_profile_catalog()
    published = _published()

    profile = resolve_paper_execution_profile(published, first)
    fill = first.fill_models[0]
    fees = first.fee_schedules[0]

    assert first == second
    assert fees.version_id == "paper-fees-v1"
    assert fees.entry_fee_rate == Decimal("0.001")
    assert fees.exit_fee_rate == Decimal("0.001")
    assert fill.version_id == "next-bar-adverse-v1"
    assert fill.reference == "next_bar_with_adverse_costs"
    assert fill.latency == timedelta(seconds=5)
    assert fill.spread_rate == Decimal("0.0005")
    assert fill.slippage_rate == Decimal("0.001")
    assert fill.adverse_funding_per_unit == Decimal("0.05")
    assert fill.max_fill_quantity_per_event is None
    assert profile.fill_model_version == fill.version_id


def test_fails_closed_when_published_fee_schedule_is_not_registered() -> None:
    published = _published(fee_schedule="fee-v99")

    with pytest.raises(ExecutionProfileResolutionError, match="(?i)fee schedule"):
        resolve_paper_execution_profile(published, _catalog())


def test_fails_closed_when_fill_model_reference_is_not_registered() -> None:
    published = _published()
    catalog = PaperExecutionProfileCatalog(
        fee_schedules=_catalog().fee_schedules,
        fill_models=(),
    )

    with pytest.raises(ExecutionProfileResolutionError, match="(?i)fill model"):
        resolve_paper_execution_profile(published, catalog)


def test_fails_closed_on_quote_asset_or_unrepresentable_fee_mismatch() -> None:
    published = _published()

    with pytest.raises(ExecutionProfileResolutionError, match="(?i)quote asset"):
        resolve_paper_execution_profile(published, _catalog(quote_asset="USDC"))
    with pytest.raises(ExecutionProfileResolutionError, match="entry/exit"):
        resolve_paper_execution_profile(published, _catalog(exit_fee="0.002"))


def test_account_validation_checks_scope_currency_and_known_state() -> None:
    published = _published()
    profile = resolve_paper_execution_profile(published, _catalog())
    account = AccountSnapshot(
        workspace_id=str(published.workspace_id),
        account_id="paper-account",
        settings_version_id=str(published.settings_version_id),
        captured_at=datetime.now(UTC),
        equity=Decimal("9000"),
        available_margin=Decimal("8000"),
        is_known=True,
    )

    profile.validate_account_snapshot(account, account_quote_asset="USDT")

    with pytest.raises(ExecutionProfileResolutionError, match="(?i)quote asset"):
        profile.validate_account_snapshot(account, account_quote_asset="USDC")
    with pytest.raises(ExecutionProfileResolutionError, match="workspace/version"):
        profile.validate_account_snapshot(
            AccountSnapshot(
                workspace_id="other-workspace",
                account_id="paper-account",
                settings_version_id=str(published.settings_version_id),
                captured_at=datetime.now(UTC),
                equity=Decimal("9000"),
                available_margin=Decimal("8000"),
                is_known=True,
            ),
            account_quote_asset="USDT",
        )
