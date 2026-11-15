"""Danh mục mã futures và icon coin tự host: có nguồn, giấy phép, SVG an toàn."""

from __future__ import annotations

import json
import re
from pathlib import Path

STATIC = Path(__file__).parents[2] / "src" / "trading_bot" / "static"
CATALOG = json.loads((STATIC / "data" / "binance-usdm-perpetual-usdt.json").read_text())
UNSAFE = re.compile(
    r"<script|\bon[a-z]+\s*=|javascript:|<foreignObject|href\s*=\s*['\"]https?:|<iframe|<!ENTITY",
    re.IGNORECASE,
)


def test_catalog_records_official_source_and_only_perpetual_usdt_symbols() -> None:
    assert CATALOG["source"] == "https://fapi.binance.com/fapi/v1/exchangeInfo"
    assert CATALOG["fetched_at"]
    symbols = CATALOG["symbols"]
    assert len(symbols) > 100
    assert all(item["quote"] == "USDT" and item["symbol"].endswith("USDT") for item in symbols)
    assert {"BTCUSDT", "ETHUSDT"} <= {item["symbol"] for item in symbols}


def test_icons_are_attributed_and_only_attached_to_identified_assets() -> None:
    source = CATALOG["icons_source"]
    assert source["license"] == "CC0-1.0"
    assert source["integrity"].startswith("sha512-")
    assert (STATIC / "coins" / "LICENSE-cryptocurrency-icons.md").exists()
    with_icon = [item for item in CATALOG["symbols"] if item.get("icon")]
    assert with_icon
    for item in with_icon:
        # Mã trùng ticker nhưng có thể là coin khác không được gắn logo.
        assert item["name"], item["symbol"]
        assert (STATIC / "coins" / f"{item['icon']}.svg").exists(), item["icon"]


def test_every_shipped_coin_svg_is_inert() -> None:
    files = sorted((STATIC / "coins").glob("*.svg"))
    assert files
    for path in files:
        text = path.read_text(errors="ignore")
        assert text.lstrip().startswith(("<svg", "<?xml")), path.name
        assert not UNSAFE.search(text), path.name
        assert len(text) < 60_000, path.name
