"""Xuất ảnh nền tối cho Admin Web từ `admin_background.html` (canvas, seed cố định).

Ảnh là tác phẩm sinh bằng mã trong repo (không lấy từ nguồn ngoài, không có vấn đề bản quyền).
Chạy lại khi muốn chỉnh màu/bố cục rồi commit ảnh mới:

    uv run python scripts/design/render_admin_background.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts" / "design" / "admin_background.html"
OUTPUT = ROOT / "src" / "trading_bot" / "static" / "bg" / "ai-aurora-dark.jpg"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 2560, "height": 1600})
        page.goto(SOURCE.as_uri())
        page.wait_for_function("document.title === 'done'", timeout=60_000)
        page.locator("#c").screenshot(path=str(OUTPUT), type="jpeg", quality=82)
        browser.close()
    print(f"Đã ghi {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size} byte).")


if __name__ == "__main__":
    main()
