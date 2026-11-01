"""Fixture Chromium cục bộ cho E2E giao diện Admin Web."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, sync_playwright


@pytest.fixture
def browser_page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()
        page.set_default_timeout(10_000)
        try:
            yield page
        finally:
            context.close()
            browser.close()
