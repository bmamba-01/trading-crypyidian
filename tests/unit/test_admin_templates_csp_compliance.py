"""Chặn mã nội tuyến trong template quản trị để tuân thủ CSP `default-src 'self'`.

Chính sách CSP của ứng dụng (SEC-27) không cho `'unsafe-inline'`, nên trình duyệt bỏ qua
mọi `<script>` nội tuyến, thuộc tính sự kiện (`onclick=...`), khối `<style>` và thuộc tính
`style="..."`. Test này đọc mã nguồn template thay vì HTML đã render để bắt cả những nhánh
Jinja không được dựng trong fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from trading_bot.common.http_security import CONTENT_SECURITY_POLICY

TEMPLATE_ROOT = Path("src/trading_bot/templates")
TEMPLATES = sorted(TEMPLATE_ROOT.rglob("*.html"))

_SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_EVENT_ATTRIBUTE = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
_STYLE_ATTRIBUTE = re.compile(r"""\sstyle\s*=\s*["']""", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style\b", re.IGNORECASE)
_JAVASCRIPT_URL = re.compile(r"""(?:href|action)\s*=\s*["']\s*javascript:""", re.IGNORECASE)


def _template_id(path: Path) -> str:
    return str(path.relative_to(TEMPLATE_ROOT))


def test_csp_does_not_allow_inline_code() -> None:
    """Nếu CSP được nới thì phải cập nhật có chủ đích cả chính sách lẫn test này."""
    assert "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY


def test_template_inventory_is_not_empty() -> None:
    assert TEMPLATES, "Không tìm thấy template nào để kiểm tra CSP."


@pytest.mark.parametrize("path", TEMPLATES, ids=_template_id)
def test_template_has_no_inline_executable_script(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    offenders = [
        attributes.strip()
        for attributes in _SCRIPT_TAG.findall(source)
        if "src=" not in attributes and 'type="application/json"' not in attributes
    ]
    assert offenders == [], f"{path}: script nội tuyến bị CSP chặn; chuyển sang /static/*.js"


@pytest.mark.parametrize("path", TEMPLATES, ids=_template_id)
def test_template_has_no_inline_event_handler_or_javascript_url(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert _EVENT_ATTRIBUTE.findall(source) == [], (
        f"{path}: thuộc tính sự kiện nội tuyến bị CSP chặn; dùng addEventListener"
    )
    assert _JAVASCRIPT_URL.findall(source) == [], f"{path}: URL javascript: bị CSP chặn"


@pytest.mark.parametrize("path", TEMPLATES, ids=_template_id)
def test_template_has_no_inline_style(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert _STYLE_BLOCK.findall(source) == [], f"{path}: khối <style> bị CSP chặn"
    assert _STYLE_ATTRIBUTE.findall(source) == [], (
        f"{path}: thuộc tính style bị CSP bỏ qua; dùng lớp trong /static/admin.css"
    )
