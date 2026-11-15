"""Unit test SIG-7: dữ liệu chart thị trường và trang/API ``/admin/chart``."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
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
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
OTHER_BOT = UUID("88228dd5-7d8e-418f-9a34-059e851b57d3")
MEMBERSHIP = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")


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
