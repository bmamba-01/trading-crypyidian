"""REST adapter Binance USDⓈ-M Algo Service (`/fapi/v1/algoOrder`, `/fapi/v1/openAlgoOrders`).

Chỉ gửi các lệnh điều kiện (`STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`,
`TRAILING_STOP_MARKET`) qua Algo Service, không bao giờ đi qua `/fapi/v1/order`.
Không tự retry khi nhận kết quả mơ hồ (`UNKNOWN`), không lưu secret/chữ ký trong lỗi.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    BinanceHttpResponse,
    DemoApiCredentials,
    SignedHttpTransport,
    TransportNotSent,
)
from trading_bot.adapters.binance_usdm.demo_target import BinanceDemoTarget
from trading_bot.adapters.binance_usdm.order_schemas import (
    AlgoOrder,
    OrderSchemaError,
    decode_order_object,
    parse_algo_order,
)
from trading_bot.adapters.binance_usdm.submission_errors import (
    BinanceResponse,
    SubmissionFailure,
    classify_submission_failure,
)
from trading_bot.domain.execution.algo_protection import AlgoOrderParams

_ALGO_ORDER_PATH = "/fapi/v1/algoOrder"
_OPEN_ALGO_ORDERS_PATH = "/fapi/v1/openAlgoOrders"


@dataclass(frozen=True, slots=True)
class AlgoOrderAccepted:
    """Kết quả sàn ACK khi đặt lệnh điều kiện qua `POST /fapi/v1/algoOrder`."""

    order: AlgoOrder


@dataclass(frozen=True, slots=True)
class AlgoOrderCanceled:
    """Kết quả hủy lệnh điều kiện qua `DELETE /fapi/v1/algoOrder`."""

    algo_id: int
    client_algo_id: str | None
    symbol: str


@dataclass(frozen=True, slots=True)
class AlgoOrderFound:
    """Kết quả truy vấn một lệnh điều kiện qua `GET /fapi/v1/algoOrder`."""

    order: AlgoOrder


@dataclass(frozen=True, slots=True)
class AlgoSubmissionFailed:
    """Kết quả thất bại đã phân loại an toàn (không chứa payload thô hay secret)."""

    failure: SubmissionFailure
    http_status: int | None = None
    error_code: int | None = None


class AlgoOrderPort(Protocol):
    """Cổng giao tiếp Algo Service cho Protection Coordinator."""

    def place_algo_order(
        self, params: AlgoOrderParams
    ) -> AlgoOrderAccepted | AlgoSubmissionFailed: ...

    def cancel_algo_order(
        self, *, symbol: str, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> AlgoOrderCanceled | AlgoSubmissionFailed: ...

    def query_algo_order(
        self, *, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> AlgoOrderFound | AlgoSubmissionFailed: ...

    def list_open_algo_orders(
        self, *, symbol: str | None = None
    ) -> tuple[AlgoOrder, ...] | AlgoSubmissionFailed: ...


class BinanceUsdMAlgoRestAdapter(AlgoOrderPort):
    """REST adapter cho Algo Service của Binance USDⓈ-M (Demo host được ghim chặt)."""

    def __init__(
        self,
        *,
        target: BinanceDemoTarget,
        credentials: DemoApiCredentials,
        transport: SignedHttpTransport,
        clock_ms: Callable[[], int] | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not 1 <= recv_window_ms <= 60_000:
            raise ValueError("recv_window_ms phải nằm trong đoạn [1, 60000].")
        self._target = target
        self._credentials = credentials
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._recv_window_ms = recv_window_ms

    def place_algo_order(self, params: AlgoOrderParams) -> AlgoOrderAccepted | AlgoSubmissionFailed:
        """Đặt lệnh điều kiện qua `POST /fapi/v1/algoOrder`."""
        query_params = params.to_query_params()
        response_or_fail = self._send_signed("POST", _ALGO_ORDER_PATH, query_params)
        if isinstance(response_or_fail, AlgoSubmissionFailed):
            return response_or_fail
        try:
            order = parse_algo_order(response_or_fail.body)
        except OrderSchemaError:
            return AlgoSubmissionFailed(
                failure=classify_submission_failure(None, request_was_sent=True),
                http_status=response_or_fail.status_code,
            )
        return AlgoOrderAccepted(order=order)

    def cancel_algo_order(
        self, *, symbol: str, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> AlgoOrderCanceled | AlgoSubmissionFailed:
        """Hủy lệnh điều kiện qua `DELETE /fapi/v1/algoOrder`."""
        if algo_id is None and client_algo_id is None:
            raise ValueError("Phải cung cấp algo_id hoặc client_algo_id khi hủy lệnh Algo.")
        params: dict[str, str] = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = str(algo_id)
        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id

        response_or_fail = self._send_signed("DELETE", _ALGO_ORDER_PATH, params)
        if isinstance(response_or_fail, AlgoSubmissionFailed):
            return response_or_fail
        try:
            data = decode_order_object(response_or_fail.body)
            returned_algo_id = int(str(data.get("algoId", algo_id or 0)))
            if returned_algo_id <= 0:
                raise OrderSchemaError("Thiếu algoId hợp lệ trong phản hồi hủy.")
            returned_client_id = (
                str(data["clientAlgoId"])
                if data.get("clientAlgoId") is not None
                else client_algo_id
            )
        except OrderSchemaError, ValueError:
            return AlgoSubmissionFailed(
                failure=classify_submission_failure(None, request_was_sent=True),
                http_status=response_or_fail.status_code,
            )
        return AlgoOrderCanceled(
            algo_id=returned_algo_id,
            client_algo_id=returned_client_id,
            symbol=symbol,
        )

    def query_algo_order(
        self, *, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> AlgoOrderFound | AlgoSubmissionFailed:
        """Truy vấn trạng thái một lệnh điều kiện qua `GET /fapi/v1/algoOrder`."""
        if algo_id is None and client_algo_id is None:
            raise ValueError("Phải cung cấp algo_id hoặc client_algo_id khi truy vấn lệnh Algo.")
        params: dict[str, str] = {}
        if algo_id is not None:
            params["algoId"] = str(algo_id)
        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id

        response_or_fail = self._send_signed("GET", _ALGO_ORDER_PATH, params)
        if isinstance(response_or_fail, AlgoSubmissionFailed):
            return response_or_fail
        try:
            order = parse_algo_order(response_or_fail.body)
        except OrderSchemaError:
            return AlgoSubmissionFailed(
                failure=classify_submission_failure(None, request_was_sent=True),
                http_status=response_or_fail.status_code,
            )
        return AlgoOrderFound(order=order)

    def list_open_algo_orders(
        self, *, symbol: str | None = None
    ) -> tuple[AlgoOrder, ...] | AlgoSubmissionFailed:
        """Lấy danh sách lệnh điều kiện đang mở qua `GET /fapi/v1/openAlgoOrders`."""
        params: dict[str, str] = {}
        if symbol is not None:
            params["symbol"] = symbol

        response_or_fail = self._send_signed("GET", _OPEN_ALGO_ORDERS_PATH, params)
        if isinstance(response_or_fail, AlgoSubmissionFailed):
            return response_or_fail
        try:
            raw = json.loads(response_or_fail.body.decode("utf-8"))
            if not isinstance(raw, list):
                raise OrderSchemaError("openAlgoOrders phải trả về danh sách JSON.")
            orders = tuple(parse_algo_order(item) for item in raw)
        except UnicodeError, ValueError, OrderSchemaError:
            return AlgoSubmissionFailed(
                failure=classify_submission_failure(None, request_was_sent=True),
                http_status=response_or_fail.status_code,
            )
        return orders

    def _send_signed(
        self, method: str, path: str, params: dict[str, str]
    ) -> BinanceHttpResponse | AlgoSubmissionFailed:
        signed_params = dict(params)
        signed_params["recvWindow"] = str(self._recv_window_ms)
        signed_params["timestamp"] = str(self._clock_ms())
        query_string = urlencode(signed_params)
        signature = hmac.new(
            self._credentials.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        full_url = f"{self._target.rest_base_url}{path}?{query_string}&signature={signature}"
        request = BinanceHttpRequest(
            method=method,
            url=full_url,
            headers=(
                ("X-MBX-APIKEY", self._credentials.api_key),
                ("User-Agent", "CrypidianBot"),
            ),
            body=b"",
        )
        try:
            response = self._transport.send(request)
        except TransportNotSent:
            return AlgoSubmissionFailed(
                failure=classify_submission_failure(None, request_was_sent=False)
            )
        except Exception:
            return AlgoSubmissionFailed(
                failure=classify_submission_failure(None, request_was_sent=True)
            )

        if 200 <= response.status_code < 300:
            return response

        error_code, message = _extract_error_fields(response.body)
        failure = classify_submission_failure(
            BinanceResponse(
                status_code=response.status_code,
                error_code=error_code,
                message=message,
            ),
            request_was_sent=True,
        )
        return AlgoSubmissionFailed(
            failure=failure,
            http_status=response.status_code,
            error_code=error_code,
        )


def _extract_error_fields(body: bytes) -> tuple[int | None, str]:
    """Trích mã lỗi và thông điệp từ body lỗi Binance mà không làm rò rỉ dữ liệu thô."""
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            code = parsed.get("code")
            msg = parsed.get("msg", "")
            return (code if isinstance(code, int) else None, str(msg))
    except UnicodeError, ValueError:
        pass
    return (None, "")
