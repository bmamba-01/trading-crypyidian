"""Chuyển update Telegram tới webhook của admin-web chạy local.

Máy phát triển không có địa chỉ HTTPS công khai để Telegram gọi webhook. Script này lấy
update bằng `getUpdates` (long polling) rồi POST nguyên văn tới
`/api/v1/telegram/webhook` của admin-web kèm header secret, nên luồng liên kết và lệnh đi
qua đúng mã kiểm tra của sản phẩm (secret, private chat, chống giả mạo, idempotent).

Chỉ dùng cho kiểm thử local:
- đích bắt buộc là địa chỉ loopback (127.0.0.1, ::1, localhost);
- từ chối chạy khi bot đang có webhook (Telegram cũng không cho getUpdates lúc đó);
- token và secret chỉ đọc từ file, không in ra log.

Tài liệu Bot API (kiểm 2026-11-14): https://core.telegram.org/bots/api#getupdates ,
https://core.telegram.org/bots/api#setwebhook (header `X-Telegram-Bot-Api-Secret-Token`).

Ví dụ:
    uv run python scripts/telegram_local_relay.py --webhook-url \\
        http://127.0.0.1:8010/api/v1/telegram/webhook
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
TELEGRAM_API = "https://api.telegram.org"
ALLOWED_UPDATES = ("message", "callback_query")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
POLL_TIMEOUT_SECONDS = 25


class RelayError(RuntimeError):
    """Cấu hình relay không an toàn hoặc Telegram trả lỗi không thể tiếp tục."""


def require_loopback_url(url: str) -> str:
    """Chỉ cho phép chuyển update tới admin-web trên chính máy này."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.hostname not in LOOPBACK_HOSTS:
        raise RelayError("Đích webhook phải là địa chỉ loopback (127.0.0.1, ::1, localhost).")
    if not parts.path.endswith("/api/v1/telegram/webhook"):
        raise RelayError("Đích webhook phải là đường dẫn /api/v1/telegram/webhook.")
    return url


def read_secret_file(path: Path) -> str:
    """Đọc secret một dòng; file rỗng nghĩa là chưa cấu hình."""
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RelayError(f"File secret {path.name} đang rỗng.")
    return value


def next_offset(updates: list[dict[str, Any]], current: int | None) -> int | None:
    """Offset kế tiếp theo Bot API: update_id lớn nhất đã xử lý + 1."""
    ids = [item["update_id"] for item in updates if isinstance(item.get("update_id"), int)]
    if not ids:
        return current
    return max(ids) + 1


@dataclass(frozen=True, slots=True)
class Relay:
    """Một vòng: lấy update từ Telegram, chuyển từng update, rồi xác nhận offset."""

    token: str
    webhook_secret: str
    webhook_url: str
    fetch: Callable[[str, dict[str, Any], float], dict[str, Any]]
    forward: Callable[[str, bytes, dict[str, str]], int]

    def _method_url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.token}/{method}"

    def ensure_no_webhook(self) -> None:
        info = self.fetch(self._method_url("getWebhookInfo"), {}, 15)
        if info.get("result", {}).get("url"):
            raise RelayError(
                "Bot đang có webhook công khai; relay local không được chạy song song."
            )

    def poll_once(self, offset: int | None) -> tuple[int | None, list[int]]:
        payload: dict[str, Any] = {
            "timeout": POLL_TIMEOUT_SECONDS,
            "allowed_updates": list(ALLOWED_UPDATES),
        }
        if offset is not None:
            payload["offset"] = offset
        response = self.fetch(self._method_url("getUpdates"), payload, POLL_TIMEOUT_SECONDS + 10)
        if response.get("ok") is not True:
            raise RelayError(f"getUpdates thất bại: {response.get('description', 'không rõ')}")
        updates = [item for item in response.get("result", []) if isinstance(item, dict)]
        statuses: list[int] = []
        for update in updates:
            status = self.forward(
                self.webhook_url,
                json.dumps(update).encode("utf-8"),
                {"Content-Type": "application/json", SECRET_HEADER: self.webhook_secret},
            )
            statuses.append(status)
            if status >= 500:
                # Chưa xác nhận offset để Telegram giao lại update ở vòng sau.
                return offset, statuses
        return next_offset(updates, offset), statuses


def _fetch(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - URL cố định tới api.telegram.org
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body: dict[str, Any] = json.loads(response.read())
            return body
    except urllib.error.HTTPError as error:
        body = json.loads(error.read() or b"{}")
        return body if isinstance(body, dict) else {"ok": False}


def _forward(url: str, data: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--webhook-url", required=True, help="Webhook admin-web trên loopback.")
    parser.add_argument("--token-file", type=Path, default=ROOT / ".secrets/telegram_bot_token")
    parser.add_argument(
        "--secret-file", type=Path, default=ROOT / ".secrets/telegram_webhook_secret"
    )
    parser.add_argument("--once", action="store_true", help="Chỉ chạy một vòng lấy update.")
    args = parser.parse_args()

    relay = Relay(
        token=read_secret_file(args.token_file),
        webhook_secret=read_secret_file(args.secret_file),
        webhook_url=require_loopback_url(args.webhook_url),
        fetch=_fetch,
        forward=_forward,
    )
    relay.ensure_no_webhook()
    print("Relay Telegram local đang chạy; Ctrl+C để dừng.", flush=True)
    offset: int | None = None
    while True:
        try:
            offset, statuses = relay.poll_once(offset)
        except (OSError, RelayError) as error:
            print(f"Lỗi tạm thời: {type(error).__name__}; thử lại sau 5 giây.", file=sys.stderr)
            time.sleep(5)
            continue
        if statuses:
            print(f"Đã chuyển {len(statuses)} update, mã phản hồi {statuses}.", flush=True)
        if args.once:
            return


if __name__ == "__main__":
    main()
