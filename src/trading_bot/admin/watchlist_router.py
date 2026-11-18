"""HTTP API quản lý phiên bản watchlist theo workspace (US-08 / WP-04A)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent, WatchlistItem, WatchlistVersion
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.events import EventEnvelope

router = APIRouter(prefix="/api/v1/watchlists", tags=["quản lý watchlist"])


class WatchlistItemInput(BaseModel):
    """Một cặp giao dịch trong phiên bản watchlist."""

    symbol: str = Field(min_length=3, max_length=32, description="Mã hợp đồng (ví dụ BTCUSDT).")
    enabled: bool = Field(default=True, description="Có bật giao dịch cho symbol này hay không.")


class CreateWatchlistVersionRequest(BaseModel):
    """Yêu cầu phát hành phiên bản watchlist mới (bất biến)."""

    items: list[WatchlistItemInput] = Field(
        min_length=1, max_length=200, description="Danh sách symbol trong watchlist."
    )
    reason: str = Field(min_length=3, max_length=500, description="Lý do cập nhật watchlist.")


def _get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = cast(
        async_sessionmaker[AsyncSession] | None,
        getattr(request.app.state, "session_factory", None),
    )
    if sessions is None:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu watchlist chưa sẵn sàng")
    return sessions


def _require_permission(identity: AuthIdentity, permission: Permission) -> None:
    if not authorize(identity.principal, permission):
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác watchlist")


def _request_id(request: Request) -> UUID:
    header = request.headers.get("X-Request-ID")
    if header:
        try:
            return UUID(header)
        except ValueError:
            pass
    return uuid4()


@router.get(
    "",
    summary="Danh sách phiên bản watchlist",
    description="Liệt kê các phiên bản watchlist bất biến và danh sách symbol của từng phiên bản.",
)
async def list_watchlists(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> dict[str, object]:
    _require_permission(identity, Permission.WORKSPACE_READ)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        versions = (
            await unit.session.scalars(
                select(WatchlistVersion)
                .where(WatchlistVersion.workspace_id == identity.principal.workspace_id)
                .order_by(WatchlistVersion.version.desc())
            )
        ).all()
        items = (
            await unit.session.scalars(
                select(WatchlistItem).where(
                    WatchlistItem.workspace_id == identity.principal.workspace_id
                )
            )
        ).all()
        items_by_version: dict[UUID, list[dict[str, Any]]] = {}
        for item in items:
            items_by_version.setdefault(item.watchlist_version_id, []).append(
                {"symbol": item.symbol, "enabled": item.enabled}
            )
        payload = [
            {
                "id": str(version.id),
                "version": version.version,
                "created_at": version.created_at.isoformat(),
                "items": sorted(items_by_version.get(version.id, []), key=lambda x: x["symbol"]),
            }
            for version in versions
        ]
    return {"watchlists": payload}


@router.post(
    "",
    status_code=201,
    summary="Tạo phiên bản watchlist mới",
    description="Phát hành phiên bản watchlist bất biến mới kèm audit và outbox.",
)
async def create_watchlist_version(
    body: CreateWatchlistVersionRequest,
    identity: Annotated[AuthIdentity, Depends(require_csrf)],
    request: Request,
    response: Response,
) -> dict[str, object]:
    _require_permission(identity, Permission.WATCHLIST_WRITE)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    req_id = _request_id(request)
    normalized: dict[str, bool] = {}
    for entry in body.items:
        symbol = entry.symbol.strip().upper()
        if not symbol.isalnum():
            raise HTTPException(status_code=422, detail=f"Symbol không hợp lệ: {entry.symbol}")
        normalized[symbol] = entry.enabled

    now = datetime.now(UTC)
    digest = hashlib.sha256(json.dumps(sorted(normalized.items())).encode("utf-8")).hexdigest()
    async with UnitOfWork(
        sessions, workspace_id=identity.principal.workspace_id, request_id=req_id
    ) as unit:
        latest_version = await unit.session.scalar(
            select(func.coalesce(func.max(WatchlistVersion.version), 0)).where(
                WatchlistVersion.workspace_id == identity.principal.workspace_id
            )
        )
        next_version = int(latest_version or 0) + 1
        version = WatchlistVersion(
            workspace_id=identity.principal.workspace_id,
            version=next_version,
            created_by=identity.principal.membership_id,
        )
        unit.session.add(version)
        await unit.session.flush()
        for symbol, enabled in sorted(normalized.items()):
            unit.session.add(
                WatchlistItem(
                    workspace_id=identity.principal.workspace_id,
                    watchlist_version_id=version.id,
                    symbol=symbol,
                    enabled=enabled,
                )
            )
        unit.session.add(
            AuditEvent(
                workspace_id=identity.principal.workspace_id,
                actor_membership_id=identity.principal.membership_id,
                action="watchlist.version_created",
                target_type="watchlist_version",
                target_id=version.id,
                reason=body.reason.strip(),
                after_hash=digest,
                request_id=req_id,
                occurred_at=now,
            )
        )
        unit.enqueue(
            EventEnvelope(
                event_type="watchlist.version_created",
                aggregate_type="watchlist_version",
                aggregate_id=version.id,
                workspace_id=identity.principal.workspace_id,
                occurred_at=now,
                correlation_id=req_id,
                idempotency_key=f"watchlist.version_created:{version.id}",
                schema_version=1,
                payload={"version": next_version, "symbols": sorted(normalized)},
            )
        )
    return {
        "watchlist": {
            "id": str(version.id),
            "version": version.version,
            "items": [{"symbol": k, "enabled": v} for k, v in sorted(normalized.items())],
        }
    }
