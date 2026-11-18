"""Đồng bộ StrategyRelease trong DB với StrategyRegistry hiện hành.

Khi mã chiến lược hoặc pipeline L0–L6 nâng cấp làm đổi ``content_hash`` của một release
(như ``CRY-437`` đối với ``sc-binance-usdm-vol-breakout-001-v1.0.0``), các bản ghi
``strategy_release`` đã publish trước đó sẽ lệch hash so với registry và bị resolver chặn
fail-closed (``BOT_CONFIG_ROOT_MISMATCH``). Module này cung cấp kế hoạch và thao tác đồng bộ
an toàn: chuyển các bản ``PUBLISHED`` lệch hash sang ``SUPERSEDED`` (giữ nguyên UUID và
``content_hash`` cũ cho khóa ngoại lịch sử) và publish phiên bản mới khớp hash registry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.entities import StrategyRelease
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.domain.strategy.registry import StrategyRegistry, UnknownStrategyReleaseError

SyncAction = Literal[
    "unchanged",
    "published_new_version",
    "republished_existing",
    "unknown_release",
]


@dataclass(frozen=True, slots=True)
class ExistingReleaseRow:
    """Ảnh chụp tối thiểu một dòng ``strategy_release`` phục vụ lập kế hoạch thuần."""

    id: UUID
    name: str
    version: int
    status: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class ReleaseSyncPlan:
    """Kế hoạch đồng bộ cho một ``release_id`` trong một workspace."""

    release_id: str
    action: SyncAction
    supersede_ids: tuple[UUID, ...]
    republish_id: UUID | None
    new_version: int | None
    previous_hashes: tuple[str, ...]
    target_hash: str | None


@dataclass(frozen=True, slots=True)
class StrategyReleaseSyncReport:
    """Báo cáo kết quả đồng bộ cho một workspace."""

    workspace_id: UUID
    plans: tuple[ReleaseSyncPlan, ...]
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "dry_run": self.dry_run,
            "items": [
                {
                    "release_id": plan.release_id,
                    "action": plan.action,
                    "supersede_ids": [str(item) for item in plan.supersede_ids],
                    "republish_id": str(plan.republish_id) if plan.republish_id else None,
                    "new_version": plan.new_version,
                    "previous_hashes": list(plan.previous_hashes),
                    "target_hash": plan.target_hash,
                }
                for plan in self.plans
            ],
        }


def plan_strategy_release_sync(
    *,
    release_id: str,
    existing_rows: Sequence[ExistingReleaseRow],
    registry: StrategyRegistry,
) -> ReleaseSyncPlan:
    """Lập kế hoạch đồng bộ thuần túy cho một ``release_id`` từ các dòng hiện có."""
    rows = [row for row in existing_rows if row.name == release_id]
    published_rows = [row for row in rows if row.status == "PUBLISHED"]
    previous_hashes = tuple(dict.fromkeys(row.content_hash for row in published_rows))

    try:
        registered = registry.get_release(release_id)
    except UnknownStrategyReleaseError:
        return ReleaseSyncPlan(
            release_id=release_id,
            action="unknown_release",
            supersede_ids=(),
            republish_id=None,
            new_version=None,
            previous_hashes=previous_hashes,
            target_hash=None,
        )

    target_hash = registered.content_hash
    matching_published = [row for row in published_rows if row.content_hash == target_hash]
    stale_published = [row for row in published_rows if row.content_hash != target_hash]

    # Đã có đúng 1 bản PUBLISHED khớp hash và không có bản PUBLISHED lệch nào khác.
    if len(matching_published) == 1 and not stale_published:
        return ReleaseSyncPlan(
            release_id=release_id,
            action="unchanged",
            supersede_ids=(),
            republish_id=None,
            new_version=None,
            previous_hashes=previous_hashes,
            target_hash=target_hash,
        )

    supersede_ids: list[UUID] = [row.id for row in stale_published]

    # Nếu đã có sẵn bản mang đúng hash (ví dụ từng bị supersede hoặc đang DRAFT), tái publish
    # bản có version cao nhất và supersede các bản PUBLISHED dư thừa khác.
    matching_any = sorted(
        (row for row in rows if row.content_hash == target_hash),
        key=lambda row: row.version,
        reverse=True,
    )
    if matching_any:
        keeper = matching_any[0]
        for extra in matching_published:
            if extra.id != keeper.id:
                supersede_ids.append(extra.id)
        return ReleaseSyncPlan(
            release_id=release_id,
            action="republished_existing",
            supersede_ids=tuple(supersede_ids),
            republish_id=keeper.id if keeper.status != "PUBLISHED" else None,
            new_version=keeper.version,
            previous_hashes=previous_hashes,
            target_hash=target_hash,
        )

    max_version = max((row.version for row in rows), default=0)
    return ReleaseSyncPlan(
        release_id=release_id,
        action="published_new_version",
        supersede_ids=tuple(supersede_ids),
        republish_id=None,
        new_version=max_version + 1,
        previous_hashes=previous_hashes,
        target_hash=target_hash,
    )


async def sync_workspace_strategy_releases(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    release_names: Sequence[str] | None = None,
    registry: StrategyRegistry | None = None,
    dry_run: bool = False,
    set_rls: bool = True,
) -> StrategyReleaseSyncReport:
    """Đồng bộ ``strategy_release`` của một workspace theo ``StrategyRegistry`` hiện hành.

    Mặc định chỉ đồng bộ những ``name`` đang có ít nhất 1 dòng ``PUBLISHED`` trong workspace,
    không tự ý publish release mới mà workspace chưa từng kích hoạt.
    """
    if set_rls:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )

    active_registry = registry or strategy_registry()
    rows = list(
        (
            await session.scalars(
                select(StrategyRelease).where(StrategyRelease.workspace_id == workspace_id)
            )
        ).all()
    )
    snapshots = [
        ExistingReleaseRow(
            id=row.id,
            name=row.name,
            version=row.version,
            status=row.status,
            content_hash=row.content_hash,
        )
        for row in rows
    ]

    if release_names is not None:
        targets = tuple(dict.fromkeys(release_names))
    else:
        targets = tuple(sorted({row.name for row in rows if row.status == "PUBLISHED"}))

    by_id = {row.id: row for row in rows}
    plans: list[ReleaseSyncPlan] = []
    for release_id in targets:
        plan = plan_strategy_release_sync(
            release_id=release_id,
            existing_rows=snapshots,
            registry=active_registry,
        )
        plans.append(plan)
        if dry_run:
            continue

        for stale_id in plan.supersede_ids:
            by_id[stale_id].status = "SUPERSEDED"

        if plan.action == "republished_existing" and plan.republish_id is not None:
            by_id[plan.republish_id].status = "PUBLISHED"
        elif (
            plan.action == "published_new_version"
            and plan.new_version is not None
            and plan.target_hash is not None
        ):
            session.add(
                StrategyRelease(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    name=release_id,
                    version=plan.new_version,
                    status="PUBLISHED",
                    recipe={},
                    content_hash=plan.target_hash,
                )
            )

    if not dry_run:
        await session.flush()

    return StrategyReleaseSyncReport(
        workspace_id=workspace_id,
        plans=tuple(plans),
        dry_run=dry_run,
    )
