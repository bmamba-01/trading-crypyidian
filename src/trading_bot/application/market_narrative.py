"""Nhận định thị trường theo mẫu câu (CK-10, MI-6 rút gọn) — không dùng LLM, không tự sinh số.

Mỗi câu ghép từ một sự kiện/số liệu đã tính sẵn trong payload chart (SIG-1, CK-3) hoặc overview
(CK-1/CK-2) và trả kèm ``source`` (loại + thời điểm/khóa) để truy ngược. Thiếu dữ liệu thì nói rõ
"chưa đủ" thay vì đoán. Giờ hiển thị theo giờ Việt Nam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.domain.strategy.setup_progress import format_setup_missing_vi

NARRATIVE_VERSION = "ck10-narrative-v1"
DECISION_NARRATIVE_VERSION = "mi6-decision-narrative-v1"
_VN = ZoneInfo("Asia/Ho_Chi_Minh")
_TREND = {"up": "TĂNG", "down": "GIẢM"}
_DIR_VI = {
    "long": "MUA (LONG)",
    "short": "BÁN (SHORT)",
    "LONG": "MUA (LONG)",
    "SHORT": "BÁN (SHORT)",
}


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


def _format_iso_vn(value: object) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_VN).strftime("%H:%M %d/%m")


def decision_narrative(
    decision: Mapping[str, Any] | None,
    *,
    setup: Mapping[str, Any] | None = None,
    card: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Tường thuật tự động từ sự kiện quyết định bot bằng template (MI-6, không dùng LLM).

    Mỗi câu chỉ đọc số liệu/lý do đã lưu từ ``decision``, ``setup`` (MI-2) và ``card`` (SIG-3),
    kèm ``source`` truy ngược về ``decision_id``, ``evaluated_at`` và ``reason_code``.
    """
    if not decision:
        return [
            {
                "text": "Chưa có quyết định bot nào được ghi nhận.",
                "source": {
                    "kind": "decision_missing",
                    "decision_id": None,
                    "evaluated_at": None,
                    "reason_code": None,
                },
            }
        ]

    decision_id = decision.get("decision_id")
    evaluated_at = decision.get("evaluated_at")
    time_label = _format_iso_vn(evaluated_at) or "chưa rõ thời điểm"
    status = str(decision.get("status") or "UNKNOWN").upper()
    reason_code = str(decision.get("reason_code") or "UNKNOWN")
    detail_vi = str(decision.get("detail_vi") or "").strip().rstrip(".")
    release_id = str(decision.get("strategy_release_id") or "—")
    base_source = {
        "decision_id": str(decision_id) if decision_id is not None else None,
        "evaluated_at": str(evaluated_at) if evaluated_at is not None else None,
        "reason_code": reason_code,
    }

    sentences: list[dict[str, Any]] = []
    candidate = (
        decision.get("candidate") if isinstance(decision.get("candidate"), Mapping) else None
    )
    direction_raw = (
        (candidate.get("direction") if candidate else None)
        or (card.get("direction") if card else None)
        or decision.get("direction")
    )
    direction_vi = _DIR_VI.get(str(direction_raw), "") if direction_raw else ""

    if status == "CANDIDATE":
        dir_part = f" {direction_vi}" if direction_vi else ""
        detail_part = f" — {detail_vi}" if detail_vi else ""
        sentences.append(
            {
                "text": (
                    f"Lúc {time_label}, bot ghi nhận ĐỦ ĐIỀU KIỆN{dir_part} "
                    f"({release_id}, mã {reason_code}){detail_part}."
                ),
                "source": {**base_source, "kind": "decision_candidate"},
            }
        )
    else:
        detail_part = f" — {detail_vi}" if detail_vi else ""
        sentences.append(
            {
                "text": (
                    f"Lúc {time_label}, bot ĐỨNG NGOÀI ({status}, {release_id}, "
                    f"mã {reason_code}){detail_part}."
                ),
                "source": {**base_source, "kind": "decision_abstain"},
            }
        )

    if setup:
        raw_req = setup.get("required")
        req: Mapping[str, Any] = raw_req if isinstance(raw_req, Mapping) else {}
        passed = req.get("passed", 0)
        total = req.get("total", 0)
        missing_vi = format_setup_missing_vi(setup)
        if missing_vi:
            cf = setup.get("counterfactual")
            cond_key = cf.get("key") if isinstance(cf, Mapping) else None
            sentences.append(
                {
                    "text": f"Tiến độ điều kiện {passed}/{total}: {missing_vi}.",
                    "source": {
                        **base_source,
                        "kind": "setup_counterfactual",
                        "condition_key": cond_key,
                    },
                }
            )
        elif total:
            sentences.append(
                {
                    "text": f"Checklist đạt {passed}/{total} điều kiện bắt buộc.",
                    "source": {**base_source, "kind": "decision_checklist"},
                }
            )
    else:
        checklist = decision.get("checklist")
        items = (
            list(checklist.get("items") or [])
            if isinstance(checklist, Mapping)
            else (list(checklist) if isinstance(checklist, Sequence) else [])
        )
        if items:
            passed = sum(
                1 for item in items if isinstance(item, Mapping) and item.get("passed") is True
            )
            sentences.append(
                {
                    "text": f"Checklist: {passed}/{len(items)} điều kiện đạt.",
                    "source": {**base_source, "kind": "decision_checklist"},
                }
            )

    if card:
        raw_zone = card.get("entry_zone")
        zone: Mapping[str, Any] = raw_zone if isinstance(raw_zone, Mapping) else {}
        low, high = zone.get("low"), zone.get("high")
        stop = card.get("stop")
        raw_conf = card.get("confidence")
        confidence: Mapping[str, Any] = raw_conf if isinstance(raw_conf, Mapping) else {}
        conf_label = confidence.get("label") or card.get("confidence_label") or "Chưa hiệu chuẩn"
        if low is not None and stop is not None:
            zone_text = (
                f"{_num(float(low))}–{_num(float(high))}"
                if high is not None and high != low
                else _num(float(low))
            )
            sentences.append(
                {
                    "text": (
                        f"Kế hoạch tham chiếu: vùng vào {zone_text}, "
                        f"SL {_num(float(stop))}, độ tin cậy: {conf_label}."
                    ),
                    "source": {**base_source, "kind": "signal_card"},
                }
            )

    return sentences


def format_decision_narrative_text(
    sentences: Sequence[Mapping[str, Any]], *, max_sentences: int = 3
) -> str:
    """Ghép các câu tường thuật MI-6 thành đoạn văn bản thuần cho Telegram/UI."""
    parts = [str(item["text"]) for item in list(sentences)[:max_sentences] if item.get("text")]
    return " ".join(parts)
