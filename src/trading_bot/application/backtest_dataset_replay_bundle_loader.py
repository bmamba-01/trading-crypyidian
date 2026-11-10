"""Nạp bundle đã lưu sau khi kiểm lại từng manifest, quyền hiện hành và bars."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol
from uuid import UUID

from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayLoader
from trading_bot.application.backtest_dataset_replay_bundle import (
    BacktestDatasetBundleMismatch,
    BacktestDatasetReplayBundle,
    BacktestDatasetReplayBundleReference,
    build_backtest_dataset_replay_bundle,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BacktestDatasetReplayBundleLookup(Protocol):
    """Tra tham chiếu bundle theo workspace và hash canonical."""

    async def get_by_hash(
        self, *, workspace_id: UUID, bundle_hash: str
    ) -> BacktestDatasetReplayBundleReference | None: ...


class BacktestDatasetReplayBundleLoader:
    """Dùng repository và child loader trong Unit of Work do caller quản lý."""

    def __init__(
        self,
        *,
        repository: BacktestDatasetReplayBundleLookup,
        replay_loader: BacktestDatasetReplayLoader,
    ) -> None:
        self._repository = repository
        self._replay_loader = replay_loader

    async def load_for_replay(
        self, *, workspace_id: UUID, bundle_hash: str, now: datetime
    ) -> BacktestDatasetReplayBundle:
        """Chỉ trả bundle hoàn chỉnh khi mọi component hiện hành còn khớp tham chiếu."""
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if not isinstance(bundle_hash, str) or _SHA256.fullmatch(bundle_hash) is None:
            raise ValueError("bundle_hash phải là SHA-256 hex.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        reference = await self._repository.get_by_hash(
            workspace_id=workspace_id, bundle_hash=bundle_hash
        )
        if reference is None:
            raise LookupError("Không tìm thấy bundle replay trong workspace.")
        if type(reference) is not BacktestDatasetReplayBundleReference:
            raise ValueError("Repository trả tham chiếu bundle không hợp lệ.")
        if reference.workspace_id != workspace_id:
            raise PermissionError("Tham chiếu bundle không khớp workspace.")
        if reference.bundle_hash != bundle_hash:
            raise BacktestDatasetBundleMismatch("Hash tham chiếu bundle không khớp request.")
        inputs = []
        for component in reference.components:
            inputs.append(
                await self._replay_loader.load_for_replay(
                    workspace_id=workspace_id, dataset_hash=component.dataset_hash, now=now
                )
            )
        bundle = build_backtest_dataset_replay_bundle(inputs)
        rebuilt = BacktestDatasetReplayBundleReference.from_bundle(bundle)
        if rebuilt != reference or bundle.dataset_hash != bundle_hash:
            raise BacktestDatasetBundleMismatch(
                "Bundle dựng lại không khớp hash, scope hoặc component đã lưu."
            )
        return bundle

    async def revalidate_current_rights(
        self, bundle: BacktestDatasetReplayBundle, *, now: datetime
    ) -> BacktestDatasetReplayBundle:
        """Cấp evidence mới trước persist mà vẫn dùng đúng snapshot bars đã replay."""
        if type(bundle) is not BacktestDatasetReplayBundle:
            raise ValueError("bundle phải là BacktestDatasetReplayBundle đã nạp.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ.")
        refreshed = []
        for component in bundle.components:
            refreshed.append(
                await self._replay_loader.revalidate_current_rights(
                    component,
                    workspace_id=bundle.workspace_id,
                    now=now,
                )
            )
        current = build_backtest_dataset_replay_bundle(refreshed)
        if (
            current.dataset_hash != bundle.dataset_hash
            or BacktestDatasetReplayBundleReference.from_bundle(current)
            != BacktestDatasetReplayBundleReference.from_bundle(bundle)
        ):
            raise BacktestDatasetBundleMismatch(
                "Revalidate quyền làm đổi scope hoặc hash bundle replay."
            )
        return current
