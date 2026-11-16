"""Cấu hình chung cho E2E Compose.

Cách ly Telegram (15/11/2026): stack local của owner giữ token Bot API thật ở
``.secrets/telegram_bot_token``; trước đây mọi project E2E mount đúng file đó nên E2E gửi request
thật lên Telegram (chat giả, lỗi) và test hỏng. E2E giờ trỏ secret sang một file token rỗng
(``TRADING_BOT_TELEGRAM_BOT_TOKEN_SECRET_FILE``), nên app luôn dùng ``RecordingTelegramSender``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EMPTY_TOKEN = _ROOT / ".secrets" / "e2e_telegram_bot_token"


@pytest.fixture(autouse=True, scope="session")
def _isolate_telegram_token() -> Iterator[None]:
    _EMPTY_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    _EMPTY_TOKEN.write_text("", encoding="utf-8")
    _EMPTY_TOKEN.chmod(0o600)
    previous = os.environ.get("TRADING_BOT_TELEGRAM_BOT_TOKEN_SECRET_FILE")
    os.environ["TRADING_BOT_TELEGRAM_BOT_TOKEN_SECRET_FILE"] = str(_EMPTY_TOKEN)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TRADING_BOT_TELEGRAM_BOT_TOKEN_SECRET_FILE", None)
        else:
            os.environ["TRADING_BOT_TELEGRAM_BOT_TOKEN_SECRET_FILE"] = previous
