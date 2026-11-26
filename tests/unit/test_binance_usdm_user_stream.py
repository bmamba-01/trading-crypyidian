"""Unit tests cho vòng đời User Data Stream & ListenKey (WP-03C.1 / CRY-292)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    BinanceHttpResponse,
    DemoApiCredentials,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.user_stream import (
    BinanceUsdMListenKeyClient,
    StreamHealthState,
    UserStreamSupervisor,
)
from trading_bot.domain.execution.venue_event_reducer import VenueEventReducer


class _FakeListenKeyTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._seq = 0
        self.fail_next_put = False

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse:
        self.calls.append((request.method, request.url))
        if request.method == "POST":
            self._seq += 1
            body = json.dumps({"listenKey": f"SECRET_LISTEN_KEY_{self._seq}"}).encode("utf-8")
            return BinanceHttpResponse(200, (), body)
        if request.method == "PUT":
            if self.fail_next_put:
                self.fail_next_put = False
                return BinanceHttpResponse(503, (), b'{"msg":"Service Unavailable."}')
            return BinanceHttpResponse(200, (), b"{}")
        if request.method == "DELETE":
            return BinanceHttpResponse(200, (), b"{}")
        return BinanceHttpResponse(404, (), b"{}")


def test_listen_key_lifecycle_and_secret_redaction() -> None:
    """Create/renew/close listenKey hoạt động chuẩn và không lộ secret/listenKey."""
    now = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
    transport = _FakeListenKeyTransport()
    client = BinanceUsdMListenKeyClient(
        target=BinanceDemoTarget(account_id="acc-1", credential_ref="ref-1"),
        credentials=DemoApiCredentials(
            workspace_id="ws-1",
            account_id="acc-1",
            credential_ref="ref-1",
            api_key="SUPER_SECRET_API_KEY_292",
            api_secret="SUPER_SECRET_HMAC_292",
        ),
        transport=transport,
        clock=lambda: now,
    )
    reducer = VenueEventReducer()
    sup = UserStreamSupervisor(
        workspace_id="ws-1",
        account_id="acc-1",
        listen_key_port=client,
        reducer=reducer,
        clock=lambda: now,
    )

    lease = sup.start()
    assert sup.entry_gate_open is False
    assert sup.reconcile_required is True
    assert "SECRET_LISTEN_KEY_1" not in repr(lease)
    assert "SECRET_LISTEN_KEY_1" not in lease.redacted_ws_url
    assert lease.redacted_ws_url == "wss://demo-fstream.binance.com/private/ws/[REDACTED]"

    telemetry = json.dumps(sup.telemetry_snapshot())
    assert "SECRET_LISTEN_KEY_1" not in telemetry
    assert "SUPER_SECRET_API_KEY_292" not in telemetry
    assert "SUPER_SECRET_HMAC_292" not in telemetry

    # Mở cổng sau khi reconcile hoàn tất
    assert sup.on_reconcile_completed(matched=True) is True
    assert sup.entry_gate_open is True
    assert sup.state is StreamHealthState.CONNECTED

    # Gia hạn sau 31 phút (trước hạn 60 phút)
    now = now + timedelta(minutes=31)
    sup.record_heartbeat()
    sup.tick()
    assert [m for m, _ in transport.calls] == ["POST", "PUT"]
    assert sup.entry_gate_open is True

    # Đóng stream gọi DELETE và xóa khỏi bộ nhớ
    assert sup.stop() is True
    assert [m for m, _ in transport.calls] == ["POST", "PUT", "DELETE"]
    assert sup.entry_gate_open is False


def test_user_stream_24h_rotation_heartbeat_expiry_and_disconnect_gates() -> None:
    """Xoay 24h, heartbeat timeout, listenKeyExpired & disconnect đều đóng gate + đòi reconcile."""
    now = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
    transport = _FakeListenKeyTransport()
    client = BinanceUsdMListenKeyClient(
        target=BinanceDemoTarget(account_id="acc-2", credential_ref="ref-2"),
        credentials=DemoApiCredentials(
            workspace_id="ws-2",
            account_id="acc-2",
            credential_ref="ref-2",
            api_key="key-2",
            api_secret="sec-2",
        ),
        transport=transport,
        clock=lambda: now,
    )
    sup = UserStreamSupervisor(
        workspace_id="ws-2",
        account_id="acc-2",
        listen_key_port=client,
        reducer=VenueEventReducer(),
        clock=lambda: now,
    )

    sup.start()
    assert sup.on_reconcile_completed(matched=True) is True

    # 1. Xoay kết nối chủ động ở 23h30m
    now = now + timedelta(hours=23, minutes=30)
    sup.record_heartbeat()
    sup.tick()
    assert sup.rotation_count == 1
    assert sup.entry_gate_open is False
    assert sup.reconcile_required is True
    assert sup.on_reconcile_completed(matched=True) is True

    # 2. Heartbeat timeout (> 90s) -> đóng gate
    now = now + timedelta(seconds=120)
    sup.tick()
    assert sup.entry_gate_open is False
    assert sup.state is StreamHealthState.RECONCILE_REQUIRED
    sup.record_heartbeat()
    assert sup.on_reconcile_completed(matched=True) is True

    # 3. Nhận listenKeyExpired -> đóng gate, không cho mở lại trước khi tạo key mới
    sup.on_raw_message({"e": "listenKeyExpired", "E": 1_730_000_000_000})
    assert sup.state is StreamHealthState.EXPIRED
    assert sup.entry_gate_open is False
    assert sup.on_reconcile_completed(matched=True) is False

    # 4. Tạo lại stream + reconcile -> mở lại, rồi gặp LIQUIDATION -> đóng gate ngay
    sup.start()
    assert sup.on_reconcile_completed(matched=True) is True
    sup.on_raw_message(
        {
            "e": "ACCOUNT_UPDATE",
            "E": 1_730_000_001_000,
            "T": 1_730_000_001_000,
            "a": {
                "m": "LIQUIDATION",
                "B": [{"a": "USDT", "wb": "900", "cw": "900"}],
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "0",
                        "ep": "0",
                        "up": "0",
                        "mt": "cross",
                        "iw": "0",
                        "ps": "BOTH",
                    }
                ],
            },
        }
    )
    assert sup.external_activity_detected is True
    assert sup.entry_gate_open is False
    assert sup.reconcile_required is True
