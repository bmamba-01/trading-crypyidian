from __future__ import annotations

import re
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


# Interval có độ dài cố định mà kho market và REST backfill cùng hỗ trợ.
_LIVE_TIMEFRAMES = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}
)
# Giữ dưới trần 1024 stream/kết nối của Binance và giới hạn tải REST lúc warm-up.
_MAX_LIVE_STREAMS = 200
_LIVE_EVENT_ORDER = ("mark_price", "liquidation", "trade_flow", "book_ticker")
_LIVE_EVENT_KINDS = frozenset(_LIVE_EVENT_ORDER)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


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
    # RT-1 lát 2: trading-runtime PAPER tự nhận CANDIDATE và đưa nến đóng đã lưu vào lifecycle
    # (khớp entry, theo dõi stop/target, hết hạn reservation). Mặc định tắt; chỉ PAPER, không gọi
    # API sàn nào (chỉ đọc `market_bar` đã persist).
    runtime_market_dispatch_enabled: bool = False
    ingestion_worker_tick_seconds: float = Field(default=5.0, gt=0, le=300)
    # Thu nến live qua WebSocket công khai; để trống symbols là tắt (mặc định fail-closed).
    ingestion_live_symbols: str = ""
    ingestion_live_timeframes: str = "15m,1h,4h"
    ingestion_live_warmup_bars: int = Field(default=300, ge=200, le=1500)
    # Stream bổ sung: mark_price, liquidation, trade_flow, book_ticker; trống là chỉ thu nến.
    ingestion_live_events: str = ""
    ingestion_live_mark_price_interval_seconds: int = Field(default=60, ge=1, le=3600)
    # CK-1: nhịp ghi giá mua/bán tốt nhất (bookTicker, route /public) vào bảng giá trực tiếp.
    ingestion_live_quote_interval_seconds: float = Field(default=2.0, ge=0.5, le=60)
    # SIG-0: collector nến công khai Binance trong ingestion-worker (owner duyệt 15/11/2026 cho
    # PAPER nội bộ). Mặc định tắt; khi bật vẫn phải có policy PAPER_DEMO hợp lệ trong registry.
    market_collector_enabled: bool = False
    market_collector_streams: str = "BTCUSDT:1m,5m,15m,1h,4h;ETHUSDT:1m,5m,15m,1h,4h"
    market_collector_tick_seconds: float = Field(default=30.0, ge=5, le=600)
    market_collector_initial_bars: int = Field(default=1000, ge=1, le=15000)
    # AI-4: lịch công bố vĩ mô chính thức (BEA, FOMC) cho H06, trong ingestion-worker. Mặc định
    # tắt; khi bật mỗi nguồn vẫn cần policy Source Registry cấp PAPER_DEMO.
    macro_calendar_enabled: bool = False
    # AI-4 lát 2: ảnh chụp phái sinh công khai Binance (funding, OI, long/short, taker) theo giờ cho
    # các symbol của market_collector_streams. Mặc định tắt; cần policy PAPER_DEMO của nguồn.
    derivatives_collector_enabled: bool = False
    # AI-4 lát 3: kiểm exchangeInfo Binance mỗi 30 phút để phát hiện niêm yết/gỡ hợp đồng.
    listings_collector_enabled: bool = False
    # Cờ kiểm thử: chấp nhận mã xác thực cố định 123456 (chỉ local/test + PAPER, mặc định tắt).
    auth_test_totp_enabled: bool = False
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
    # CRY-430: chu kỳ worker chấm kết quả tín hiệu chạy trong market-signal-worker (0 = tắt).
    signal_outcome_tick_seconds: float = Field(default=60.0, ge=0, le=3600)
    # TG-2: chống spam theo subscription — cooldown tín hiệu trùng và trần số tin/60 phút.
    signal_worker_cooldown_minutes: int = Field(default=90, ge=0, le=1440)
    signal_worker_max_messages_per_hour: int = Field(default=6, ge=1, le=60)
    # TG-3: gửi tín hiệu/kết quả/tổng kết dạng ảnh thẻ (PNG + caption); False = văn bản thuần.
    telegram_card_images_enabled: bool = True
    # TG-3 (quyết định owner 15/11/2026): signal-worker gửi kết quả trade PAPER (mở/đóng) tới
    # subscription đã bật và tin tổng hợp định kỳ. Chỉ đọc ledger PAPER, không phát lệnh.
    telegram_trade_results_enabled: bool = True
    # Chỉ báo sự kiện ghi trong khoảng này (tránh dội tin lịch sử khi mới bật/khởi động lại).
    telegram_trade_result_max_age_minutes: int = Field(default=60, ge=1, le=1440)
    # Chu kỳ tin tổng hợp so sánh các bot; 0 = tắt. Tối thiểu 15 phút để tránh spam.
    telegram_report_interval_minutes: int = Field(default=240, ge=0, le=10_080)

    # Chính sách TEST scorer phải có version/hash giống hệt report và deployment được ghim.
    scorer_test_policy_version: str = Field(
        default="wp06-scorer-test-v1", min_length=1, max_length=120
    )
    scorer_test_policy_minimum_sample_count: int = Field(default=1_000, ge=1)
    scorer_test_policy_minimum_class_support: int = Field(default=100, ge=1)
    scorer_test_policy_maximum_calibration_error: float = Field(default=0.05, ge=0, le=1)
    scorer_test_policy_maximum_log_loss_regression: float = Field(
        default=0.01, ge=0, allow_inf_nan=False
    )
    scorer_test_policy_maximum_brier_regression: float = Field(
        default=0.01, ge=0, allow_inf_nan=False
    )
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
    def validate_auth_test_totp(self) -> AppSettings:
        """Mã xác thực cố định là lối tắt MFA: cấm tuyệt đối ngoài local/test và PAPER."""
        if self.auth_test_totp_enabled and (
            self.app_env not in {"local", "test"} or self.trading_mode != "PAPER"
        ):
            raise ValueError(
                "Chế độ kiểm thử mã xác thực chỉ bật được khi app_env là local/test và mode PAPER."
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
        if self.runtime_market_dispatch_enabled and (
            self.process_role != "trading-runtime" or self.trading_mode != "PAPER"
        ):
            raise ValueError(
                "runtime_market_dispatch_enabled chỉ dành cho trading-runtime ở chế độ PAPER."
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
        if self.macro_calendar_enabled and self.process_role != "ingestion-worker":
            raise ValueError("macro calendar chỉ chạy trong process_role ingestion-worker.")
        if self.derivatives_collector_enabled and self.process_role != "ingestion-worker":
            raise ValueError("derivatives collector chỉ chạy trong process_role ingestion-worker.")
        if self.listings_collector_enabled and self.process_role != "ingestion-worker":
            raise ValueError("listings collector chỉ chạy trong process_role ingestion-worker.")
        if self.market_collector_enabled:
            if self.process_role != "ingestion-worker":
                raise ValueError("market collector chỉ chạy trong process_role ingestion-worker.")
            if self.trading_mode != "PAPER":
                raise ValueError("market collector SIG-0 chỉ được duyệt cho chế độ PAPER.")
            from trading_bot.application.market_bar_collector import parse_collector_streams

            parse_collector_streams(self.market_collector_streams)
        return self

    @property
    def ingestion_live_subscriptions(self) -> tuple[tuple[str, str], ...]:
        """Tích symbol × timeframe đã sắp xếp; rỗng khi live ingest đang tắt."""
        symbols = _split_csv(self.ingestion_live_symbols)
        timeframes = _split_csv(self.ingestion_live_timeframes)
        return tuple(
            (symbol, timeframe) for symbol in sorted(symbols) for timeframe in sorted(timeframes)
        )

    @model_validator(mode="after")
    def validate_live_ingestion(self) -> AppSettings:
        """Chỉ ingestion worker được bật live ingest, với symbol/timeframe hợp lệ."""
        symbols = _split_csv(self.ingestion_live_symbols)
        if not symbols:
            if _split_csv(self.ingestion_live_events):
                raise ValueError("ingestion_live_events cần ingestion_live_symbols.")
            return self
        if self.process_role != "ingestion-worker":
            raise ValueError(
                "ingestion_live_symbols chỉ được cấu hình cho process_role ingestion-worker."
            )
        timeframes = _split_csv(self.ingestion_live_timeframes)
        if len(set(symbols)) != len(symbols) or any(
            re.fullmatch(r"[A-Z0-9]{3,32}", symbol) is None for symbol in symbols
        ):
            raise ValueError("ingestion_live_symbols phải là mã USD-M viết hoa, không trùng lặp.")
        if (
            not timeframes
            or len(set(timeframes)) != len(timeframes)
            or not set(timeframes) <= _LIVE_TIMEFRAMES
        ):
            raise ValueError("ingestion_live_timeframes chứa interval không được hỗ trợ.")
        if len(symbols) * len(timeframes) > _MAX_LIVE_STREAMS:
            raise ValueError("ingestion_live_symbols vượt giới hạn stream của một worker.")
        events = _split_csv(self.ingestion_live_events)
        if len(set(events)) != len(events) or not set(events) <= _LIVE_EVENT_KINDS:
            raise ValueError("ingestion_live_events chứa loại stream không được hỗ trợ.")
        if len(symbols) * len(events) > _MAX_LIVE_STREAMS:
            raise ValueError("ingestion_live_events vượt giới hạn stream của một worker.")
        return self

    @property
    def ingestion_live_event_kinds(self) -> tuple[str, ...]:
        """Loại stream sự kiện đã bật, theo thứ tự cố định."""
        enabled = set(_split_csv(self.ingestion_live_events))
        return tuple(kind for kind in _LIVE_EVENT_ORDER if kind in enabled)

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
        if 0 < self.telegram_report_interval_minutes < 15:
            raise ValueError("telegram_report_interval_minutes phải là 0 (tắt) hoặc ≥ 15 phút.")
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
