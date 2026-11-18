"""Màn hình theo dõi bot trực tiếp (LIVE-MON, CRY-429): trang, API đọc và luồng SSE.

Chỉ đọc. Mọi thao tác điều khiển (tạm dừng, kill switch, đóng toàn bộ vị thế) vẫn đi qua các
form hiện có có CSRF + MFA; trang này chỉ dẫn link tới đó.

Giao thức đẩy: Server-Sent Events gốc của FastAPI (``fastapi.sse``, có từ 0.135; repo ghim
0.156.4) — một chiều server→trình duyệt, tự kết nối lại kèm ``Last-Event-ID``, đi qua cookie
phiên/CSP ``default-src 'self'`` như request thường. WebSocket không cần vì client không gửi gì.
Mỗi luồng tự đóng sau ``STREAM_MAX_SECONDS`` để trình duyệt kết nối lại (xác thực lại phiên)
và số luồng đồng thời mỗi membership bị giới hạn để chịu được nhiều tab/bot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy.exc import SQLAlchemyError

from trading_bot.admin.web_router import (
    AdminHTMLRoute,
    _auth_service,
    _operations,
    _runtime_dict,
    _templates,
)
from trading_bot.application.bot_runtime_control import BotRuntimeControlError
from trading_bot.application.live_monitor import MAX_LIVE_BOTS, LiveMonitorService
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.route_policy import bot_scoped_route
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity
from trading_bot.domain.monitoring.live_monitor import LiveCursor, dedupe_events

router = APIRouter(prefix="/admin", tags=["admin web"], route_class=AdminHTMLRoute)

STREAM_TICK_SECONDS = 1.5
STREAM_STATE_EVERY_TICKS = 2
STREAM_AUTH_RECHECK_SECONDS = 30.0
STREAM_MAX_SECONDS = 300.0
STREAM_RETRY_MS = 3000
STREAM_MAX_ERRORS = 3
MAX_STREAMS_PER_MEMBERSHIP = 4
POLL_INTERVAL_MS = 4000


def _service(request: Request) -> LiveMonitorService:
    service = cast(
        LiveMonitorService | None, getattr(request.app.state, "live_monitor_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ theo dõi trực tiếp chưa sẵn sàng")
    return service


async def _runtime(request: Request, identity: AuthIdentity) -> dict[str, dict[str, Any]]:
    """Trạng thái điều khiển các bot principal được xem (đã lọc ``bot:read``)."""
    try:
        operations = _operations(request, identity)
        rows = [_runtime_dict(item) for item in await operations.list_bots()]
    except BotRuntimeControlError as error:
        raise HTTPException(status_code=503, detail="Không thể tải trạng thái bot.") from error
    for row in rows:
        row["entry_allowed"] = bool(row.get("entry_allowed"))
    return {str(row["id"]): row for row in rows}


def _selected(runtime: dict[str, dict[str, Any]], requested: list[UUID]) -> list[UUID]:
    """Bot được chọn; mặc định mọi bot đang bật (nếu không có thì toàn bộ), tối đa 50."""
    if requested:
        chosen = [bot_id for bot_id in dict.fromkeys(requested) if str(bot_id) in runtime]
    else:
        enabled = [key for key, row in runtime.items() if row.get("status") == "ENABLED"]
        chosen = [UUID(key) for key in (enabled or list(runtime))]
    return chosen[:MAX_LIVE_BOTS]


BotQuery = Annotated[list[UUID], Query(alias="bot", max_length=MAX_LIVE_BOTS)]


@router.get(
    "/live",
    dependencies=[bot_scoped_route(BotPermission.READ, param_name=None, param_location="service")],
)
async def live_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_ids: BotQuery = [],  # noqa: B006 - FastAPI đọc mặc định này như danh sách rỗng bất biến
) -> Response:
    """Trang Live toàn màn hình; dữ liệu được nạp bằng SSE (fallback poll) sau khi tải trang."""
    runtime = await _runtime(request, identity)
    selected = _selected(runtime, bot_ids)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/live.html",
        context={
            "identity": identity,
            "bots": sorted(runtime.values(), key=lambda row: str(row.get("name") or "")),
            "selected": {str(bot_id) for bot_id in selected},
            "selected_ids": [str(bot_id) for bot_id in selected],
            "max_bots": MAX_LIVE_BOTS,
            "poll_interval_ms": POLL_INTERVAL_MS,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/api/live/snapshot",
    dependencies=[
        bot_scoped_route(BotPermission.REPORT_READ, param_name=None, param_location="service")
    ],
)
async def live_snapshot(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_ids: BotQuery = [],  # noqa: B006
    since: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    """Đường poll dự phòng khi trình duyệt/proxy không giữ được SSE: sự kiện mới + trạng thái."""
    response.headers["Cache-Control"] = "no-store"
    runtime = await _runtime(request, identity)
    try:
        return await _service(request).read(
            identity.principal,
            bot_ids=_selected(runtime, bot_ids),
            since=LiveCursor.parse(since),
            include_state=True,
            runtime=runtime,
        )
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="Không thể đọc ledger PAPER.") from error


def _stream_slots(request: Request) -> dict[str, dict[str, float]]:
    """Bảng slot luồng theo membership, lưu trên ``app.state`` (một tiến trình admin-web)."""
    slots = getattr(request.app.state, "live_stream_slots", None)
    if not isinstance(slots, dict):
        slots = {}
        request.app.state.live_stream_slots = slots
    return slots


def _acquire_stream_slot(request: Request, identity: AuthIdentity) -> str:
    """Giới hạn số luồng SSE đồng thời của một membership; slot cũ quá hạn tự được dọn."""
    slots = _stream_slots(request)
    key = str(identity.principal.membership_id)
    now = time.monotonic()
    active = {
        token: started
        for token, started in slots.get(key, {}).items()
        if now - started < STREAM_MAX_SECONDS + 30
    }
    if len(active) >= MAX_STREAMS_PER_MEMBERSHIP:
        raise HTTPException(
            status_code=429,
            detail="Quá nhiều màn hình Live đang mở.",
            headers={"Retry-After": "30"},
        )
    token = uuid4().hex
    active[token] = now
    slots[key] = active
    return token


def _release_stream_slot(request: Request, identity: AuthIdentity, token: str) -> None:
    slots = _stream_slots(request)
    slots.get(str(identity.principal.membership_id), {}).pop(token, None)


class _StreamContext:
    """Kiểm tra trước khi mở luồng (chạy như dependency để lỗi trả đúng mã HTTP)."""

    def __init__(
        self,
        service: LiveMonitorService,
        token: str,
        runtime: dict[str, dict[str, Any]],
        selected: list[UUID],
    ) -> None:
        self.service = service
        self.token = token
        self.runtime = runtime
        self.selected = selected


async def _stream_context(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    bot_ids: BotQuery = [],  # noqa: B006
) -> _StreamContext:
    service = _service(request)
    runtime = await _runtime(request, identity)
    token = _acquire_stream_slot(request, identity)
    return _StreamContext(service, token, runtime, _selected(runtime, bot_ids))


@router.get(
    "/api/live/stream",
    response_class=EventSourceResponse,
    dependencies=[
        bot_scoped_route(BotPermission.REPORT_READ, param_name=None, param_location="service")
    ],
)
async def live_stream(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    context: Annotated[_StreamContext, Depends(_stream_context)],
    since: Annotated[str | None, Query(max_length=64)] = None,
    last_event_id: Annotated[str | None, Header(max_length=64)] = None,
) -> AsyncIterator[ServerSentEvent]:
    """Luồng SSE: ``events`` (sự kiện ledger mới, ``id`` = con trỏ) và ``state`` (vị thế, vốn,
    sức khỏe). Kết nối lại dùng ``Last-Event-ID`` để không mất/không lặp sự kiện."""
    service = context.service
    runtime = context.runtime
    selected = context.selected
    cursor = LiveCursor.parse(last_event_id) or LiveCursor.parse(since)
    seen: dict[str, None] = {}
    started = time.monotonic()
    last_auth = started
    errors = 0
    tick = 0
    try:
        yield ServerSentEvent(comment="crypidian live", retry=STREAM_RETRY_MS)
        while True:
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - started >= STREAM_MAX_SECONDS:
                # Đóng chủ động; EventSource tự nối lại sau `retry` kèm Last-Event-ID.
                yield ServerSentEvent(event="rotate", data={"reason": "max_age"})
                return
            include_state = tick % STREAM_STATE_EVERY_TICKS == 0
            if now - last_auth >= STREAM_AUTH_RECHECK_SECONDS:
                try:
                    await _auth_service(request).authenticate(read_session_cookie(request))
                except AuthenticationFailure:
                    yield ServerSentEvent(event="auth", data={"status": 401})
                    return
                last_auth = now
                try:
                    runtime = await _runtime(request, identity)
                except HTTPException:
                    pass
            try:
                payload = await service.read(
                    identity.principal,
                    bot_ids=selected,
                    since=cursor,
                    include_state=include_state,
                    runtime=runtime,
                )
            except SQLAlchemyError:
                errors += 1
                yield ServerSentEvent(
                    event="degraded", data={"message": "Không đọc được ledger PAPER; thử lại."}
                )
                if errors >= STREAM_MAX_ERRORS:
                    return
                await asyncio.sleep(STREAM_TICK_SECONDS * errors)
                continue
            errors = 0
            fresh = dedupe_events(payload["events"], seen)
            cursor = LiveCursor.parse(payload["cursor"]) or cursor
            if fresh or tick == 0:
                yield ServerSentEvent(
                    event="events",
                    id=payload["cursor"],
                    data={"events": fresh, "cursor": payload["cursor"], "initial": tick == 0},
                )
            if payload["state"] is not None:
                yield ServerSentEvent(event="state", data=payload["state"])
            tick += 1
            await asyncio.sleep(STREAM_TICK_SECONDS)
    finally:
        _release_stream_slot(request, identity, context.token)
