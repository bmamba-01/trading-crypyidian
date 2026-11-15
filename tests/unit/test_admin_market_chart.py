"""Unit test SIG-7: dữ liệu chart thị trường và trang/API ``/admin/chart``."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.application.bot_reporting import BotReportError
from trading_bot.application.market_chart import (
    NOTICE_NO_TAKER,
    NOTICE_SESSIONS_HIDDEN,
    build_market_chart_payload,
)
from trading_bot.application.market_overview import build_market_overview
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
OTHER_BOT = UUID("88228dd5-7d8e-418f-9a34-059e851b57d3")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")
OVERVIEW_NOW = datetime(2026, 11, 16, 9, 2, 30, tzinfo=UTC)


def _bars(count: int, *, start: datetime, step: timedelta, taker: bool = False) -> list[dict]:
    """Nến hình sin có xu hướng nhẹ để sinh swing/BOS; dữ liệu tổng hợp cho test."""
    output = []
    for index in range(count):
        base = 100 + 4 * math.sin(index / 9) + index * 0.01
        close = base + 0.3 * math.sin(index / 2)
        bar = {
            "time": int((start + step * index).timestamp()),
            "open": base,
            "high": max(base, close) + 0.4,
            "low": min(base, close) - 0.4,
            "close": close,
            "volume": 10.0 + (index % 5),
        }
        if taker:
            bar["taker_buy_volume"] = bar["volume"] * (0.6 if close > base else 0.4)
        output.append(bar)
    return output


START = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)


def test_payload_has_overlays_from_domain_features_and_hides_missing_taker() -> None:
    bars = _bars(576, start=START, step=timedelta(minutes=5))  # 2 ngày 5m
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="5m", bars=bars)
    assert len(payload["bars"]) == 576
    assert payload["lines"]["ema_20"]
    assert payload["lines"]["session_vwap"]
    assert payload["panes"]["macd"]["histogram"]
    assert payload["panes"]["cvd"] is None
    assert payload["structure"]["swings"]
    assert payload["structure"]["breaks"]
    assert {item["key"] for item in payload["sessions"]} == {"asia", "london", "new_york"}
    assert payload["levels"]
    assert all(
        level["state"] in {"armed", "rejected", "accepted", "expired"}
        for level in payload["levels"]
    )
    labels = [profile["label"] for profile in payload["volume_profiles"]]
    assert labels == ["Ngày UTC trước", "Ngày UTC"]
    assert all(not profile["has_split"] for profile in payload["volume_profiles"])
    assert all(b["buy"] is None for b in payload["volume_profiles"][0]["bins"])
    assert NOTICE_NO_TAKER in payload["notices"]
    # Volume tô theo chiều nến khi không có delta; không bịa delta.
    assert all(item["delta"] is None for item in payload["volume"])
    # Swing chỉ có từ thời điểm xác nhận (nến thứ 3 bên phải).
    for swing in payload["structure"]["swings"]:
        assert swing["confirmed_time"] - swing["time"] == 3 * 300


def test_payload_with_taker_buy_has_delta_cvd_and_split_profile() -> None:
    bars = _bars(300, start=START, step=timedelta(minutes=5), taker=True)
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="5m", bars=bars)
    assert payload["panes"]["cvd"]
    assert payload["volume"][0]["delta"] is not None
    assert payload["volume_profiles"][-1]["has_split"] is True
    assert "đang hình thành" in payload["volume_profiles"][-1]["label"]
    assert NOTICE_NO_TAKER not in payload["notices"]


def test_payload_hides_sessions_on_4h_and_handles_empty() -> None:
    bars = _bars(120, start=START, step=timedelta(hours=4))
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="4h", bars=bars)
    assert payload["sessions"] == []
    assert payload["levels"] == []
    assert payload["volume_profiles"] == []
    assert NOTICE_SESSIONS_HIDDEN in payload["notices"]
    empty = build_market_chart_payload(symbol="BTCUSDT", timeframe="1h", bars=[])
    assert empty["bars"] == []
    assert empty["notices"]


SIGNAL_VIEW = {
    "latest_decision": {
        "decision_id": "d1",
        "status": "ABSTAIN",
        "reason_code": "H01_ADX_BELOW_20",
        "detail_vi": "ADX14 1h phải đạt tối thiểu 20.",
        "evaluated_at": "2026-11-16T10:00:00+00:00",
        "strategy_release_id": "h01-v1",
        "gate_details": [],
        "checklist": [
            {
                "key": "adx",
                "label_vi": "ADX14 1h đủ mạnh",
                "actual": "12",
                "operator": ">=",
                "threshold": "20",
                "passed": False,
            },
        ],
    },
    "card": {
        "card_version": "sig3-card-v1",
        "symbol": "BTCUSDT",
        "direction": "short",
        "strategy_id": "H06",
        "strategy_release_id": "h06-v1",
        "reason_code": "H06_SHORT_ENTRY",
        "signal_at": "2026-11-16T09:00:00+00:00",
        "expires_at": "2026-11-16T09:15:00+00:00",
        "reference_price": 101.0,
        "entry_zone": {"low": 100.9, "high": 101.0},
        "stop": 104.0,
        "risk_per_unit": 3.1,
        "targets": [
            {
                "label": "Mốc 1R (tham chiếu)",
                "price": 97.8,
                "r_multiple": 1.0,
                "source": "r_ladder",
            },
            {"label": "Mục tiêu recipe", "price": 97.0, "r_multiple": 1.26, "source": "recipe"},
        ],
        "plan_source": "approved_plan",
        "checklist": [
            {
                "key": "extreme",
                "label_vi": "Z20 và RSI14 cùng vượt ngưỡng",
                "actual": "Z20 2.3",
                "operator": "vượt",
                "threshold": "Z > 2",
                "passed": True,
            },
        ],
        "checklist_passed": 1,
        "checklist_total": 1,
        "invalidation": ["Giá chạm SL 104 (dừng lỗ theo recipe/kế hoạch)."],
        "confidence": {"tier": "uncalibrated", "label": "Chưa hiệu chuẩn"},
        "decision_id": "d0",
        "evaluated_at": "2026-11-16T09:00:01+00:00",
        "is_expired": False,
    },
    "card_error": None,
    "unverified_decisions": 0,
}


class _AdminService:
    def __init__(self) -> None:
        self.bots = [
            {
                "id": str(BOT_ID),
                "name": "Bot BTC",
                "status": "ENABLED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            },
            {
                "id": str(OTHER_BOT),
                "name": "Bot khác",
                "status": "PAUSED",
                "account": {"mode": "PAPER", "status": "ACTIVE"},
            },
        ]

    async def list_bots(self, principal: Principal) -> list[dict[str, Any]]:
        return self.bots


class _Reporting:
    def __init__(
        self,
        bars: list[dict],
        error: BotReportError | None = None,
        series: dict[str, list[str]] | None = None,
    ) -> None:
        self.bars = bars
        self.error = error
        self.series = series if series is not None else {"BTCUSDT": ["1m", "5m", "1h"]}
        self.calls: list[dict[str, Any]] = []
        self.overview_calls: list[dict[str, Any]] = []

    async def list_market_series(self, principal: Principal, *, bot_id: UUID) -> dict:
        if self.error is not None:
            raise self.error
        return self.series

    async def get_latest_signal(self, principal: Principal, *, bot_id: UUID) -> dict:
        return SIGNAL_VIEW

    async def get_market_bars(self, principal: Principal, **kwargs: Any) -> list[dict]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.bars

    async def get_market_overview(self, principal: Principal, **kwargs: Any) -> dict:
        self.overview_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return build_market_overview(
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
            now=OVERVIEW_NOW,
            bars_by_timeframe={kwargs["timeframe"]: self.bars},
            quote={
                "bid_price": Decimal("100.10"),
                "ask_price": Decimal("100.30"),
                "bid_quantity": Decimal("1"),
                "ask_quantity": Decimal("2"),
                "event_at": OVERVIEW_NOW - timedelta(seconds=1),
            },
            mark=None,
        )


def _client(reporting: _Reporting) -> TestClient:
    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP,
        user_id=USER_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=datetime.now(UTC),
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@crypidian.test"
    )
    app.state.bot_admin_service = _AdminService()
    app.state.bot_reporting_service = reporting
    return TestClient(app)


def _embedded_payload(html: str) -> dict:
    match = re.search(
        r'<script id="market-chart-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert match is not None
    return json.loads(match.group(1))


def test_chart_page_renders_stored_symbol_payload_and_attribution() -> None:
    reporting = _Reporting(_bars(200, start=START, step=timedelta(minutes=5)))
    response = _client(reporting).get("/admin/chart?timeframe=5m")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    assert 'href="/admin/chart"' in html  # link sidebar
    assert reporting.calls[0] == {
        "bot_id": BOT_ID,
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "limit": 600,
    }
    data = _embedded_payload(html)
    assert data["symbol"] == "BTCUSDT"
    assert data["timeframe"] == "5m"
    assert 'data-symbol="BTCUSDT"' in html
    # Khung chưa có nến được đánh dấu mờ, khung có dữ liệu thì không.
    assert re.search(r'timeframe=15m" class="mc-timeframe is-empty"', html)
    assert re.search(r'timeframe=1h" class="mc-timeframe"', html)
    assert "TradingView Lightweight Charts" in html
    assert "lightweight-charts-NOTICE.txt" in html
    inline = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", html)
    assert inline == []
    assert ' style="' not in html


def test_chart_page_without_stored_bars_shows_empty_state() -> None:
    response = _client(_Reporting([], series={})).get("/admin/chart")
    assert response.status_code == 200
    assert "chưa có nến đã đóng" in response.text
    assert "market-chart-data" not in response.text


def test_chart_page_rejects_bad_inputs() -> None:
    client = _client(_Reporting([]))
    assert client.get("/admin/chart?timeframe=10m").status_code == 422
    assert client.get("/admin/chart?bot_id=khong-phai-uuid").status_code == 422
    assert client.get(f"/admin/chart?bot_id={uuid4()}").status_code == 404
    assert client.get("/admin/chart?symbol=ETHUSDT").status_code == 404
    assert client.get("/admin/chart?symbol=btc%20usdt").status_code == 422


def test_chart_api_returns_unchanged_for_same_fingerprint() -> None:
    client = _client(_Reporting(_bars(120, start=START, step=timedelta(minutes=5))))
    url = f"/admin/api/chart?bot_id={BOT_ID}&symbol=BTCUSDT&timeframe=5m"
    first = client.get(url)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "ok"
    assert body["bars"]
    second = client.get(f"{url}&fp={body['fingerprint']}")
    assert second.json() == {"status": "unchanged", "fingerprint": body["fingerprint"]}


def test_chart_api_maps_report_permission_error_and_validates_input() -> None:
    denied = _client(
        _Reporting([], BotReportError(404, "REPORT_NOT_FOUND", "Không tìm thấy báo cáo bot."))
    )
    base = f"/admin/api/chart?bot_id={BOT_ID}"
    assert denied.get(f"{base}&symbol=BTCUSDT").status_code == 404
    client = _client(_Reporting([]))
    assert client.get(base).status_code == 422  # thiếu symbol
    assert client.get(f"{base}&symbol=BTC-USDT").status_code == 422
    assert client.get(f"{base}&symbol=BTCUSDT&limit=5").status_code == 422
    assert client.get(f"{base}&symbol=BTCUSDT&timeframe=2h").status_code == 422


def test_chart_page_shows_signal_card_checklist_and_overlay_payload() -> None:
    reporting = _Reporting(_bars(200, start=START, step=timedelta(minutes=5)))
    html = _client(reporting).get("/admin/chart?timeframe=5m").text
    assert "Thẻ tín hiệu gần nhất" in html
    assert "BÁN" in html
    assert "Chưa hiệu chuẩn" in html
    assert "Quyết định mới nhất: không vào lệnh" in html
    assert "H01_ADX_BELOW_20" in html
    data = _embedded_payload(html)
    assert data["signal_card"]["stop"] == 104.0


def test_chart_page_renders_live_header_and_mtf_matrix() -> None:
    reporting = _Reporting(_bars(200, start=START, step=timedelta(minutes=5)))
    html = _client(reporting).get("/admin/chart?timeframe=5m").text
    assert reporting.overview_calls == [{"bot_id": BOT_ID, "symbol": "BTCUSDT", "timeframe": "5m"}]
    assert 'id="mc-live-strip"' in html
    assert 'data-overview-url="/admin/api/chart/overview?' in html
    # Bid / ask (định dạng VI) hiện sẵn khi tải trang.
    assert "100,1" in html
    assert "100,3" in html
    assert 'id="mc-mtf-matrix"' in html
    for role in ("Xu hướng chính", "Xác nhận", "Động lượng", "Thời điểm"):
        assert role in html
    assert "Chưa có nến 4H đã đóng trong kho dữ liệu." in html
    assert ' style="' not in html


def test_overview_api_is_no_store_and_maps_errors() -> None:
    client = _client(_Reporting(_bars(120, start=START, step=timedelta(minutes=5))))
    url = f"/admin/api/chart/overview?bot_id={BOT_ID}&symbol=BTCUSDT&timeframe=5m"
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "ok"
    assert body["quote"]["bid"] == 100.10
    assert [cell["timeframe"] for cell in body["mtf"]["cells"]] == ["4h", "1h", "15m", "5m"]
    assert client.get(f"/admin/api/chart/overview?bot_id={BOT_ID}").status_code == 422
    assert client.get(f"{url.replace('5m', '2h')}").status_code == 422
    denied = _client(
        _Reporting([], BotReportError(404, "REPORT_NOT_FOUND", "Không tìm thấy báo cáo bot."))
    )
    assert denied.get(url).status_code == 404


def _with_gap(bars: list[dict]) -> list[dict]:
    """Chèn nhịp tăng mạnh 3 nến để tạo FVG tăng rõ ở cuối dữ liệu."""
    last = bars[-1]
    step = bars[-1]["time"] - bars[-2]["time"]
    price = last["close"]
    extra = []
    for offset, (open_, close) in enumerate(
        [(price, price + 3), (price + 3, price + 9), (price + 9, price + 10)], start=1
    ):
        extra.append(
            {
                "time": last["time"] + step * offset,
                "open": open_,
                "high": close + 0.2,
                "low": open_ - 0.2,
                "close": close,
                "volume": 12.0,
            }
        )
    return bars + extra


def test_payload_has_ict_layer_with_killzones_zones_and_counts() -> None:
    bars = _with_gap(_bars(300, start=START, step=timedelta(minutes=5)))
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="5m", bars=bars)
    ict = payload["ict"]
    assert payload["formula_versions"]["ict"] == "ck3-ict-v1"
    assert payload["formula_versions"]["killzone_table"] == "ck3-killzone-v1"
    # 5m: có dải killzone trong khoảng dữ liệu, tối đa 3 ngày gần nhất.
    assert ict["killzones"]
    first_bar, last_bar = bars[0]["time"], bars[-1]["time"] + 300
    assert all(first_bar <= item["end"] and item["start"] <= last_bar for item in ict["killzones"])
    assert {item["kind"] for item in ict["killzones"]} <= {"killzone", "silver_bullet", "macro"}
    # FVG tăng ở nhịp cuối: chưa bị chạm, xác nhận đúng nến thứ ba.
    fvg = [z for z in ict["zones"] if z["kind"] == "fvg" and z["direction"] == "bullish"]
    newest = max(fvg, key=lambda z: z["confirmed_time"])
    assert newest["confirmed_time"] == bars[-1]["time"]
    assert newest["start_time"] == bars[-3]["time"]
    assert newest["state"] == "active"
    assert newest["label"] == "FVG tăng"
    # Mọi vùng trong payload đều đã được xác nhận trước nến cuối, không vùng nào "tương lai".
    assert all(z["confirmed_time"] <= bars[-1]["time"] for z in ict["zones"])
    assert ict["counts"]["active"] == sum(
        1 for z in ict["zones"] if z["state"] in {"active", "partial", "inverted"}
    )
    assert len(ict["zones"]) <= 24


def test_payload_hides_killzones_above_15m_but_keeps_zones() -> None:
    bars = _with_gap(_bars(200, start=START, step=timedelta(hours=1)))
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="1h", bars=bars)
    assert payload["ict"]["killzones"] == []
    assert any(z["kind"] == "fvg" for z in payload["ict"]["zones"])


def test_payload_has_tpo_profile_naked_pocs_and_previous_value_area() -> None:
    bars = _bars(600, start=START, step=timedelta(minutes=5))  # 16/11, 17/11 đủ ngày + 2 giờ 18/11
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="5m", bars=bars)
    assert payload["formula_versions"]["tpo"] == "ck4-tpo-v1"
    for profile in payload["volume_profiles"]:
        tpo = profile["tpo"]
        assert tpo is not None
        assert tpo["val"] <= tpo["poc"] <= tpo["vah"]
        assert sum(row["count"] for row in tpo["rows"]) == tpo["total"]
    levels = payload["profile_levels"]
    kinds = {level["kind"] for level in levels}
    assert {"vah", "val"} <= kinds
    previous_day_end = int(datetime(2026, 11, 18, tzinfo=UTC).timestamp())
    for level in levels:
        assert level["start_time"] >= int(datetime(2026, 11, 17, tzinfo=UTC).timestamp())
        if level["kind"].startswith("npoc"):
            # Mức trần: không nến nào sau khi phiên đóng chạm tới giá.
            later = [bar for bar in bars if bar["time"] >= level["start_time"]]
            assert all(not (bar["low"] <= level["price"] <= bar["high"]) for bar in later)
        else:
            assert level["start_time"] == previous_day_end
            assert level["end_time"] == bars[-1]["time"] + 300


def test_payload_without_tpo_above_30m() -> None:
    bars = _bars(200, start=START, step=timedelta(hours=1))
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="1h", bars=bars)
    assert all(profile["tpo"] is None for profile in payload["volume_profiles"])
    assert all(not level["kind"].endswith("tpo") for level in payload["profile_levels"])


def test_payload_has_vsa_labels_and_non_repainting_zigzag() -> None:
    bars = _bars(400, start=START, step=timedelta(minutes=5))
    payload = build_market_chart_payload(symbol="BTCUSDT", timeframe="5m", bars=bars)
    assert payload["formula_versions"]["vsa"] == "ck5-vsa-v1"
    assert payload["formula_versions"]["zigzag"] == "ck5-zigzag-v1"
    times = {bar["time"] for bar in bars}
    for item in payload["vsa"]:
        assert item["time"] in times
        assert item["group"] in {"strength", "weakness"}
        assert item["label"]
    zig = payload["zigzag"]
    assert len(zig["points"]) >= 2
    kinds = [point["kind"] for point in zig["points"]]
    assert all(a != b for a, b in zip(kinds, kinds[1:], strict=False))  # đỉnh/đáy xen kẽ
    # Điểm đã chốt không đổi khi có thêm nến (streaming = batch).
    shorter = build_market_chart_payload(symbol="BTCUSDT", timeframe="5m", bars=bars[:300])
    final_cut = bars[299]["time"]
    assert [p for p in shorter["zigzag"]["points"] if p["final_time"] <= final_cut] == [
        p for p in zig["points"] if p["final_time"] <= final_cut
    ]
