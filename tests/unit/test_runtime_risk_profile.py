from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from tests.unit.test_risk_engine import _settings

from trading_bot.application.runtime_risk_profile import (
    RiskProfileRuntimeError,
    load_risk_profile,
    risk_limits_from_profile,
)
from trading_bot.domain.risk.planner import PlannerSettings


def _caps() -> dict[str, object]:
    return {
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
    }


class ProfileSession:
    def __init__(self, row: object | None) -> None:
        self.row = row
        self.statement = ""

    async def scalar(self, statement: object) -> object | None:
        self.statement = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
        return self.row


@pytest.mark.asyncio
async def test_loads_workspace_scoped_profile_and_maps_explicit_caps() -> None:
    workspace_id = uuid4()
    profile_id = uuid4()
    row = SimpleNamespace(
        id=profile_id,
        workspace_id=workspace_id,
        name="Conservative",
        version=3,
        max_risk_fraction=Decimal("0.01"),
        settings=_caps(),
    )
    session = ProfileSession(row)

    profile = await load_risk_profile(session, workspace_id=workspace_id, profile_id=profile_id)  # type: ignore[arg-type]

    assert profile.profile_id == profile_id
    assert profile.workspace_id == workspace_id
    assert profile.limits.per_trade_risk_fraction == Decimal("0.01")
    assert profile.limits.max_aggregate_open_risk == Decimal("250")
    assert "risk_profile_version.workspace_id" in session.statement
    assert "risk_profile_version.id" in session.statement


@pytest.mark.asyncio
async def test_missing_profile_fails_closed() -> None:
    with pytest.raises(RiskProfileRuntimeError, match="không tồn tại"):
        await load_risk_profile(
            ProfileSession(None),
            workspace_id=uuid4(),
            profile_id=uuid4(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_empty_legacy_settings_rejected_with_schema_blocker() -> None:
    row = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Legacy",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        settings={},
    )

    with pytest.raises(RiskProfileRuntimeError, match="schema_version"):
        await load_risk_profile(
            ProfileSession(row),
            workspace_id=row.workspace_id,
            profile_id=row.id,  # type: ignore[arg-type]
        )


def test_rejects_unknown_or_non_decimal_cap_values() -> None:
    valid = _caps()
    valid["surprise"] = True
    row = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Bad",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        settings=valid,
    )

    with pytest.raises(RiskProfileRuntimeError, match="không hợp lệ"):
        risk_limits_from_profile(row)


def test_applies_limits_only_to_matching_workspace_and_profile() -> None:
    workspace_id = uuid4()
    profile_id = uuid4()
    row = SimpleNamespace(
        id=profile_id,
        workspace_id=workspace_id,
        name="Safe",
        version=1,
        max_risk_fraction=Decimal("0.015"),
        settings=_caps(),
    )
    profile = risk_limits_from_profile(row)
    planner_settings: PlannerSettings = _settings()
    planner_settings = PlannerSettings(
        workspace_id=str(workspace_id),
        settings_version_id=planner_settings.settings_version_id,
        risk_profile_version_id=str(profile_id),
        published=True,
        active_release_ids=planner_settings.active_release_ids,
        leverage=planner_settings.leverage,
        exchange=planner_settings.exchange,
        limits=planner_settings.limits,
        release_content_hashes=planner_settings.release_content_hashes,
    )

    updated = profile.apply_to(planner_settings)

    assert updated.limits.per_trade_risk_fraction == Decimal("0.015")
    assert updated.limits.max_data_age == timedelta(seconds=300)
    with pytest.raises(RiskProfileRuntimeError, match="workspace"):
        profile.apply_to(_settings())
