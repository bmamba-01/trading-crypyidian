"""Bộ dữ liệu QA tất định cho dự án Crypidian multi-bot PAPER.

Package chỉ chứa builder thuần (không I/O, không đọc đồng hồ, không sinh ngẫu nhiên):
cùng một phiên bản code luôn tạo cùng `QaDataset`. `scripts/seed_qa_data.py` ghi dataset
vào PostgreSQL theo từng lớp; tài liệu `docs/testing/qa-test-data.md` mô tả cách mở rộng.

Các lớp hiện có (thứ tự seed): `access` (workspace/thành viên/account/grant),
`root_settings` (risk profile, settings v1, seed vốn PAPER), `market` (instrument, nến).
WP sau thêm lớp mới thành module riêng + trường mới trên `QaDataset`, không sửa lớp cũ.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from tests.fixtures.qa_dataset.access import (
    QaBotGrant,
    QaMember,
    QaTradingAccount,
    QaWorkspace,
    build_accounts,
    build_grants,
    build_members,
    build_workspaces,
)
from tests.fixtures.qa_dataset.identity import (
    DATASET_VERSION,
    MARKET_END,
    QA_NAMESPACE,
    QA_OTHER_WORKSPACE_KEY,
    QA_WORKSPACE_KEY,
    SEEDED_AT,
    qa_id,
)
from tests.fixtures.qa_dataset.market import (
    FIXTURE_SOURCE,
    INSTRUMENT_SOURCE,
    QaBar,
    QaInstrument,
    build_bars,
    build_instruments,
)
from tests.fixtures.qa_dataset.root_settings import (
    QaPaperSeed,
    QaRiskProfile,
    QaSettingsVersion,
    build_paper_seeds,
    build_risk_profiles,
    build_settings_versions,
)

from trading_bot.domain.market.models import OHLCVBar


@dataclass(frozen=True, slots=True)
class QaDataset:
    """Toàn bộ dataset QA; mỗi nhóm trường tương ứng một lớp seed."""

    version: str
    workspaces: tuple[QaWorkspace, ...]
    members: tuple[QaMember, ...]
    accounts: tuple[QaTradingAccount, ...]
    grants: tuple[QaBotGrant, ...]
    risk_profiles: tuple[QaRiskProfile, ...]
    settings_versions: tuple[QaSettingsVersion, ...]
    paper_seeds: tuple[QaPaperSeed, ...]
    instruments: tuple[QaInstrument, ...]
    bars: tuple[QaBar, ...]

    def workspace(self, key: str) -> QaWorkspace:
        return _single(item for item in self.workspaces if item.key == key)

    def member(self, key: str) -> QaMember:
        return _single(item for item in self.members if item.key == key)

    def account(self, key: str) -> QaTradingAccount:
        return _single(item for item in self.accounts if item.key == key)

    def members_of(self, workspace_key: str) -> tuple[QaMember, ...]:
        return tuple(item for item in self.members if item.workspace_key == workspace_key)

    def accounts_of(self, workspace_key: str) -> tuple[QaTradingAccount, ...]:
        return tuple(item for item in self.accounts if item.workspace_key == workspace_key)

    def grants_of(self, member_key: str) -> tuple[QaBotGrant, ...]:
        return tuple(item for item in self.grants if item.member_key == member_key)

    def settings_for(self, workspace_key: str) -> QaSettingsVersion:
        return _single(
            item for item in self.settings_versions if item.workspace_key == workspace_key
        )

    def bars_for(self, symbol: str, timeframe: str) -> tuple[OHLCVBar, ...]:
        return tuple(
            item.bar
            for item in self.bars
            if item.bar.symbol == symbol and item.bar.timeframe == timeframe
        )


def _single[T](items: Iterable[T]) -> T:
    matches = list(items)
    if len(matches) != 1:
        raise KeyError(f"Dataset QA cần đúng một phần tử, nhận {len(matches)}.")
    return matches[0]


def build_qa_dataset() -> QaDataset:
    """Dựng dataset QA đầy đủ; gọi lại luôn trả kết quả bằng nhau."""
    accounts = build_accounts()
    risk_profiles = build_risk_profiles()
    settings_versions = build_settings_versions(risk_profiles)
    return QaDataset(
        version=DATASET_VERSION,
        workspaces=build_workspaces(),
        members=build_members(),
        accounts=accounts,
        grants=build_grants(),
        risk_profiles=risk_profiles,
        settings_versions=settings_versions,
        paper_seeds=build_paper_seeds(accounts, settings_versions),
        instruments=build_instruments(),
        bars=build_bars(),
    )


__all__ = [
    "DATASET_VERSION",
    "FIXTURE_SOURCE",
    "INSTRUMENT_SOURCE",
    "MARKET_END",
    "QA_NAMESPACE",
    "QA_OTHER_WORKSPACE_KEY",
    "QA_WORKSPACE_KEY",
    "SEEDED_AT",
    "QaBar",
    "QaBotGrant",
    "QaDataset",
    "QaInstrument",
    "QaMember",
    "QaPaperSeed",
    "QaRiskProfile",
    "QaSettingsVersion",
    "QaTradingAccount",
    "QaWorkspace",
    "build_qa_dataset",
    "qa_id",
]
