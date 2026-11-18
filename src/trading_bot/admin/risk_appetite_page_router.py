"""Trang HTML "Khẩu vị rủi ro" (CRY-428): xem/chỉnh giới hạn rủi ro workspace, xem TP/SL."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trading_bot.admin.form_choices import RISK_FIELD_SPECS
from trading_bot.admin.risk_appetite_service import (
    RiskAppetiteFormError,
    RiskAppetiteService,
    parse_risk_appetite_form,
)
from trading_bot.admin.settings_service import SettingsAdminError
from trading_bot.admin.web_router import (
    AdminHTMLRoute,
    _auth_service,
    _parse_form,
    _templates,
    _verify_form_csrf,
)
from trading_bot.auth.cookies import read_session_cookie
from trading_bot.auth.policy import Permission, authorize
from trading_bot.auth.route_policy import CsrfMode, permission_route
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity

router = APIRouter(prefix="/admin", tags=["admin web"], route_class=AdminHTMLRoute)

_LABELS = {name: (label, help_vi) for name, label, help_vi, *_ in RISK_FIELD_SPECS}
# Ô của form workspace: (tên, nhãn, gợi ý, đơn vị hiển thị).
FORM_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "per_trade_risk_fraction",
        _LABELS["per_trade_risk_fraction"][0],
        "Tỷ lệ vốn (0,005 = 0,5%). Planner tính khối lượng sao cho chạm SL mất đúng phần này.",
        "fraction",
    ),
    (
        "account_max_open_risk_fraction",
        "Tổng rủi ro mở của mỗi account",
        "Tỷ lệ vốn account đang chịu rủi ro cùng lúc trên mọi vị thế.",
        "fraction",
    ),
    (
        "workspace_max_open_risk_fraction",
        "Tổng rủi ro mở toàn workspace",
        "Tỷ lệ vốn toàn workspace đang chịu rủi ro cùng lúc (mọi bot cộng lại).",
        "fraction",
    ),
    (
        "max_concentration_fraction",
        _LABELS["max_concentration_fraction"][0],
        _LABELS["max_concentration_fraction"][1],
        "fraction",
    ),
    ("max_positions", _LABELS["max_positions"][0], _LABELS["max_positions"][1], "plain"),
    (
        "max_leverage",
        _LABELS["max_leverage"][0],
        "Trần đòn bẩy; runtime PAPER dùng đúng trần này làm đòn bẩy của lệnh.",
        "plain",
    ),
    ("max_notional", _LABELS["max_notional"][0], _LABELS["max_notional"][1], "amount"),
    (
        "max_symbol_notional",
        _LABELS["max_symbol_notional"][0],
        _LABELS["max_symbol_notional"][1],
        "amount",
    ),
    (
        "max_aggregate_open_risk",
        _LABELS["max_aggregate_open_risk"][0],
        _LABELS["max_aggregate_open_risk"][1],
        "amount",
    ),
)
BREAKER_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "max_daily_loss",
        _LABELS["max_daily_loss"][0],
        "Từ 00:00 UTC. Chặn lệnh mới khi lỗ đã chốt + rủi ro đang mở + lỗ dự kiến của lệnh mới "
        "vượt trần.",
        "amount",
    ),
    (
        "max_rolling_loss",
        _LABELS["max_rolling_loss"][0],
        "Như trần lỗ ngày nhưng trên cửa sổ 24 giờ trượt.",
        "amount",
    ),
    (
        "max_drawdown_fraction",
        "Ngắt mạch drawdown từ đỉnh",
        "Tỷ lệ (0,10 = 10%). Vốn sụt từ đỉnh tới ngưỡng thì chặn lệnh mới, vẫn cho thoát lệnh; "
        "mở lại bằng Tạm dừng → Tiếp tục bot (cần MFA).",
        "fraction",
    ),
)


_FIELD_LABELS: dict[str, tuple[str]] = {
    name: (label,) for name, label, *_ in (*FORM_FIELDS, *BREAKER_FIELDS)
} | {"reason": ("Lý do thay đổi",)}


EXIT_LABELS = {
    "stop_atr_multiple": "SL ×ATR",
    "trailing_atr_multiple": "trailing ×ATR",
    "take_profit_atr_multiple": "TP ×ATR",
    "max_hold_bars": "giữ tối đa (nến)",
    "ladder": "thang TP",
}


def pct_vi(value: object, places: int | None = None) -> str:
    """Tỷ lệ dạng chuỗi Decimal → phần trăm kiểu Việt Nam, không qua float (0,005 → "0,5").

    ``places`` làm tròn số đo (drawdown) để không hiện hàng chục chữ số thập phân.
    """
    try:
        number = Decimal(str(value)) * 100
    except InvalidOperation:
        return str(value)
    if not number.is_finite():
        return str(value)
    if places is not None:
        number = number.quantize(Decimal(1).scaleb(-places))
    text = format(number.normalize(), "f")
    return text.replace(".", ",")


def _service(request: Request) -> RiskAppetiteService:
    service = cast(
        RiskAppetiteService | None, getattr(request.app.state, "risk_appetite_service", None)
    )
    if service is None:
        raise HTTPException(status_code=503, detail="Dịch vụ khẩu vị rủi ro chưa sẵn sàng.")
    return service


def _form_values(overview: dict[str, Any]) -> dict[str, str]:
    profile = overview.get("profile") or {}
    limits = profile.get("limits") or {}
    settings = overview.get("settings") or {}
    values = {name: str(value) for name, value in limits.items() if value is not None}
    for name in ("workspace_max_open_risk_fraction", "max_drawdown_fraction"):
        if settings.get(name) is not None:
            values[name] = str(settings[name])
    return values


async def _render(
    request: Request,
    identity: AuthIdentity,
    *,
    symbol: str | None,
    status_code: int = 200,
    form_values: dict[str, str] | None = None,
    error: str | None = None,
    error_field: str | None = None,
    saved: str | None = None,
) -> Response:
    overview = await _service(request).overview(identity.principal, symbol=symbol)
    csrf_token = await _auth_service(request).issue_csrf_token(read_session_cookie(request))
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/risk_appetite.html",
        context={
            "identity": identity,
            "csrf_token": csrf_token,
            "overview": overview,
            "form_fields": FORM_FIELDS,
            "breaker_fields": BREAKER_FIELDS,
            "values": form_values or _form_values(overview),
            "can_edit": authorize(identity.principal, Permission.RISK_PROFILE_WRITE),
            "error": error,
            "error_field": error_field,
            "saved": saved,
            "field_labels": _FIELD_LABELS,
            "pct_vi": pct_vi,
            "exit_labels": EXIT_LABELS,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/risk", dependencies=[permission_route(Permission.WORKSPACE_READ)])
async def risk_appetite_page(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
    symbol: str | None = None,
    saved: str | None = None,
) -> Response:
    saved_text = saved if saved and (saved.isdigit() or saved == "unchanged") else None
    return await _render(request, identity, symbol=symbol, saved=saved_text)


@router.post(
    "/risk",
    dependencies=[
        permission_route(
            Permission.RISK_PROFILE_WRITE,
            csrf=CsrfMode.FORM,
            denied_detail="Không đủ quyền thay đổi khẩu vị rủi ro.",
        )
    ],
)
async def risk_appetite_save(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_identity)],
) -> Response:
    values = await _parse_form(request)
    await _verify_form_csrf(request, values)
    submitted = {key: value for key, value in values.items() if key != "csrf_token"}
    try:
        update = parse_risk_appetite_form(values)
    except RiskAppetiteFormError as error:
        return await _render(
            request,
            identity,
            symbol=None,
            status_code=422,
            form_values=submitted,
            error=error.message,
            error_field=error.field,
        )
    try:
        result = await _service(request).save(identity.principal, update)
    except SettingsAdminError as error:
        return await _render(
            request,
            identity,
            symbol=None,
            status_code=error.status_code,
            form_values=submitted,
            error=error.message,
        )
    saved = "unchanged" if result.get("status") == "unchanged" else str(result.get("version"))
    return Response(status_code=303, headers={"Location": f"/admin/risk?saved={saved}"})


__all__ = ["router"]
