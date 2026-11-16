"""CK-10: câu nhận định theo mẫu, mỗi câu truy ngược về sự kiện/số liệu nguồn (không LLM)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trading_bot.application.market_narrative import NARRATIVE_VERSION, market_narrative

T0 = int(datetime(2026, 11, 16, 2, 0, tzinfo=UTC).timestamp())

PAYLOAD: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "timeframe": "5m",
    "structure": {
        "trend": "up",
        "breaks": [
            {"time": T0, "kind": "CHOCH", "direction": "bullish", "level": 100.0},
            {"time": T0 + 600, "kind": "BOS", "direction": "bullish", "level": 101.5},
        ],
    },
    "levels": [
        {
            "label": "Đỉnh phiên London",
            "state": "rejected",
            "event_time": T0 + 300,
            "price": 102.0,
            "side": "high",
        },
    ],
    "lines": {"session_vwap": [{"time": T0, "value": 100.2}, {"time": T0 + 600, "value": 100.8}]},
    "ict": {"counts": {"active": 3}},
}
OVERVIEW: dict[str, Any] = {
    "candle": {"price": 101.0, "price_source": "quote_mid"},
    "mtf": {"consensus": {"label": "Khung chính 4H TĂNG · 2/3 khung có xu hướng cùng chiều"}},
    "sessions": [{"key": "london", "label": "Phiên London"}],
    "killzones": [],
    "mark": {"funding_rate_pct": 0.01, "funding_seconds_left": 3000},
    "positions": [],
}


def test_sentences_cite_their_sources() -> None:
    sentences = market_narrative(PAYLOAD, OVERVIEW)
    assert NARRATIVE_VERSION == "ck10-narrative-v1"
    by_key = {item["source"]["kind"]: item for item in sentences}
    bos = by_key["structure_break"]
    assert bos["text"] == "Cấu trúc 5M TĂNG: BOS tăng phá 101,5 lúc 09:10 16/11."
    assert bos["source"]["time"] == T0 + 600
    sweep = by_key["liquidity_sweep"]
    assert "Quét Đỉnh phiên London 102" in sweep["text"]
    assert by_key["vwap"]["text"] == "Giá 101 đang trên VWAP ngày 100,8."
    assert by_key["mtf"]["text"].startswith("Đa khung: Khung chính 4H TĂNG")
    assert by_key["session"]["text"] == "Đang trong Phiên London; ngoài killzone ICT."
    assert by_key["funding"]["text"] == "Funding dự kiến 0,0100%, còn 50 phút."
    assert by_key["ict"]["text"] == "Có 3 vùng ICT còn hiệu lực trên khung đang xem."
    # Mọi câu đều có nguồn để truy ngược.
    assert all(item["source"]["kind"] for item in sentences)


def test_missing_inputs_produce_explicit_unknowns() -> None:
    sentences = market_narrative({"symbol": "BTCUSDT", "timeframe": "5m"}, None)
    assert [item["text"] for item in sentences] == ["Chưa đủ nến đã đóng để xác định cấu trúc 5M."]
