"""Bảng kiểm toán AI Analyst (AI-2): session theo chủ đề và từng lượt phân tích."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.adapters.database.models.base import Base, CreatedAt, UuidPrimaryKey


class AnalystSessionRecord(UuidPrimaryKey, CreatedAt, Base):
    """Session CLI agent của một chủ đề (vd ``family:H03``); tiếp tục để tích lũy ngữ cảnh."""

    __tablename__ = "analyst_session"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "topic_key", "engine", "session_id", name="uq_analyst_session_ref"
        ),
        CheckConstraint("status IN ('ACTIVE', 'RETIRED')", name="analyst_session_status_valid"),
        CheckConstraint("run_count >= 0", name="analyst_session_run_count_nonnegative"),
        Index("ix_analyst_session_topic", "workspace_id", "topic_key", "engine", "status"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    topic_key: Mapped[str] = mapped_column(String(120), nullable=False)
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalystRunRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lượt phân tích: đầu vào băm, đầu ra thô, kết quả validator số liệu (append-only)."""

    __tablename__ = "analyst_run"
    __table_args__ = (
        Index("ix_analyst_run_topic_created", "workspace_id", "topic_key", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    topic_key: Mapped[str] = mapped_column(String(120), nullable=False)
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    output_version: Mapped[str] = mapped_column(String(40), nullable=False)
    cli_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw_stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    validation_errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # CRY-384: bối cảnh ngoài Run Report đã đưa cho AI (lịch vĩ mô, phái sinh, niêm yết) để tái lập.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


class PaperCandidateOutcomeRecord(UuidPrimaryKey, CreatedAt, Base):
    """Lý do một candidate không thành lệnh PAPER (giai đoạn + mã), mỗi cặp ghi một lần.

    Trước 16/11/2026 lý do từ chối plan/risk/entry chỉ nằm trong log runtime nên Run Report và AI
    Analyst không phân tích được tầng nghẽn sau tín hiệu.
    """

    __tablename__ = "paper_candidate_outcome"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "decision_id", "stage", "reason_code", name="uq_paper_candidate_outcome"
        ),
        CheckConstraint(
            "stage IN ('ENTRY_BLOCK', 'STRATEGY', 'PLAN', 'RISK', 'ERROR')",
            name="paper_candidate_outcome_stage_valid",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["bot_decision.workspace_id", "bot_decision.id"],
            ondelete="CASCADE",
        ),
        Index("ix_paper_candidate_outcome_bot", "workspace_id", "bot_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)


class ProposalBacktestRecord(UuidPrimaryKey, CreatedAt, Base):
    """Kết quả backtest một đề xuất AI (AI-3): chỉ số bản gốc/bản đề xuất (append-only)."""

    __tablename__ = "proposal_backtest"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "analyst_run_id", "proposal_index", name="uq_proposal_backtest_ref"
        ),
        CheckConstraint(
            "verdict IN ('BETTER', 'WORSE', 'INCONCLUSIVE', 'UNSUPPORTED')",
            name="proposal_backtest_verdict_valid",
        ),
        CheckConstraint("proposal_index >= 0", name="proposal_backtest_index_nonnegative"),
        CheckConstraint(
            "source IN ('AI_PROPOSAL', 'OWNER_DRAFT')", name="proposal_backtest_source_valid"
        ),
        CheckConstraint(
            "(source = 'AI_PROPOSAL') = (analyst_run_id IS NOT NULL)",
            name="proposal_backtest_source_ref",
        ),
        Index("ix_proposal_backtest_created", "workspace_id", "created_at"),
        Index(
            "ix_proposal_backtest_owner_draft",
            "workspace_id",
            "bot_id",
            "created_at",
            postgresql_where=text("source = 'OWNER_DRAFT'"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    # CRY-428: NULL khi ``source = OWNER_DRAFT`` (backtest bản nháp cấu hình của owner).
    analyst_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analyst_run.id", ondelete="CASCADE"), nullable=True
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="AI_PROPOSAL", server_default="AI_PROPOSAL"
    )
    proposal_index: Mapped[int] = mapped_column(Integer, nullable=False)
    topic_key: Mapped[str] = mapped_column(String(120), nullable=False)
    bot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    bot_name: Mapped[str] = mapped_column(String(160), nullable=False)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)


class MacroCalendarFetchRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lần tải lịch công bố vĩ mô chính thức (AI-4): danh sách sự kiện bất biến + hash."""

    __tablename__ = "macro_calendar_fetch"
    __table_args__ = (
        CheckConstraint("event_count >= 0", name="macro_calendar_fetch_count_nonnegative"),
        CheckConstraint(
            "events_sha256 ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="macro_calendar_fetch_hash_sha256",
        ),
        Index("ix_macro_calendar_fetch_asof", "workspace_id", "source_id", "fetched_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(80), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(String(300), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    events_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


_STAT = Numeric(38, 18)


class DerivativesSnapshotRecord(UuidPrimaryKey, CreatedAt, Base):
    """Ảnh chụp phái sinh Binance một symbol (AI-4 lát 2): append-only, theo ``collected_at``."""

    __tablename__ = "derivatives_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "symbol", "collected_at", name="uq_derivatives_snapshot_point"
        ),
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="derivatives_snapshot_hash_sha256",
        ),
        CheckConstraint(
            "premium_time <= collected_at"
            " AND (oi_at IS NULL OR oi_at <= collected_at)"
            " AND (ls_at IS NULL OR ls_at <= collected_at)"
            " AND (top_at IS NULL OR top_at <= collected_at)"
            " AND (taker_at IS NULL OR taker_at <= collected_at)"
            " AND (basis_at IS NULL OR basis_at <= collected_at)"
            " AND (ticker_at IS NULL OR ticker_at <= collected_at)",
            name="derivatives_snapshot_timestamps_causal",
        ),
        CheckConstraint(
            "mark_price > 0"
            " AND (oi_value_usdt IS NULL OR oi_value_usdt >= 0)"
            " AND (index_price IS NULL OR index_price > 0)"
            " AND (ticker_last_price IS NULL OR ticker_last_price > 0)"
            " AND (ticker_quote_volume_24h IS NULL OR ticker_quote_volume_24h >= 0)",
            name="derivatives_snapshot_values_valid",
        ),
        Index("ix_derivatives_snapshot_asof", "workspace_id", "collected_at", "symbol"),
        Index("ix_derivatives_snapshot_expires", "workspace_id", "expires_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(_STAT, nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(_STAT, nullable=False)
    next_funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    premium_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    oi_contracts: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    oi_value_usdt: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    oi_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oi_value_24h_ago: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    long_short_account_ratio: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    ls_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    top_position_ratio: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    top_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    taker_buy_sell_ratio: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    taker_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_price: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    basis: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    basis_rate: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    basis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ticker_last_price: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    ticker_quote_volume_24h: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    ticker_price_change_pct_24h: Mapped[Decimal | None] = mapped_column(_STAT, nullable=True)
    ticker_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ListingSnapshotRecord(UuidPrimaryKey, CreatedAt, Base):
    """Một lần kiểm ``exchangeInfo`` (AI-4 lát 3); ``entries`` chỉ lưu khi nội dung đổi."""

    __tablename__ = "listing_snapshot"
    __table_args__ = (
        CheckConstraint(
            "entries_sha256 ~ '^[0-9a-f]{64}$' AND policy_fingerprint ~ '^[0-9a-f]{64}$'",
            name="listing_snapshot_hash_sha256",
        ),
        CheckConstraint(
            "changed = (entries IS NOT NULL) AND entry_count > 0",
            name="listing_snapshot_entries_iff_changed",
        ),
        Index("ix_listing_snapshot_asof", "workspace_id", "fetched_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    entries_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # none_as_null: lần kiểm không đổi lưu SQL NULL (mặc định JSONB ghi `None` thành JSON 'null'
    # → vi phạm ràng buộc `changed = (entries IS NOT NULL)`; lỗi thấy ở lần kiểm thứ hai 16/11).
    entries: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )


class RunReportRecord(UuidPrimaryKey, CreatedAt, Base):
    """Run Report tất định đã lưu (CRY-380): bất biến (trigger chặn UPDATE/DELETE)."""

    __tablename__ = "run_report"
    __table_args__ = (
        UniqueConstraint("workspace_id", "report_hash", name="uq_run_report_hash"),
        CheckConstraint("report_hash ~ '^[0-9a-f]{64}$'", name="run_report_hash_sha256"),
        CheckConstraint(
            "payload->>'report_hash' = report_hash", name="run_report_payload_hash_matches"
        ),
        CheckConstraint("window_start < window_end", name="run_report_window_ordered"),
        CheckConstraint("bot_count >= 0", name="run_report_bot_count_nonnegative"),
        Index("ix_run_report_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_version: Mapped[str] = mapped_column(String(40), nullable=False)
    run_label: Mapped[str] = mapped_column(String(120), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
