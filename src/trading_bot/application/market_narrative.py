"""Nhận định thị trường theo mẫu câu (CK-10, MI-6 rút gọn) — không dùng LLM, không tự sinh số.

Mỗi câu ghép từ một sự kiện/số liệu đã tính sẵn trong payload chart (SIG-1, CK-3) hoặc overview
(CK-1/CK-2) và trả kèm ``source`` (loại + thời điểm/khóa) để truy ngược. Thiếu dữ liệu thì nói rõ
"chưa đủ" thay vì đoán. Giờ hiển thị theo giờ Việt Nam.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

NARRATIVE_VERSION = "ck10-narrative-v1"
_VN = ZoneInfo("Asia/Ho_Chi_Minh")
_TREND = {"up": "TĂNG", "down": "GIẢM"}


def _num(value: float) -> str:
    """Số kiểu Việt Nam: dấu chấm nghìn, dấu phẩy thập phân, tối đa 2 chữ số (≥ 1)."""
    digits = 2 if abs(value) >= 1 else 8
    text = f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",").replace(" ", ".")


def _time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(_VN).strftime("%H:%M %d/%m")


def market_narrative(
    payload: Mapping[str, Any], overview: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Danh sách ``{text, source}``; thứ tự: cấu trúc, quét thanh khoản, VWAP, đa khung, phiên…"""
    frame = str(payload.get("timeframe", "")).upper()
    sentences: list[dict[str, Any]] = []
    structure = payload.get("structure") or {}
    breaks = structure.get("breaks") or []
    if breaks:
        last = breaks[-1]
        kind = "CHoCH" if last["kind"] == "CHOCH" else "BOS"
        direction = "tăng" if last["direction"] == "bullish" else "giảm"
        trend = _TREND.get(str(structure.get("trend")), "chưa rõ")
        sentences.append(
            {
                "text": (
                    f"Cấu trúc {frame} {trend}: {kind} {direction} phá "
                    f"{_num(float(last['level']))} lúc {_time(int(last['time']))}."
                ),
                "source": {"kind": "structure_break", "time": int(last["time"]), "label": kind},
            }
        )
    else:
        sentences.append(
            {
                "text": f"Chưa đủ nến đã đóng để xác định cấu trúc {frame}.",
                "source": {"kind": "structure_break", "time": None, "label": "none"},
            }
        )
    sweeps = [
        level
        for level in payload.get("levels") or []
        if level.get("state") == "rejected" and level.get("event_time") is not None
    ]
    if sweeps:
        last_sweep = max(sweeps, key=lambda level: level["event_time"])
        sentences.append(
            {
                "text": (
                    f"Quét {last_sweep['label']} {_num(float(last_sweep['price']))} rồi đóng cửa "
                    f"quay lại lúc {_time(int(last_sweep['event_time']))}."
                ),
                "source": {"kind": "liquidity_sweep", "time": int(last_sweep["event_time"])},
            }
        )
    vwap = (payload.get("lines") or {}).get("session_vwap") or []
    price = ((overview or {}).get("candle") or {}).get("price")
    if vwap and price is not None:
        last_vwap = float(vwap[-1]["value"])
        side = "trên" if price >= last_vwap else "dưới"
        sentences.append(
            {
                "text": f"Giá {_num(float(price))} đang {side} VWAP ngày {_num(last_vwap)}.",
                "source": {"kind": "vwap", "time": int(vwap[-1]["time"])},
            }
        )
    if overview:
        consensus = ((overview.get("mtf") or {}).get("consensus") or {}).get("label")
        if consensus:
            sentences.append(
                {"text": f"Đa khung: {consensus}.", "source": {"kind": "mtf", "time": None}}
            )
        sessions = [item["label"] for item in overview.get("sessions") or []]
        killzones = [item["label"] for item in overview.get("killzones") or []]
        session_text = (
            f"Đang trong {', '.join(sessions)}" if sessions else "Ngoài phiên Á/Âu/Mỹ"
        ) + (f"; trong {', '.join(killzones)}." if killzones else "; ngoài killzone ICT.")
        sentences.append({"text": session_text, "source": {"kind": "session", "time": None}})
        mark = overview.get("mark") or {}
        if (
            mark.get("funding_rate_pct") is not None
            and mark.get("funding_seconds_left") is not None
        ):
            rate = f"{float(mark['funding_rate_pct']):.4f}".replace(".", ",")
            minutes = int(mark["funding_seconds_left"]) // 60
            sentences.append(
                {
                    "text": f"Funding dự kiến {rate}%, còn {minutes} phút.",
                    "source": {"kind": "funding", "time": None},
                }
            )
        positions = overview.get("positions") or []
        if positions:
            sentences.append(
                {
                    "text": f"Bot đang giữ {len(positions)} vị thế PAPER trên mã này.",
                    "source": {"kind": "positions", "time": None},
                }
            )
    active = ((payload.get("ict") or {}).get("counts") or {}).get("active")
    if active:
        sentences.append(
            {
                "text": f"Có {active} vùng ICT còn hiệu lực trên khung đang xem.",
                "source": {"kind": "ict", "time": None},
            }
        )
    return sentences
