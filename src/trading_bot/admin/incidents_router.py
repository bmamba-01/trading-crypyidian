"""HTTP API tổng quát cho sự cố tài khoản và thí nghiệm backtest/huấn luyện (WP-04A)."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AccountActivityIncidentRecord, BacktestRunRecord
from trading_bot.application.operational_alert_outbox import list_operational_alerts
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.route_policy import permission_route
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/api/v1", tags=["sự cố và thí nghiệm"])


def _get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = cast(
        async_sessionmaker[AsyncSession] | None,
        getattr(request.app.state, "session_factory", None),
    )
    if sessions is None:
        raise HTTPException(status_code=503, detail="Cơ sở dữ liệu sự cố/thí nghiệm chưa sẵn sàng")
    return sessions


def _require_permission(identity: AuthIdentity, permission: Permission) -> None:
    if not authorize(identity.principal, permission):
        raise HTTPException(status_code=403, detail="Không đủ quyền truy cập dữ liệu này")


@router.get(
    "/incidents",
    summary="Danh sách sự cố hoạt động tài khoản",
    description="Liệt kê các sự cố hoạt động bất thường / đối soát trong workspace hiện tại.",
    dependencies=[permission_route(Permission.TRADE_READ)],
)
async def list_incidents(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    _require_permission(identity, Permission.TRADE_READ)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        records = (
            await unit.session.scalars(
                select(AccountActivityIncidentRecord)
                .where(
                    AccountActivityIncidentRecord.workspace_id == identity.principal.workspace_id
                )
                .order_by(AccountActivityIncidentRecord.created_at.desc())
                .limit(limit)
            )
        ).all()
        items: list[dict[str, Any]] = [
            {
                "id": str(item.id),
                "bot_id": str(item.bot_id),
                "account_id": str(item.account_id),
                "source": item.source,
                "status": item.status,
                "resolution_code": item.resolution_code,
                "resolution_reason": item.resolution_reason,
                "detected_at": item.detected_at.isoformat(),
                "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
            }
            for item in records
        ]
    return {"incidents": items}


@router.get(
    "/incidents/operational-alerts",
    summary="Danh sách cảnh báo vận hành từ outbox (kể cả dead-letter khi Telegram lỗi)",
    description=(
        "Hiển thị cảnh báo vận hành (pause/kill/flatten/sự cố/cầu dao) từ outbox để "
        "vận hành luôn thấy trên Admin Web kể cả khi Telegram gián đoạn."
    ),
    dependencies=[permission_route(Permission.TRADE_READ)],
)
async def list_operational_alert_items(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    include_delivered: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    _require_permission(identity, Permission.TRADE_READ)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        alerts = await list_operational_alerts(
            unit.session,
            workspace_id=identity.principal.workspace_id,
            include_delivered=include_delivered,
            limit=limit,
        )
    return {
        "alerts": [
            {
                "event_id": str(item.event_id),
                "event_type": item.event_type,
                "title_vi": item.title_vi,
                "status": item.status,
                "attempts": item.attempts,
                "last_error_code": item.last_error_code,
                "mode": item.mode,
                "reason": item.reason,
                "admin_link": item.admin_link,
                "created_at": item.created_at.isoformat(),
                "available_at": item.available_at.isoformat(),
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            }
            for item in alerts
        ]
    }


@router.get(
    "/experiments",
    summary="Danh sách thí nghiệm backtest và đánh giá",
    description="Liệt kê các lần chạy backtest / thí nghiệm trong workspace theo thứ tự mới nhất.",
    dependencies=[permission_route(Permission.WORKSPACE_READ)],
)
async def list_experiments(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    _require_permission(identity, Permission.WORKSPACE_READ)
    response.headers["Cache-Control"] = "no-store"
    sessions = _get_sessions(request)
    async with UnitOfWork(sessions, workspace_id=identity.principal.workspace_id) as unit:
        records = (
            await unit.session.scalars(
                select(BacktestRunRecord)
                .where(BacktestRunRecord.workspace_id == identity.principal.workspace_id)
                .order_by(BacktestRunRecord.created_at.desc())
                .limit(limit)
            )
        ).all()
        items: list[dict[str, Any]] = [
            {
                "id": str(item.id),
                "manifest_hash": item.manifest_hash,
                "strategy_name": item.manifest.get("strategy_name"),
                "strategy_version": item.manifest.get("strategy_version"),
                "symbol": item.manifest.get("symbol"),
                "timeframe": item.manifest.get("timeframe"),
                "created_at": item.created_at.isoformat(),
            }
            for item in records
        ]
    return {"experiments": items}
