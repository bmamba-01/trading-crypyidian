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
    "signal-worker",
    "market-signal-worker",
    "trainer-worker",
    "backtest-worker",
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
    ingestion_workspace_id: UUID | None = None
    signal_workspace_id: UUID | None = None
    market_signal_workspace_id: UUID | None = None
    backtest_worker_workspace_id: UUID | None = None
    backtest_worker_tick_seconds: float = Field(default=2.0, gt=0, le=60)
    backtest_worker_lease_seconds: int = Field(default=120, ge=15, le=900)
    backtest_trainer_database_user: str | None = None
    backtest_trainer_database_password_file: Path | None = None
    # Lease một writer theo bot (MB-05): TTL ≤ 300 giây (khớp trigger DB), tick supervisor
    # phải ngắn hơn TTL để kịp gia hạn trước khi hết hạn.
    runtime_lease_ttl_seconds: int = Field(default=30, ge=3, le=300)
    runtime_supervisor_tick_seconds: float = Field(default=5.0, gt=0, le=60)
    ingestion_worker_tick_seconds: float = Field(default=5.0, gt=0, le=300)
    mfa_encryption_key_file: Path | None = None
    mfa_secret_directory: Path | None = None
    bootstrap_token_file: Path | None = None
    # Webhook Telegram (MB-07a ruling 37): gắn với một workspace deployment và một secret riêng;
    # chỉ admin-web phục vụ webhook. Secret chỉ đọc từ file, không nhận qua biến môi trường.
    telegram_workspace_id: UUID | None = None
    telegram_webhook_secret_file: Path | None = None
    telegram_bot_username: str | None = None
    telegram_bot_token_file: Path | None = None
    signal_worker_tick_seconds: float = Field(default=5.0, gt=0, le=300)
    signal_worker_max_candidate_age_seconds: int = Field(default=7200, ge=5, le=14_400)
    signal_worker_max_attempts: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_telegram_webhook(self) -> AppSettings:
        """Webhook Telegram cần đủ workspace + file secret và chỉ chạy ở admin-web."""
        if self.telegram_bot_username == "":
            self.telegram_bot_username = None
        configured = (self.telegram_workspace_id, self.telegram_webhook_secret_file)
        if any(value is not None for value in configured):
            if any(value is None for value in configured):
                raise ValueError(
                    "Webhook Telegram cần cả telegram_workspace_id và telegram_webhook_secret_file."
                )
            if self.process_role != "admin-web":
                raise ValueError("Webhook Telegram chỉ cấu hình cho process_role admin-web.")
        if self.telegram_bot_username is not None:
            import re

            if self.process_role != "admin-web" or not re.fullmatch(
                r"[A-Za-z0-9_]{5,32}", self.telegram_bot_username
            ):
                raise ValueError("telegram_bot_username không hợp lệ hoặc sai process role.")
        if self.telegram_bot_token_file is not None and self.process_role not in {
            "admin-web",
            "signal-worker",
        }:
            raise ValueError("Token Telegram chỉ được cấu hình cho admin-web hoặc signal-worker.")
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

    @model_validator(mode="after")
    def validate_ingestion_identity(self) -> AppSettings:
        """Buộc ingestion worker giới hạn vào một workspace deployment."""
        if self.process_role == "ingestion-worker":
            if self.ingestion_workspace_id is None:
                raise ValueError(
                    "ingestion-worker cần ingestion_workspace_id từ deployment environment."
                )
        elif self.ingestion_workspace_id is not None:
            raise ValueError(
                "ingestion_workspace_id chỉ được cấu hình cho process_role ingestion-worker."
            )
        return self

    @model_validator(mode="after")
    def validate_signal_worker_identity(self) -> AppSettings:
        """Buộc signal worker có workspace riêng và token Bot API từ secret file."""
        if self.process_role == "signal-worker":
            if self.signal_workspace_id is None:
                raise ValueError("signal-worker cần signal_workspace_id từ deployment environment.")
            if self.telegram_bot_token_file is None:
                raise ValueError("signal-worker cần telegram_bot_token_file từ secret file.")
        elif self.signal_workspace_id is not None:
            raise ValueError("signal_workspace_id chỉ dành cho process_role signal-worker.")
        return self

    @model_validator(mode="after")
    def validate_market_signal_worker_identity(self) -> AppSettings:
        """Buộc market signal producer chạy riêng, theo workspace triển khai."""
        if self.process_role == "market-signal-worker":
            if self.market_signal_workspace_id is None:
                raise ValueError(
                    "market-signal-worker cần market_signal_workspace_id từ deployment environment."
                )
            if self.market_signal_workspace_id.int == 0:
                raise ValueError(
                    "market-signal-worker không được dùng workspace UUID placeholder toàn-0."
                )
            if self.telegram_bot_token_file is not None:
                raise ValueError("market-signal-worker không được nhận token Telegram.")
        elif self.market_signal_workspace_id is not None:
            raise ValueError(
                "market_signal_workspace_id chỉ dành cho process_role market-signal-worker."
            )
        return self

    @model_validator(mode="after")
    def validate_backtest_worker_identity(self) -> AppSettings:
        """Buộc replay worker vào một tenant và persistence role riêng."""
        configured_trainer = (
            self.backtest_trainer_database_user,
            self.backtest_trainer_database_password_file,
        )
        if self.process_role == "backtest-worker":
            if (
                self.backtest_worker_workspace_id is None
                or self.backtest_worker_workspace_id.int == 0
            ):
                raise ValueError(
                    "backtest-worker cần workspace UUID đã provision, không dùng UUID toàn-0."
                )
            if self.trading_mode != "PAPER":
                raise ValueError("backtest-worker hiện chỉ hỗ trợ chạy PAPER replay.")
            if any(value is None for value in configured_trainer):
                raise ValueError(
                    "backtest-worker cần credential riêng của trainer_worker để persist."
                )
            if self.database_user != "crypidian_backtest_worker":
                raise ValueError("backtest-worker phải dùng login database replay riêng.")
            if self.backtest_trainer_database_user != "crypidian_trainer_worker":
                raise ValueError("Credential persist phải dùng trainer_worker.")
        else:
            if self.backtest_worker_workspace_id is not None:
                raise ValueError("backtest_worker_workspace_id chỉ dành cho backtest-worker.")
            if any(value is not None for value in configured_trainer):
                raise ValueError("Trainer credential chỉ được gắn vào backtest-worker.")
        return self
