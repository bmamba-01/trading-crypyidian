"""Vòng đời User Data Stream & ListenKey cho Binance USDⓈ-M (WP-03C.1 / CRY-292).

Nguyên tắc bảo mật và an toàn:
- `listenKey` là ranh giới mang credential, chỉ giữ trong bộ nhớ (`repr=False`),
  không bao giờ xuất hiện trong log, exception hay telemetry.
- Gia hạn `listenKey` định kỳ trước hạn 60 phút (`PUT /fapi/v1/listenKey`).
- Xoay kết nối WebSocket chủ động trước giới hạn 24 giờ của Binance USDⓈ-M.
- Mọi sự kiện mất kết nối, heartbeat timeout, `listenKeyExpired`, hoặc phát hiện
  `LIQUIDATION`/hoạt động ngoài luồng đều đóng ngay `entry_gate_open=False` và
  yêu cầu đối soát (`reconcile_required=True`) trước khi mở lại cổng.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    DemoApiCredentials,
    SignedHttpTransport,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.domain.execution.venue_event_reducer import (
    CanonicalVenueEventEnvelope,
    VenueEventReducer,
    VenueEventType,
    normalize_user_stream_payload,
)

_LISTEN_KEY_PATH = "/fapi/v1/listenKey"
_DEFAULT_RENEW_INTERVAL = timedelta(minutes=30)
_DEFAULT_KEY_TTL = timedelta(minutes=60)
_DEFAULT_ROTATION_AGE = timedelta(hours=23, minutes=30)
_DEFAULT_HEARTBEAT_TIMEOUT = timedelta(seconds=90)


class UserStreamError(RuntimeError):
    """Lỗi sanitized của User Data Stream; không bao giờ chứa listenKey hoặc API key."""


class StreamHealthState(StrEnum):
    """Trạng thái sức khỏe vòng đời kết nối User Data Stream."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    ROTATING = "ROTATING"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class ListenKeyLease:
    """Thông tin phiên listenKey giữ hoàn toàn trong bộ nhớ (ẩn khỏi repr)."""

    listen_key: str = field(repr=False)
    ws_base_url: str
    created_at: datetime
    renewed_at: datetime
    expires_at: datetime

    @property
    def ws_connect_url(self) -> str:
        """URL WebSocket nội bộ chứa listenKey; chỉ dùng tại lớp socket, cấm log."""
        return f"{self.ws_base_url.rstrip('/')}/private/ws/{self.listen_key}"

    @property
    def redacted_ws_url(self) -> str:
        """URL an toàn đã khử listenKey dùng cho log và telemetry."""
        return f"{self.ws_base_url.rstrip('/')}/private/ws/[REDACTED]"


class ListenKeyPort(Protocol):
    """Cổng REST quản lý vòng đời listenKey (`/fapi/v1/listenKey`)."""

    def create_listen_key(self) -> ListenKeyLease: ...

    def renew_listen_key(self, lease: ListenKeyLease) -> ListenKeyLease: ...

    def close_listen_key(self, lease: ListenKeyLease) -> bool: ...


class BinanceUsdMListenKeyClient(ListenKeyPort):
    """Client REST quản lý `POST/PUT/DELETE /fapi/v1/listenKey` trên host Demo đã ghim."""

    def __init__(
        self,
        *,
        target: BinanceDemoTarget,
        credentials: DemoApiCredentials,
        transport: SignedHttpTransport,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = target
        self._credentials = credentials
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def create_listen_key(self) -> ListenKeyLease:
        """Khởi tạo listenKey mới qua `POST /fapi/v1/listenKey`."""
        now = self._clock()
        req = BinanceHttpRequest(
            method="POST",
            url=f"{self._target.rest_base_url}{_LISTEN_KEY_PATH}",
            headers=(
                ("X-MBX-APIKEY", self._credentials.api_key),
                ("User-Agent", "CrypidianBot"),
            ),
            body=b"",
        )
        try:
            res = self._transport.send(req)
        except Exception as exc:
            raise UserStreamError("Không thể kết nối tạo listenKey.") from exc
        if res.status_code != 200:
            raise UserStreamError(f"Tạo listenKey thất bại với HTTP {res.status_code}.")
        try:
            payload = json.loads(res.body.decode("utf-8"))
            key = payload.get("listenKey")
        except (UnicodeError, ValueError) as exc:
            raise UserStreamError("Phản hồi tạo listenKey không phải JSON hợp lệ.") from exc
        if not isinstance(key, str) or not key.strip():
            raise UserStreamError("Phản hồi tạo listenKey thiếu trường listenKey hợp lệ.")
        return ListenKeyLease(
            listen_key=key,
            ws_base_url=self._target.private_ws_base_url,
            created_at=now,
            renewed_at=now,
            expires_at=now + _DEFAULT_KEY_TTL,
        )

    def renew_listen_key(self, lease: ListenKeyLease) -> ListenKeyLease:
        """Gia hạn listenKey thêm 60 phút qua `PUT /fapi/v1/listenKey`."""
        now = self._clock()
        req = BinanceHttpRequest(
            method="PUT",
            url=f"{self._target.rest_base_url}{_LISTEN_KEY_PATH}",
            headers=(
                ("X-MBX-APIKEY", self._credentials.api_key),
                ("User-Agent", "CrypidianBot"),
            ),
            body=b"",
        )
        try:
            res = self._transport.send(req)
        except Exception as exc:
            raise UserStreamError("Không thể kết nối gia hạn listenKey.") from exc
        if res.status_code != 200:
            raise UserStreamError(f"Gia hạn listenKey thất bại với HTTP {res.status_code}.")
        return replace(lease, renewed_at=now, expires_at=now + _DEFAULT_KEY_TTL)

    def close_listen_key(self, lease: ListenKeyLease) -> bool:
        """Đóng stream và hủy hiệu lực listenKey qua `DELETE /fapi/v1/listenKey`."""
        req = BinanceHttpRequest(
            method="DELETE",
            url=f"{self._target.rest_base_url}{_LISTEN_KEY_PATH}",
            headers=(
                ("X-MBX-APIKEY", self._credentials.api_key),
                ("User-Agent", "CrypidianBot"),
            ),
            body=b"",
        )
        try:
            res = self._transport.send(req)
        except Exception:
            return False
        return res.status_code == 200


class UserStreamSupervisor:
    """Giám sát vòng đời User Data Stream, 24h rotation, heartbeat và cổng entry."""

    def __init__(
        self,
        *,
        workspace_id: str,
        account_id: str,
        listen_key_port: ListenKeyPort,
        reducer: VenueEventReducer,
        clock: Callable[[], datetime] | None = None,
        renew_interval: timedelta = _DEFAULT_RENEW_INTERVAL,
        rotation_age: timedelta = _DEFAULT_ROTATION_AGE,
        heartbeat_timeout: timedelta = _DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> None:
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._listen_key_port = listen_key_port
        self._reducer = reducer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._renew_interval = renew_interval
        self._rotation_age = rotation_age
        self._heartbeat_timeout = heartbeat_timeout

        self._lease: ListenKeyLease | None = None
        self._state: StreamHealthState = StreamHealthState.DISCONNECTED
        self._entry_gate_open: bool = False
        self._reconcile_required: bool = True
        self._connected_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        self._rotation_count: int = 0
        self._events_processed: int = 0
        self._external_activity_detected: bool = False

    @property
    def state(self) -> StreamHealthState:
        return self._state

    @property
    def entry_gate_open(self) -> bool:
        return self._entry_gate_open

    @property
    def reconcile_required(self) -> bool:
        return self._reconcile_required

    @property
    def external_activity_detected(self) -> bool:
        return self._external_activity_detected

    @property
    def rotation_count(self) -> int:
        return self._rotation_count

    def start(self) -> ListenKeyLease:
        """Khởi động stream: tạo listenKey nhưng vẫn giữ cổng đóng cho tới khi reconcile xong."""
        now = self._clock()
        self._lease = self._listen_key_port.create_listen_key()
        self._connected_at = now
        self._last_heartbeat_at = now
        self._state = StreamHealthState.RECONCILE_REQUIRED
        self._reconcile_required = True
        self._entry_gate_open = False
        return self._lease

    def record_heartbeat(self) -> None:
        """Ghi nhận pong/heartbeat từ kết nối WebSocket."""
        self._last_heartbeat_at = self._clock()

    def on_reconcile_completed(self, *, matched: bool) -> bool:
        """Mở cổng entry CHỈ khi reconcile đạt và kết nối stream đang khỏe."""
        if not matched or self._lease is None or self._state is StreamHealthState.DISCONNECTED:
            self._entry_gate_open = False
            self._reconcile_required = True
            return False
        if self._state is StreamHealthState.EXPIRED:
            self._entry_gate_open = False
            return False
        self._reconcile_required = False
        self._external_activity_detected = False
        self._state = StreamHealthState.CONNECTED
        self._entry_gate_open = True
        return True

    def on_disconnect(self) -> None:
        """Đóng ngay cổng entry và yêu cầu reconcile khi mất kết nối stream."""
        self._state = StreamHealthState.DISCONNECTED
        self._entry_gate_open = False
        self._reconcile_required = True

    def tick(self) -> StreamHealthState:
        """Kiểm tra định kỳ: heartbeat timeout, xoay kết nối 24h và gia hạn listenKey 60m."""
        now = self._clock()
        if self._lease is None or self._connected_at is None:
            self._entry_gate_open = False
            self._reconcile_required = True
            return self._state

        # 1. Kiểm tra heartbeat timeout
        if (
            self._last_heartbeat_at is not None
            and (now - self._last_heartbeat_at) > self._heartbeat_timeout
        ):
            self._state = StreamHealthState.RECONCILE_REQUIRED
            self._entry_gate_open = False
            self._reconcile_required = True
            return self._state

        # 2. Xoay kết nối chủ động trước giới hạn 24 giờ của Binance
        if (now - self._connected_at) >= self._rotation_age:
            self._state = StreamHealthState.ROTATING
            self._entry_gate_open = False
            self._reconcile_required = True
            try:
                self._lease = self._listen_key_port.create_listen_key()
                self._connected_at = now
                self._last_heartbeat_at = now
                self._rotation_count += 1
                self._state = StreamHealthState.RECONCILE_REQUIRED
            except UserStreamError:
                self._state = StreamHealthState.DISCONNECTED
            return self._state

        # 3. Gia hạn listenKey định kỳ trước hạn 60 phút
        if (now - self._lease.renewed_at) >= self._renew_interval:
            try:
                self._lease = self._listen_key_port.renew_listen_key(self._lease)
            except UserStreamError:
                self._state = StreamHealthState.RECONCILE_REQUIRED
                self._entry_gate_open = False
                self._reconcile_required = True
        return self._state

    def on_raw_message(self, payload: object) -> CanonicalVenueEventEnvelope:
        """Nhận message thô từ stream, chuẩn hóa và áp dụng vào `VenueEventReducer`."""
        now = self._clock()
        self._last_heartbeat_at = now
        envelope = normalize_user_stream_payload(
            payload,
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            received_at=now,
        )
        self._events_processed += 1

        if envelope.event_type is VenueEventType.LISTEN_KEY_EXPIRED:
            self._state = StreamHealthState.EXPIRED
            self._entry_gate_open = False
            self._reconcile_required = True
            return envelope

        if envelope.is_liquidation_or_external:
            self._external_activity_detected = True
            self._entry_gate_open = False
            self._reconcile_required = True
            self._state = StreamHealthState.RECONCILE_REQUIRED

        self._reducer.apply(envelope)
        return envelope

    def stop(self) -> bool:
        """Đóng stream, hủy listenKey trên sàn và xóa khỏi bộ nhớ."""
        self._entry_gate_open = False
        self._state = StreamHealthState.DISCONNECTED
        self._reconcile_required = True
        if self._lease is None:
            return True
        closed = self._listen_key_port.close_listen_key(self._lease)
        self._lease = None
        self._connected_at = None
        return closed

    def telemetry_snapshot(self) -> dict[str, Any]:
        """Trả về ảnh chụp telemetry đã khử 100% listenKey và API key."""
        now = self._clock()
        age_sec = (
            int((now - self._connected_at).total_seconds()) if self._connected_at is not None else 0
        )
        return {
            "workspace_id": self._workspace_id,
            "account_id": self._account_id,
            "state": self._state.value,
            "entry_gate_open": self._entry_gate_open,
            "reconcile_required": self._reconcile_required,
            "external_activity_detected": self._external_activity_detected,
            "ws_url_redacted": (self._lease.redacted_ws_url if self._lease is not None else None),
            "connection_age_seconds": age_sec,
            "rotation_count": self._rotation_count,
            "events_processed": self._events_processed,
        }
