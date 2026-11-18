"""Registry phân quyền deny-by-default cho toàn bộ route HTTP (SEC-18 / WP-04A)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from trading_bot.auth.policy import Permission


class RouteAccessLevel(StrEnum):
    """Mức truy cập tối thiểu của một route HTTP."""

    PUBLIC = "PUBLIC"
    AUTHENTICATED = "AUTHENTICATED"
    WORKSPACE_PERMISSION = "WORKSPACE_PERMISSION"
    BOT_PERMISSION = "BOT_PERMISSION"


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """Chính sách phân quyền khai báo tường minh cho từng (method, path)."""

    access_level: RouteAccessLevel
    permission: Permission | None = None
    bot_permission: str | None = None
    description_vi: str = ""


def _pub(desc: str = "") -> RoutePolicy:
    return RoutePolicy(RouteAccessLevel.PUBLIC, description_vi=desc)


def _auth(desc: str = "") -> RoutePolicy:
    return RoutePolicy(RouteAccessLevel.AUTHENTICATED, description_vi=desc)


def _perm(permission: Permission, desc: str = "") -> RoutePolicy:
    return RoutePolicy(
        RouteAccessLevel.WORKSPACE_PERMISSION, permission=permission, description_vi=desc
    )


def _bot(bot_permission: str, desc: str = "") -> RoutePolicy:
    return RoutePolicy(
        RouteAccessLevel.BOT_PERMISSION, bot_permission=bot_permission, description_vi=desc
    )


ROUTE_PERMISSION_REGISTRY: dict[tuple[str, str], RoutePolicy] = {
    # Health & Webhook công khai có xác thực riêng
    ("GET", "/health/live"): _pub("Kiểm tra sống"),
    ("GET", "/health/ready"): _pub("Kiểm tra sẵn sàng"),
    ("POST", "/api/v1/telegram/webhook"): _pub("Webhook Telegram xác thực bằng secret header"),
    # Auth
    ("GET", "/admin/login"): _pub("Trang đăng nhập Admin"),
    ("POST", "/admin/login"): _pub("Đăng nhập Admin"),
    ("POST", "/api/v1/auth/bootstrap"): _pub("Khởi tạo tài khoản owner đầu tiên"),
    ("POST", "/api/v1/auth/bootstrap/verify"): _pub("Xác minh MFA khi bootstrap"),
    ("GET", "/api/v1/auth/csrf"): _auth("Lấy CSRF token theo phiên"),
    ("POST", "/api/v1/auth/login"): _pub("Đăng nhập API"),
    ("POST", "/api/v1/auth/logout"): _auth("Đăng xuất phiên"),
    ("GET", "/api/v1/auth/me"): _auth("Thông tin phiên hiện tại"),
    ("POST", "/api/v1/auth/reauthenticate"): _auth("Xác thực lại MFA step-up"),
    # Dashboard & Admin HTML pages
    ("GET", "/admin"): _perm(Permission.WORKSPACE_READ, "Trang tổng quan Admin"),
    ("GET", "/admin/audit"): _perm(Permission.SETTINGS_READ, "Trang nhật ký audit"),
    ("GET", "/admin/ai-analyses"): _perm(Permission.TRADE_READ, "Danh sách phân tích AI"),
    ("GET", "/admin/ai-analyses/{run_id}"): _perm(Permission.TRADE_READ, "Chi tiết phân tích AI"),
    ("GET", "/admin/backtests"): _perm(Permission.WORKSPACE_READ, "Trang backtest"),
    ("POST", "/admin/backtests/submit"): _perm(Permission.STRATEGY_WRITE, "Gửi job backtest"),
    ("POST", "/admin/backtests/{job_id}/cancel"): _perm(
        Permission.STRATEGY_WRITE, "Hủy job backtest"
    ),
    ("GET", "/admin/bots"): _perm(Permission.WORKSPACE_READ, "Danh sách bot"),
    ("GET", "/admin/bots/new"): _perm(Permission.STRATEGY_WRITE, "Form tạo bot"),
    ("POST", "/admin/bots/new"): _perm(Permission.STRATEGY_WRITE, "Tạo bot mới"),
    ("GET", "/admin/bots/{bot_id}"): _bot("READ", "Chi tiết bot"),
    ("POST", "/admin/bots/{bot_id}/ai-proposals/{backtest_id}/draft"): _bot(
        "CONFIG_WRITE", "Tạo draft từ đề xuất AI"
    ),
    ("POST", "/admin/bots/{bot_id}/config-drafts"): _bot("CONFIG_WRITE", "Tạo draft cấu hình bot"),
    ("POST", "/admin/bots/{bot_id}/config-drafts/{draft_id}/publish"): _bot(
        "CONFIG_WRITE", "Publish cấu hình bot"
    ),
    ("POST", "/admin/bots/{bot_id}/config-drafts/{draft_id}/validate"): _bot(
        "CONFIG_WRITE", "Kiểm tra draft cấu hình bot"
    ),
    ("POST", "/admin/bots/{bot_id}/disable"): _bot("CONTROL_RUNTIME", "Vô hiệu hóa bot"),
    ("POST", "/admin/bots/{bot_id}/enable"): _bot("CONTROL_RUNTIME", "Kích hoạt bot"),
    ("POST", "/admin/bots/{bot_id}/pause"): _bot("CONTROL_RUNTIME", "Tạm dừng bot"),
    ("POST", "/admin/bots/{bot_id}/resume"): _bot("CONTROL_RUNTIME", "Tiếp tục bot"),
    ("POST", "/admin/bots/{bot_id}/telegram-signal-filters"): _bot(
        "CONFIG_WRITE", "Cập nhật bộ lọc tín hiệu Telegram"
    ),
    ("POST", "/admin/bots/{bot_id}/telegram-signals"): _bot(
        "CONFIG_WRITE", "Cập nhật cấu hình tín hiệu Telegram"
    ),
    ("GET", "/admin/calendar"): _perm(Permission.WORKSPACE_READ, "Lịch kinh tế"),
    ("POST", "/admin/calendar"): _perm(Permission.SETTINGS_WRITE, "Thêm sự kiện lịch kinh tế"),
    ("POST", "/admin/calendar/{event_id}"): _perm(
        Permission.SETTINGS_WRITE, "Cập nhật sự kiện lịch kinh tế"
    ),
    ("POST", "/admin/calendar/{event_id}/delete"): _perm(
        Permission.SETTINGS_WRITE, "Xóa sự kiện lịch kinh tế"
    ),
    ("GET", "/admin/chart"): _perm(Permission.TRADE_READ, "Trang biểu đồ"),
    ("GET", "/admin/dashboard/bots/{bot_id}/snapshot"): _bot("READ", "Snapshot dashboard bot"),
    ("GET", "/admin/flatten"): _perm(Permission.WORKSPACE_KILL, "Trang flatten toàn workspace"),
    ("POST", "/admin/flatten/confirm"): _perm(Permission.WORKSPACE_KILL, "Xác nhận flatten"),
    ("GET", "/admin/flatten/operations/{operation_id}"): _perm(
        Permission.WORKSPACE_READ, "Trạng thái flatten"
    ),
    ("POST", "/admin/flatten/preview"): _perm(Permission.WORKSPACE_KILL, "Xem trước flatten"),
    ("GET", "/admin/live"): _perm(Permission.TRADE_READ, "Trang giám sát live"),
    ("GET", "/admin/reports"): _perm(Permission.TRADE_READ, "Trang báo cáo"),
    ("GET", "/admin/reports/export.csv"): _perm(Permission.TRADE_READ, "Xuất CSV báo cáo"),
    ("GET", "/admin/research-sources"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Trang nguồn nghiên cứu"
    ),
    ("POST", "/admin/research-sources/{action}"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Thao tác nguồn nghiên cứu"
    ),
    ("GET", "/admin/risk"): _perm(Permission.SETTINGS_READ, "Trang khẩu vị rủi ro"),
    ("POST", "/admin/risk"): _perm(Permission.RISK_PROFILE_WRITE, "Cập nhật khẩu vị rủi ro"),
    ("GET", "/admin/run-reports/{report_hash}"): _perm(Permission.TRADE_READ, "Xem báo cáo chạy"),
    ("GET", "/admin/run-reports/{report_hash}/report.json"): _perm(
        Permission.TRADE_READ, "Tải JSON báo cáo chạy"
    ),
    ("GET", "/admin/scanner"): _perm(Permission.WORKSPACE_READ, "Trang quét thị trường"),
    ("POST", "/admin/scanner"): _perm(Permission.SETTINGS_WRITE, "Chạy quét thị trường"),
    ("GET", "/admin/signal-outcomes"): _perm(Permission.TRADE_READ, "Trang kết quả tín hiệu"),
    ("GET", "/admin/strategy-cards"): _perm(Permission.WORKSPACE_READ, "Trang Strategy Cards"),
    ("GET", "/admin/strategy-cards/{card_id}/microscope"): _perm(
        Permission.WORKSPACE_READ, "Soi chi tiết Strategy Card"
    ),
    ("POST", "/admin/strategy-releases/publish"): _perm(
        Permission.STRATEGY_WRITE, "Publish strategy release"
    ),
    ("POST", "/admin/strategy-releases/retire"): _perm(
        Permission.STRATEGY_WRITE, "Retire strategy release"
    ),
    ("GET", "/admin/telegram"): _perm(Permission.WORKSPACE_READ, "Trang liên kết Telegram"),
    ("POST", "/admin/telegram/link"): _perm(Permission.MEMBER_WRITE, "Liên kết Telegram"),
    ("POST", "/admin/telegram/revoke/{membership_id}"): _perm(
        Permission.MEMBER_WRITE, "Thu hồi Telegram"
    ),
    ("POST", "/admin/telegram/topics/{membership_id}"): _perm(
        Permission.MEMBER_WRITE, "Cấu hình topic Telegram"
    ),
    ("POST", "/admin/workspace/kill"): _perm(Permission.WORKSPACE_KILL, "Kill switch workspace"),
    ("POST", "/admin/workspace/release"): _perm(
        Permission.WORKSPACE_KILL, "Mở khóa kill switch workspace"
    ),
    # Admin JSON endpoints (/admin/api/*)
    ("GET", "/admin/api/bots/{bot_id}/realtime"): _bot("READ", "Dữ liệu realtime bot"),
    ("GET", "/admin/api/chart"): _perm(Permission.TRADE_READ, "Dữ liệu biểu đồ"),
    ("GET", "/admin/api/chart/decision"): _perm(Permission.TRADE_READ, "Quyết định trên biểu đồ"),
    ("GET", "/admin/api/chart/decisions"): _perm(
        Permission.TRADE_READ, "Danh sách quyết định biểu đồ"
    ),
    ("GET", "/admin/api/chart/history"): _perm(Permission.TRADE_READ, "Lịch sử biểu đồ"),
    ("GET", "/admin/api/chart/overview"): _perm(Permission.TRADE_READ, "Tổng quan biểu đồ"),
    ("GET", "/admin/api/chart/validate"): _perm(Permission.TRADE_READ, "Kiểm tra biểu đồ"),
    ("GET", "/admin/api/dashboard/realtime"): _perm(
        Permission.WORKSPACE_READ, "Realtime dashboard"
    ),
    ("GET", "/admin/api/live/snapshot"): _perm(Permission.TRADE_READ, "Snapshot live"),
    ("GET", "/admin/api/live/stream"): _perm(Permission.TRADE_READ, "Stream live SSE"),
    ("POST", "/admin/api/operations/breaker/resume"): _perm(
        Permission.RUNTIME_RESUME, "Mở lại circuit breaker"
    ),
    ("GET", "/admin/api/operations/health"): _perm(Permission.WORKSPACE_READ, "Sức khỏe vận hành"),
    ("GET", "/admin/api/strategy-cards"): _perm(Permission.WORKSPACE_READ, "API Strategy Cards"),
    ("POST", "/admin/api/strategy-cards/{card_id}/inspect-bar"): _perm(
        Permission.WORKSPACE_READ, "Soi bar Strategy Card"
    ),
    ("GET", "/admin/api/strategy-releases"): _perm(
        Permission.WORKSPACE_READ, "Danh sách strategy release"
    ),
    ("POST", "/admin/api/strategy-releases/publish"): _perm(
        Permission.STRATEGY_WRITE, "API publish strategy release"
    ),
    ("POST", "/admin/api/strategy-releases/retire"): _perm(
        Permission.STRATEGY_WRITE, "API retire strategy release"
    ),
    ("POST", "/admin/api/trades/{trade_id}/ingest-negative-sample"): _perm(
        Permission.TRADE_REVIEW, "Nạp mẫu âm từ trade"
    ),
    ("POST", "/admin/api/trades/{trade_id}/queue-ai-retraining"): _perm(
        Permission.LEARNING_TRAINING_MANAGE, "Đưa vào hàng đợi huấn luyện lại"
    ),
    # Learning pages & APIs
    ("GET", "/admin/learning/models"): _perm(
        Permission.LEARNING_MODEL_READ, "Trang model registry"
    ),
    ("GET", "/admin/learning/observations"): _perm(
        Permission.LEARNING_MODEL_READ, "Trang quan sát learning"
    ),
    ("GET", "/admin/learning/paper-performance"): _perm(
        Permission.LEARNING_MODEL_READ, "Trang hiệu năng PAPER"
    ),
    ("GET", "/admin/learning/scoring"): _perm(Permission.LEARNING_MODEL_READ, "Trang scorer"),
    ("POST", "/admin/learning/scoring/decisions/{bot_decision_id}/block-paper-entry"): _perm(
        Permission.LEARNING_INCIDENT_MANAGE, "Chặn entry PAPER theo quyết định"
    ),
    ("GET", "/admin/learning/splits"): _perm(
        Permission.LEARNING_SPLIT_APPROVE, "Trang duyệt split"
    ),
    ("GET", "/admin/learning/training"): _perm(
        Permission.LEARNING_TRAINING_MANAGE, "Trang job huấn luyện"
    ),
    ("GET", "/api/v1/learning-models"): _perm(Permission.LEARNING_MODEL_READ, "Danh sách model"),
    ("POST", "/api/v1/learning-models"): _perm(Permission.LEARNING_MODEL_WRITE, "Đăng ký model"),
    ("GET", "/api/v1/learning-models/active-pointer"): _perm(
        Permission.LEARNING_MODEL_READ, "Con trỏ model đang chạy"
    ),
    ("GET", "/api/v1/learning-models/{registry_id}"): _perm(
        Permission.LEARNING_MODEL_READ, "Chi tiết model"
    ),
    ("POST", "/api/v1/learning-models/{registry_id}/activate"): _perm(
        Permission.LEARNING_MODEL_APPROVE, "Kích hoạt model"
    ),
    ("POST", "/api/v1/learning-models/{registry_id}/approve"): _perm(
        Permission.LEARNING_MODEL_APPROVE, "Phê duyệt model"
    ),
    ("POST", "/api/v1/learning-models/{registry_id}/evaluation"): _perm(
        Permission.LEARNING_MODEL_WRITE, "Ghi đánh giá model"
    ),
    ("GET", "/api/v1/learning-models/{registry_id}/events"): _perm(
        Permission.LEARNING_MODEL_READ, "Sự kiện model"
    ),
    ("POST", "/api/v1/learning-models/{registry_id}/reject"): _perm(
        Permission.LEARNING_MODEL_APPROVE, "Từ chối model"
    ),
    ("POST", "/api/v1/learning-models/{registry_id}/retire"): _perm(
        Permission.LEARNING_MODEL_APPROVE, "Ngừng dùng model"
    ),
    ("POST", "/api/v1/learning-models/{registry_id}/rollback"): _perm(
        Permission.LEARNING_MODEL_APPROVE, "Rollback model"
    ),
    ("GET", "/api/v1/learning-scorer/deployment"): _perm(
        Permission.LEARNING_MODEL_READ, "Trạng thái triển khai scorer"
    ),
    ("POST", "/api/v1/learning-scorer/deployment"): _perm(
        Permission.LEARNING_MODEL_APPROVE, "Triển khai scorer"
    ),
    ("GET", "/api/v1/learning-splits/candidates"): _perm(
        Permission.LEARNING_SPLIT_APPROVE, "Danh sách candidate split"
    ),
    ("POST", "/api/v1/learning-splits/candidates/{candidate_id}/approve"): _perm(
        Permission.LEARNING_SPLIT_APPROVE, "Duyệt candidate split"
    ),
    ("GET", "/api/v1/learning-training/jobs"): _perm(
        Permission.LEARNING_TRAINING_MANAGE, "Danh sách job huấn luyện"
    ),
    ("POST", "/api/v1/learning-training/jobs"): _perm(
        Permission.LEARNING_TRAINING_MANAGE, "Tạo job huấn luyện"
    ),
    ("POST", "/api/v1/learning-training/jobs/{job_id}/cancel"): _perm(
        Permission.LEARNING_TRAINING_MANAGE, "Hủy job huấn luyện"
    ),
    ("GET", "/api/v1/learning/incident-entry-holds"): _perm(
        Permission.LEARNING_INCIDENT_MANAGE, "Danh sách hold entry"
    ),
    ("POST", "/api/v1/learning/incident-entry-holds/{proposal_id}/hold"): _perm(
        Permission.LEARNING_INCIDENT_MANAGE, "Bật hold entry"
    ),
    ("POST", "/api/v1/learning/incident-entry-holds/{proposal_id}/release"): _perm(
        Permission.LEARNING_INCIDENT_MANAGE, "Gỡ hold entry"
    ),
    ("POST", "/api/v1/learning/paper-performance/assess"): _perm(
        Permission.LEARNING_MODEL_READ, "Đánh giá hiệu năng PAPER"
    ),
    # Core API v1
    ("GET", "/api/v1/audit"): _perm(Permission.SETTINGS_READ, "Tra cứu nhật ký audit"),
    ("GET", "/api/v1/bots"): _perm(Permission.WORKSPACE_READ, "Danh sách bot"),
    ("POST", "/api/v1/bots"): _perm(Permission.STRATEGY_WRITE, "Tạo bot"),
    ("GET", "/api/v1/bots/{bot_id}"): _bot("READ", "Chi tiết bot"),
    ("POST", "/api/v1/bots/{bot_id}/config-drafts"): _bot("CONFIG_WRITE", "Tạo draft cấu hình bot"),
    ("PUT", "/api/v1/bots/{bot_id}/config-drafts/{draft_id}"): _bot(
        "CONFIG_WRITE", "Cập nhật draft bot"
    ),
    ("POST", "/api/v1/bots/{bot_id}/config-drafts/{draft_id}/publish"): _bot(
        "CONFIG_WRITE", "Publish cấu hình bot"
    ),
    ("POST", "/api/v1/bots/{bot_id}/config-drafts/{draft_id}/validate"): _bot(
        "CONFIG_WRITE", "Kiểm tra draft bot"
    ),
    ("POST", "/api/v1/bots/{bot_id}/config-versions/{version}/rollback"): _bot(
        "CONFIG_WRITE", "Rollback cấu hình bot"
    ),
    ("POST", "/api/v1/bots/{bot_id}/disable"): _bot("CONTROL_RUNTIME", "Vô hiệu hóa bot"),
    ("POST", "/api/v1/bots/{bot_id}/enable"): _bot("CONTROL_RUNTIME", "Kích hoạt bot"),
    ("GET", "/api/v1/bots/{bot_id}/runtime"): _bot("READ", "Trạng thái runtime bot"),
    ("POST", "/api/v1/bots/{bot_id}/runtime/pause"): _bot("CONTROL_RUNTIME", "Tạm dừng bot"),
    ("POST", "/api/v1/bots/{bot_id}/runtime/resume"): _bot("CONTROL_RUNTIME", "Tiếp tục bot"),
    ("GET", "/api/v1/experiments"): _perm(Permission.WORKSPACE_READ, "Danh sách thí nghiệm"),
    ("GET", "/api/v1/incidents"): _perm(Permission.TRADE_READ, "Danh sách sự cố"),
    ("GET", "/api/v1/members"): _perm(Permission.WORKSPACE_READ, "Danh sách thành viên"),
    ("GET", "/api/v1/members/invitations"): _perm(Permission.MEMBER_WRITE, "Danh sách lời mời"),
    ("POST", "/api/v1/members/invitations"): _perm(Permission.MEMBER_WRITE, "Tạo lời mời"),
    ("POST", "/api/v1/members/invitations/{invitation_id}/revoke"): _perm(
        Permission.MEMBER_WRITE, "Thu hồi lời mời"
    ),
    ("POST", "/api/v1/members/recovery/mfa-reset"): _perm(
        Permission.MEMBER_WRITE, "Khôi phục MFA thành viên"
    ),
    ("PATCH", "/api/v1/members/{membership_id}/role"): _perm(
        Permission.MEMBER_WRITE, "Đổi vai trò thành viên"
    ),
    ("GET", "/api/v1/reports"): _perm(Permission.TRADE_READ, "Báo cáo bot/workspace"),
    ("GET", "/api/v1/research-sources"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Danh sách nguồn nghiên cứu"
    ),
    ("POST", "/api/v1/research-sources"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Tạo nguồn nghiên cứu"
    ),
    ("GET", "/api/v1/research-sources/{source_id}"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Chi tiết nguồn nghiên cứu"
    ),
    ("POST", "/api/v1/research-sources/{source_id}/revisions"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Tạo revision nguồn nghiên cứu"
    ),
    ("POST", "/api/v1/research-sources/{source_id}/revisions/{policy_id}/approve"): _perm(
        Permission.RESEARCH_SOURCE_APPROVE, "Duyệt revision nguồn nghiên cứu"
    ),
    ("POST", "/api/v1/research-sources/{source_id}/revisions/{policy_id}/license-evidence"): _perm(
        Permission.RESEARCH_SOURCE_WRITE, "Nộp bằng chứng bản quyền nguồn"
    ),
    ("GET", "/api/v1/risk-profiles"): _perm(Permission.SETTINGS_READ, "Danh sách risk profile"),
    ("POST", "/api/v1/risk-profiles"): _perm(Permission.RISK_PROFILE_WRITE, "Tạo risk profile mới"),
    ("GET", "/api/v1/settings/current"): _perm(Permission.SETTINGS_READ, "Cấu hình hiện hành"),
    ("POST", "/api/v1/settings/drafts"): _perm(Permission.SETTINGS_WRITE, "Tạo draft cấu hình"),
    ("PATCH", "/api/v1/settings/drafts/{draft_id}"): _perm(
        Permission.SETTINGS_WRITE, "Cập nhật draft cấu hình"
    ),
    ("POST", "/api/v1/settings/drafts/{draft_id}/approve"): _perm(
        Permission.SETTINGS_WRITE, "Phê duyệt cấu hình (maker-checker)"
    ),
    ("POST", "/api/v1/settings/drafts/{draft_id}/propose"): _perm(
        Permission.SETTINGS_WRITE, "Đề xuất cấu hình"
    ),
    ("POST", "/api/v1/settings/drafts/{draft_id}/publish"): _perm(
        Permission.SETTINGS_WRITE, "Publish cấu hình"
    ),
    ("POST", "/api/v1/settings/drafts/{draft_id}/validate"): _perm(
        Permission.SETTINGS_WRITE, "Kiểm tra draft cấu hình"
    ),
    ("GET", "/api/v1/settings/versions"): _perm(Permission.SETTINGS_READ, "Lịch sử cấu hình"),
    ("POST", "/api/v1/settings/versions/{version}/rollback"): _perm(
        Permission.SETTINGS_WRITE, "Rollback cấu hình"
    ),
    ("POST", "/api/v1/telegram-binding/link"): _perm(
        Permission.MEMBER_WRITE, "Liên kết tài khoản Telegram"
    ),
    ("POST", "/api/v1/telegram-binding/{membership_id}/revoke"): _perm(
        Permission.MEMBER_WRITE, "Thu hồi liên kết Telegram"
    ),
    ("POST", "/api/v1/telegram-binding/{membership_id}/topics"): _perm(
        Permission.MEMBER_WRITE, "Cập nhật topic Telegram"
    ),
    ("DELETE", "/api/v1/telegram-binding/{membership_id}/topics"): _perm(
        Permission.MEMBER_WRITE, "Xóa topic Telegram"
    ),
    ("PUT", "/api/v1/telegram-signals/{bot_id}/subscription"): _bot(
        "CONFIG_WRITE", "Đăng ký tín hiệu Telegram cho bot"
    ),
    ("GET", "/api/v1/telegram-signals/{bot_id}/subscription"): _bot(
        "READ", "Xem đăng ký tín hiệu Telegram"
    ),
    ("GET", "/api/v1/watchlists"): _perm(
        Permission.WORKSPACE_READ, "Danh sách phiên bản watchlist"
    ),
    ("POST", "/api/v1/watchlists"): _perm(
        Permission.WATCHLIST_WRITE, "Phát hành phiên bản watchlist"
    ),
    ("POST", "/api/v1/workspace/flatten/confirm"): _perm(
        Permission.WORKSPACE_KILL, "Xác nhận flatten workspace"
    ),
    ("GET", "/api/v1/workspace/flatten/operations/{operation_id}"): _perm(
        Permission.WORKSPACE_READ, "Xem tiến trình flatten"
    ),
    ("POST", "/api/v1/workspace/flatten/preview"): _perm(
        Permission.WORKSPACE_KILL, "Xem trước flatten workspace"
    ),
    ("GET", "/api/v1/workspace/runtime"): _perm(
        Permission.WORKSPACE_READ, "Trạng thái runtime workspace"
    ),
    ("POST", "/api/v1/workspace/runtime/kill"): _perm(
        Permission.WORKSPACE_KILL, "Kill switch workspace"
    ),
    ("POST", "/api/v1/workspace/runtime/release"): _perm(
        Permission.WORKSPACE_KILL, "Mở khóa kill switch workspace"
    ),
}


def iter_app_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Duyệt đệ quy toàn bộ APIRoute trong FastAPI app (bao gồm _IncludedRouter)."""
    discovered: list[tuple[str, str, APIRoute]] = []

    def _walk(routes: list[Any], prefix: str = "") -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                full_path = f"{prefix}{route.path}"
                for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
                    discovered.append((method, full_path, route))
            elif hasattr(route, "original_router"):
                ctx = getattr(route, "include_context", None)
                ctx_prefix = getattr(ctx, "prefix", "") or ""
                _walk(route.original_router.routes, f"{prefix}{ctx_prefix}")

    _walk(list(app.routes))
    return discovered


def verify_app_routes_registered(app: FastAPI) -> None:
    """Bảo đảm deny-by-default: mọi route HTTP phải có chính sách trong registry."""
    missing: list[str] = []
    for method, path, _route in iter_app_routes(app):
        if (method, path) not in ROUTE_PERMISSION_REGISTRY:
            missing.append(f"{method} {path}")
    if missing:
        raise AssertionError(
            "Các route sau chưa khai báo trong ROUTE_PERMISSION_REGISTRY (deny-by-default): "
            + ", ".join(sorted(missing))
        )
