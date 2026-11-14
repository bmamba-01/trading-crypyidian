"""Trang quản trị tạo và theo dõi các lượt kiểm thử dữ liệu đã lưu."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import RedirectResponse

from trading_bot.admin.web_router import (
    AdminHTMLRoute,
    _auth_service,
    _parse_form,
    _templates,
    _verify_form_csrf,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/admin/backtests", tags=["admin web"], route_class=AdminHTMLRoute)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FORM_BYTES = 16_384
_STATUS_LABELS = {
    "QUEUED": "Đang chờ",
    "RUNNING": "Đang chạy",
    "CANCEL_REQUESTED": "Đang yêu cầu hủy",
    "CANCELLED": "Đã hủy",
    "SUCCEEDED": "Hoàn tất",
    "FAILED": "Thất bại",
}
_STAGE_LABELS = {
    "queued": "Đang chờ tiến trình nền",
    "load": "Đang kiểm tra dữ liệu và quyền sử dụng",
    "replay": "Đang chạy mô phỏng PAPER",
    "persist": "Đang lưu kết quả",
    "complete": "Đã lưu kết quả",
    "failed": "Không thể hoàn tất",
    "cancelled": "Đã hủy",
}


@dataclass(frozen=True, slots=True)
class BacktestJobView:
    """Dữ liệu đã lọc để hiển thị, không đưa payload cơ sở dữ liệu vào template."""

    id: str
    status: str
    stage: str
    progress: int
    bundle_hash_short: str
    strategy_name: str
    execution_interval: str
    created_at: Any
    created_at_compact: str
    error_code: str | None
    run_ids: tuple[str, ...]
    can_cancel: bool


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "backtest_replay_job_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ kiểm thử chưa sẵn sàng.")
    return service


def _require_owner(identity: AuthIdentity) -> None:
    if Role.OWNER not in identity.principal.roles:
        raise HTTPException(status_code=403, detail="Chỉ owner được quản lý lượt kiểm thử.")


def _decimal(values: dict[str, str], key: str, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(values[key].strip())
    except (InvalidOperation, KeyError, AttributeError) as error:
        raise ValueError from error
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise ValueError
    return value


def _submission(values: dict[str, str]) -> BacktestReplayJobSubmission:
    bundle_hash = values.get("bundle_hash", "").strip()
    strategy_release_id = values.get("strategy_release_id", "").strip()
    execution_interval = values.get("execution_interval", "").strip()
    known_releases = {release.release_id for release in strategy_registry().list_releases()}
    try:
        seed = int(values.get("seed", ""))
    except ValueError as error:
        raise ValueError from error
    if (
        _SHA256.fullmatch(bundle_hash) is None
        or strategy_release_id not in known_releases
        or not execution_interval
        or len(execution_interval) > 12
        or not 0 <= seed <= 2**31 - 1
    ):
        raise ValueError
    risk_fraction = _decimal(values, "per_trade_risk_fraction", positive=True)
    if risk_fraction > Decimal("0.05"):
        raise ValueError
    return BacktestReplayJobSubmission(
        bundle_hash=bundle_hash,
        strategy_release_id=strategy_release_id,
        execution_interval=execution_interval,
        starting_equity=_decimal(values, "starting_equity", positive=True),
        per_trade_risk_fraction=risk_fraction,
        fee_rate=_decimal(values, "fee_rate"),
        slippage_rate=_decimal(values, "slippage_rate"),
        seed=seed,
    )


def _job_view(job: Any) -> BacktestJobView:
    status = str(getattr(job, "status", "UNKNOWN"))
    cancel_requested = bool(getattr(job, "cancel_requested", False))
    status_label = (
        "Đang yêu cầu hủy"
        if cancel_requested and status in {"QUEUED", "RUNNING"}
        else _STATUS_LABELS.get(status, "Không xác định")
    )
    stage_label = _STAGE_LABELS.get(str(getattr(job, "stage", "")), "Đang xử lý")
    if stage_label == status_label:
        stage_label = ""
    release_id = str(getattr(job, "strategy_release_id", ""))
    release = next(
        (item for item in strategy_registry().list_releases() if item.release_id == release_id),
        None,
    )
    raw_progress = getattr(job, "progress", 0)
    progress = max(0, min(100, int(raw_progress)))
    error_code = getattr(job, "error_code", None)
    if error_code is not None and re.fullmatch(r"[A-Z0-9_]{1,80}", str(error_code)) is None:
        error_code = "BACKTEST_FAILED"
    result_run_ids = tuple(str(value) for value in getattr(job, "result_run_ids", ()))
    created_at = getattr(job, "created_at", None)
    created_at_compact = (
        created_at.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%y %H:%M")
        if isinstance(created_at, datetime)
        and created_at.tzinfo is not None
        and created_at.utcoffset() is not None
        else "Chưa xác định"
    )
    return BacktestJobView(
        id=str(getattr(job, "id", "")),
        status=status_label,
        stage=stage_label,
        progress=progress,
        bundle_hash_short=str(getattr(job, "bundle_hash", ""))[:12],
        strategy_name=release.name_vi if release else "Không xác định",
        execution_interval=str(getattr(job, "execution_interval", "")),
        created_at=created_at,
        created_at_compact=created_at_compact,
        error_code=str(error_code) if error_code else None,
        run_ids=result_run_ids,
        can_cancel=(
            status in {"QUEUED", "RUNNING"}
            and str(getattr(job, "stage", "")) != "persist"
            and not cancel_requested
        ),
    )


def _compact_local_date(value: object) -> str:
    """Rút gọn ngày trong nhãn chọn gói; phần trợ giúp vẫn hiển thị đủ giờ Việt Nam."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return "Chưa rõ ngày"
    return value.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%y")


def _compact_local_time(value: object) -> str:
    """Hiển thị mốc đủ phút; nhãn kế bên nói rõ đây là giờ Việt Nam."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return "Chưa rõ thời gian"
    return value.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M")


@router.get("")
async def backtest_replay_page(
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    error: str | None = None,
    submitted: bool = False,
    bundle_hash: str | None = None,
) -> Response:
    """Hiển thị form tạo lượt chạy và trạng thái các lượt trong workspace."""
    response.headers["Cache-Control"] = "no-store"
    _require_owner(identity)
    service = _service(request)
    try:
        jobs = tuple(_job_view(item) for item in await service.list(identity.principal))
        list_bundles = getattr(service, "list_bundles", None)
        bundle_choices = (
            tuple(await list_bundles(identity.principal)) if callable(list_bundles) else ()
        )
        csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    except (PermissionError, ValueError) as service_error:
        raise HTTPException(
            status_code=403, detail="Không đủ quyền xem danh sách kiểm thử."
        ) from service_error
    templates = _templates(request)
    templates.env.filters["compact_local_date"] = _compact_local_date
    templates.env.filters["compact_local_time"] = _compact_local_time
    active_jobs = any(job.status in {"Đang chạy", "Đang chờ", "Đang yêu cầu hủy"} for job in jobs)
    selected_bundle_hash = (
        bundle_hash
        if any(item.bundle_hash == bundle_hash for item in bundle_choices)
        else (bundle_choices[0].bundle_hash if bundle_choices else "")
    )
    selected_bundle = next(
        (item for item in bundle_choices if item.bundle_hash == selected_bundle_hash), None
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/backtest_replays.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "jobs": jobs,
            "bundle_choices": bundle_choices,
            "show_bundle_hash_input": not callable(list_bundles),
            "selected_bundle_hash": selected_bundle_hash,
            "selected_bundle": selected_bundle,
            "strategies": strategy_registry().list_releases(),
            "active_jobs": active_jobs,
            "error_message": (
                "Không thể tạo lượt kiểm thử. Kiểm tra gói dữ liệu và các giá trị đã nhập."
                if error == "invalid"
                else None
            ),
            "submitted": submitted,
            "default_form": {
                "starting_equity": "10000",
                "per_trade_risk_fraction": "0.01",
                "fee_rate": "0.001",
                "slippage_rate": "0.0005",
                "seed": "7",
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/submit")
async def submit_backtest_replay(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Kiểm CSRF và owner trước khi đưa yêu cầu hợp lệ vào hàng đợi."""
    _require_owner(identity)
    values = await _parse_form(request, max_bytes=_MAX_FORM_BYTES)
    await _verify_form_csrf(request, values)
    try:
        submission = _submission(values)
        await _service(request).submit(identity.principal, submission)
    except ValueError, InvalidOperation:
        return RedirectResponse(
            "/admin/backtests?error=invalid",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="Không đủ quyền tạo lượt kiểm thử.") from error
    except LookupError:
        return RedirectResponse(
            "/admin/backtests?error=invalid",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(
        "/admin/backtests?submitted=1",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{job_id}/cancel")
async def cancel_backtest_replay(
    job_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    """Yêu cầu tiến trình nền dừng lượt đang chạy; thao tác hủy không xóa bằng chứng."""
    _require_owner(identity)
    values = await _parse_form(request, max_bytes=4096)
    await _verify_form_csrf(request, values)
    try:
        await _service(request).cancel(identity.principal, job_id)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="Không đủ quyền hủy lượt kiểm thử.") from error
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=404, detail="Không tìm thấy lượt kiểm thử.") from error
    return RedirectResponse(
        "/admin/backtests",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["BacktestJobView", "router"]
