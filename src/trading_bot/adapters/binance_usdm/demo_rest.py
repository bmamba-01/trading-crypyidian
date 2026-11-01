"""REST adapter USDⓈ-M Demo cho order thường, không có retry tự động."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Protocol
from urllib.parse import urlencode
from uuid import UUID

from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.submission_errors import (
    BinanceResponse,
    SubmissionFailure,
    classify_submission_failure,
)
from trading_bot.domain.execution.venue_orders import VenueOrderIntent, VenueOrderState

_ORDER_PATH = "/fapi/v1/order"
_CLIENT_ID_PATTERN = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")
_ORDER_STATUS = {
    "NEW": VenueOrderState.ACKNOWLEDGED,
    "PARTIALLY_FILLED": VenueOrderState.PARTIALLY_FILLED,
    "FILLED": VenueOrderState.FILLED,
    "CANCELED": VenueOrderState.CANCELED,
    "REJECTED": VenueOrderState.REJECTED,
    "EXPIRED": VenueOrderState.EXPIRED,
}


class BinanceAdapterError(RuntimeError):
    """Lỗi sanitized của adapter; không giữ request/response thô."""

    def __init__(
        self,
        message: str = "Binance Demo REST request không hoàn tất an toàn.",
        *,
        failure: SubmissionFailure | None = None,
        retry_not_before: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.retry_not_before = retry_not_before


class TransportNotSent(RuntimeError):
    """Transport xác nhận request chưa rời client; không mang thông tin nhạy cảm."""


@dataclass(frozen=True, slots=True)
class DemoApiCredentials:
    """Credential tạm thời được bind vào workspace, target và reference."""

    workspace_id: str = field(repr=False)
    account_id: str = field(repr=False)
    credential_ref: str = field(repr=False)
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("workspace_id", "account_id", "credential_ref", "api_key", "api_secret"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} phải là chuỗi không trống.")


@dataclass(frozen=True, slots=True)
class BinanceHttpRequest:
    """Request đã ký; trường có thể chứa credential/signature bị ẩn khỏi repr."""

    method: str
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class BinanceHttpResponse:
    """Response transport; headers và body không xuất hiện trong repr."""

    status_code: int
    headers: tuple[tuple[str, str], ...] | dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class SignedHttpTransport(Protocol):
    """Transport ký request bắt buộc tắt redirect và không ghi header/query nhạy cảm.

    Concrete transport còn phải giới hạn timeout, xác minh TLS, không dùng proxy từ
    môi trường và không chuyển tiếp API key/chữ ký tới URL khác. Slice hiện tại
    chưa cung cấp transport kết nối mạng.
    """

    def send(self, request: BinanceHttpRequest) -> BinanceHttpResponse: ...


@dataclass(frozen=True, slots=True)
class OrderAccepted:
    venue_order_id: str
    client_order_id: str
    symbol: str
    venue_status: VenueOrderState


@dataclass(frozen=True, slots=True)
class SubmissionFailed:
    failure: SubmissionFailure
    retry_not_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderFound:
    venue_order_id: str
    client_order_id: str
    symbol: str
    state: VenueOrderState
    cumulative_filled: Decimal
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OrderMissing:
    """Order query vắng không chứng minh query cộng trade history đều absent."""

    client_order_id: str
    authoritative_absence: bool = False


class DemoUsdMRestAdapter:
    """Submit/query order thường qua đúng host Demo đã pin trong target.

    Claim attempt hiện chỉ chống gửi lặp qua cùng instance. Composition root cần
    một claim dùng chung/durable trước khi nhiều instance hoặc worker được bật.
    """

    def __init__(
        self,
        *,
        target: BinanceDemoTarget,
        workspace_id: str,
        credentials: DemoApiCredentials,
        transport: SignedHttpTransport,
        clock_ms: Callable[[], int] | None = None,
        recv_window_ms: int = 5000,
        retry_backoff_seconds: int = 1,
        timeout_seconds: float = 10.0,
    ) -> None:
        if credentials.credential_ref != target.credential_ref:
            raise ValueError("credentials.credential_ref không khớp target.")
        if credentials.workspace_id != workspace_id:
            raise ValueError("credentials.workspace_id không khớp adapter scope.")
        if credentials.account_id != target.account_id:
            raise ValueError("credentials.account_id không khớp target.")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ValueError("workspace_id phải là chuỗi không trống.")
        if type(recv_window_ms) is not int or not 1 <= recv_window_ms <= 60_000:
            raise ValueError("recv_window_ms phải trong khoảng 1..60000.")
        if type(retry_backoff_seconds) is not int or retry_backoff_seconds < 1:
            raise ValueError("retry_backoff_seconds phải là số nguyên dương.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds phải dương.")
        self._target = target
        self._workspace_id = workspace_id
        self._credentials = credentials
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._recv_window_ms = recv_window_ms
        self._retry_backoff_seconds = retry_backoff_seconds
        self._timeout_seconds = timeout_seconds
        self._claimed_attempts: set[tuple[UUID, int]] = set()
        self._attempt_lock = Lock()

    def submit_order(self, intent: VenueOrderIntent) -> OrderAccepted | SubmissionFailed:
        self._validate_intent_scope(intent)
        if (
            intent.state is not VenueOrderState.SUBMITTING
            or intent.attempt_count < 1
            or intent.last_attempt_at is None
        ):
            raise BinanceAdapterError("Chỉ SUBMITTING với attempt đã bắt đầu mới được gửi.")
        if not _CLIENT_ID_PATTERN.fullmatch(intent.client_order_id):
            raise BinanceAdapterError()
        params: list[tuple[str, str]] = [
            ("symbol", intent.symbol),
            ("side", intent.side),
            ("type", intent.order_type),
            ("quantity", _decimal_text(intent.quantity)),
            ("newClientOrderId", intent.client_order_id),
        ]
        if intent.order_type == "LIMIT":
            if intent.limit_price is None:
                raise BinanceAdapterError()
            params.extend((("price", _decimal_text(intent.limit_price)), ("timeInForce", "GTC")))
        elif intent.order_type != "MARKET":
            raise BinanceAdapterError()
        params.extend(self._signed_time_params())
        request = self._signed_request("POST", params)
        self._claim_attempt(intent.intent_id, intent.attempt_count)
        try:
            response = self._transport.send(request)
        except TransportNotSent:
            failure = classify_submission_failure(None, request_was_sent=False)
            return SubmissionFailed(failure)
        except Exception:
            return SubmissionFailed(classify_submission_failure(None))

        payload = _json_object(response.body)
        if response.status_code < 200 or response.status_code >= 300:
            return self._submission_failure(response, payload, attempt_count=intent.attempt_count)
        try:
            if payload is None:
                raise ValueError
            order_id = payload.get("orderId")
            client_order_id = payload.get("clientOrderId")
            symbol = payload.get("symbol")
            state = _order_state(payload.get("status"))
            if (
                type(order_id) not in {int, str}
                or not str(order_id)
                or client_order_id != intent.client_order_id
                or symbol != intent.symbol
            ):
                raise ValueError
            return OrderAccepted(str(order_id), client_order_id, symbol, state)
        except TypeError, ValueError:
            # 2xx chỉ cho biết transport phản hồi; payload hỏng vẫn có thể là order đã nhận.
            return SubmissionFailed(classify_submission_failure(None))

    def query_order(self, intent: VenueOrderIntent) -> OrderFound | OrderMissing:
        self._validate_intent_scope(intent)
        params = [
            ("symbol", intent.symbol),
            ("origClientOrderId", intent.client_order_id),
            *self._signed_time_params(),
        ]
        request = self._signed_request("GET", params)
        try:
            response = self._transport.send(request)
        except TransportNotSent:
            raise BinanceAdapterError(
                failure=classify_submission_failure(None, request_was_sent=False)
            ) from None
        except Exception:
            raise BinanceAdapterError(failure=classify_submission_failure(None)) from None

        payload = _json_object(response.body)
        if response.status_code < 200 or response.status_code >= 300:
            failure = _binance_failure(response, payload)
            if response.status_code == 400 and _integer(payload, "code") == -2013:
                return OrderMissing(client_order_id=intent.client_order_id)
            raise BinanceAdapterError(
                failure=failure,
                retry_not_before=self._backoff_deadline(
                    failure, attempt_count=intent.attempt_count
                ),
            ) from None
        try:
            if payload is None:
                raise ValueError
            if (
                payload.get("clientOrderId") != intent.client_order_id
                or payload.get("symbol") != intent.symbol
            ):
                raise ValueError
            order_id = payload.get("orderId")
            if type(order_id) not in {int, str} or not str(order_id):
                raise ValueError
            executed = Decimal(_required_string(payload, "executedQty"))
            if not executed.is_finite() or executed < 0 or executed > intent.quantity:
                raise ValueError
            update_time = payload.get("updateTime")
            if type(update_time) is not int or update_time < 0:
                raise ValueError
            return OrderFound(
                venue_order_id=str(order_id),
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                state=_order_state(payload.get("status")),
                cumulative_filled=executed,
                updated_at=datetime.fromtimestamp(update_time / 1000, tz=UTC),
            )
        except ArithmeticError, OverflowError, TypeError, ValueError:
            raise BinanceAdapterError(failure=classify_submission_failure(None)) from None

    def _validate_intent_scope(self, intent: VenueOrderIntent) -> None:
        if (
            intent.workspace_id != self._workspace_id
            or intent.account_id != self._target.account_id
            or intent.mode.value != "DEMO"
        ):
            raise BinanceAdapterError()

    def _claim_attempt(self, intent_id: UUID, attempt_count: int) -> None:
        """Ngăn cùng instance gửi cùng intent attempt nhiều lần."""
        key = (intent_id, attempt_count)
        with self._attempt_lock:
            if key in self._claimed_attempts:
                raise BinanceAdapterError("Intent attempt này đã được claim trong adapter.")
            self._claimed_attempts.add(key)

    def _signed_time_params(self) -> list[tuple[str, str]]:
        return [
            ("timestamp", str(self._clock_ms())),
            ("recvWindow", str(self._recv_window_ms)),
        ]

    def _signed_request(self, method: str, params: list[tuple[str, str]]) -> BinanceHttpRequest:
        encoded = urlencode(params)
        signature = hmac.new(
            self._credentials.api_secret.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        signed = f"{encoded}&signature={signature}"
        headers: tuple[tuple[str, str], ...] = (("X-MBX-APIKEY", self._credentials.api_key),)
        base = self._target.rest_base_url
        if base != "https://demo-fapi.binance.com":
            raise BinanceAdapterError()
        if method == "POST":
            headers += (("Content-Type", "application/x-www-form-urlencoded"),)
            return BinanceHttpRequest(
                method=method,
                url=f"{base}{_ORDER_PATH}",
                headers=headers,
                body=signed.encode("ascii"),
                timeout_seconds=self._timeout_seconds,
            )
        if method == "GET":
            return BinanceHttpRequest(
                method=method,
                url=f"{base}{_ORDER_PATH}?{signed}",
                headers=headers,
                body=b"",
                timeout_seconds=self._timeout_seconds,
            )
        raise BinanceAdapterError()

    def _submission_failure(
        self,
        response: BinanceHttpResponse,
        payload: dict[str, object] | None,
        *,
        attempt_count: int,
    ) -> SubmissionFailed:
        failure = _binance_failure(response, payload)
        return SubmissionFailed(
            failure, self._backoff_deadline(failure, attempt_count=attempt_count)
        )

    def _backoff_deadline(
        self, failure: SubmissionFailure, *, attempt_count: int
    ) -> datetime | None:
        if not failure.backoff_required:
            return None
        exponent = min(max(attempt_count - 1, 0), 30)
        delay_seconds = min(30, self._retry_backoff_seconds * (2**exponent))
        deadline_ms = self._clock_ms() + delay_seconds * 1000
        return datetime.fromtimestamp(deadline_ms / 1000, tz=UTC)


def _binance_failure(
    response: BinanceHttpResponse, payload: dict[str, object] | None
) -> SubmissionFailure:
    code = _integer(payload, "code")
    message = payload.get("msg", "") if payload is not None else ""
    return classify_submission_failure(
        BinanceResponse(
            status_code=response.status_code,
            error_code=code,
            message=message if isinstance(message, str) else "",
        )
    )


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError, TypeError:
        return None
    return value if isinstance(value, dict) else None


def _integer(payload: dict[str, object] | None, key: str) -> int | None:
    value = payload.get(key) if payload is not None else None
    return value if type(value) is int else None


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _order_state(value: object) -> VenueOrderState:
    if not isinstance(value, str) or value not in _ORDER_STATUS:
        raise ValueError
    return _ORDER_STATUS[value]


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite() or value <= 0:
        raise ValueError("quantity/price phải là Decimal hữu hạn dương.")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered
