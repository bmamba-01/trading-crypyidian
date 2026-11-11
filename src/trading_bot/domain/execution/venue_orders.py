"""Hợp đồng domain cho intent order venue; không gọi mạng hoặc gửi order."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from threading import RLock
from typing import Any
from uuid import UUID

ZERO = Decimal("0")


class VenueOrderConflict(RuntimeError):
    """Intent, idempotency key hoặc trạng thái venue xung đột."""


class ExecutionMode(StrEnum):
    """Môi trường duy nhất contract hiện tại cho phép."""

    DEMO = "DEMO"


class VenueOrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    RETRYABLE = "RETRYABLE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


_TRANSITIONS = {
    VenueOrderState.CREATED: frozenset({VenueOrderState.SUBMITTING}),
    VenueOrderState.SUBMITTING: frozenset(
        {
            VenueOrderState.ACKNOWLEDGED,
            VenueOrderState.RETRYABLE,
            VenueOrderState.REJECTED,
            VenueOrderState.UNKNOWN,
        }
    ),
    VenueOrderState.RETRYABLE: frozenset({VenueOrderState.SUBMITTING}),
    VenueOrderState.ACKNOWLEDGED: frozenset(
        {
            VenueOrderState.ACKNOWLEDGED,
            VenueOrderState.PARTIALLY_FILLED,
            VenueOrderState.FILLED,
            VenueOrderState.CANCELED,
            VenueOrderState.EXPIRED,
        }
    ),
    VenueOrderState.PARTIALLY_FILLED: frozenset(
        {
            VenueOrderState.PARTIALLY_FILLED,
            VenueOrderState.FILLED,
            VenueOrderState.CANCELED,
            VenueOrderState.EXPIRED,
        }
    ),
    VenueOrderState.UNKNOWN: frozenset(
        {
            VenueOrderState.UNKNOWN,
            VenueOrderState.RETRYABLE,
            VenueOrderState.ACKNOWLEDGED,
            VenueOrderState.PARTIALLY_FILLED,
            VenueOrderState.FILLED,
            VenueOrderState.CANCELED,
            VenueOrderState.REJECTED,
            VenueOrderState.EXPIRED,
        }
    ),
    VenueOrderState.FILLED: frozenset({VenueOrderState.FILLED}),
    VenueOrderState.CANCELED: frozenset({VenueOrderState.CANCELED}),
    VenueOrderState.REJECTED: frozenset({VenueOrderState.REJECTED}),
    VenueOrderState.EXPIRED: frozenset({VenueOrderState.EXPIRED}),
}


class SubmissionOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    DEFINITIVE_REJECTION = "DEFINITIVE_REJECTION"
    NOT_SENT = "NOT_SENT"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    UNKNOWN = "UNKNOWN"


class ReconciliationSource(StrEnum):
    USER_STREAM = "USER_STREAM"
    REST_ORDER_QUERY = "REST_ORDER_QUERY"
    ORDER_QUERY_AND_TRADE_HISTORY = "ORDER_QUERY_AND_TRADE_HISTORY"


class ReconciliationOutcome(StrEnum):
    ORDER_ABSENT = "ORDER_ABSENT"
    ORDER_OPEN = "ORDER_OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class VenueOrderUpdate:
    """Một sự kiện đã được adapter chuẩn hóa, không chứa payload thô."""

    event_id: str
    source: ReconciliationSource
    observed_at: datetime
    venue_order_id: str
    cumulative_filled: Decimal
    status: VenueOrderState

    def __post_init__(self) -> None:
        _text(self.event_id, "event_id")
        _text(self.venue_order_id, "venue_order_id")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))
        _quantity(self.cumulative_filled, "cumulative_filled", allow_zero=True)
        if type(self.source) is not ReconciliationSource:
            raise VenueOrderConflict("source phải là ReconciliationSource tường minh.")
        if type(self.status) is not VenueOrderState or self.status not in {
            VenueOrderState.ACKNOWLEDGED,
            VenueOrderState.PARTIALLY_FILLED,
            VenueOrderState.FILLED,
            VenueOrderState.CANCELED,
            VenueOrderState.REJECTED,
            VenueOrderState.EXPIRED,
        }:
            raise VenueOrderConflict("status update không phải trạng thái venue chuẩn hóa.")
        if self.status is VenueOrderState.FILLED and self.cumulative_filled == ZERO:
            raise VenueOrderConflict("FILLED cần cumulative fill dương.")
        if self.status is VenueOrderState.ACKNOWLEDGED and self.cumulative_filled > ZERO:
            raise VenueOrderConflict("ACKNOWLEDGED không được có cumulative fill.")
        if self.status is VenueOrderState.PARTIALLY_FILLED and self.cumulative_filled == ZERO:
            raise VenueOrderConflict("PARTIALLY_FILLED cần cumulative fill dương.")


@dataclass(frozen=True, slots=True)
class VenueReconciliation:
    """Bằng chứng đã chuẩn hóa để phân giải intent UNKNOWN."""

    outcome: ReconciliationOutcome
    source: ReconciliationSource
    source_ref: str
    workspace_id: str
    account_id: str
    client_order_id: str
    checked_at: datetime
    cumulative_filled: Decimal
    venue_order_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.outcome) is not ReconciliationOutcome:
            raise VenueOrderConflict("outcome phải là ReconciliationOutcome tường minh.")
        if type(self.source) is not ReconciliationSource:
            raise VenueOrderConflict("source phải là ReconciliationSource tường minh.")
        _text(self.source_ref, "source_ref")
        _text(self.workspace_id, "workspace_id")
        _text(self.account_id, "account_id")
        _text(self.client_order_id, "client_order_id")
        object.__setattr__(self, "checked_at", _aware_utc(self.checked_at, "checked_at"))
        _quantity(self.cumulative_filled, "cumulative_filled", allow_zero=True)
        if self.venue_order_id is not None:
            _text(self.venue_order_id, "venue_order_id")
        if self.outcome is ReconciliationOutcome.ORDER_ABSENT:
            if self.source is not ReconciliationSource.ORDER_QUERY_AND_TRADE_HISTORY:
                raise VenueOrderConflict("ORDER_ABSENT cần query order và trade history.")
            if self.venue_order_id is not None or self.cumulative_filled != ZERO:
                raise VenueOrderConflict("Bằng chứng ORDER_ABSENT mâu thuẫn với order/fill.")
        elif self.venue_order_id is None:
            raise VenueOrderConflict("Order hiện diện cần venue_order_id.")
        if self.outcome is ReconciliationOutcome.FILLED and self.cumulative_filled == ZERO:
            raise VenueOrderConflict("FILLED reconcile cần cumulative fill dương.")


@dataclass(frozen=True, slots=True)
class VenueOrderIntent:
    """Intent bất biến gắn order với workspace/account/risk và lineage kế hoạch."""

    intent_id: UUID
    workspace_id: str
    account_id: str
    idempotency_key: str
    reservation_id: UUID
    plan_hash: str
    settings_version: str
    symbol: str
    side: str
    quantity: Decimal
    client_order_id: str
    created_at: datetime
    order_type: str = "MARKET"
    limit_price: Decimal | None = None
    mode: ExecutionMode = ExecutionMode.DEMO
    state: VenueOrderState = VenueOrderState.CREATED
    state_changed_at: datetime | None = None
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    retry_not_before: datetime | None = None
    venue_order_id: str | None = None
    filled_quantity: Decimal = ZERO
    last_event_at: datetime | None = None
    seen_event_ids: tuple[str, ...] = ()
    seen_event_fingerprints: tuple[tuple[str, str], ...] = ()
    request_fingerprint: str = ""
    _transition_proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "account_id",
            "idempotency_key",
            "settings_version",
            "symbol",
            "client_order_id",
        ):
            _text(getattr(self, name), name)
        if type(self.intent_id) is not UUID or type(self.reservation_id) is not UUID:
            raise VenueOrderConflict("intent_id và reservation_id phải là UUID.")
        if type(self.mode) is not ExecutionMode or self.mode is not ExecutionMode.DEMO:
            raise VenueOrderConflict("mode hiện chỉ hỗ trợ ExecutionMode.DEMO.")
        if type(self.state) is not VenueOrderState:
            raise VenueOrderConflict("state phải là VenueOrderState tường minh.")
        if self.state is VenueOrderState.CREATED and self._transition_proof is not None:
            raise VenueOrderConflict("Intent CREATED không được có transition proof.")
        if type(self.side) is not str or self.side not in {"BUY", "SELL"}:
            raise VenueOrderConflict("side chỉ nhận BUY hoặc SELL.")
        _hash(self.plan_hash, "plan_hash")
        _quantity(self.quantity, "quantity")
        _quantity(self.filled_quantity, "filled_quantity", allow_zero=True)
        if self.filled_quantity > self.quantity:
            raise VenueOrderConflict("filled_quantity không được vượt quantity.")
        object.__setattr__(self, "created_at", _aware_utc(self.created_at, "created_at"))
        if self.last_attempt_at is not None:
            object.__setattr__(
                self, "last_attempt_at", _aware_utc(self.last_attempt_at, "last_attempt_at")
            )
        if self.state_changed_at is not None:
            object.__setattr__(
                self, "state_changed_at", _aware_utc(self.state_changed_at, "state_changed_at")
            )
        if self.retry_not_before is not None:
            object.__setattr__(
                self, "retry_not_before", _aware_utc(self.retry_not_before, "retry_not_before")
            )
        if self.last_event_at is not None:
            object.__setattr__(
                self, "last_event_at", _aware_utc(self.last_event_at, "last_event_at")
            )
        event_fingerprints = tuple(sorted(self.seen_event_fingerprints))
        if len({event_id for event_id, _ in event_fingerprints}) != len(event_fingerprints):
            raise VenueOrderConflict("event fingerprint không được duplicate.")
        for event_id, event_hash in event_fingerprints:
            _text(event_id, "event_id")
            _hash(event_hash, "event_fingerprint")
        if set(self.seen_event_ids) != {event_id for event_id, _ in event_fingerprints}:
            raise VenueOrderConflict("event IDs phải khớp tập event fingerprint.")
        object.__setattr__(self, "seen_event_fingerprints", event_fingerprints)
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise VenueOrderConflict("attempt_count không được âm.")
        if self.state is VenueOrderState.CREATED:
            if (
                self.attempt_count != 0
                or self.last_attempt_at is not None
                or self.state_changed_at is not None
                or self.retry_not_before is not None
                or self.venue_order_id is not None
                or self.filled_quantity != ZERO
                or self.seen_event_ids
                or self.seen_event_fingerprints
            ):
                raise VenueOrderConflict("Intent CREATED không được có trạng thái chuyển tiếp.")
        elif (
            self.attempt_count == 0 or self.last_attempt_at is None or self.state_changed_at is None
        ):
            raise VenueOrderConflict("Intent đã chuyển trạng thái cần attempt và timestamp.")
        if self.state in {VenueOrderState.SUBMITTING, VenueOrderState.RETRYABLE}:
            if self.venue_order_id is not None or self.filled_quantity != ZERO:
                raise VenueOrderConflict("SUBMITTING/RETRYABLE không được gắn venue order/fill.")
        if self.state is VenueOrderState.ACKNOWLEDGED and self.filled_quantity != ZERO:
            raise VenueOrderConflict("ACKNOWLEDGED không được có fill.")
        if self.state is VenueOrderState.PARTIALLY_FILLED and not (
            ZERO < self.filled_quantity < self.quantity
        ):
            raise VenueOrderConflict("PARTIALLY_FILLED cần fill dương dưới quantity.")
        if self.state is VenueOrderState.FILLED and self.filled_quantity != self.quantity:
            raise VenueOrderConflict("FILLED cần fill đúng bằng quantity.")
        if (
            self.state
            in {
                VenueOrderState.ACKNOWLEDGED,
                VenueOrderState.PARTIALLY_FILLED,
                VenueOrderState.FILLED,
                VenueOrderState.CANCELED,
                VenueOrderState.EXPIRED,
            }
            and self.venue_order_id is None
        ):
            raise VenueOrderConflict("Trạng thái venue hiện diện cần venue_order_id.")
        if self.retry_not_before is not None and self.state is not VenueOrderState.RETRYABLE:
            raise VenueOrderConflict("retry_not_before chỉ hợp lệ khi RETRYABLE.")
        if self.venue_order_id is not None:
            _text(self.venue_order_id, "venue_order_id")
        if self.order_type not in {"MARKET", "LIMIT"}:
            raise VenueOrderConflict("order_type hiện chỉ nhận MARKET hoặc LIMIT.")
        if self.order_type == "LIMIT":
            if self.limit_price is None:
                raise VenueOrderConflict("LIMIT cần limit_price.")
            _quantity(self.limit_price, "limit_price")
        elif self.limit_price is not None:
            raise VenueOrderConflict("MARKET không được có limit_price.")
        events = tuple(dict.fromkeys(self.seen_event_ids))
        for event_id in events:
            _text(event_id, "event_id")
        object.__setattr__(self, "seen_event_ids", events)
        expected = _fingerprint(self)
        if self.request_fingerprint and self.request_fingerprint != expected:
            raise VenueOrderConflict("request_fingerprint không khớp payload chuẩn hóa.")
        object.__setattr__(self, "request_fingerprint", expected)
        if self.state is not VenueOrderState.CREATED and not _valid_transition_proof(self):
            raise VenueOrderConflict("Bằng chứng chuyển trạng thái không hợp lệ hoặc không khớp.")

    @property
    def reservation_action(self) -> str:
        """Ý định xử lý reservation; caller phải áp dụng đồng bộ trong transaction."""
        if self.filled_quantity > ZERO:
            if (
                self.state
                in {
                    VenueOrderState.CREATED,
                    VenueOrderState.SUBMITTING,
                    VenueOrderState.RETRYABLE,
                    VenueOrderState.ACKNOWLEDGED,
                    VenueOrderState.PARTIALLY_FILLED,
                    VenueOrderState.UNKNOWN,
                }
                and self.filled_quantity < self.quantity
            ):
                return "TRANSFER_FILLED_AND_RETAIN_REMAINDER"
            return "TRANSFER_TO_EXPOSURE"
        if self.state in {
            VenueOrderState.CANCELED,
            VenueOrderState.REJECTED,
            VenueOrderState.EXPIRED,
        }:
            return "RELEASE"
        return "RETAIN"

    def begin_submission(self, *, at: datetime) -> VenueOrderIntent:
        moment = _aware_utc(at, "at")
        if self.state not in {VenueOrderState.CREATED, VenueOrderState.RETRYABLE}:
            raise VenueOrderConflict("Chỉ intent CREATED/RETRYABLE mới được submit.")
        if moment < self.created_at:
            raise VenueOrderConflict("Thời điểm submit không được trước created_at.")
        if self.retry_not_before is not None and moment < self.retry_not_before:
            raise VenueOrderConflict("Chưa đến thời điểm retry.")
        return _transition_replace(
            self,
            state=VenueOrderState.SUBMITTING,
            state_changed_at=moment,
            attempt_count=self.attempt_count + 1,
            last_attempt_at=moment,
            retry_not_before=None,
        )

    def resolve_submission(
        self,
        *,
        outcome: SubmissionOutcome,
        observed_at: datetime,
        venue_order_id: str | None = None,
        retry_not_before: datetime | None = None,
    ) -> VenueOrderIntent:
        moment = _aware_utc(observed_at, "observed_at")
        if self.state is not VenueOrderState.SUBMITTING:
            raise VenueOrderConflict("Chỉ SUBMITTING mới nhận kết quả submit.")
        if self.last_attempt_at is None or moment < self.last_attempt_at:
            raise VenueOrderConflict("Kết quả submit không được trước lần thử gần nhất.")
        if type(outcome) is not SubmissionOutcome:
            raise VenueOrderConflict("outcome phải là SubmissionOutcome tường minh.")
        if retry_not_before is not None:
            retry_not_before = _aware_utc(retry_not_before, "retry_not_before")
            if retry_not_before < moment:
                raise VenueOrderConflict("retry_not_before không được trước observed_at.")
        if outcome is SubmissionOutcome.RETRYABLE_FAILURE:
            if retry_not_before is None:
                raise VenueOrderConflict("retry_not_before bắt buộc sau lỗi retryable.")
            if retry_not_before <= moment:
                raise VenueOrderConflict("retry_not_before phải sau observed_at để có backoff.")
        if retry_not_before is not None and outcome not in {
            SubmissionOutcome.NOT_SENT,
            SubmissionOutcome.RETRYABLE_FAILURE,
        }:
            raise VenueOrderConflict("Chỉ kết quả an toàn retry mới nhận retry_not_before.")
        if outcome is SubmissionOutcome.ACCEPTED:
            if venue_order_id is None:
                raise VenueOrderConflict("ACCEPTED cần venue_order_id.")
            _text(venue_order_id, "venue_order_id")
            return _transition_replace(
                self,
                state=VenueOrderState.ACKNOWLEDGED,
                state_changed_at=moment,
                venue_order_id=venue_order_id,
            )
        if venue_order_id is not None:
            raise VenueOrderConflict("Kết quả không accepted không được gắn venue_order_id.")
        if outcome is SubmissionOutcome.DEFINITIVE_REJECTION:
            return _transition_replace(
                self, state=VenueOrderState.REJECTED, state_changed_at=moment
            )
        if outcome in {SubmissionOutcome.NOT_SENT, SubmissionOutcome.RETRYABLE_FAILURE}:
            return _transition_replace(
                self,
                state=VenueOrderState.RETRYABLE,
                state_changed_at=moment,
                retry_not_before=retry_not_before,
            )
        return _transition_replace(self, state=VenueOrderState.UNKNOWN, state_changed_at=moment)

    def apply_update(self, update: VenueOrderUpdate) -> VenueOrderIntent:
        update_hash = _update_fingerprint(update)
        existing_hash = dict(self.seen_event_fingerprints).get(update.event_id)
        if existing_hash is not None:
            if existing_hash != update_hash:
                raise VenueOrderConflict("event ID bị dùng lại với payload khác.")
            return self
        if self.state in {VenueOrderState.CREATED, VenueOrderState.RETRYABLE}:
            raise VenueOrderConflict(
                "Không nhận venue update khi intent chưa submit/đã xác nhận absent."
            )
        if self.venue_order_id is not None and update.venue_order_id != self.venue_order_id:
            raise VenueOrderConflict("Venue update order ID không khớp intent.")
        if update.cumulative_filled > self.quantity:
            raise VenueOrderConflict("Cumulative fill vượt quantity đã yêu cầu.")
        if update.status is VenueOrderState.FILLED and update.cumulative_filled != self.quantity:
            raise VenueOrderConflict("FILLED phải bằng requested quantity.")
        if (
            update.status is VenueOrderState.PARTIALLY_FILLED
            and update.cumulative_filled >= self.quantity
        ):
            raise VenueOrderConflict("PARTIALLY_FILLED phải nhỏ hơn requested quantity.")

        filled = max(self.filled_quantity, update.cumulative_filled)
        state = self.state
        newer = self.last_event_at is None or update.observed_at >= self.last_event_at
        if state is VenueOrderState.UNKNOWN:
            # Event cập nhật bằng chứng/fill nhưng UNKNOWN chỉ do reconcile mới giải quyết.
            pass
        elif (
            state
            not in {
                VenueOrderState.FILLED,
                VenueOrderState.CANCELED,
                VenueOrderState.REJECTED,
                VenueOrderState.EXPIRED,
            }
            and newer
        ):
            state = _state_for_update(update.status, filled, self.quantity)
        elif (
            state
            not in {
                VenueOrderState.FILLED,
                VenueOrderState.CANCELED,
                VenueOrderState.REJECTED,
                VenueOrderState.EXPIRED,
            }
            and filled > ZERO
        ):
            state = (
                VenueOrderState.FILLED
                if filled == self.quantity
                else VenueOrderState.PARTIALLY_FILLED
            )
        elif filled == self.quantity:
            state = VenueOrderState.FILLED
        return _transition_replace(
            self,
            state=state,
            state_changed_at=update.observed_at if state != self.state else self.state_changed_at,
            filled_quantity=filled,
            venue_order_id=self.venue_order_id or update.venue_order_id,
            last_event_at=max(filter(None, (self.last_event_at, update.observed_at))),
            seen_event_ids=(*self.seen_event_ids, update.event_id),
            seen_event_fingerprints=(*self.seen_event_fingerprints, (update.event_id, update_hash)),
        )

    def resolve_unknown(self, evidence: VenueReconciliation) -> VenueOrderIntent:
        if self.state is not VenueOrderState.UNKNOWN:
            raise VenueOrderConflict("Chỉ UNKNOWN mới được resolve bằng reconcile.")
        if (
            evidence.workspace_id != self.workspace_id
            or evidence.account_id != self.account_id
            or evidence.client_order_id != self.client_order_id
        ):
            raise VenueOrderConflict("Reconcile evidence không khớp intent scope.")
        latest_known_at = max(
            filter(None, (self.last_attempt_at, self.last_event_at, self.created_at))
        )
        if evidence.checked_at < latest_known_at:
            raise VenueOrderConflict("Reconcile evidence cũ hơn trạng thái gần nhất đã biết.")
        if evidence.outcome is ReconciliationOutcome.ORDER_ABSENT:
            if self.venue_order_id is not None:
                raise VenueOrderConflict(
                    "Không thể xác nhận absent sau khi đã thấy venue order ID."
                )
            if self.filled_quantity > ZERO:
                raise VenueOrderConflict("Không thể xác nhận absent khi đã ghi nhận fill.")
            return _transition_replace(
                self,
                state=VenueOrderState.RETRYABLE,
                state_changed_at=evidence.checked_at,
                retry_not_before=evidence.checked_at,
            )
        if self.venue_order_id is not None and evidence.venue_order_id != self.venue_order_id:
            raise VenueOrderConflict("Reconcile order ID không khớp intent.")
        if evidence.cumulative_filled > self.quantity:
            raise VenueOrderConflict("Reconcile cumulative fill vượt quantity.")
        if (
            evidence.outcome is ReconciliationOutcome.FILLED
            and evidence.cumulative_filled != self.quantity
        ):
            raise VenueOrderConflict("FILLED reconcile phải bằng requested quantity.")
        if (
            evidence.outcome is ReconciliationOutcome.ORDER_OPEN
            and evidence.cumulative_filled >= self.quantity
        ):
            raise VenueOrderConflict("ORDER_OPEN phải còn quantity chưa khớp.")
        if evidence.cumulative_filled < self.filled_quantity:
            raise VenueOrderConflict("Reconcile không được làm giảm cumulative fill.")
        state = _state_for_reconciliation(
            evidence.outcome, evidence.cumulative_filled, self.quantity
        )
        return _transition_replace(
            self,
            state=state,
            state_changed_at=evidence.checked_at,
            venue_order_id=evidence.venue_order_id,
            filled_quantity=evidence.cumulative_filled,
            last_event_at=evidence.checked_at,
        )


class VenueOrderBook:
    """Kho contract trong tiến trình; chưa thay thế persistence/DB repository."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._orders: dict[UUID, VenueOrderIntent] = {}
        self._idempotency: dict[tuple[str, str, str], UUID] = {}

    def prepare(self, intent: VenueOrderIntent) -> VenueOrderIntent:
        if intent.state is not VenueOrderState.CREATED:
            raise VenueOrderConflict("Chỉ intent CREATED mới được prepare.")
        key = (intent.workspace_id, intent.account_id, intent.idempotency_key)
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id is not None:
                existing = self._orders[existing_id]
                if existing.request_fingerprint != intent.request_fingerprint:
                    raise VenueOrderConflict("idempotency key xung đột request fingerprint.")
                return existing
            if intent.intent_id in self._orders:
                raise VenueOrderConflict("intent_id đã được dùng cho một intent khác.")
            self._orders[intent.intent_id] = intent
            self._idempotency[key] = intent.intent_id
            return intent


def _state_for_update(
    status: VenueOrderState, filled: Decimal, requested: Decimal
) -> VenueOrderState:
    if filled == requested:
        return VenueOrderState.FILLED
    if status in {
        VenueOrderState.CANCELED,
        VenueOrderState.REJECTED,
        VenueOrderState.EXPIRED,
    }:
        return status
    if status is VenueOrderState.PARTIALLY_FILLED or filled > ZERO:
        return VenueOrderState.PARTIALLY_FILLED
    return status


def _state_for_reconciliation(
    outcome: ReconciliationOutcome, filled: Decimal, requested: Decimal
) -> VenueOrderState:
    if filled == requested:
        return VenueOrderState.FILLED
    if outcome is ReconciliationOutcome.ORDER_OPEN:
        return VenueOrderState.PARTIALLY_FILLED if filled > ZERO else VenueOrderState.ACKNOWLEDGED
    return VenueOrderState(outcome.value)


def _fingerprint_from_values(values: dict[str, Any]) -> str:
    mode = values.get("mode", ExecutionMode.DEMO)
    mode_value = mode.value if isinstance(mode, ExecutionMode) else str(mode)
    limit_price = values.get("limit_price")
    payload = {
        "mode": mode_value,
        "workspace_id": values["workspace_id"],
        "account_id": values["account_id"],
        "idempotency_key": values["idempotency_key"],
        "plan_hash": values["plan_hash"],
        "settings_version": values["settings_version"],
        "symbol": values["symbol"],
        "side": values["side"],
        "quantity": _decimal_text(values["quantity"]),
        "order_type": values.get("order_type", "MARKET"),
        "limit_price": (
            _decimal_text(limit_price) if limit_price is not None else None
        ),
        "client_order_id": values["client_order_id"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(intent: VenueOrderIntent) -> str:
    return _fingerprint_from_values(
        {
            "mode": intent.mode,
            "workspace_id": intent.workspace_id,
            "account_id": intent.account_id,
            "idempotency_key": intent.idempotency_key,
            "plan_hash": intent.plan_hash,
            "settings_version": intent.settings_version,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "order_type": intent.order_type,
            "limit_price": intent.limit_price,
            "client_order_id": intent.client_order_id,
        }
    )


def _make_transition_proof_tools() -> tuple[
    Callable[..., VenueOrderIntent],
    Callable[[VenueOrderIntent], bool],
    Callable[..., VenueOrderIntent],
]:
    key = secrets.token_bytes(32)

    def payload(values: dict[str, object]) -> bytes:
        serializable: dict[str, object] = {}
        for name, value in values.items():
            if name == "_transition_proof":
                continue
            if name == "seen_event_fingerprints" and isinstance(value, tuple):
                value = tuple(sorted(value))
            serializable[name] = _proof_value(value)
        return json.dumps(serializable, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def sign(values: dict[str, object]) -> str:
        return hmac.new(key, payload(values), hashlib.sha256).hexdigest()

    def valid(order: VenueOrderIntent) -> bool:
        proof = order._transition_proof
        if not isinstance(proof, str):
            return False
        values = {
            item.name: getattr(order, item.name)
            for item in fields(VenueOrderIntent)
            if item.name != "_transition_proof"
        }
        return hmac.compare_digest(proof, sign(values))

    def transition_replace(order: VenueOrderIntent, **changes: Any) -> VenueOrderIntent:
        state = changes.get("state", order.state)
        if type(state) is not VenueOrderState or state not in _TRANSITIONS[order.state]:
            raise VenueOrderConflict("State transition không hợp lệ.")
        values = {
            item.name: getattr(order, item.name)
            for item in fields(VenueOrderIntent)
            if item.name != "_transition_proof"
        }
        values.update(changes)
        values.pop("_transition_proof", None)
        changes["_transition_proof"] = sign(values)
        return replace(order, **changes)

    def rehydrate(**values: Any) -> VenueOrderIntent:
        """Tái tạo VenueOrderIntent từ database record sau khi tiến trình restart."""
        state = values.get("state", VenueOrderState.CREATED)
        if type(state) is not VenueOrderState:
            raise VenueOrderConflict("state phải là VenueOrderState tường minh.")
        for item in fields(VenueOrderIntent):
            if item.name not in values and item.default is not MISSING:
                values[item.name] = item.default
        if state is VenueOrderState.CREATED:
            values.pop("_transition_proof", None)
            return VenueOrderIntent(**values)
        if "created_at" in values and isinstance(values["created_at"], datetime):
            values["created_at"] = _aware_utc(values["created_at"], "created_at")
        if values.get("last_attempt_at") and isinstance(values["last_attempt_at"], datetime):
            values["last_attempt_at"] = _aware_utc(values["last_attempt_at"], "last_attempt_at")
        if values.get("state_changed_at") and isinstance(values["state_changed_at"], datetime):
            values["state_changed_at"] = _aware_utc(
                values["state_changed_at"], "state_changed_at"
            )
        if values.get("retry_not_before") and isinstance(values["retry_not_before"], datetime):
            values["retry_not_before"] = _aware_utc(
                values["retry_not_before"], "retry_not_before"
            )
        if values.get("last_event_at") and isinstance(values["last_event_at"], datetime):
            values["last_event_at"] = _aware_utc(values["last_event_at"], "last_event_at")
        seen_prints = values.get("seen_event_fingerprints")
        if isinstance(seen_prints, tuple):
            values["seen_event_fingerprints"] = tuple(sorted(seen_prints))
        seen_ids = values.get("seen_event_ids")
        if isinstance(seen_ids, tuple):
            values["seen_event_ids"] = tuple(dict.fromkeys(seen_ids))
        values["request_fingerprint"] = _fingerprint_from_values(values)
        proof_values = {
            item.name: values.get(item.name)
            for item in fields(VenueOrderIntent)
            if item.name != "_transition_proof"
        }
        values["_transition_proof"] = sign(proof_values)
        return VenueOrderIntent(**values)

    return transition_replace, valid, rehydrate


_transition_replace, _valid_transition_proof, rehydrate_venue_order_intent = (
    _make_transition_proof_tools()
)


def _proof_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_proof_value(item) for item in value]
    return value


def _update_fingerprint(update: VenueOrderUpdate) -> str:
    payload = {
        "event_id": update.event_id,
        "source": update.source.value,
        "observed_at": update.observed_at.isoformat(),
        "venue_order_id": update.venue_order_id,
        "cumulative_filled": _decimal_text(update.cumulative_filled),
        "status": update.status.value,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise VenueOrderConflict(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _quantity(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise VenueOrderConflict(f"{name} phải là Decimal hữu hạn.")
    if value < ZERO or (value == ZERO and not allow_zero):
        qualifier = "không âm" if allow_zero else "dương"
        raise VenueOrderConflict(f"{name} phải {qualifier}.")


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise VenueOrderConflict(f"{name} không được để trống.")


def _hash(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise VenueOrderConflict(f"{name} phải là SHA-256 dạng hex chữ thường.")


def _decimal_text(value: Decimal) -> str:
    """Chuẩn hóa Decimal mà không dùng phép làm tròn theo context."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
