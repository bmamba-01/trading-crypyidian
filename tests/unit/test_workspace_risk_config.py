"""Kiểm thử cấu hình rủi ro workspace, account profile, bot overlay và advisory lock (MB04.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_bot.adapters.database.models.entities import RiskProfileVersion
from trading_bot.adapters.database.repositories.workspace_settings import (
    WorkspaceSettingsRepository,
    canonical_settings,
    settings_hash,
)
from trading_bot.application.bot_config_resolver import (
    EffectiveBotConfig,
    resolve_effective_bot_config,
    validate_bot_overlay,
)
from trading_bot.application.runtime_risk_profile import (
    risk_limits_from_profile,
)
from trading_bot.config.bot_settings import (
    RISK_FRACTION_FIELDS,
    BotConfigOverlay,
    BotRiskOverlay,
    canonical_overlay,
)
from trading_bot.config.workspace_settings import (
    PublishedWorkspaceSettings,
    RiskSettings,
    WorkspaceSettings,
)
from trading_bot.domain.risk.planner import RiskLimits
from trading_bot.domain.strategy.registry import StrategyRegistry

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

HARD_CAPS: dict[str, object] = {
    "max_aggregate_open_risk": "250",
    "max_notional": "10000",
    "max_leverage": "5",
    "max_positions": 2,
    "max_symbol_notional": "2000",
    "max_concentration_fraction": "0.20",
    "max_daily_loss": "200",
    "max_rolling_loss": "500",
    "max_account_age_seconds": 30,
    "max_data_age_seconds": 300,
}


def valid_workspace_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "timeframes": ["5m", "1h", "4h"],
            "max_age_seconds": 30,
        },
        "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h01-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        },
        "risk": {"profile_version_id": str(uuid4())},
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": "10000",
            "fee_schedule_version": "paper-fees-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }


# ==============================================================================
# 1. Root Settings (WorkspaceSettings.risk)
# ==============================================================================


def test_root_risk_settings_has_safe_defaults() -> None:
    """Khi nạp payload cũ chỉ có profile_version_id, các trường mới nhận default an toàn."""
    profile_id = uuid4()
    risk = RiskSettings(profile_version_id=profile_id)

    assert risk.profile_version_id == profile_id
    assert risk.workspace_max_open_risk_fraction == Decimal("1.0")
    assert risk.max_equity_age_seconds == 300
    assert risk.max_conversion_age_seconds == 300
    assert risk.max_mark_age_seconds == 60


def test_root_risk_settings_accepts_valid_custom_values() -> None:
    """Nhận đúng các giá trị tùy biến hợp lệ."""
    profile_id = uuid4()
    risk = RiskSettings(
        profile_version_id=profile_id,
        workspace_max_open_risk_fraction=Decimal("0.75"),
        max_equity_age_seconds=120,
        max_conversion_age_seconds=180,
        max_mark_age_seconds=45,
    )

    assert risk.workspace_max_open_risk_fraction == Decimal("0.75")
    assert risk.max_equity_age_seconds == 120
    assert risk.max_conversion_age_seconds == 180
    assert risk.max_mark_age_seconds == 45


@pytest.mark.parametrize(
    "invalid_fraction",
    [
        Decimal("0"),
        Decimal("-0.1"),
        Decimal("1.0000000001"),
        "0",
        "-0.5",
        "1.0001",
        0.5,  # float bị cấm tuyệt đối
        1.0,  # float bị cấm tuyệt đối
        True,  # bool bị cấm
        False,
        "not-a-number",
    ],
)
def test_root_risk_settings_rejects_invalid_fraction(invalid_fraction: object) -> None:
    """workspace_max_open_risk_fraction phải thuộc (0, 1], không chấp nhận float/bool/NaN."""
    with pytest.raises((ValidationError, ValueError)):
        RiskSettings(
            profile_version_id=uuid4(),
            workspace_max_open_risk_fraction=invalid_fraction,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("max_equity_age_seconds", 0),
        ("max_equity_age_seconds", -1),
        ("max_equity_age_seconds", 86401),
        ("max_conversion_age_seconds", 0),
        ("max_conversion_age_seconds", 86401),
        ("max_mark_age_seconds", 0),
        ("max_mark_age_seconds", 3601),
    ],
)
def test_root_risk_settings_rejects_out_of_bounds_ages(field: str, invalid_value: int) -> None:
    """Tuổi tối đa của equity, conversion, mark phải nằm trong biên an toàn."""
    with pytest.raises(ValidationError):
        RiskSettings(profile_version_id=uuid4(), **{field: invalid_value})


def test_root_risk_settings_extra_fields_forbidden() -> None:
    """Từ chối khóa lạ trong khối risk."""
    with pytest.raises(ValidationError):
        RiskSettings(profile_version_id=uuid4(), unknown_field="hacked")  # type: ignore[call-arg]


def test_root_risk_settings_canonical_serialization() -> None:
    """Serialization chuẩn hóa số Decimal sang chuỗi gọn gàng, không thừa số 0."""
    payload = valid_workspace_payload()
    payload["risk"] = {
        "profile_version_id": str(uuid4()),
        "workspace_max_open_risk_fraction": "0.5000000000",
        "max_equity_age_seconds": 300,
        "max_conversion_age_seconds": 300,
        "max_mark_age_seconds": 60,
    }
    settings = WorkspaceSettings.model_validate(payload)
    serialized = canonical_settings(settings)

    assert serialized["risk"]["workspace_max_open_risk_fraction"] == "0.5"
    # settings_hash tính ổn định
    h1 = settings_hash(serialized)
    h2 = settings_hash(canonical_settings(settings))
    assert h1 == h2


# ==============================================================================
# 2. Account Risk Profile & RiskLimits
# ==============================================================================


def test_risk_limits_includes_account_max_open_risk_fraction() -> None:
    """RiskLimits có account_max_open_risk_fraction riêng biệt per_trade_risk_fraction."""
    limits = RiskLimits(
        per_trade_risk_fraction=Decimal("0.02"),
        max_aggregate_open_risk=Decimal("250"),
        max_notional=Decimal("10000"),
        max_leverage=Decimal("5"),
        max_positions=2,
        max_symbol_notional=Decimal("2000"),
        max_concentration_fraction=Decimal("0.2"),
        max_daily_loss=Decimal("200"),
        max_rolling_loss=Decimal("500"),
        max_account_age=timedelta(seconds=30),
        max_data_age=timedelta(seconds=300),
        account_max_open_risk_fraction=Decimal("0.80"),
    )

    assert limits.per_trade_risk_fraction == Decimal("0.02")
    assert limits.account_max_open_risk_fraction == Decimal("0.80")


@pytest.mark.parametrize(
    "invalid_fraction",
    [Decimal("0"), Decimal("-0.1"), Decimal("1.00001")],
)
def test_risk_limits_rejects_out_of_bounds_account_fraction(invalid_fraction: Decimal) -> None:
    """account_max_open_risk_fraction trong RiskLimits phải nằm trong (0, 1]."""
    with pytest.raises(ValueError, match="Tỷ lệ risk mở tối đa tài khoản phải nằm trong"):
        RiskLimits(
            per_trade_risk_fraction=Decimal("0.01"),
            max_aggregate_open_risk=Decimal("250"),
            max_notional=Decimal("10000"),
            max_leverage=Decimal("5"),
            max_positions=2,
            max_symbol_notional=Decimal("2000"),
            max_concentration_fraction=Decimal("0.2"),
            max_daily_loss=Decimal("200"),
            max_rolling_loss=Decimal("500"),
            max_account_age=timedelta(seconds=30),
            max_data_age=timedelta(seconds=300),
            account_max_open_risk_fraction=invalid_fraction,
        )


def test_runtime_risk_profile_loads_account_max_open_risk_fraction() -> None:
    """risk_limits_from_profile nạp đúng account_max_open_risk_fraction từ model."""
    record = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Conservative",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        max_open_risk_fraction=Decimal("0.60"),
        settings={"schema_version": 1, "hard_caps": dict(HARD_CAPS)},
    )
    profile = risk_limits_from_profile(record)

    assert profile.limits.per_trade_risk_fraction == Decimal("0.01")
    assert profile.limits.account_max_open_risk_fraction == Decimal("0.60")


def test_runtime_risk_profile_fallback_for_legacy_record() -> None:
    """Record cũ chưa có max_open_risk_fraction nhận default an toàn Decimal('1.0')."""
    record = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Conservative",
        version=1,
        max_risk_fraction=Decimal("0.01"),
        # Không có max_open_risk_fraction
        settings={"schema_version": 1, "hard_caps": dict(HARD_CAPS)},
    )
    profile = risk_limits_from_profile(record)

    assert profile.limits.account_max_open_risk_fraction == Decimal("1.0")


def test_risk_profile_version_model_has_column_and_constraint() -> None:
    """Kiểm tra model RiskProfileVersion có cột max_open_risk_fraction và CheckConstraint."""
    table = RiskProfileVersion.__table__
    assert "max_open_risk_fraction" in table.columns
    col = table.columns["max_open_risk_fraction"]
    assert col.nullable is False

    constraint_names = {c.name for c in table.constraints}
    assert any("open_risk_fraction_range" in name for name in constraint_names if name)


# ==============================================================================
# 3. Bot Overlay & Resolver Hierarchy
# ==============================================================================


def test_bot_risk_overlay_accepts_bot_max_open_risk_fraction() -> None:
    """BotRiskOverlay nhận bot_max_open_risk_fraction hợp lệ."""
    assert "bot_max_open_risk_fraction" in RISK_FRACTION_FIELDS
    overlay = BotRiskOverlay(bot_max_open_risk_fraction=Decimal("0.40"))
    assert overlay.bot_max_open_risk_fraction == Decimal("0.40")


@pytest.mark.parametrize("invalid", [0.5, True, Decimal("0"), Decimal("1.1"), "0", "1.5"])
def test_bot_risk_overlay_rejects_invalid_fraction(invalid: object) -> None:
    """BotRiskOverlay từ chối float/bool/out-of-bounds cho bot_max_open_risk_fraction."""
    with pytest.raises(ValidationError):
        BotRiskOverlay(bot_max_open_risk_fraction=invalid)  # type: ignore[arg-type]


class ResolverFixture:
    """Bộ khung dựng dữ liệu để test bot_config_resolver."""

    def __init__(
        self,
        *,
        workspace_cap: str = "1.0",
        account_cap: str = "0.80",
    ) -> None:
        self.workspace_id = uuid4()
        self.account_id = uuid4()
        self.bot_id = uuid4()
        self.profile_id = uuid4()

        payload = valid_workspace_payload()
        payload["strategy"] = {
            "default_release": "h01-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        }
        payload["risk"] = {
            "profile_version_id": str(self.profile_id),
            "workspace_max_open_risk_fraction": workspace_cap,
            "max_equity_age_seconds": 300,
            "max_conversion_age_seconds": 300,
            "max_mark_age_seconds": 60,
        }
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
                max_open_risk_fraction=Decimal(account_cap),
                settings={"schema_version": 1, "hard_caps": dict(HARD_CAPS)},
            )
        )
        self.account = SimpleNamespace(
            id=self.account_id,
            workspace_id=self.workspace_id,
            mode="PAPER",
            venue="binance_usdm",
            status="ENABLED",
            max_notional=Decimal("10000"),
        )
        self.releases = [
            SimpleNamespace(
                id=uuid4(),
                workspace_id=self.workspace_id,
                name="h01-v1",
                status="PUBLISHED",
                content_hash=StrategyRegistry().get_release("h01-v1").content_hash,
            )
        ]

    def version(self, overlay: dict[str, Any]) -> SimpleNamespace:
        parsed = BotConfigOverlay.model_validate(overlay)
        payload = canonical_overlay(parsed)
        return SimpleNamespace(
            id=uuid4(),
            workspace_id=self.workspace_id,
            bot_id=self.bot_id,
            account_id=self.account_id,
            status="PUBLISHED",
            schema_version=1,
            overlay_json=payload,
            overlay_hash=settings_hash(payload),
            effective_at=NOW - timedelta(hours=1),
        )

    def resolve(self, bot_version: SimpleNamespace) -> EffectiveBotConfig:
        return resolve_effective_bot_config(
            self.root,
            bot_version,  # type: ignore[arg-type]
            self.account,  # type: ignore[arg-type]
            NOW,
            root_risk_profile=self.risk_profile,
            strategy_releases=self.releases,  # type: ignore[arg-type]
        )


def test_resolver_hierarchy_valid_tightening() -> None:
    """Thứ bậc 0 < bot (0.5) <= account (0.8) <= workspace (1.0) hợp lệ."""
    fix = ResolverFixture(workspace_cap="1.0", account_cap="0.80")
    version = fix.version(
        {
            "schema_version": 1,
            "risk": {"bot_max_open_risk_fraction": "0.50"},
        }
    )
    effective = fix.resolve(version)

    assert effective.risk_limits.account_max_open_risk_fraction == Decimal("0.80")
    assert getattr(effective.risk_limits, "bot_max_open_risk_fraction", None) == Decimal("0.50")


def test_resolver_hierarchy_boundary_equality() -> None:
    """Biên bằng nhau 0 < bot (0.8) == account (0.8) == workspace (0.8) hợp lệ."""
    fix = ResolverFixture(workspace_cap="0.80", account_cap="0.80")
    version = fix.version(
        {
            "schema_version": 1,
            "risk": {"bot_max_open_risk_fraction": "0.80"},
        }
    )
    effective = fix.resolve(version)
    assert getattr(effective.risk_limits, "bot_max_open_risk_fraction", None) == Decimal("0.80")


def _validate(fix: ResolverFixture, overlay: dict[str, Any]) -> list[str]:
    issues = validate_bot_overlay(
        BotConfigOverlay.model_validate(overlay),
        workspace_settings=fix.root,
        account=fix.account,  # type: ignore[arg-type]
        root_risk_profile=fix.risk_profile,
        strategy_releases=fix.releases,  # type: ignore[arg-type]
    )
    return [issue.field for issue in issues]


def test_overlay_publish_rejects_bot_greater_than_account_but_runtime_clamps() -> None:
    """Bot (0.85) > account (0.80): publish/validate từ chối (không nới, không bỏ qua âm thầm).

    Overlay đã publish hợp lệ rồi profile siết sau đó: resolve runtime kẹp cap bot về account
    thay vì làm bot không resolve được (review I4).
    """
    fix = ResolverFixture(workspace_cap="1.0", account_cap="0.80")
    overlay = {"schema_version": 1, "risk": {"bot_max_open_risk_fraction": "0.85"}}

    assert "risk.bot_max_open_risk_fraction" in _validate(fix, overlay)
    effective = fix.resolve(fix.version(overlay))
    assert effective.risk_limits.bot_max_open_risk_fraction == Decimal("0.80")


def test_overlay_publish_rejects_bot_greater_than_workspace_but_runtime_clamps() -> None:
    """Bot (0.80) <= account (0.90) nhưng > workspace (0.70): validate từ chối, resolve kẹp."""
    fix = ResolverFixture(workspace_cap="0.70", account_cap="0.90")
    overlay = {"schema_version": 1, "risk": {"bot_max_open_risk_fraction": "0.80"}}

    assert "risk.bot_max_open_risk_fraction" in _validate(fix, overlay)
    effective = fix.resolve(fix.version(overlay))
    assert effective.risk_limits.bot_max_open_risk_fraction == Decimal("0.70")


def test_root_tightening_below_profile_account_cap_keeps_bots_resolvable() -> None:
    """Review I4: workspace (0.70) < account profile (0.80) không làm overlay mất resolve.

    Cap account áp trên vốn account, cap workspace trên vốn workspace (khác mẫu số); siết root
    được ghi breach khi publish và chặn ở reserve, không biến mọi bot có overlay thành lỗi.
    """
    fix = ResolverFixture(workspace_cap="0.70", account_cap="0.80")

    plain = fix.resolve(fix.version({"schema_version": 1}))
    tightened = fix.resolve(
        fix.version({"schema_version": 1, "risk": {"bot_max_open_risk_fraction": "0.50"}})
    )

    assert plain.risk_limits.account_max_open_risk_fraction == Decimal("0.80")
    assert plain.risk_limits.bot_max_open_risk_fraction is None
    assert tightened.risk_limits.bot_max_open_risk_fraction == Decimal("0.50")
    assert _validate(fix, {"schema_version": 1}) == []


# ==============================================================================
# 4. Advisory Lock khi Publish Root Settings
# ==============================================================================


@pytest.mark.asyncio
async def test_repository_lock_workspace_risk() -> None:
    """WorkspaceSettingsRepository có lock_workspace_risk thực thi advisory lock."""
    session = AsyncMock()
    repo = WorkspaceSettingsRepository(session)
    ws_id = uuid4()

    await repo.lock_workspace_risk(ws_id)

    session.execute.assert_awaited_once()
    call_args = session.execute.await_args
    sql_text = str(call_args[0][0])
    params = call_args[0][1]

    assert "pg_advisory_xact_lock" in sql_text
    assert "workspace-risk:" in params["risk_lock"]
    assert str(ws_id) in params["risk_lock"]
