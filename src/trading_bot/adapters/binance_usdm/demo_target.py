"""Ranh giới cấu hình execution Demo, không tải credential hoặc gọi mạng."""

from dataclasses import dataclass, field
from enum import StrEnum


class DemoEnvironment(StrEnum):
    """Chỉ biểu diễn môi trường Demo đã được duyệt."""

    DEMO = "DEMO"


@dataclass(frozen=True, slots=True, kw_only=True)
class BinanceDemoTarget:
    """Gắn tài khoản và tham chiếu credential với đúng cặp địa chỉ Demo.

    Chỉ nhận chuỗi địa chỉ chính xác; không chuẩn hóa hoặc suy luận môi trường.
    Tham chiếu chưa chứng minh quyền hoặc môi trường của key: bước tích hợp sau
    phải xác minh bằng secret provider và readiness trước khi dùng transport.
    """

    account_id: str = field(repr=False)
    credential_ref: str = field(repr=False)
    environment: DemoEnvironment = DemoEnvironment.DEMO
    rest_base_url: str = "https://demo-fapi.binance.com"
    private_ws_base_url: str = "wss://demo-fstream.binance.com"

    def __post_init__(self) -> None:
        if self.environment is not DemoEnvironment.DEMO:
            raise ValueError("environment phải là DemoEnvironment.DEMO.")
        for name in ("account_id", "credential_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} phải là chuỗi không trống.")
        if self.rest_base_url != "https://demo-fapi.binance.com":
            raise ValueError("rest_base_url phải là địa chỉ Demo đã pin.")
        if self.private_ws_base_url != "wss://demo-fstream.binance.com":
            raise ValueError("private_ws_base_url phải là địa chỉ Demo đã pin.")
