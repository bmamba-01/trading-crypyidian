"""Lớp cấu hình gốc: risk profile, `WorkspaceSettings` v1 đã publish và seed vốn PAPER.

Payload settings và hard caps phản chiếu seed của E2E H03 hiện có để runtime/profile
resolver hiện hành đọc được. Hash tính bằng đúng hàm của repository settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from tests.fixtures.qa_dataset.access import QaTradingAccount
from tests.fixtures.qa_dataset.identity import (
    QA_OTHER_WORKSPACE_KEY,
    QA_WORKSPACE_KEY,
    SEEDED_AT,
    qa_id,
)

from trading_bot.adapters.database.repositories.workspace_settings import (
    canonical_settings,
    settings_hash,
)
from trading_bot.config.workspace_settings import WorkspaceSettings


@dataclass(frozen=True, slots=True)
class QaRiskProfile:
    """Risk profile mà `WorkspaceSettings.risk.profile_version_id` tham chiếu."""

    id: UUID
    workspace_key: str
    name: str
    version: int
    max_risk_fraction: Decimal

    settings: dict[str, Any]
    is_active: bool
    max_open_risk_fraction: Decimal = Decimal("1.0")


@dataclass(frozen=True, slots=True)
class QaSettingsVersion:
    """Phiên bản settings gốc đã PUBLISHED, bất biến sau khi ghi."""

    id: UUID
    workspace_key: str
    version: int
    settings: WorkspaceSettings
    settings_json: dict[str, Any]
    settings_hash: str
    created_by_member_key: str
    approved_by_member_key: str
    published_at: datetime
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class QaPaperSeed:
    """Vốn khởi tạo bất biến của một PAPER account, ghim settings nguồn."""

    id: UUID
    workspace_key: str
    account_key: str
    quote_asset: str
    initial_equity: Decimal
    settings_version_id: UUID
    settings_hash: str
    idempotency_key: str


def _hard_caps() -> dict[str, Any]:
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


def build_risk_profiles() -> tuple[QaRiskProfile, ...]:
    return tuple(
        QaRiskProfile(
            id=qa_id("risk_profile_version", f"{workspace_key}:qa-conservative:1"),
            workspace_key=workspace_key,
            name="QA conservative",
            version=1,
            max_risk_fraction=Decimal("0.01"),
            settings=_hard_caps(),
            is_active=True,
        )
        for workspace_key in (QA_WORKSPACE_KEY, QA_OTHER_WORKSPACE_KEY)
    )


_WORKSPACE_MARKETS = {
    QA_WORKSPACE_KEY: (("BTCUSDT", "ETHUSDT"), "10000"),
    QA_OTHER_WORKSPACE_KEY: (("BTCUSDT",), "5000"),
}


def _settings_payload(profile_id: UUID, symbols: tuple[str, ...], equity: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": list(symbols),
            "timeframes": ["1h", "4h"],
            "max_age_seconds": 30,
        },
        "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h03-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        },
        "risk": {"profile_version_id": str(profile_id)},
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": equity,
            "fee_schedule_version": "paper-fees-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }


def build_settings_versions(
    risk_profiles: tuple[QaRiskProfile, ...],
) -> tuple[QaSettingsVersion, ...]:
    versions = []
    for profile in risk_profiles:
        symbols, equity = _WORKSPACE_MARKETS[profile.workspace_key]
        settings = WorkspaceSettings.model_validate(_settings_payload(profile.id, symbols, equity))
        payload = canonical_settings(settings)
        owner_key = f"{profile.workspace_key}.owner"
        versions.append(
            QaSettingsVersion(
                id=qa_id("workspace_settings_version", f"{profile.workspace_key}:1"),
                workspace_key=profile.workspace_key,
                version=1,
                settings=settings,
                settings_json=payload,
                settings_hash=settings_hash(payload),
                created_by_member_key=owner_key,
                approved_by_member_key=owner_key,
                published_at=SEEDED_AT,
                effective_at=SEEDED_AT,
            )
        )
    return tuple(versions)


def build_paper_seeds(
    accounts: tuple[QaTradingAccount, ...],
    settings_versions: tuple[QaSettingsVersion, ...],
) -> tuple[QaPaperSeed, ...]:
    """Seed vốn theo đúng quy ước `PaperAccountSeedRepository` (khóa `paper-seed:<id>`)."""
    by_workspace = {version.workspace_key: version for version in settings_versions}
    seeds = []
    for account in accounts:
        version = by_workspace[account.workspace_key]
        paper = version.settings.paper_execution
        seeds.append(
            QaPaperSeed(
                id=qa_id("paper_account_seed", account.key),
                workspace_key=account.workspace_key,
                account_key=account.key,
                quote_asset=paper.quote_asset,
                initial_equity=paper.starting_equity,
                settings_version_id=version.id,
                settings_hash=version.settings_hash,
                idempotency_key=f"paper-seed:{account.id}",
            )
        )
    return tuple(seeds)
