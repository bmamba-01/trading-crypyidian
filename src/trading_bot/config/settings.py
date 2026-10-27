from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TradingMode = Literal["PAPER", "DEMO"]
ProcessRole = Literal[
    "admin-web",
    "trading-runtime",
    "execution-gateway",
    "ingestion-worker",
    "trainer-worker",
]


class AppSettings(BaseSettings):
    """Cấu hình an toàn cho lần khởi động đầu tiên của ứng dụng."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_BOT_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
    )

    app_env: Literal["local", "test", "production"] = "local"
    trading_mode: TradingMode = "PAPER"
    process_role: ProcessRole = "admin-web"
    database_user: str | None = None
    database_password_file: Path | None = None
    database_host: str = "postgres"
    database_name: str = "crypidian"
    runtime_workspace_id: UUID | None = None
    # Lease một writer theo bot (MB-05): TTL ≤ 300 giây (khớp trigger DB), tick supervisor
    # phải ngắn hơn TTL để kịp gia hạn trước khi hết hạn.
    runtime_lease_ttl_seconds: int = Field(default=30, ge=3, le=300)
    runtime_supervisor_tick_seconds: float = Field(default=5.0, gt=0, le=60)
    mfa_encryption_key_file: Path | None = None
    mfa_secret_directory: Path | None = None
    bootstrap_token_file: Path | None = None
    # Webhook Telegram (MB-07a ruling 37): gắn với một workspace deployment và một secret riêng;
    # chỉ admin-web phục vụ webhook. Secret chỉ đọc từ file, không nhận qua biến môi trường.
    telegram_workspace_id: UUID | None = None
    telegram_webhook_secret_file: Path | None = None

    @model_validator(mode="after")
    def validate_telegram_webhook(self) -> AppSettings:
        """Webhook Telegram cần đủ workspace + file secret và chỉ chạy ở admin-web."""
        configured = (self.telegram_workspace_id, self.telegram_webhook_secret_file)
        if any(value is not None for value in configured):
            if any(value is None for value in configured):
                raise ValueError(
                    "Webhook Telegram cần cả telegram_workspace_id và telegram_webhook_secret_file."
                )
            if self.process_role != "admin-web":
                raise ValueError("Webhook Telegram chỉ cấu hình cho process_role admin-web.")
        return self

    @model_validator(mode="after")
    def validate_runtime_identity(self) -> AppSettings:
        """Buộc PAPER runtime được giới hạn vào một workspace deployment."""
        if self.process_role == "trading-runtime":
            if self.runtime_workspace_id is None:
                raise ValueError(
                    "trading-runtime cần runtime_workspace_id từ deployment environment."
                )
            if self.trading_mode != "PAPER":
                raise ValueError("trading-runtime hiện chỉ hỗ trợ mode PAPER.")
            if self.runtime_supervisor_tick_seconds >= self.runtime_lease_ttl_seconds:
                raise ValueError("Tick supervisor phải ngắn hơn TTL lease runtime.")
        elif self.runtime_workspace_id is not None:
            raise ValueError(
                "runtime_workspace_id chỉ được cấu hình cho process_role trading-runtime."
            )
        return self
