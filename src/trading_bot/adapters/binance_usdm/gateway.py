"""Thực thi ExchangeGateway cho Binance USDⓈ-M Futures.

Bao gồm phân tách ranh giới host/mode, bảo vệ secret và bộ readiness probes độc lập.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from trading_bot.adapters.binance_usdm.demo_rest import (
    BinanceHttpRequest,
    SignedHttpTransport,
)
from trading_bot.adapters.binance_usdm.gateway_boundary import (
    ApiKeyCredentials,
    BinanceExecutionMode,
    redact_sensitive_text,
    sign_hmac_sha256,
    validate_host_boundary,
    validate_recv_window,
)
from trading_bot.ports.exchange_gateway import (
    ExchangeGateway,
    GatewayEndpoints,
    GatewayReadinessReport,
    ProbeCategory,
    ReadinessProbeResult,
    ReadinessStatus,
)


class BinanceUsdMExchangeGateway(ExchangeGateway):
    """Cổng giao dịch Binance USDⓈ-M với kiểm soát ranh giới bảo mật nghiêm ngặt."""

    def __init__(
        self,
        *,
        account_id: str,
        mode: BinanceExecutionMode | str,
        rest_base_url: str,
        ws_base_url: str,
        credentials: ApiKeyCredentials,
        transport: SignedHttpTransport,
        clock_ms: Callable[[], int] | None = None,
        recv_window_ms: int = 5000,
        max_drift_tolerance_ms: int = 1000,
        allow_local_mock: bool = False,
    ) -> None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id phải là chuỗi không trống.")

        # 1. Xác thực ranh giới host và mode (fail-closed)
        validate_host_boundary(
            mode=mode,
            rest_base_url=rest_base_url,
            ws_base_url=ws_base_url,
            allow_local_mock=allow_local_mock,
        )

        # 2. Xác thực recvWindow
        validate_recv_window(recv_window_ms)

        self._account_id = account_id
        self._mode = BinanceExecutionMode(mode)
        self._rest_base_url = rest_base_url.rstrip("/")
        self._ws_base_url = ws_base_url.rstrip("/")
        self._credentials = credentials
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._recv_window_ms = recv_window_ms
        self._max_drift_tolerance_ms = max_drift_tolerance_ms
        self._allow_local_mock = allow_local_mock

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def mode(self) -> str:
        return self._mode.value

    @property
    def endpoints(self) -> GatewayEndpoints:
        return GatewayEndpoints(
            rest_base_url=self._rest_base_url,
            ws_base_url=self._ws_base_url,
        )

    def probe_public_data(self) -> tuple[ReadinessProbeResult, int | None]:
        """Thăm dò kết nối dữ liệu công khai (REST time endpoint)."""
        url = f"{self._rest_base_url}/fapi/v1/time"
        req = BinanceHttpRequest(
            method="GET",
            url=url,
            headers=(("User-Agent", "CrypidianBot"),),
            body=b"",
        )
        start_time = time.perf_counter()
        try:
            res = self._transport.send(req)
            latency_ms = (time.perf_counter() - start_time) * 1000
            if res.status_code == 200:
                payload = json.loads(res.body.decode("utf-8"))
                server_time = payload.get("serverTime")
                if isinstance(server_time, int):
                    return (
                        ReadinessProbeResult(
                            category=ProbeCategory.PUBLIC_DATA,
                            status=ReadinessStatus.READY,
                            message="Kết nối dữ liệu công khai Binance USDⓈ-M thành công.",
                            latency_ms=round(latency_ms, 2),
                            details={"serverTime": server_time},
                        ),
                        server_time,
                    )
            return (
                ReadinessProbeResult(
                    category=ProbeCategory.PUBLIC_DATA,
                    status=ReadinessStatus.FAILED,
                    message=f"Endpoint public data trả về HTTP {res.status_code}.",
                    latency_ms=round(latency_ms, 2),
                ),
                None,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return (
                ReadinessProbeResult(
                    category=ProbeCategory.PUBLIC_DATA,
                    status=ReadinessStatus.FAILED,
                    message=f"Lỗi kết nối public API: {redact_sensitive_text(str(exc))}",
                    latency_ms=round(latency_ms, 2),
                ),
                None,
            )

    def probe_user_data(self) -> tuple[ReadinessProbeResult, dict[str, Any] | None]:
        """Thăm dò quyền truy cập USER_DATA qua signed request /fapi/v3/account."""
        ts = self._clock_ms()
        query = f"timestamp={ts}&recvWindow={self._recv_window_ms}"
        sig = sign_hmac_sha256(self._credentials.secret_key, query)
        url = f"{self._rest_base_url}/fapi/v3/account?{query}&signature={sig}"
        req = BinanceHttpRequest(
            method="GET",
            url=url,
            headers=(
                ("X-MBX-APIKEY", self._credentials.api_key),
                ("User-Agent", "CrypidianBot"),
            ),
            body=b"",
        )
        start_time = time.perf_counter()
        try:
            res = self._transport.send(req)
            latency_ms = (time.perf_counter() - start_time) * 1000
            if res.status_code == 200:
                payload = json.loads(res.body.decode("utf-8"))
                return (
                    ReadinessProbeResult(
                        category=ProbeCategory.USER_DATA,
                        status=ReadinessStatus.READY,
                        message="Xác thực signed USER_DATA thành công.",
                        latency_ms=round(latency_ms, 2),
                    ),
                    payload,
                )
            if res.status_code in (401, 403):
                return (
                    ReadinessProbeResult(
                        category=ProbeCategory.USER_DATA,
                        status=ReadinessStatus.FAILED,
                        message=(
                            "Xác thực USER_DATA thất bại (401/403): API key sai hoặc thiếu quyền."
                        ),
                        latency_ms=round(latency_ms, 2),
                    ),
                    None,
                )
            if res.status_code == 429:
                return (
                    ReadinessProbeResult(
                        category=ProbeCategory.USER_DATA,
                        status=ReadinessStatus.DEGRADED,
                        message="Bị giới hạn tốc độ gọi USER_DATA (Rate limit 429).",
                        latency_ms=round(latency_ms, 2),
                    ),
                    None,
                )
            return (
                ReadinessProbeResult(
                    category=ProbeCategory.USER_DATA,
                    status=ReadinessStatus.FAILED,
                    message=f"USER_DATA trả về HTTP {res.status_code}.",
                    latency_ms=round(latency_ms, 2),
                ),
                None,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return (
                ReadinessProbeResult(
                    category=ProbeCategory.USER_DATA,
                    status=ReadinessStatus.FAILED,
                    message=f"Lỗi truy vấn USER_DATA: {redact_sensitive_text(str(exc))}",
                    latency_ms=round(latency_ms, 2),
                ),
                None,
            )

    def probe_trade_capability(self, account_data: dict[str, Any] | None) -> ReadinessProbeResult:
        """Thăm dò năng lực đặt lệnh (TRADE) từ thông tin tài khoản."""
        if account_data is None:
            return ReadinessProbeResult(
                category=ProbeCategory.TRADE_CAPABILITY,
                status=ReadinessStatus.FAILED,
                message="Không thể kiểm tra năng lực TRADE do probe USER_DATA thất bại.",
            )

        can_trade = account_data.get("canTrade")
        if can_trade is True:
            return ReadinessProbeResult(
                category=ProbeCategory.TRADE_CAPABILITY,
                status=ReadinessStatus.READY,
                message="Tài khoản và API key có đầy đủ quyền TRADE.",
            )

        return ReadinessProbeResult(
            category=ProbeCategory.TRADE_CAPABILITY,
            status=ReadinessStatus.FAILED,
            message="Tài khoản hoặc API key bị vô hiệu hóa quyền giao dịch (canTrade=False).",
        )

    def probe_stream_and_clock(self, server_time_ms: int | None) -> ReadinessProbeResult:
        """Thăm dò đồng bộ thời gian (clock skew) và luồng WebSocket User Stream."""
        local_time_ms = self._clock_ms()
        # 1. Kiểm tra clock drift
        if server_time_ms is None:
            return ReadinessProbeResult(
                category=ProbeCategory.STREAM_AND_CLOCK,
                status=ReadinessStatus.FAILED,
                message="Thiếu dữ liệu serverTime để kiểm tra clock drift.",
            )

        drift_ms = abs(local_time_ms - server_time_ms)
        if drift_ms > self._max_drift_tolerance_ms:
            return ReadinessProbeResult(
                category=ProbeCategory.STREAM_AND_CLOCK,
                status=ReadinessStatus.FAILED,
                message=(
                    f"Clock drift ({drift_ms}ms) vượt quá dung sai tối đa "
                    f"cho phép ({self._max_drift_tolerance_ms}ms)."
                ),
            )

        # 2. Thăm dò stream listenKey
        url = f"{self._rest_base_url}/fapi/v1/listenKey"
        req = BinanceHttpRequest(
            method="POST",
            url=url,
            headers=(
                ("X-MBX-APIKEY", self._credentials.api_key),
                ("User-Agent", "CrypidianBot"),
            ),
            body=b"",
        )
        try:
            res = self._transport.send(req)
            if res.status_code == 200:
                payload = json.loads(res.body.decode("utf-8"))
                if "listenKey" in payload:
                    return ReadinessProbeResult(
                        category=ProbeCategory.STREAM_AND_CLOCK,
                        status=ReadinessStatus.READY,
                        message=(
                            "Đồng bộ thời gian chuẩn xác và khởi tạo listenKey stream thành công."
                        ),
                    )
            return ReadinessProbeResult(
                category=ProbeCategory.STREAM_AND_CLOCK,
                status=ReadinessStatus.FAILED,
                message=f"Tạo listenKey thất bại: HTTP {res.status_code}.",
            )
        except Exception as exc:
            return ReadinessProbeResult(
                category=ProbeCategory.STREAM_AND_CLOCK,
                status=ReadinessStatus.FAILED,
                message=f"Lỗi khởi tạo stream: {redact_sensitive_text(str(exc))}",
            )

    def check_readiness(self) -> GatewayReadinessReport:
        """Kiểm tra toàn bộ 4 probes độc lập và tổng hợp báo cáo."""
        public_result, server_time_ms = self.probe_public_data()
        user_result, account_data = self.probe_user_data()
        trade_result = self.probe_trade_capability(account_data)
        stream_clock_result = self.probe_stream_and_clock(server_time_ms)

        probes: dict[ProbeCategory, ReadinessProbeResult] = {
            ProbeCategory.PUBLIC_DATA: public_result,
            ProbeCategory.USER_DATA: user_result,
            ProbeCategory.TRADE_CAPABILITY: trade_result,
            ProbeCategory.STREAM_AND_CLOCK: stream_clock_result,
        }

        # Đánh giá overall_status
        has_failed = any(p.status is ReadinessStatus.FAILED for p in probes.values())
        has_degraded = any(p.status is ReadinessStatus.DEGRADED for p in probes.values())

        if has_failed:
            overall_status = ReadinessStatus.FAILED
            is_ready = False
        elif has_degraded:
            overall_status = ReadinessStatus.DEGRADED
            is_ready = False
        else:
            overall_status = ReadinessStatus.READY
            is_ready = True

        return GatewayReadinessReport(
            overall_status=overall_status,
            is_ready=is_ready,
            probes=probes,
            checked_at=datetime.now(UTC),
            environment=self._mode.value,
            account_id=self._account_id,
        )
