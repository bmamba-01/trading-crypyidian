from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import model_validator
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
    mfa_encryption_key_file: Path | None = None
    mfa_secret_directory: Path | None = None
    bootstrap_token_file: Path | None = None

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
        elif self.runtime_workspace_id is not None:
            raise ValueError(
                "runtime_workspace_id chỉ được cấu hình cho process_role trading-runtime."
            )
        return self
