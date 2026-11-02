"""Quyết định quyền phục vụ revision theo quyền, thời gian và takedown hiện hành."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceRegistryEntry


class SourceAccessMode(StrEnum):
    """Phân biệt yêu cầu hiện hành với replay tại một thời điểm quá khứ."""

    CURRENT = "current"
    HISTORICAL_REPLAY = "historical_replay"


class SourceAccessReason(StrEnum):
    """Mã giải thích ổn định cho quyết định phục vụ revision."""

    ALLOWED = "allowed"
    INVALID_REQUEST = "invalid_request"
    SOURCE_MISMATCH = "source_mismatch"
    POLICY_UNAVAILABLE_AS_OF = "policy_unavailable_as_of"
    CURRENT_POLICY_NOT_VERIFIED = "current_policy_not_verified"
    HISTORICAL_POLICY_NOT_VERIFIED = "historical_policy_not_verified"
    POLICY_EXPIRED = "policy_expired"
    PURPOSE_NOT_APPROVED = "purpose_not_approved"
    NOT_AVAILABLE_AS_OF = "not_available_as_of"
    REVISION_TOMBSTONED = "revision_tombstoned"
    CURRENT_TAKEDOWN = "current_takedown"
    RETENTION_EXPIRED = "retention_expired"


@dataclass(frozen=True, slots=True)
class SourceAccessDecision:
    """Kết quả quyết định; policy_id cho biết snapshot đã được xét."""

    permitted: bool
    mode: SourceAccessMode
    reason: SourceAccessReason
    policy_id: str | None
    as_of: datetime


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def evaluate_source_revision_access(
    *,
    revision: SourceItemRevision,
    policy_history: SourceRegistryEntry,
    purpose: SourceUsePurpose,
    mode: SourceAccessMode,
    as_of: datetime,
    evaluated_at: datetime,
    current_tombstoned: bool,
    retention_expires_at: datetime | None = None,
    takedown_at: datetime | None = None,
) -> SourceAccessDecision:
    """Kiểm quyền cho revision cụ thể, đồng thời áp dụng takedown hiện hành.

    ``policy_history`` và ``current_tombstoned`` phải đến từ repository có thẩm
    quyền. Mọi lần phục vụ đều cần quyền hiện hành còn hiệu lực; replay lịch sử
    đồng thời chọn policy tại ``as_of``. Vì vậy replay không hồi sinh quyền đã bị
    thu hồi/hết hạn, cũng không bỏ qua tombstone/takedown hoặc retention hiện tại.
    """
    instant = _utc(as_of, "as_of")
    evaluated = _utc(evaluated_at, "evaluated_at")
    expires = _utc(retention_expires_at, "retention_expires_at") if retention_expires_at else None
    withdrawn = _utc(takedown_at, "takedown_at") if takedown_at else None
    if not isinstance(mode, SourceAccessMode):
        raise ValueError("mode không hợp lệ.")
    if type(current_tombstoned) is not bool:
        raise ValueError("current_tombstoned phải là boolean.")
    if mode is SourceAccessMode.CURRENT and instant != evaluated:
        raise ValueError("as_of phải bằng evaluated_at với yêu cầu current.")
    if mode is SourceAccessMode.HISTORICAL_REPLAY and instant >= evaluated:
        raise ValueError("as_of phải trước evaluated_at với historical replay.")

    def decision(
        reason: SourceAccessReason,
        *,
        policy_id: str | None = None,
    ) -> SourceAccessDecision:
        return SourceAccessDecision(
            permitted=reason is SourceAccessReason.ALLOWED,
            mode=mode,
            reason=reason,
            policy_id=policy_id,
            as_of=instant,
        )

    if not isinstance(revision, SourceItemRevision) or not isinstance(
        policy_history, SourceRegistryEntry
    ):
        return decision(SourceAccessReason.INVALID_REQUEST)
    if not isinstance(purpose, SourceUsePurpose):
        return decision(SourceAccessReason.INVALID_REQUEST)
    if revision.source_id != policy_history.source_id:
        return decision(SourceAccessReason.SOURCE_MISMATCH)

    # Takedown/retention là trạng thái hiện hành, do đó lịch sử không được dùng
    # để hồi sinh revision đã bị rút hoặc hết hạn lưu.
    if revision.tombstoned:
        return decision(SourceAccessReason.REVISION_TOMBSTONED)
    if current_tombstoned or (withdrawn is not None and withdrawn <= evaluated):
        return decision(SourceAccessReason.CURRENT_TAKEDOWN)
    if expires is not None and expires <= evaluated:
        return decision(SourceAccessReason.RETENTION_EXPIRED)
    if revision.available_at is None or revision.available_at > instant:
        return decision(SourceAccessReason.NOT_AVAILABLE_AS_OF)

    # Replay lịch sử không được né policy hiện hành đã bị thu hồi, khóa, hết hạn
    # hoặc chuyển về chờ rà soát; trạng thái đó phải chặn ngay cả khi chưa có
    # tombstone riêng cho từng item.
    current_policy = policy_history.current.policy
    if current_policy.state is not SourcePolicyState.VERIFIED:
        return decision(
            SourceAccessReason.CURRENT_POLICY_NOT_VERIFIED,
            policy_id=current_policy.policy_id,
        )
    if current_policy.valid_until is not None and current_policy.valid_until <= evaluated:
        return decision(
            SourceAccessReason.POLICY_EXPIRED,
            policy_id=current_policy.policy_id,
        )
    if not current_policy.permits(purpose, at=evaluated):
        return decision(
            SourceAccessReason.PURPOSE_NOT_APPROVED,
            policy_id=current_policy.policy_id,
        )

    policy_at = evaluated if mode is SourceAccessMode.CURRENT else instant
    eligible = [entry for entry in policy_history.revisions if entry.created_at <= policy_at]
    if not eligible:
        return decision(SourceAccessReason.POLICY_UNAVAILABLE_AS_OF)
    selected = eligible[-1]
    policy = selected.policy
    if policy.state is not SourcePolicyState.VERIFIED:
        reason = (
            SourceAccessReason.CURRENT_POLICY_NOT_VERIFIED
            if mode is SourceAccessMode.CURRENT
            else SourceAccessReason.HISTORICAL_POLICY_NOT_VERIFIED
        )
        return decision(reason, policy_id=policy.policy_id)
    if policy.valid_until is not None and policy.valid_until <= policy_at:
        return decision(SourceAccessReason.POLICY_EXPIRED, policy_id=policy.policy_id)
    if not policy.permits(purpose, at=policy_at):
        return decision(SourceAccessReason.PURPOSE_NOT_APPROVED, policy_id=policy.policy_id)
    return decision(SourceAccessReason.ALLOWED, policy_id=policy.policy_id)
