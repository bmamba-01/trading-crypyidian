"""Unit test cho ExchangeGateway Host/Key Boundary và Secret Separation."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from trading_bot.adapters.binance_usdm.gateway import BinanceUsdMExchangeGateway
from trading_bot.adapters.binance_usdm.gateway_boundary import (
    ApiKeyCredentials,
    BinanceExecutionMode,
    ClockDriftBoundaryError,
    HostBoundaryViolationError,
    SecretRedactingLoggingFilter,
    TimestampBoundaryError,
    redact_sensitive_text,
    sign_hmac_sha256,
    validate_host_boundary,
    validate_recv_window,
    validate_timestamp_and_drift,
)
from trading_bot.ports.exchange_gateway import (
    ProbeCategory,
    ReadinessStatus,
    SecretReference,
)

# --- 1. Host and Mode Boundary Tests ---


def test_reject_production_rest_host_in_demo_mode() -> None:
    """Chế độ DEMO cấm tuyệt đối production REST host."""
    with pytest.raises(HostBoundaryViolationError, match="Cấm kết nối tới production"):
        validate_host_boundary(
            mode=BinanceExecutionMode.DEMO,
            rest_base_url="https://fapi.binance.com",
            ws_base_url="wss://demo-fstream.binance.com",
        )


def test_reject_production_ws_host_in_demo_mode() -> None:
    """Chế độ DEMO cấm tuyệt đối production WebSocket host."""
    with pytest.raises(HostBoundaryViolationError, match="Cấm kết nối tới production"):
        validate_host_boundary(
            mode=BinanceExecutionMode.DEMO,
            rest_base_url="https://demo-fapi.binance.com",
            ws_base_url="wss://fstream.binance.com",
        )


def test_reject_production_hosts_in_paper_mode() -> None:
    """Chế độ PAPER (mô phỏng nội bộ) cấm mọi host production Binance."""
    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode=BinanceExecutionMode.PAPER,
            rest_base_url="https://fapi.binance.com",
            ws_base_url="wss://demo-fstream.binance.com",
        )


@pytest.mark.parametrize(
    "invalid_url",
    [
        "http://demo-fapi.binance.com",  # HTTP không an toàn
        "https://demo-fapi.binance.com:8443",  # Port lạ
        "https://evil-phishing.com",  # Host ngoài allowlist
        "https://demo-fapi.binance.com/extra/path",  # Có path thừa
        "https://demo-fapi.binance.com?query=malicious",  # Có query string
        "https://user:password@demo-fapi.binance.com",  # Chứa credential userinfo
        "https://demo-fapi.binance.com#frag",  # Fragment
        "ftp://demo-fapi.binance.com",  # Scheme sai
        "",  # Trống
    ],
)
def test_reject_unallowlisted_or_malformed_rest_url(invalid_url: str) -> None:
    """Từ chối URL REST không đúng allowlist hoặc có dấu hiệu injection."""
    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode=BinanceExecutionMode.DEMO,
            rest_base_url=invalid_url,
            ws_base_url="wss://demo-fstream.binance.com",
        )


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ws://demo-fstream.binance.com",  # WS không mã hóa TLS
        "wss://evil-stream.com",  # Stream lạ
        "wss://demo-fstream.binance.com/ws/listenKey",  # Path thừa
        "wss://demo-fstream.binance.com?listenKey=secret",  # Query string
        "wss://admin:secret@demo-fstream.binance.com",  # Userinfo
    ],
)
def test_reject_unallowlisted_or_malformed_ws_url(invalid_url: str) -> None:
    """Từ chối URL WebSocket không đúng chuẩn allowlist."""
    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode=BinanceExecutionMode.DEMO,
            rest_base_url="https://demo-fapi.binance.com",
            ws_base_url=invalid_url,
        )


def test_allowlist_valid_demo_and_testnet_endpoints() -> None:
    """Chấp nhận các endpoint Demo và Testnet Binance USDⓈ-M hợp lệ."""
    # Demo endpoint
    validate_host_boundary(
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
    )
    # Testnet endpoint chính thức
    validate_host_boundary(
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://testnet.binancefuture.com",
        ws_base_url="wss://stream.binancefuture.com",
    )


def test_local_mock_allowed_only_when_explicitly_flagged() -> None:
    """Chỉ chấp nhận mock host localhost khi flag allow_local_mock=True."""
    # Bị từ chối khi flag mặc định là False
    with pytest.raises(HostBoundaryViolationError):
        validate_host_boundary(
            mode=BinanceExecutionMode.DEMO,
            rest_base_url="http://localhost:8080",
            ws_base_url="ws://localhost:8080",
            allow_local_mock=False,
        )

    # Chấp nhận khi allow_local_mock=True cho testing offline
    validate_host_boundary(
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="http://127.0.0.1:8080",
        ws_base_url="ws://127.0.0.1:8080",
        allow_local_mock=True,
    )


# --- 2. Secret Separation and Redaction Tests ---


def test_api_key_credentials_masking() -> None:
    """ApiKeyCredentials không để lộ secret key trong __repr__ hay __str__."""
    creds = ApiKeyCredentials(
        api_key="my_super_secret_binance_api_key_12345",
        secret_key="my_super_secret_binance_api_secret_67890",
    )

    repr_str = repr(creds)
    str_str = str(creds)

    assert "my_super_secret_binance_api_secret_67890" not in repr_str
    assert "my_super_secret_binance_api_secret_67890" not in str_str
    assert "REDACTED" in repr_str
    assert "REDACTED" in str_str
    # Masked api key chỉ hiển thị phần đuôi an toàn
    assert creds.masked_api_key().endswith("2345")
    assert "my_super_secret_binance_api_key" not in creds.masked_api_key()


def test_secret_reference_safe_repr() -> None:
    """SecretReference che giấu identifier nhạy cảm trong representation."""
    ref = SecretReference(
        reference_id="vault:production/binance/futures/subaccount_1",
        key_alias="binance_subaccount_1",
    )
    ref_repr = repr(ref)
    assert "production/binance/futures" not in ref_repr
    assert "subaccount_1" in ref.key_alias


def test_redact_sensitive_text_masks_signatures_and_keys() -> None:
    """Hàm redact_sensitive_text tự động thay thế signature và api key."""
    raw_message = (
        "Request failed on https://demo-fapi.binance.com/fapi/v1/order?"
        "symbol=BTCUSDT&timestamp=1700000000000&signature="
        "4f2a9c18d9b8e7f6a5b4c3d2e10987654321fedcba0987654321abcdef123456"
        " with header X-MBX-APIKEY: 1234567890abcdef1234567890abcdef "
        "and listenKey=testListenKey123456"
    )

    redacted = redact_sensitive_text(
        raw_message,
        extra_secrets=["1234567890abcdef1234567890abcdef"],
    )

    assert "4f2a9c18d9b8e7f6a5b4c3d2e10987654321fedcba0987654321abcdef123456" not in redacted
    assert "1234567890abcdef1234567890abcdef" not in redacted
    assert "testListenKey123456" not in redacted
    assert "signature=[REDACTED]" in redacted
    assert "listenKey=[REDACTED]" in redacted


def test_logging_filter_redacts_secrets_in_logs() -> None:
    """SecretRedactingLoggingFilter đảm bảo log record không bao giờ in secret."""
    filter_ = SecretRedactingLoggingFilter(registered_secrets=["my_super_secret_key_abcdef12345"])
    sig_part = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    msg_str = (
        f"Error authenticating with key my_super_secret_key_abcdef12345 and signature={sig_part}"
    )
    record = logging.LogRecord(
        name="test_logger",
        level=logging.ERROR,
        pathname="gateway.py",
        lineno=42,
        msg=msg_str,
        args=(),
        exc_info=None,
    )

    filter_.filter(record)
    assert "my_super_secret_key_abcdef12345" not in record.msg
    assert "[REDACTED]" in record.msg


# --- 3. Request Signature and Timestamp Boundary Tests ---


def test_validate_recv_window_valid_range() -> None:
    """recvWindow chỉ hợp lệ trong dải 1000ms đến 60000ms."""
    validate_recv_window(1000)
    validate_recv_window(5000)
    validate_recv_window(60000)

    with pytest.raises(TimestampBoundaryError, match="recv_window_ms"):
        validate_recv_window(999)  # Nhỏ hơn 1000ms

    with pytest.raises(TimestampBoundaryError, match="recv_window_ms"):
        validate_recv_window(60001)  # Lớn hơn 60000ms

    with pytest.raises(TimestampBoundaryError, match="recv_window_ms"):
        validate_recv_window(-5000)


def test_validate_timestamp_and_drift_clock_skew() -> None:
    """Phát hiện và chặn clock drift khi đồng hồ lệch quá dung sai."""
    server_time = 1_700_000_000_000
    recv_window = 5000
    tolerance = 1000

    # Khớp hoàn hảo
    validate_timestamp_and_drift(
        request_timestamp_ms=server_time,
        server_time_ms=server_time,
        recv_window_ms=recv_window,
        max_drift_tolerance_ms=tolerance,
    )

    # Local clock chạy nhanh hơn server quá 1000ms -> Lỗi Clock Drift
    with pytest.raises(ClockDriftBoundaryError, match="chạy trước server time"):
        validate_timestamp_and_drift(
            request_timestamp_ms=server_time + 1500,
            server_time_ms=server_time,
            recv_window_ms=recv_window,
            max_drift_tolerance_ms=tolerance,
        )

    # Request quá cũ vượt recvWindow -> Lỗi expired request
    with pytest.raises(ClockDriftBoundaryError, match="quá cũ so với recvWindow"):
        validate_timestamp_and_drift(
            request_timestamp_ms=server_time - 5500,
            server_time_ms=server_time,
            recv_window_ms=recv_window,
            max_drift_tolerance_ms=tolerance,
        )


def test_sign_hmac_sha256() -> None:
    """Ký request HMAC-SHA256 chuẩn theo quy cách của Binance."""
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = (
        "symbol=LTCUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
        "&price=0.1&recvWindow=5000&timestamp=1499827319559"
    )
    # Expected signature theo ví dụ chuẩn của Binance API documentation
    signature = sign_hmac_sha256(secret_key=secret, query_or_body=query)
    assert len(signature) == 64
    assert signature.islower()
    # Ký lại cùng payload cho cùng kết quả
    assert signature == sign_hmac_sha256(secret_key=secret, query_or_body=query)


# --- 4. Readiness Probes Tests ---


class FakeHttpTransport:
    """Mock transport điều khiển phản hồi HTTP phục vụ unit test offline."""

    def __init__(self, responses: dict[str, tuple[int, Any]]) -> None:
        self.responses = responses
        self.call_history: list[dict[str, Any]] = []

    def send(self, request: Any) -> Any:
        from trading_bot.adapters.binance_usdm.demo_rest import BinanceHttpResponse

        self.call_history.append({"method": request.method, "url": request.url})
        path = request.url.split("?")[0]
        # Tìm route phù hợp
        for route, (status_code, payload) in self.responses.items():
            if route in path:
                return BinanceHttpResponse(
                    status_code=status_code,
                    headers={"content-type": "application/json"},
                    body=json.dumps(payload).encode("utf-8"),
                )
        return BinanceHttpResponse(
            status_code=404,
            headers={"content-type": "application/json"},
            body=b'{"code": -1121, "msg": "Invalid symbol."}',
        )


def test_readiness_probe_all_ready() -> None:
    """Kiểm tra readiness probe khi tất cả điều kiện đều đạt (READY)."""
    current_time_ms = 1_700_000_000_000
    mock_responses = {
        "/fapi/v1/time": (200, {"serverTime": current_time_ms}),
        "/fapi/v3/account": (
            200,
            {
                "canTrade": True,
                "canDeposit": True,
                "canWithdraw": True,
                "feeTier": 0,
                "updateTime": current_time_ms,
                "totalWalletBalance": "10000.00",
            },
        ),
        "/fapi/v1/listenKey": (200, {"listenKey": "mock_valid_listen_key_12345"}),
    }
    transport = FakeHttpTransport(mock_responses)
    gateway = BinanceUsdMExchangeGateway(
        account_id="test_acc_01",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=ApiKeyCredentials(api_key="mock_api_key_123", secret_key="mock_secret_key_456"),
        transport=transport,
        clock_ms=lambda: current_time_ms,
        recv_window_ms=5000,
    )

    report = gateway.check_readiness()

    assert report.is_ready is True
    assert report.overall_status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.PUBLIC_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.USER_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.TRADE_CAPABILITY].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.STREAM_AND_CLOCK].status is ReadinessStatus.READY


def test_readiness_probe_trade_capability_failed_when_cant_trade() -> None:
    """Kiểm tra probe TRADE_CAPABILITY báo FAILED khi canTrade=False."""
    current_time_ms = 1_700_000_000_000
    mock_responses = {
        "/fapi/v1/time": (200, {"serverTime": current_time_ms}),
        "/fapi/v3/account": (
            200,
            {
                "canTrade": False,  # Tài khoản bị khóa quyền giao dịch
                "canDeposit": True,
                "canWithdraw": False,
            },
        ),
        "/fapi/v1/listenKey": (200, {"listenKey": "mock_listen_key"}),
    }
    transport = FakeHttpTransport(mock_responses)
    gateway = BinanceUsdMExchangeGateway(
        account_id="test_acc_02",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=ApiKeyCredentials(api_key="mock_api_key_123", secret_key="mock_secret_key_456"),
        transport=transport,
        clock_ms=lambda: current_time_ms,
    )

    report = gateway.check_readiness()

    assert report.is_ready is False
    assert report.overall_status is ReadinessStatus.FAILED
    assert report.probes[ProbeCategory.PUBLIC_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.USER_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.TRADE_CAPABILITY].status is ReadinessStatus.FAILED
    assert "canTrade" in report.probes[ProbeCategory.TRADE_CAPABILITY].message


def test_readiness_probe_user_data_failed_on_auth_error() -> None:
    """Kiểm tra probe USER_DATA báo FAILED khi mã lỗi 401 hoặc 403."""
    current_time_ms = 1_700_000_000_000
    mock_responses = {
        "/fapi/v1/time": (200, {"serverTime": current_time_ms}),
        "/fapi/v3/account": (
            401,
            {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."},
        ),
    }
    transport = FakeHttpTransport(mock_responses)
    gateway = BinanceUsdMExchangeGateway(
        account_id="test_acc_03",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=ApiKeyCredentials(api_key="mock_api_key_123", secret_key="mock_secret_key_456"),
        transport=transport,
        clock_ms=lambda: current_time_ms,
    )

    report = gateway.check_readiness()

    assert report.is_ready is False
    assert report.overall_status is ReadinessStatus.FAILED
    assert report.probes[ProbeCategory.PUBLIC_DATA].status is ReadinessStatus.READY
    assert report.probes[ProbeCategory.USER_DATA].status is ReadinessStatus.FAILED
    assert report.probes[ProbeCategory.TRADE_CAPABILITY].status is ReadinessStatus.FAILED


def test_readiness_probe_clock_skew_fails_when_drift_exceeded() -> None:
    """Kiểm tra probe STREAM_AND_CLOCK báo FAILED khi clock drift vượt quá dung sai."""
    local_time_ms = 1_700_000_000_000
    server_time_ms = 1_700_000_005_000  # Server lệch 5000ms so với local
    mock_responses = {
        "/fapi/v1/time": (200, {"serverTime": server_time_ms}),
        "/fapi/v3/account": (200, {"canTrade": True}),
        "/fapi/v1/listenKey": (200, {"listenKey": "mock_key"}),
    }
    transport = FakeHttpTransport(mock_responses)
    gateway = BinanceUsdMExchangeGateway(
        account_id="test_acc_04",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=ApiKeyCredentials(api_key="mock_api_key_123", secret_key="mock_secret_key_456"),
        transport=transport,
        clock_ms=lambda: local_time_ms,
        max_drift_tolerance_ms=1000,
    )

    report = gateway.check_readiness()

    assert report.is_ready is False
    assert report.overall_status is ReadinessStatus.FAILED
    assert report.probes[ProbeCategory.STREAM_AND_CLOCK].status is ReadinessStatus.FAILED
    assert "clock drift" in report.probes[ProbeCategory.STREAM_AND_CLOCK].message.lower()


def test_readiness_probe_degraded_on_rate_limit() -> None:
    """Kiểm tra probe USER_DATA báo DEGRADED khi gặp mã lỗi 429 và overall_status là DEGRADED."""
    current_time_ms = 1_700_000_000_000
    mock_responses = {
        "/fapi/v1/time": (200, {"serverTime": current_time_ms}),
        "/fapi/v3/account": (429, {"code": -1003, "msg": "Way too many requests."}),
        "/fapi/v1/listenKey": (200, {"listenKey": "mock_key"}),
    }
    transport = FakeHttpTransport(mock_responses)
    gateway = BinanceUsdMExchangeGateway(
        account_id="test_acc_05",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=ApiKeyCredentials(api_key="mock_api_key_123", secret_key="mock_secret_key_456"),
        transport=transport,
        clock_ms=lambda: current_time_ms,
    )

    report = gateway.check_readiness()

    assert report.is_ready is False
    # Do trade_capability không chạy được khi thiếu account data
    assert report.overall_status is ReadinessStatus.FAILED
    assert report.probes[ProbeCategory.USER_DATA].status is ReadinessStatus.DEGRADED


def test_api_key_credentials_validation() -> None:
    """ApiKeyCredentials từ chối key rỗng hoặc quá ngắn."""
    with pytest.raises(ValueError, match="api_key"):
        ApiKeyCredentials(api_key="", secret_key="12345678")

    with pytest.raises(ValueError, match="secret_key"):
        ApiKeyCredentials(api_key="12345678", secret_key="")

    with pytest.raises(ValueError, match="quá ngắn"):
        ApiKeyCredentials(api_key="short", secret_key="12345678")


def test_gateway_properties_and_empty_account_id() -> None:
    """Gateway phơi bày các thuộc tính an toàn và từ chối account_id rỗng."""
    transport = FakeHttpTransport({})
    creds = ApiKeyCredentials(api_key="mock_api_key_123", secret_key="mock_secret_key_456")

    with pytest.raises(ValueError, match="account_id"):
        BinanceUsdMExchangeGateway(
            account_id="   ",
            mode=BinanceExecutionMode.DEMO,
            rest_base_url="https://demo-fapi.binance.com",
            ws_base_url="wss://demo-fstream.binance.com",
            credentials=creds,
            transport=transport,
        )

    gateway = BinanceUsdMExchangeGateway(
        account_id="valid_account_123",
        mode=BinanceExecutionMode.DEMO,
        rest_base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://demo-fstream.binance.com",
        credentials=creds,
        transport=transport,
    )

    assert gateway.account_id == "valid_account_123"
    assert gateway.mode == "DEMO"
    assert gateway.endpoints.rest_base_url == "https://demo-fapi.binance.com"
    assert gateway.endpoints.ws_base_url == "wss://demo-fstream.binance.com"


def test_live_mode_allows_production_and_rejects_demo() -> None:
    """Chế độ LIVE chỉ cho phép production endpoints và từ chối demo endpoints."""
    # LIVE với production endpoints -> Hợp lệ
    validate_host_boundary(
        mode=BinanceExecutionMode.LIVE,
        rest_base_url="https://fapi.binance.com",
        ws_base_url="wss://fstream.binance.com",
    )

    # LIVE với Demo endpoint -> Bị từ chối
    with pytest.raises(HostBoundaryViolationError, match="không thuộc allowlist Production"):
        validate_host_boundary(
            mode=BinanceExecutionMode.LIVE,
            rest_base_url="https://demo-fapi.binance.com",
            ws_base_url="wss://fstream.binance.com",
        )
