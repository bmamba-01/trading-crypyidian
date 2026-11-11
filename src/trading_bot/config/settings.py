from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from trading_bot.application.learning_scorer_evaluation import (
        ScorerTestEvaluationPolicy,
    )

TradingMode = Literal["PAPER", "DEMO"]
ProcessRole = Literal[
    "admin-web",
    "trading-runtime",
    "execution-gateway",
    "ingestion-worker",
    "signal-worker",
    "market-signal-worker",
    "trainer-worker",
    "learning-shadow-observer",
    "learning-evaluator",
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
    learning_training_workspace_id: UUID | None = None
    learning_training_worker_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    learning_training_worker_max_backoff_seconds: float = Field(default=60.0, gt=0, le=3600)
    learning_shadow_observer_workspace_id: UUID | None = None
    learning_shadow_observer_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    learning_shadow_observer_max_backoff_seconds: float = Field(default=60.0, gt=0, le=3600)
    backtest_worker_workspace_id: UUID | None = None
    backtest_worker_tick_seconds: float = Field(default=2.0, gt=0, le=60)
    backtest_worker_lease_seconds: int = Field(default=120, ge=15, le=900)
    backtest_replay_database_user: str | None = None
    backtest_replay_database_password_file: Path | None = None
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

    # Chính sách TEST scorer phải có version/hash giống hệt report và deployment được ghim.
    scorer_test_policy_version: str = Field(
        default="wp06-scorer-test-v1", min_length=1, max_length=120
    )
    scorer_test_policy_minimum_sample_count: int = Field(default=1_000, ge=1)
    scorer_test_policy_minimum_class_support: int = Field(default=100, ge=1)
    scorer_test_policy_maximum_calibration_error: float = Field(default=0.05, ge=0, le=1)
    scorer_test_policy_maximum_log_loss_regression: float = Field(default=0.01, ge=0)
    scorer_test_policy_maximum_brier_regression: float = Field(default=0.01, ge=0)
    scorer_test_policy_minimum_coverage: float = Field(default=0.80, ge=0, le=1)
    scorer_test_policy_minimum_candidate_retention: float = Field(default=0.20, ge=0, le=1)
    scorer_test_policy_maximum_false_veto_rate: float = Field(default=0.10, ge=0, le=1)
    scorer_test_policy_approve_probability_threshold: float = Field(default=0.60, ge=0, le=1)
    scorer_test_policy_minimum_confidence: float = Field(default=0.55, ge=0, le=1)
    scorer_test_policy_reliability_bin_count: int = Field(default=10, ge=2, le=100)
    # Giới hạn độ lệch chuẩn hóa theo từng đặc trưng để runtime fail-closed khi OOD.
    scorer_test_policy_maximum_standardized_feature_deviation: float = Field(
        default=6.0, gt=0, allow_inf_nan=False
    )
    # Deadline riêng cho phép tính scorer thuần; không tính truy vấn/commit DB.
    scorer_test_policy_runtime_inference_timeout_ms: int = Field(default=100, gt=0, le=10_000)

    def scorer_test_evaluation_policy(self) -> ScorerTestEvaluationPolicy:
        """Tạo policy bất biến; thay đổi env tạo hash mới để report cũ bị từ chối."""
        from trading_bot.application.learning_scorer_evaluation import (
            ScorerTestEvaluationPolicy,
        )

        return ScorerTestEvaluationPolicy(
            policy_version=self.scorer_test_policy_version,
            minimum_sample_count=self.scorer_test_policy_minimum_sample_count,
            minimum_class_support=self.scorer_test_policy_minimum_class_support,
            maximum_calibration_error=self.scorer_test_policy_maximum_calibration_error,
            maximum_log_loss_regression=self.scorer_test_policy_maximum_log_loss_regression,
            maximum_brier_regression=self.scorer_test_policy_maximum_brier_regression,
            minimum_coverage=self.scorer_test_policy_minimum_coverage,
            minimum_candidate_retention=self.scorer_test_policy_minimum_candidate_retention,
            maximum_false_veto_rate=self.scorer_test_policy_maximum_false_veto_rate,
            approve_probability_threshold=self.scorer_test_policy_approve_probability_threshold,
            minimum_confidence=self.scorer_test_policy_minimum_confidence,
            reliability_bin_count=self.scorer_test_policy_reliability_bin_count,
            maximum_standardized_feature_deviation=(
                self.scorer_test_policy_maximum_standardized_feature_deviation
            ),
            runtime_inference_timeout_ms=self.scorer_test_policy_runtime_inference_timeout_ms,
        )

    @model_validator(mode="after")
    def validate_scorer_test_policy_support(self) -> AppSettings:
        """Ba lớp đều cần đủ chỗ trong tổng cỡ mẫu tối thiểu."""
        if self.scorer_test_policy_minimum_sample_count < (
            self.scorer_test_policy_minimum_class_support * 3
        ):
            raise ValueError(
                "scorer_test_policy_minimum_sample_count phải >= 3 lần "
                "scorer_test_policy_minimum_class_support."
            )
        return self

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
        """Buộc replay worker vào tenant và credential persist riêng."""
        configured_replay_writer = (
            self.backtest_replay_database_user,
            self.backtest_replay_database_password_file,
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
            if any(value is None for value in configured_replay_writer):
                raise ValueError("backtest-worker cần credential replay-writer riêng để persist.")
            if self.database_user != "crypidian_backtest_worker":
                raise ValueError("backtest-worker phải dùng login database replay riêng.")
            if self.backtest_replay_database_user != "crypidian_replay_writer":
                raise ValueError("Credential persist phải dùng crypidian_replay_writer.")
        else:
            if self.backtest_worker_workspace_id is not None:
                raise ValueError("backtest_worker_workspace_id chỉ dành cho backtest-worker.")
            if any(value is not None for value in configured_replay_writer):
                raise ValueError("Replay-writer credential chỉ được gắn vào backtest-worker.")
        return self

    @model_validator(mode="after")
    def validate_learning_evaluator_identity(self) -> AppSettings:
        """Evaluator không thể khởi động với credential admin hoặc trainer."""
        if self.process_role == "learning-evaluator":
            if self.trading_mode != "PAPER":
                raise ValueError("learning-evaluator chỉ chạy trong chế độ PAPER.")
            if self.database_user != "crypidian_learning_evaluator":
                raise ValueError("learning-evaluator cần login database evaluator riêng.")
            if self.database_password_file is None:
                raise ValueError("learning-evaluator cần secret database evaluator riêng.")
        return self

    @model_validator(mode="after")
    def validate_trainer_worker_identity(self) -> AppSettings:
        """Trainer chỉ dùng PAPER, một workspace và login database riêng."""
        if self.process_role == "trainer-worker":
            if self.trading_mode != "PAPER":
                raise ValueError("trainer-worker hiện chỉ chạy trong chế độ PAPER.")
            if self.database_user != "crypidian_trainer_worker":
                raise ValueError("trainer-worker cần login database trainer riêng.")
            if self.database_password_file is None:
                raise ValueError("trainer-worker cần secret database trainer riêng.")
            if self.learning_training_workspace_id is None:
                raise ValueError(
                    "trainer-worker cần learning_training_workspace_id từ deployment environment."
                )
            if self.learning_training_workspace_id.int == 0 and self.app_env != "local":
                raise ValueError("trainer-worker chỉ cho workspace UUID toàn-0 trong local.")
            if (
                self.learning_training_worker_max_backoff_seconds
                < self.learning_training_worker_poll_seconds
            ):
                raise ValueError("Backoff tối đa phải lớn hơn hoặc bằng chu kỳ poll.")
        elif self.learning_training_workspace_id is not None:
            raise ValueError(
                "learning_training_workspace_id chỉ dành cho process_role trainer-worker."
            )
        return self

    @model_validator(mode="after")
    def validate_learning_shadow_observer_identity(self) -> AppSettings:
        """Giới hạn observer vào PAPER, một workspace và credential database riêng."""
        if self.process_role == "learning-shadow-observer":
            if self.trading_mode != "PAPER":
                raise ValueError("learning-shadow-observer chỉ chạy trong chế độ PAPER.")
            if self.database_user != "crypidian_learning_shadow_observer":
                raise ValueError("learning-shadow-observer cần login database observer riêng.")
            if self.database_password_file != Path(
                "/run/secrets/database_learning_shadow_observer_password"
            ):
                raise ValueError("learning-shadow-observer cần secret database observer cố định.")
            if self.learning_shadow_observer_workspace_id is None:
                raise ValueError(
                    "learning-shadow-observer cần learning_shadow_observer_workspace_id "
                    "từ deployment environment."
                )
            if self.learning_shadow_observer_workspace_id.int == 0 and self.app_env != "local":
                raise ValueError(
                    "learning-shadow-observer chỉ cho workspace UUID toàn-0 trong local."
                )
            if (
                self.learning_shadow_observer_max_backoff_seconds
                < self.learning_shadow_observer_poll_seconds
            ):
                raise ValueError("Backoff tối đa phải lớn hơn hoặc bằng chu kỳ poll.")
        elif self.learning_shadow_observer_workspace_id is not None:
            raise ValueError(
                "learning_shadow_observer_workspace_id chỉ dành cho process_role "
                "learning-shadow-observer."
            )
        return self
