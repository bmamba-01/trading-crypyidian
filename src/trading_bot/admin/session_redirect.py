"""Đưa người dùng về trang đăng nhập Admin khi phiên hết hạn trên trang HTML.

Chỉ áp dụng cho điều hướng tài liệu của trình duyệt (mở trang hoặc gửi form HTML) dưới
`/admin`. API JSON (`/api/v1/...`, `/admin/api/...`) và mọi fetch/XHR vẫn nhận 401 JSON để
script tự xử lý. Tham số `next` chỉ nhận đường dẫn nội bộ `/admin...` để tránh open redirect
(chuyển hướng mở — kẻ tấn công lợi dụng tên miền tin cậy để đẩy người dùng sang trang ngoài).
"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import Request, Response

ADMIN_HOME_PATH = "/admin"
ADMIN_LOGIN_PATH = "/admin/login"
NEXT_PARAM = "next"
_MAX_NEXT_LENGTH = 2048
_FORM_CONTENT_TYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data"})


def safe_next_path(value: str | None) -> str | None:
    """Trả lại `value` nếu là đường dẫn nội bộ `/admin...` an toàn, ngược lại `None`.

    Từ chối: URL có scheme/host, `//host` (protocol-relative), dấu `\\` (trình duyệt coi như
    `/`), ký tự điều khiển (chống chèn header), segment `..`, fragment, trang đăng nhập (tránh
    vòng lặp) và mọi đường dẫn ngoài cây `/admin`.
    """
    if not value or len(value) > _MAX_NEXT_LENGTH:
        return None
    if (
        "\\" in value
        or "#" in value
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    path = parts.path
    if path != ADMIN_HOME_PATH and not path.startswith(ADMIN_HOME_PATH + "/"):
        return None
    if ".." in path.split("/"):
        return None
    if path == ADMIN_LOGIN_PATH or path.startswith(ADMIN_LOGIN_PATH + "/"):
        return None
    return urlunsplit(("", "", path, parts.query, ""))


def _is_admin_page_path(path: str) -> bool:
    if path != ADMIN_HOME_PATH and not path.startswith(ADMIN_HOME_PATH + "/"):
        return False
    if path.startswith(ADMIN_HOME_PATH + "/api/"):
        return False
    return path != ADMIN_LOGIN_PATH


def wants_html_navigation(request: Request) -> bool:
    """Request có phải người dùng mở trang/gửi form HTML Admin bằng trình duyệt không.

    Dựa vào Fetch Metadata (`Sec-Fetch-Mode`/`Sec-Fetch-Dest`): fetch/XHR gửi `cors`,
    `same-origin` hoặc `no-cors` nên bị loại. Trình duyệt không gửi metadata thì dựa vào
    `Accept: text/html` (fetch mặc định gửi `*/*`).
    """
    if not _is_admin_page_path(request.url.path):
        return False
    mode = request.headers.get("sec-fetch-mode")
    if mode is not None and mode.lower() != "navigate":
        return False
    destination = request.headers.get("sec-fetch-dest")
    if destination is not None and destination.lower() not in {"document", "iframe"}:
        return False
    if "text/html" not in request.headers.get("accept", "").lower():
        return False
    method = request.method.upper()
    if method in {"GET", "HEAD"}:
        return True
    if method == "POST":
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return content_type in _FORM_CONTENT_TYPES
    return False


def _next_for(request: Request) -> str:
    """Trang quay lại sau đăng nhập.

    GET: chính trang đang mở. POST form: không phát lại POST (giữ an toàn CSRF), chỉ quay về
    trang chứa form theo `Referer` cùng origin; thiếu/khác origin thì về trang chủ Admin.
    """
    if request.method.upper() in {"GET", "HEAD"}:
        query = request.url.query
        candidate = request.url.path + (f"?{query}" if query else "")
        return safe_next_path(candidate) or ADMIN_HOME_PATH
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme == request.url.scheme and parts.netloc == request.url.netloc:
            candidate = parts.path + (f"?{parts.query}" if parts.query else "")
            return safe_next_path(candidate) or ADMIN_HOME_PATH
    return ADMIN_HOME_PATH


def login_url(next_path: str | None) -> str:
    """URL trang đăng nhập kèm `next` đã kiểm tra (bỏ qua nếu là trang chủ hoặc không an toàn)."""
    safe = safe_next_path(next_path)
    if safe is None:
        return ADMIN_LOGIN_PATH
    return f"{ADMIN_LOGIN_PATH}?{urlencode({NEXT_PARAM: safe})}"


def login_redirect_for(request: Request, status_code: int) -> Response | None:
    """Phản hồi 303 về trang đăng nhập nếu đây là lỗi 401 của điều hướng HTML; ngược lại None."""
    if status_code != 401 or not wants_html_navigation(request):
        return None
    return Response(
        status_code=303,
        headers={"Location": login_url(_next_for(request)), "Cache-Control": "no-store"},
    )
