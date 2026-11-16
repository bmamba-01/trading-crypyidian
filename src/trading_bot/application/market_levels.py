"""Bảng "Mức giá" cho cột phải của chart (CK-10): gom mức từ payload đã tính, chỉ mô tả.

Nguồn: mức thanh khoản phiên/ngày (SIG-1), POC "trần" và VAH/VAL ngày trước (CK-4), vùng ICT và
đỉnh/đáy bằng nhau (CK-3). Chỉ giữ mức còn hiệu lực (chưa bị quét/chấp nhận, vùng chưa mất hiệu
lực). Vùng giá dùng cạnh gần giá hiện tại. Khoảng cách % = (mức − giá)/giá × 100.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LEVELS_VERSION = "ck10-levels-v1"
_LIVE_ZONE_STATES = {"active", "partial", "inverted"}


def price_levels(
    payload: Mapping[str, Any], *, current_price: float | None
) -> list[dict[str, Any]]:
    """Danh sách mức, sắp theo khoảng cách tuyệt đối tới giá (không có giá thì giữ thứ tự nguồn)."""
    items: list[dict[str, Any]] = []
    for level in payload.get("levels") or []:
        if level.get("state") != "armed":
            continue
        items.append(
            {
                "group": "liquidity",
                "label": level["label"],
                "price": float(level["price"]),
                "time": level.get("armed_time"),
            }
        )
    for level in payload.get("profile_levels") or []:
        items.append(
            {
                "group": "profile",
                "label": level["label"],
                "price": float(level["price"]),
                "time": level.get("start_time"),
            }
        )
    ict = payload.get("ict") or {}
    for zone in ict.get("zones") or []:
        if zone.get("state") not in _LIVE_ZONE_STATES:
            continue
        top, bottom = float(zone["top"]), float(zone["bottom"])
        near = (
            top
            if current_price is None or abs(top - current_price) <= abs(bottom - current_price)
            else bottom
        )
        items.append(
            {
                "group": "ict",
                "label": zone["label"],
                "price": near,
                "time": zone.get("start_time"),
                "range": [bottom, top],
            }
        )
    for level in ict.get("equal_levels") or []:
        if level.get("swept"):
            continue
        items.append(
            {
                "group": "ict",
                "label": level["label"],
                "price": float(level["price"]),
                "time": level.get("start_time"),
            }
        )
    for item in items:
        if current_price:
            item["distance_pct"] = (item["price"] - current_price) / current_price * 100
            item["side"] = "above" if item["price"] >= current_price else "below"
        else:
            item["distance_pct"] = None
            item["side"] = None
    if current_price:
        items.sort(key=lambda item: abs(item["distance_pct"]))
    return items
