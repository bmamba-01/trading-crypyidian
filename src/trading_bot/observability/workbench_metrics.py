"""Chỉ số freshness/coverage/lag/latency và cảnh báo vận hành WP-07E (CRY-376)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil
from typing import Any

from trading_bot.observability.redaction import REDACTED_TOKEN, redact_value


class AlertSeverity(StrEnum):
    """Mức độ nghiêm trọng của cảnh báo vận hành workbench."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCategory(StrEnum):
    """Nhóm nguyên nhân cảnh báo theo hợp đồng WP-07E."""

    DATA_STALE = "data_stale"
    SCANNER_LAG = "scanner_lag"
    STRATEGY_UNPUBLISHED = "strategy_unpublished"
    BINDING_REVOKED = "binding_revoked"
    SUBSCRIPTION_INVALID = "subscription_invalid"
    DATASET_RIGHTS_EXPIRED = "dataset_rights_expired"
    DELIVERY_DEAD_LETTER = "delivery_dead_letter"


@dataclass(frozen=True, slots=True)
class BarFreshnessMetric:
    """Độ tươi (freshness), độ phủ (coverage) và số gap của chuỗi nến."""

    symbol: str
    timeframe: str
    latest_bar_close_at: datetime | None
    age_seconds: float | None
    expected_bars: int
    actual_bars: int
    coverage_ratio: float
    gap_count: int
    is_stale: bool
    trace_run_id: str | None = None
    evidence_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "latest_bar_close_at": (
                self.latest_bar_close_at.isoformat() if self.latest_bar_close_at else None
            ),
            "age_seconds": round(self.age_seconds, 2) if self.age_seconds is not None else None,
            "expected_bars": self.expected_bars,
            "actual_bars": self.actual_bars,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "gap_count": self.gap_count,
            "is_stale": self.is_stale,
            "trace_run_id": self.trace_run_id,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True, slots=True)
class ScannerLagMetric:
    """Độ trễ chu kỳ quét tín hiệu thị trường."""

    last_scan_at: datetime | None
    lag_seconds: float | None
    is_lagging: bool
    trace_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "lag_seconds": round(self.lag_seconds, 2) if self.lag_seconds is not None else None,
            "is_lagging": self.is_lagging,
            "trace_run_id": self.trace_run_id,
        }


@dataclass(frozen=True, slots=True)
class DeliveryLatencyMetric:
    """Độ trễ từ lúc tạo candidate đến lúc gửi Telegram cùng bộ đếm retry/dead-letter."""

    sample_count: int
    p50_seconds: float | None
    p95_seconds: float | None
    max_seconds: float | None
    retry_count: int
    dead_letter_count: int
    trace_outbox_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "p50_seconds": round(self.p50_seconds, 3) if self.p50_seconds is not None else None,
            "p95_seconds": round(self.p95_seconds, 3) if self.p95_seconds is not None else None,
            "max_seconds": round(self.max_seconds, 3) if self.max_seconds is not None else None,
            "retry_count": self.retry_count,
            "dead_letter_count": self.dead_letter_count,
            "trace_outbox_id": self.trace_outbox_id,
        }


@dataclass(frozen=True, slots=True)
class BackfillProgressMetric:
    """Tiến độ và checksum archive của một job backfill lịch sử."""

    job_id: str
    manifest_id: str | None
    processed_bars: int
    total_bars: int
    progress_ratio: float
    status: str
    archive_checksum: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "manifest_id": self.manifest_id,
            "processed_bars": self.processed_bars,
            "total_bars": self.total_bars,
            "progress_ratio": round(self.progress_ratio, 4),
            "status": self.status,
            "archive_checksum": self.archive_checksum,
        }


@dataclass(frozen=True, slots=True)
class WorkbenchAlert:
    """Cảnh báo vận hành tiếng Việt có mã truy vết tới run/manifest/evidence."""

    code: str
    severity: AlertSeverity
    category: AlertCategory
    title_vi: str
    detail_vi: str
    trace_ref: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Che toàn bộ thông tin nhạy cảm/PII trước khi xuất JSON hoặc log.
        safe_title = str(redact_value(self.title_vi))
        safe_detail = str(redact_value(self.detail_vi))
        safe_meta = _sanitize_alert_metadata(self.metadata)
        return {
            "code": self.code,
            "severity": self.severity.value,
            "category": self.category.value,
            "title_vi": safe_title,
            "detail_vi": safe_detail,
            "trace_ref": str(redact_value(self.trace_ref)) if self.trace_ref else None,
            "detected_at": self.detected_at.isoformat(),
            "metadata": safe_meta,
        }


@dataclass(frozen=True, slots=True)
class WorkbenchMetricsReport:
    """Báo cáo tổng hợp metric & cảnh báo WP-07E cho Admin API và Dashboard."""

    generated_at: datetime
    bar_freshness: tuple[BarFreshnessMetric, ...]
    scanner_lag: ScannerLagMetric
    delivery_latency: DeliveryLatencyMetric
    backfill_progress: tuple[BackfillProgressMetric, ...]
    alerts: tuple[WorkbenchAlert, ...]

    def to_dict(self) -> dict[str, Any]:
        critical_count = sum(1 for item in self.alerts if item.severity == AlertSeverity.CRITICAL)
        warning_count = sum(1 for item in self.alerts if item.severity == AlertSeverity.WARNING)
        stale_bars = sum(1 for item in self.bar_freshness if item.is_stale)
        total_gaps = sum(item.gap_count for item in self.bar_freshness)
        return {
            "generated_at": self.generated_at.isoformat(),
            "summary": {
                "alert_count": len(self.alerts),
                "critical_count": critical_count,
                "warning_count": warning_count,
                "stale_bar_streams": stale_bars,
                "total_bar_gaps": total_gaps,
                "scanner_lagging": self.scanner_lag.is_lagging,
                "delivery_dead_letters": self.delivery_latency.dead_letter_count,
                "active_backfills": sum(
                    1
                    for item in self.backfill_progress
                    if item.status.upper() in {"RUNNING", "PENDING", "QUEUED"}
                ),
            },
            "bar_freshness": [item.to_dict() for item in self.bar_freshness],
            "scanner_lag": self.scanner_lag.to_dict(),
            "delivery_latency": self.delivery_latency.to_dict(),
            "backfill_progress": [item.to_dict() for item in self.backfill_progress],
            "alerts": [item.to_dict() for item in self.alerts],
        }


@dataclass(frozen=True, slots=True)
class StrategyPublishInput:
    """Trạng thái publish strategy của bot hoặc release cần kiểm tra."""

    bot_name: str
    release_id: str
    status: str
    trace_ref: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramBindingHealthInput:
    """Trạng thái binding Telegram đã che danh tính (không truyền raw chat/token)."""

    binding_id: str
    revoked_at: datetime | None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramSubscriptionHealthInput:
    """Trạng thái subscription tín hiệu Telegram cần rà soát hợp lệ."""

    subscription_id: str
    bot_name: str
    enabled: bool
    binding_active: bool
    invalid_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetRightsInput:
    """Trạng thái quyền dataset/source grant hiện hành."""

    grant_id: str
    source_code: str
    status: str
    expires_at: datetime | None = None
    manifest_id: str | None = None


_PII_METADATA_KEYS: frozenset[str] = frozenset(
    {"chat_id", "telegram_chat_id", "telegram_user_id", "username", "email", "phone", "bot_token"}
)


def _sanitize_alert_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Che token, secret và PII (chat_id, telegram_user_id, email) khỏi metadata cảnh báo."""
    cleaned: dict[str, Any] = {}
    for key, val in metadata.items():
        lowered = str(key).strip().lower()
        if lowered in _PII_METADATA_KEYS:
            cleaned[str(key)] = REDACTED_TOKEN
        else:
            cleaned[str(key)] = redact_value(val, key_hint=str(key))
    return cleaned


def compute_percentile_seconds(values: Sequence[float], quantile: float) -> float | None:
    """Tính phân vị (0..1) của chuỗi độ trễ giây bằng nearest-rank."""
    clean = sorted(max(0.0, float(v)) for v in values)
    if not clean:
        return None
    rank = max(1, ceil(quantile * len(clean)))
    return clean[min(rank - 1, len(clean) - 1)]


def evaluate_bar_freshness(
    *,
    symbol: str,
    timeframe: str,
    latest_bar_close_at: datetime | None,
    expected_bars: int,
    actual_bars: int,
    gap_count: int,
    max_age_seconds: int,
    now: datetime,
    trace_run_id: str | None = None,
    evidence_id: str | None = None,
) -> BarFreshnessMetric:
    """Tính freshness, coverage và trạng thái stale cho một cặp symbol/timeframe."""
    safe_expected = max(0, expected_bars)
    safe_actual = max(0, actual_bars)
    coverage = 1.0 if safe_expected == 0 else min(1.0, safe_actual / safe_expected)
    if latest_bar_close_at is None:
        age_seconds: float | None = None
        is_stale = True
    else:
        aware_close = (
            latest_bar_close_at
            if latest_bar_close_at.tzinfo is not None
            else latest_bar_close_at.replace(tzinfo=UTC)
        )
        age_seconds = max(0.0, (now - aware_close).total_seconds())
        is_stale = age_seconds > max(1, max_age_seconds)
    return BarFreshnessMetric(
        symbol=symbol,
        timeframe=timeframe,
        latest_bar_close_at=latest_bar_close_at,
        age_seconds=age_seconds,
        expected_bars=safe_expected,
        actual_bars=safe_actual,
        coverage_ratio=coverage,
        gap_count=max(0, gap_count),
        is_stale=is_stale,
        trace_run_id=trace_run_id,
        evidence_id=evidence_id,
    )


def evaluate_workbench_metrics_and_alerts(
    *,
    now: datetime | None = None,
    bar_freshness: Sequence[BarFreshnessMetric] = (),
    last_scan_at: datetime | None = None,
    scanner_max_lag_seconds: int = 180,
    scanner_trace_run_id: str | None = None,
    delivery_latencies_seconds: Sequence[float] = (),
    delivery_retry_count: int = 0,
    delivery_dead_letter_count: int = 0,
    delivery_trace_outbox_id: str | None = None,
    backfill_progress: Sequence[BackfillProgressMetric] = (),
    strategy_statuses: Sequence[StrategyPublishInput] = (),
    telegram_bindings: Sequence[TelegramBindingHealthInput] = (),
    telegram_subscriptions: Sequence[TelegramSubscriptionHealthInput] = (),
    dataset_rights: Sequence[DatasetRightsInput] = (),
) -> WorkbenchMetricsReport:
    """Tổng hợp metric WP-07E và sinh cảnh báo tiếng Việt có truy vết run/manifest/evidence."""
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)

    # 1. Đánh giá scanner lag
    if last_scan_at is None:
        scanner_lag = ScannerLagMetric(
            last_scan_at=None,
            lag_seconds=None,
            is_lagging=False,
            trace_run_id=scanner_trace_run_id,
        )
    else:
        aware_scan = (
            last_scan_at if last_scan_at.tzinfo is not None else last_scan_at.replace(tzinfo=UTC)
        )
        lag_sec = max(0.0, (current - aware_scan).total_seconds())
        scanner_lag = ScannerLagMetric(
            last_scan_at=aware_scan,
            lag_seconds=lag_sec,
            is_lagging=lag_sec > max(1, scanner_max_lag_seconds),
            trace_run_id=scanner_trace_run_id,
        )

    # 2. Đánh giá candidate -> delivery latency & retry/dead-letter
    delivery_latency = DeliveryLatencyMetric(
        sample_count=len(delivery_latencies_seconds),
        p50_seconds=compute_percentile_seconds(delivery_latencies_seconds, 0.50),
        p95_seconds=compute_percentile_seconds(delivery_latencies_seconds, 0.95),
        max_seconds=max((max(0.0, float(v)) for v in delivery_latencies_seconds), default=None),
        retry_count=max(0, delivery_retry_count),
        dead_letter_count=max(0, delivery_dead_letter_count),
        trace_outbox_id=delivery_trace_outbox_id,
    )

    alerts: list[WorkbenchAlert] = []

    # 3. Cảnh báo dữ liệu nến bị stale hoặc thiếu gap
    for bar in bar_freshness:
        if bar.is_stale or bar.gap_count > 0:
            trace = bar.evidence_id or bar.trace_run_id
            reason_parts: list[str] = []
            if bar.is_stale:
                age_text = (
                    f"{int(bar.age_seconds)}s"
                    if bar.age_seconds is not None
                    else "chưa có dữ liệu"
                )
                reason_parts.append(f"dữ liệu cũ ({age_text})")
            if bar.gap_count > 0:
                reason_parts.append(f"phát hiện {bar.gap_count} khoảng trống nến")
            alerts.append(
                WorkbenchAlert(
                    code="MARKET_DATA_STALE",
                    severity=(
                        AlertSeverity.WARNING
                        if bar.latest_bar_close_at
                        else AlertSeverity.CRITICAL
                    ),
                    category=AlertCategory.DATA_STALE,
                    title_vi=f"Dữ liệu {bar.symbol} ({bar.timeframe}) bị chậm hoặc thiếu nến",
                    detail_vi=(
                        f"{bar.symbol} khung {bar.timeframe}: {', '.join(reason_parts)}; "
                        f"độ phủ {round(bar.coverage_ratio * 100, 1)}%."
                    ),
                    trace_ref=trace,
                    detected_at=current,
                    metadata={"symbol": bar.symbol, "timeframe": bar.timeframe},
                )
            )

    # 4. Cảnh báo scanner bị trễ
    if scanner_lag.is_lagging and scanner_lag.lag_seconds is not None:
        alerts.append(
            WorkbenchAlert(
                code="SCANNER_LAG_HIGH",
                severity=AlertSeverity.WARNING,
                category=AlertCategory.SCANNER_LAG,
                title_vi="Scanner tín hiệu bị trễ nhịp",
                detail_vi=(
                    f"Lần quét gần nhất cách đây {int(scanner_lag.lag_seconds)}s, "
                    f"vượt ngưỡng {scanner_max_lag_seconds}s."
                ),
                trace_ref=scanner_lag.trace_run_id,
                detected_at=current,
            )
        )

    # 5. Cảnh báo chiến thuật chưa publish hoặc lệch release
    for item in strategy_statuses:
        if item.status.upper() != "PUBLISHED":
            alerts.append(
                WorkbenchAlert(
                    code="STRATEGY_NOT_PUBLISHED",
                    severity=AlertSeverity.CRITICAL,
                    category=AlertCategory.STRATEGY_UNPUBLISHED,
                    title_vi=f"Chiến thuật của bot {item.bot_name} chưa được publish",
                    detail_vi=(
                        f"Release '{item.release_id}' đang ở trạng thái '{item.status}', "
                        "không thể phát tín hiệu hoặc chạy PAPER hợp lệ."
                    ),
                    trace_ref=item.trace_ref,
                    detected_at=current,
                    metadata={"bot_name": item.bot_name, "release_id": item.release_id},
                )
            )

    # 6. Cảnh báo Telegram binding bị revoke
    for binding in telegram_bindings:
        if binding.revoked_at is not None:
            alerts.append(
                WorkbenchAlert(
                    code="TELEGRAM_BINDING_REVOKED",
                    severity=AlertSeverity.WARNING,
                    category=AlertCategory.BINDING_REVOKED,
                    title_vi="Liên kết Telegram đã bị thu hồi",
                    detail_vi=(
                        f"Binding {binding.binding_id} đã bị revoke lúc "
                        f"{binding.revoked_at.isoformat()}; cần liên kết lại để nhận tín hiệu."
                    ),
                    trace_ref=binding.binding_id,
                    detected_at=current,
                    metadata={"binding_id": binding.binding_id},
                )
            )

    # 7. Cảnh báo subscription Telegram không hợp lệ
    for sub in telegram_subscriptions:
        if sub.enabled and (not sub.binding_active or sub.invalid_reason):
            reason = sub.invalid_reason or "binding Telegram không còn hiệu lực"
            alerts.append(
                WorkbenchAlert(
                    code="TELEGRAM_SUBSCRIPTION_INVALID",
                    severity=AlertSeverity.WARNING,
                    category=AlertCategory.SUBSCRIPTION_INVALID,
                    title_vi=f"Đăng ký nhận tín hiệu bot {sub.bot_name} không hợp lệ",
                    detail_vi=f"Subscription {sub.subscription_id}: {reason}.",
                    trace_ref=sub.subscription_id,
                    detected_at=current,
                    metadata={"subscription_id": sub.subscription_id, "bot_name": sub.bot_name},
                )
            )

    # 8. Cảnh báo quyền dataset / source grant hết hạn hoặc bị thu hồi
    for right in dataset_rights:
        aware_exp = (
            right.expires_at
            if right.expires_at is None or right.expires_at.tzinfo is not None
            else right.expires_at.replace(tzinfo=UTC)
        )
        expired = aware_exp is not None and aware_exp <= current
        revoked = right.status.upper() in {"REVOKED", "EXPIRED", "DENIED"}
        if expired or revoked:
            reason = (
                f"đã hết hạn lúc {aware_exp.isoformat()}"
                if expired and aware_exp is not None
                else f"đang ở trạng thái {right.status}"
            )
            alerts.append(
                WorkbenchAlert(
                    code="DATASET_RIGHTS_EXPIRED",
                    severity=AlertSeverity.CRITICAL,
                    category=AlertCategory.DATASET_RIGHTS_EXPIRED,
                    title_vi=f"Quyền dữ liệu nguồn {right.source_code} không còn hiệu lực",
                    detail_vi=f"Grant {right.grant_id} ({right.source_code}) {reason}.",
                    trace_ref=right.manifest_id or right.grant_id,
                    detected_at=current,
                    metadata={"grant_id": right.grant_id, "source_code": right.source_code},
                )
            )

    # 9. Cảnh báo dead-letter giao nhận Telegram
    if delivery_latency.dead_letter_count > 0:
        alerts.append(
            WorkbenchAlert(
                code="TELEGRAM_DELIVERY_DEAD_LETTER",
                severity=AlertSeverity.CRITICAL,
                category=AlertCategory.DELIVERY_DEAD_LETTER,
                title_vi="Có tín hiệu Telegram rơi vào dead-letter",
                detail_vi=(
                    f"Phát hiện {delivery_latency.dead_letter_count} lượt gửi thất bại hoàn toàn "
                    f"(tổng retry: {delivery_latency.retry_count})."
                ),
                trace_ref=delivery_latency.trace_outbox_id,
                detected_at=current,
            )
        )

    return WorkbenchMetricsReport(
        generated_at=current,
        bar_freshness=tuple(bar_freshness),
        scanner_lag=scanner_lag,
        delivery_latency=delivery_latency,
        backfill_progress=tuple(backfill_progress),
        alerts=tuple(alerts),
    )


__all__ = [
    "AlertCategory",
    "AlertSeverity",
    "BackfillProgressMetric",
    "BarFreshnessMetric",
    "DatasetRightsInput",
    "DeliveryLatencyMetric",
    "ScannerLagMetric",
    "StrategyPublishInput",
    "TelegramBindingHealthInput",
    "TelegramSubscriptionHealthInput",
    "WorkbenchAlert",
    "WorkbenchMetricsReport",
    "compute_percentile_seconds",
    "evaluate_bar_freshness",
    "evaluate_workbench_metrics_and_alerts",
]
