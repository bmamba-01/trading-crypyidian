"""Kiểm định tính toàn vẹn chuỗi băm nhật ký audit bất biến theo workspace (SEC-33)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent
from trading_bot.application.outbox import UnitOfWork

if TYPE_CHECKING:
    from trading_bot.observability.security_alerts import SecurityAnomalyDetector

GENESIS_PREV_HASH = "GENESIS"
AUDIT_HASH_HEX_LENGTH = 64
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def format_audit_timestamp(value: datetime) -> str:
    """Chuẩn hóa timestamp sang UTC định dạng microsecond khớp hàm to_char của PostgreSQL."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def canonical_audit_payload(
    *,
    id: UUID | str,  # noqa: A002
    workspace_id: UUID | str,
    actor_membership_id: UUID | str | None,
    action: str,
    target_type: str,
    target_id: UUID | str | None,
    reason: str | None,
    before_hash: str | None,
    after_hash: str | None,
    request_id: UUID | str,
    occurred_at: datetime,
    seq: int,
    prev_hash: str,
    db_role: str,
    recorded_at: datetime,
    session_hash: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    outcome: str | None = None,
) -> str:
    """Tạo chuỗi JSON chuẩn hóa có thứ tự khóa cố định khớp tuyệt đối với trigger PL/pgSQL."""
    payload: dict[str, Any] = {
        "action": str(action),
        "actor_membership_id": (
            str(actor_membership_id) if actor_membership_id is not None else None
        ),
        "after_hash": str(after_hash) if after_hash is not None else None,
        "before_hash": str(before_hash) if before_hash is not None else None,
        "client_ip": str(client_ip) if client_ip is not None else None,
        "db_role": str(db_role),
        "id": str(id),
        "occurred_at": format_audit_timestamp(occurred_at),
        "outcome": str(outcome) if outcome is not None else None,
        "prev_hash": str(prev_hash),
        "reason": str(reason) if reason is not None else None,
        "recorded_at": format_audit_timestamp(recorded_at),
        "request_id": str(request_id),
        "seq": int(seq),
        "session_hash": str(session_hash) if session_hash is not None else None,
        "target_id": str(target_id) if target_id is not None else None,
        "target_type": str(target_type),
        "user_agent": str(user_agent) if user_agent is not None else None,
        "workspace_id": str(workspace_id),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_audit_row_hash(
    *,
    id: UUID | str,  # noqa: A002
    workspace_id: UUID | str,
    actor_membership_id: UUID | str | None,
    action: str,
    target_type: str,
    target_id: UUID | str | None,
    reason: str | None,
    before_hash: str | None,
    after_hash: str | None,
    request_id: UUID | str,
    occurred_at: datetime,
    seq: int,
    prev_hash: str,
    db_role: str,
    recorded_at: datetime,
    session_hash: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    outcome: str | None = None,
) -> str:
    """Tính mã băm SHA-256 hex cho dòng audit: sha256(prev_hash || canonical_json)."""
    canonical = canonical_audit_payload(
        id=id,
        workspace_id=workspace_id,
        actor_membership_id=actor_membership_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        before_hash=before_hash,
        after_hash=after_hash,
        request_id=request_id,
        occurred_at=occurred_at,
        seq=seq,
        prev_hash=prev_hash,
        db_role=db_role,
        recorded_at=recorded_at,
        session_hash=session_hash,
        client_ip=client_ip,
        user_agent=user_agent,
        outcome=outcome,
    )
    return hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()


def compute_hash_for_audit_event(event: Any) -> str:
    """Tính lại row_hash từ đối tượng AuditEvent (hoặc bản ghi tương đương)."""
    return compute_audit_row_hash(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_membership_id=getattr(event, "actor_membership_id", None),
        action=event.action,
        target_type=event.target_type,
        target_id=getattr(event, "target_id", None),
        reason=getattr(event, "reason", None),
        before_hash=getattr(event, "before_hash", None),
        after_hash=getattr(event, "after_hash", None),
        request_id=event.request_id,
        occurred_at=event.occurred_at,
        seq=int(event.seq),
        prev_hash=str(event.prev_hash),
        db_role=str(event.db_role),
        recorded_at=event.recorded_at,
        session_hash=getattr(event, "session_hash", None),
        client_ip=getattr(event, "client_ip", None),
        user_agent=getattr(event, "user_agent", None),
        outcome=getattr(event, "outcome", None),
    )


@dataclass(frozen=True, slots=True)
class AuditChainViolation:
    """Chi tiết một vi phạm toàn vẹn trên chuỗi băm audit_event."""

    code: str
    seq: int | None
    expected_seq: int
    event_id: UUID | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "seq": self.seq,
            "expected_seq": self.expected_seq,
            "event_id": str(self.event_id) if self.event_id is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AuditChainVerificationResult:
    """Kết quả xác minh toàn vẹn chuỗi băm audit của một workspace."""

    workspace_id: UUID
    verified: bool
    checked_count: int
    first_seq: int | None
    last_seq: int | None
    head_hash: str | None
    verified_at: datetime
    violations: tuple[AuditChainViolation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "verified": self.verified,
            "checked_count": self.checked_count,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "head_hash": self.head_hash,
            "verified_at": self.verified_at.isoformat(),
            "violations": [violation.to_dict() for violation in self.violations],
        }


def verify_audit_rows(
    workspace_id: UUID,
    rows: Sequence[Any],
    *,
    expected_min_last_seq: int | None = None,
    expected_head_hash: str | None = None,
) -> AuditChainVerificationResult:
    """Kiểm tra tuần tự danh sách bản ghi audit_event đã sắp theo seq tăng dần."""
    now = datetime.now(UTC)
    violations: list[AuditChainViolation] = []
    expected_seq = 1
    expected_prev_hash = GENESIS_PREV_HASH
    seen_seqs: set[int] = set()
    first_seq: int | None = None
    last_seq: int | None = None
    head_hash: str | None = None

    for row in rows:
        event_id = getattr(row, "id", None)
        row_ws = getattr(row, "workspace_id", None)
        seq = getattr(row, "seq", None)
        prev_hash = getattr(row, "prev_hash", None)
        row_hash = getattr(row, "row_hash", None)
        db_role = getattr(row, "db_role", None)
        recorded_at = getattr(row, "recorded_at", None)
        occurred_at = getattr(row, "occurred_at", None)

        if row_ws != workspace_id:
            violations.append(
                AuditChainViolation(
                    code="WORKSPACE_MISMATCH",
                    seq=seq if isinstance(seq, int) else None,
                    expected_seq=expected_seq,
                    event_id=event_id,
                    detail="Dòng audit không thuộc workspace đang kiểm tra",
                )
            )
            continue

        if (
            seq is None
            or not isinstance(seq, int)
            or not prev_hash
            or not row_hash
            or not db_role
            or recorded_at is None
            or occurred_at is None
        ):
            violations.append(
                AuditChainViolation(
                    code="MISSING_CHAIN_METADATA",
                    seq=seq if isinstance(seq, int) else None,
                    expected_seq=expected_seq,
                    event_id=event_id,
                    detail=(
                        "Thiếu metadata chuỗi băm bắt buộc "
                        "(seq/prev_hash/row_hash/db_role/recorded_at)"
                    ),
                )
            )
            expected_seq += 1
            continue

        if first_seq is None:
            first_seq = seq
        last_seq = seq
        head_hash = str(row_hash)

        if seq in seen_seqs:
            violations.append(
                AuditChainViolation(
                    code="DUPLICATE_SEQ",
                    seq=seq,
                    expected_seq=expected_seq,
                    event_id=event_id,
                    detail=f"Phát hiện số thứ tự seq bị trùng: {seq}",
                )
            )
        else:
            seen_seqs.add(seq)
            if seq != expected_seq:
                violations.append(
                    AuditChainViolation(
                        code="SEQ_GAP",
                        seq=seq,
                        expected_seq=expected_seq,
                        event_id=event_id,
                        detail=f"Đứt quãng số thứ tự audit: kỳ vọng {expected_seq}, thực tế {seq}",
                    )
                )

        if str(prev_hash) != expected_prev_hash:
            violations.append(
                AuditChainViolation(
                    code="PREV_HASH_MISMATCH",
                    seq=seq,
                    expected_seq=expected_seq,
                    event_id=event_id,
                    detail="Liên kết prev_hash không khớp với row_hash của dòng liền trước",
                )
            )

        if not _SHA256_HEX_RE.fullmatch(str(row_hash)):
            violations.append(
                AuditChainViolation(
                    code="INVALID_HASH_FORMAT",
                    seq=seq,
                    expected_seq=expected_seq,
                    event_id=event_id,
                    detail="Định dạng row_hash không phải SHA-256 hex 64 ký tự",
                )
            )
        else:
            recomputed = compute_hash_for_audit_event(row)
            if recomputed != str(row_hash):
                violations.append(
                    AuditChainViolation(
                        code="ROW_HASH_MISMATCH",
                        seq=seq,
                        expected_seq=expected_seq,
                        event_id=event_id,
                        detail=f"Nội dung dòng audit seq={seq} đã bị thay đổi trái phép",
                    )
                )

        expected_prev_hash = str(row_hash)
        expected_seq = seq + 1

    if expected_min_last_seq is not None and (last_seq or 0) < expected_min_last_seq:
        violations.append(
            AuditChainViolation(
                code="TAIL_ANCHOR_MISMATCH",
                seq=last_seq,
                expected_seq=expected_min_last_seq,
                event_id=None,
                detail=(
                    f"Số thứ tự cuối chuỗi ({last_seq}) thấp hơn mốc neo đã ghi nhận "
                    f"({expected_min_last_seq})"
                ),
            )
        )

    if expected_head_hash is not None and head_hash != expected_head_hash:
        violations.append(
            AuditChainViolation(
                code="TAIL_ANCHOR_MISMATCH",
                seq=last_seq,
                expected_seq=expected_min_last_seq or (last_seq or 0),
                event_id=None,
                detail="Mã băm đầu chuỗi (head_hash) không khớp với mốc neo đã lưu",
            )
        )

    return AuditChainVerificationResult(
        workspace_id=workspace_id,
        verified=len(violations) == 0,
        checked_count=len(rows),
        first_seq=first_seq,
        last_seq=last_seq,
        head_hash=head_hash,
        verified_at=now,
        violations=tuple(violations),
    )


async def verify_audit_chain(
    sessions: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    anomaly_detector: SecurityAnomalyDetector | None = None,
    expected_min_last_seq: int | None = None,
    expected_head_hash: str | None = None,
) -> AuditChainVerificationResult:
    """Truy vấn toàn bộ chuỗi audit_event của workspace, xác minh băm và báo động nếu đứt chuỗi."""
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.workspace_id == workspace_id)
            .order_by(
                AuditEvent.seq.asc().nulls_first(),
                AuditEvent.recorded_at.asc().nulls_first(),
                AuditEvent.id.asc(),
            )
        )
        rows = (await unit.session.scalars(stmt)).all()
        result = verify_audit_rows(
            workspace_id,
            rows,
            expected_min_last_seq=expected_min_last_seq,
            expected_head_hash=expected_head_hash,
        )

    if not result.verified and anomaly_detector is not None:
        await anomaly_detector.report_audit_chain_broken(result)

    return result
