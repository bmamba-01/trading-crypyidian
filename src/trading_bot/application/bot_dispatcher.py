"""Fan-out shared signal evidence tới strategy riêng của từng bot.

Evidence chung chỉ là đầu vào trung lập. Mỗi bot nhận đúng một job idempotent
theo `(workspace_id, bot_id, evidence_id, bot_config_version_id)`, tự chạy
strategy release/tham số của mình và ghi `BotDecision` bot-scoped. Module này
không giữ planner, risk engine hay PAPER gateway nên không thể tạo plan,
reservation, intent hoặc lệnh venue; plan/risk theo bot thuộc MB-04.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid5

from trading_bot.application.paper_decision_cycle import strategy_selection_for_release
from trading_bot.domain.market.signal_evidence import SharedSignalEvidence
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    NoTrade,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.registry import StrategyRegistry, UnknownStrategyReleaseError

EVIDENCE_RECORDED_EVENT = "signal.evidence.recorded.v1"
BOT_DECISION_SCHEMA_VERSION = "crypidian-bot-decision-v1"
BOT_DECISION_CONTEXT_SCHEMA_VERSION = "crypidian-bot-decision-context-v1"
# Namespace cố định cho UUID tất định của job/decision/outbox event; đổi sẽ đổi mọi ID.
BOT_DECISION_NAMESPACE = UUID("8f1d6c2e-4b7a-5e93-a0c5-71d2e94b3f60")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class EvidenceScopeError(ValueError):
    """Evidence/context không cùng workspace, trùng bot/account hoặc lineage không khớp."""


class BotDecisionConflict(RuntimeError):
    """Cùng idempotency key nhưng payload khác; fail-closed theo contract §6."""


class BotDecisionStatus(StrEnum):
    """CANDIDATE có candidate riêng; ABSTAIN bot chủ động đứng ngoài; VETO cổng bot chặn."""

    CANDIDATE = "CANDIDATE"
    ABSTAIN = "ABSTAIN"
    VETO = "VETO"


@dataclass(frozen=True, slots=True)
class BotDecisionContext:
    """Phần cấu hình hiệu lực của một bot mà dispatcher cần (adapter từ EffectiveBotConfig)."""

    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    bot_config_version_id: UUID
    bot_config_hash: str
    root_settings_version_id: UUID
    root_settings_hash: str
    strategy_release_id: str
    symbols: tuple[str, ...]
    effective_at: datetime
    strategy_parameters: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "bot_id",
            "account_id",
            "bot_config_version_id",
            "root_settings_version_id",
        ):
            if not isinstance(getattr(self, name), UUID):
                raise ValueError(f"{name} phải là UUID.")
        for name in ("bot_config_hash", "root_settings_hash"):
            if not _SHA256_HEX.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
        if not self.strategy_release_id.strip():
            raise ValueError("strategy_release_id không được để trống.")
        symbols = tuple(self.symbols)
        if not symbols or len(set(symbols)) != len(symbols) or any(not s.strip() for s in symbols):
            raise ValueError("symbols của bot phải không rỗng và không trùng.")
        object.__setattr__(self, "symbols", tuple(sorted(symbols)))
        _require_aware(self.effective_at, "effective_at")
        object.__setattr__(self, "effective_at", self.effective_at.astimezone(UTC))
        object.__setattr__(self, "strategy_parameters", tuple(sorted(self.strategy_parameters)))
        # Kiểm tra tham số bằng cùng quy tắc với root settings trước khi context được dùng.
        self.strategy_selection()

    def strategy_selection(self) -> StrategySelection:
        """Selection riêng của bot (release + tham số của bot).

        `settings_version_id` là root `workspace_settings_version` theo ruling controller:
        planner (STALE_SETTINGS_VERSION), FK `order_intent.settings_version_id` và kiểm tra
        ledger đọc nó như root. Bot config version chỉ nằm ở `bot_config_version_id`.
        """
        return strategy_selection_for_release(
            release_id=self.strategy_release_id,
            parameters=dict(self.strategy_parameters),
            settings_version_id=str(self.root_settings_version_id),
        )

    def canonical_payload(self) -> dict[str, Any]:
        """Payload canonical để ghim context vào job và tính hash."""
        return {
            "account_id": str(self.account_id),
            "bot_config_hash": self.bot_config_hash,
            "bot_config_version_id": str(self.bot_config_version_id),
            "bot_id": str(self.bot_id),
            "effective_at": _utc_iso(self.effective_at),
            "root_settings_hash": self.root_settings_hash,
            "root_settings_version_id": str(self.root_settings_version_id),
            "schema_version": BOT_DECISION_CONTEXT_SCHEMA_VERSION,
            "strategy_parameters": [[key, value] for key, value in self.strategy_parameters],
            "strategy_release_id": self.strategy_release_id,
            "symbols": list(self.symbols),
            "workspace_id": str(self.workspace_id),
        }

    @property
    def context_hash(self) -> str:
        """Hash của context đã ghim, dùng để phát hiện job cùng key khác cấu hình."""
        return _hash(self.canonical_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BotDecisionContext:
        """Khôi phục context từ payload canonical đã lưu."""
        if payload.get("schema_version") != BOT_DECISION_CONTEXT_SCHEMA_VERSION:
            raise ValueError("Schema version của bot decision context không được hỗ trợ.")
        return cls(
            workspace_id=UUID(payload["workspace_id"]),
            bot_id=UUID(payload["bot_id"]),
            account_id=UUID(payload["account_id"]),
            bot_config_version_id=UUID(payload["bot_config_version_id"]),
            bot_config_hash=payload["bot_config_hash"],
            root_settings_version_id=UUID(payload["root_settings_version_id"]),
            root_settings_hash=payload["root_settings_hash"],
            strategy_release_id=payload["strategy_release_id"],
            symbols=tuple(payload["symbols"]),
            effective_at=_parse_utc(payload["effective_at"]),
            strategy_parameters=tuple(
                (str(key), int(value)) for key, value in payload["strategy_parameters"]
            ),
        )


class BotDecisionContextProvider(Protocol):
    """Trả context các bot đủ điều kiện có hiệu lực tại `as_of` (MB-02 resolver)."""

    async def contexts_for(
        self, *, workspace_id: UUID, as_of: datetime
    ) -> Sequence[BotDecisionContext]: ...


class DecisionSnapshotProvider(Protocol):
    """Dựng DecisionSnapshot point-in-time cho strategy của một bot tại `evaluated_at`."""

    async def snapshot_for(
        self,
        *,
        evidence: SharedSignalEvidence,
        context: BotDecisionContext,
        evaluated_at: datetime,
    ) -> DecisionSnapshot: ...


@dataclass(frozen=True, slots=True)
class BotDecisionJob:
    """Job bot-scoped ghim evidence, context và thời điểm đánh giá."""

    job_id: UUID
    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    evidence_id: UUID
    evidence_hash: str
    bot_config_version_id: UUID
    evaluated_at: datetime
    context: BotDecisionContext

    def __post_init__(self) -> None:
        context = self.context
        if (
            context.workspace_id,
            context.bot_id,
            context.account_id,
            context.bot_config_version_id,
        ) != (self.workspace_id, self.bot_id, self.account_id, self.bot_config_version_id):
            raise ValueError("Scope của job không khớp context đã ghim.")
        if not _SHA256_HEX.fullmatch(self.evidence_hash):
            raise ValueError("evidence_hash của job phải là SHA-256 hex chữ thường.")
        _require_aware(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "evaluated_at", self.evaluated_at.astimezone(UTC))
        if self.job_id != uuid5(BOT_DECISION_NAMESPACE, "job:" + self.idempotency_key):
            raise ValueError("job_id không khớp idempotency key của job.")

    @classmethod
    def create(
        cls,
        *,
        evidence: SharedSignalEvidence,
        context: BotDecisionContext,
        evaluated_at: datetime,
    ) -> BotDecisionJob:
        """Tạo job tất định cho một bot từ evidence đã kiểm scope."""
        if evidence.workspace_id != context.workspace_id:
            raise EvidenceScopeError("Evidence không thuộc workspace của bot.")
        key = decision_idempotency_key(
            workspace_id=context.workspace_id,
            bot_id=context.bot_id,
            account_id=context.account_id,
            evidence_id=evidence.evidence_id,
            bot_config_version_id=context.bot_config_version_id,
        )
        return cls(
            job_id=uuid5(BOT_DECISION_NAMESPACE, "job:" + key),
            workspace_id=context.workspace_id,
            bot_id=context.bot_id,
            account_id=context.account_id,
            evidence_id=evidence.evidence_id,
            evidence_hash=evidence.evidence_hash,
            bot_config_version_id=context.bot_config_version_id,
            evaluated_at=evaluated_at,
            context=context,
        )

    @property
    def idempotency_key(self) -> str:
        """Khóa quyết định duy nhất theo workspace/bot/account/evidence/config version."""
        return decision_idempotency_key(
            workspace_id=self.workspace_id,
            bot_id=self.bot_id,
            account_id=self.account_id,
            evidence_id=self.evidence_id,
            bot_config_version_id=self.bot_config_version_id,
        )

    def canonical_payload(self) -> dict[str, Any]:
        """Payload canonical của job (context lồng trong)."""
        return {
            "context": self.context.canonical_payload(),
            "evaluated_at": _utc_iso(self.evaluated_at),
            "evidence_hash": self.evidence_hash,
            "evidence_id": str(self.evidence_id),
            "job_id": str(self.job_id),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BotDecisionJob:
        """Khôi phục job; scope lấy từ context đã ghim để không thể lệch nhau."""
        context = BotDecisionContext.from_payload(payload["context"])
        return cls(
            job_id=UUID(payload["job_id"]),
            workspace_id=context.workspace_id,
            bot_id=context.bot_id,
            account_id=context.account_id,
            evidence_id=UUID(payload["evidence_id"]),
            evidence_hash=payload["evidence_hash"],
            bot_config_version_id=context.bot_config_version_id,
            evaluated_at=_parse_utc(payload["evaluated_at"]),
            context=context,
        )


def reconcile_existing_job(stored: BotDecisionJob, requested: BotDecisionJob) -> BotDecisionJob:
    """Job đã có cùng key (ruling 17): chỉ khác `evaluated_at` thì trả job cũ (ack).

    `evaluated_at` được ghim từ event đầu tiên; retry chỉ khác mốc thời gian không phải job mới
    và không phải conflict (trước MB05.8 nó làm event bị DEAD). Khác evidence/context là
    `BotDecisionConflict` (fail-closed).
    """
    if (
        stored.job_id,
        stored.evidence_id,
        stored.evidence_hash,
        stored.context.context_hash,
    ) != (
        requested.job_id,
        requested.evidence_id,
        requested.evidence_hash,
        requested.context.context_hash,
    ) or stored.context != requested.context:
        raise BotDecisionConflict("Job cùng key nhưng khác evidence/context đã ghim.")
    return stored


@dataclass(frozen=True, slots=True)
class JobRetryPolicy:
    """Số lần thử và backoff có trần cho job lỗi (mô hình dead-letter, ruling 16)."""

    max_attempts: int = 5
    base_delay_seconds: int = 30
    max_delay_seconds: int = 900

    def __post_init__(self) -> None:
        if (
            self.max_attempts < 1
            or self.base_delay_seconds < 1
            or self.max_delay_seconds < self.base_delay_seconds
        ):
            raise ValueError("JobRetryPolicy không hợp lệ.")

    def delay_seconds(self, attempt: int) -> int:
        """Exponential backoff có trần: base * 2^(attempt-1) nhưng không vượt max."""
        if attempt < 1:
            raise ValueError("attempt phải >= 1.")
        if attempt - 1 >= (self.max_delay_seconds // self.base_delay_seconds).bit_length():
            return self.max_delay_seconds
        return min(self.base_delay_seconds << (attempt - 1), self.max_delay_seconds)


_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def job_error_code(error: BaseException) -> str:
    """Mã lỗi an toàn lưu `last_error_code`: reason code máy đọc hoặc tên lớp dạng SNAKE.

    Không bao giờ lưu thông điệp tự do (có thể chứa dữ liệu nhạy cảm/stack).
    """
    code = getattr(error, "reason_code", None) or getattr(error, "code", None)
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).upper()
    snake = re.sub(r"[^A-Z0-9_]", "_", snake)[:80]
    return snake if _SAFE_CODE.fullmatch(snake) else "JOB_EVALUATION_FAILED"


@dataclass(frozen=True, slots=True)
class BotDecision:
    """Quyết định bất biến của một bot cho một evidence, kèm đầy đủ lineage."""

    decision_id: UUID
    job_id: UUID
    workspace_id: UUID
    bot_id: UUID
    account_id: UUID
    evidence_id: UUID
    evidence_hash: str
    feature_snapshot_id: str
    bot_config_version_id: UUID
    bot_config_hash: str
    root_settings_version_id: UUID
    root_settings_hash: str
    strategy_release_id: str
    strategy_release_hash: str | None
    strategy_parameters: tuple[tuple[str, int], ...]
    model_release_id: UUID | None
    evaluated_at: datetime
    status: BotDecisionStatus
    reason_code: str
    detail_vi: str
    decision_snapshot_id: str | None
    candidate_json: str | None
    gate_details: tuple[str, ...]
    idempotency_key: str
    decision_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, BotDecisionStatus):
            raise ValueError("status phải là BotDecisionStatus.")
        if not self.reason_code.strip() or not self.detail_vi.strip():
            raise ValueError("Quyết định cần reason_code và giải thích tiếng Việt.")
        if (self.status is BotDecisionStatus.CANDIDATE) != (self.candidate_json is not None):
            raise ValueError("Chỉ quyết định CANDIDATE mới có candidate.")
        _require_aware(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "evaluated_at", self.evaluated_at.astimezone(UTC))
        if self.decision_id != uuid5(BOT_DECISION_NAMESPACE, "decision:" + self.idempotency_key):
            raise ValueError("decision_id không khớp idempotency key.")
        if self.decision_hash != _hash(self.canonical_payload()):
            raise ValueError("decision_hash không khớp payload canonical.")

    @property
    def candidate_id(self) -> str | None:
        """ID candidate riêng của bot (None khi ABSTAIN/VETO)."""
        if self.candidate_json is None:
            return None
        return str(json.loads(self.candidate_json)["candidate_id"])

    def canonical_payload(self) -> dict[str, Any]:
        """Payload canonical không chứa đồng hồ wall-clock nên replay cho cùng hash."""
        return _decision_payload({name: getattr(self, name) for name in self.__dataclass_fields__})

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], decision_hash: str) -> BotDecision:
        """Khôi phục quyết định đã lưu và xác minh hash."""
        if payload.get("schema_version") != BOT_DECISION_SCHEMA_VERSION:
            raise ValueError("Schema version của bot decision không được hỗ trợ.")
        model_release = payload["model_release_id"]
        candidate = payload["candidate"]
        return cls(
            decision_id=UUID(payload["decision_id"]),
            job_id=UUID(payload["job_id"]),
            workspace_id=UUID(payload["workspace_id"]),
            bot_id=UUID(payload["bot_id"]),
            account_id=UUID(payload["account_id"]),
            evidence_id=UUID(payload["evidence_id"]),
            evidence_hash=payload["evidence_hash"],
            feature_snapshot_id=payload["feature_snapshot_id"],
            bot_config_version_id=UUID(payload["bot_config_version_id"]),
            bot_config_hash=payload["bot_config_hash"],
            root_settings_version_id=UUID(payload["root_settings_version_id"]),
            root_settings_hash=payload["root_settings_hash"],
            strategy_release_id=payload["strategy_release_id"],
            strategy_release_hash=payload["strategy_release_hash"],
            strategy_parameters=tuple(
                (str(key), int(value)) for key, value in payload["strategy_parameters"]
            ),
            model_release_id=None if model_release is None else UUID(model_release),
            evaluated_at=_parse_utc(payload["evaluated_at"]),
            status=BotDecisionStatus(payload["status"]),
            reason_code=payload["reason_code"],
            detail_vi=payload["detail_vi"],
            decision_snapshot_id=payload["decision_snapshot_id"],
            candidate_json=None if candidate is None else _canonical_json(candidate),
            gate_details=tuple(payload["gate_details"]),
            idempotency_key=payload["idempotency_key"],
            decision_hash=decision_hash,
        )


@dataclass(frozen=True, slots=True)
class EvidenceRecordedEvent:
    """Outbox event báo evidence mới cần fan-out; ghim thời điểm đánh giá."""

    event_id: UUID
    workspace_id: UUID
    evidence_id: UUID
    evidence_hash: str
    evaluated_at: datetime

    @classmethod
    def for_evidence(
        cls, evidence: SharedSignalEvidence, *, evaluated_at: datetime
    ) -> EvidenceRecordedEvent:
        """Event ID tất định theo evidence để ghi lặp không tạo event thứ hai."""
        _require_aware(evaluated_at, "evaluated_at")
        return cls(
            event_id=uuid5(BOT_DECISION_NAMESPACE, f"evidence-event:{evidence.evidence_id}"),
            workspace_id=evidence.workspace_id,
            evidence_id=evidence.evidence_id,
            evidence_hash=evidence.evidence_hash,
            evaluated_at=evaluated_at.astimezone(UTC),
        )

    def payload(self) -> dict[str, str]:
        """Payload outbox chỉ chứa tham chiếu, không chứa cấu hình hay dữ liệu bot."""
        return {
            "evaluated_at": _utc_iso(self.evaluated_at),
            "evidence_hash": self.evidence_hash,
            "evidence_id": str(self.evidence_id),
        }

    @classmethod
    def from_payload(
        cls, *, event_id: UUID, workspace_id: UUID, payload: Mapping[str, Any]
    ) -> EvidenceRecordedEvent:
        """Đọc lại event đã claim từ outbox."""
        return cls(
            event_id=event_id,
            workspace_id=workspace_id,
            evidence_id=UUID(payload["evidence_id"]),
            evidence_hash=str(payload["evidence_hash"]),
            evaluated_at=_parse_utc(str(payload["evaluated_at"])),
        )


@dataclass(frozen=True, slots=True)
class RecordedEvidence:
    """Evidence chuẩn đã lưu (bản ghi đầu tiên của observation) và cờ vừa tạo mới."""

    evidence: SharedSignalEvidence
    created: bool


type DecisionEvaluator = Callable[[SharedSignalEvidence, BotDecisionJob], Awaitable[BotDecision]]


class SignalDecisionStore(Protocol):
    """Cổng lưu trữ: evidence workspace-scoped, job/decision bot-scoped, outbox fan-out."""

    async def record_evidence(
        self, evidence: SharedSignalEvidence, *, evaluated_at: datetime
    ) -> RecordedEvidence: ...

    async def load_evidence(
        self, *, workspace_id: UUID, evidence_id: UUID
    ) -> SharedSignalEvidence | None: ...

    async def enqueue_job(self, job: BotDecisionJob) -> BotDecisionJob: ...

    async def pending_jobs(
        self, *, workspace_id: UUID, account_id: UUID, limit: int, now: datetime | None = None
    ) -> tuple[BotDecisionJob, ...]: ...

    async def complete_job(
        self, job: BotDecisionJob, evaluate: DecisionEvaluator
    ) -> BotDecision: ...

    async def record_job_failure(
        self, job: BotDecisionJob, *, error_code: str, now: datetime, policy: JobRetryPolicy
    ) -> str: ...

    async def claim_evidence_events(
        self,
        *,
        workspace_id: UUID,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
        limit: int,
    ) -> tuple[EvidenceRecordedEvent, ...]: ...

    async def acknowledge_event(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        consumer_name: str,
        now: datetime,
    ) -> None: ...

    async def record_event_failure(
        self,
        *,
        workspace_id: UUID,
        event_id: UUID,
        worker_id: str,
        now: datetime,
        error: BaseException,
        max_attempts: int,
    ) -> str: ...


class BotEvidenceDispatcher:
    """Ghi evidence một lần, fan-out job theo bot và đánh giá strategy riêng của từng bot."""

    def __init__(
        self,
        store: SignalDecisionStore,
        *,
        strategies: StrategyRegistry,
        snapshots: DecisionSnapshotProvider,
    ) -> None:
        self._store = store
        self._strategies = strategies
        self._snapshots = snapshots

    async def record_evidence(
        self, evidence: SharedSignalEvidence, *, evaluated_at: datetime
    ) -> RecordedEvidence:
        """Ghi evidence + outbox event cùng transaction; observation đã có trả bản ghi đầu tiên."""
        evidence.require_usable_at(evaluated_at)
        return await self._store.record_evidence(evidence, evaluated_at=evaluated_at)

    async def fan_out(
        self,
        evidence: SharedSignalEvidence,
        bots: Sequence[BotDecisionContext],
        as_of: datetime,
    ) -> tuple[BotDecisionJob, ...]:
        """Tạo đúng một job cho mỗi bot; retry trả job đã có, khác payload là conflict."""
        _validate_scope(evidence, bots)
        evidence.require_usable_at(as_of)
        jobs = []
        for context in bots:
            job = BotDecisionJob.create(evidence=evidence, context=context, evaluated_at=as_of)
            jobs.append(await self._store.enqueue_job(job))
        return tuple(jobs)

    async def process_job(self, job: BotDecisionJob) -> BotDecision:
        """Đánh giá job đúng một lần; job đã xong trả quyết định đã lưu."""
        return await self._store.complete_job(job, self._evaluate)

    async def process_pending(
        self,
        *,
        workspace_id: UUID,
        account_id: UUID,
        limit: int = 20,
        now: datetime | None = None,
        policy: JobRetryPolicy | None = None,
    ) -> tuple[BotDecision, ...]:
        """Xử lý job còn chờ của một bot/account; poison job không chặn job sau (ruling 16).

        Mỗi job một transaction. Job lỗi được ghi trong transaction riêng (`attempts += 1`,
        `last_error_code` an toàn, `next_attempt_at` theo backoff có trần); quá ngưỡng thì
        `DEAD` kèm audit; vòng lặp tiếp tục job kế tiếp.
        """
        moment = now or datetime.now(UTC)
        retry = policy or JobRetryPolicy()
        jobs = await self._store.pending_jobs(
            workspace_id=workspace_id, account_id=account_id, limit=limit, now=moment
        )
        decisions: list[BotDecision] = []
        for job in jobs:
            try:
                decisions.append(await self.process_job(job))
            except Exception as error:
                await self._store.record_job_failure(
                    job, error_code=job_error_code(error), now=moment, policy=retry
                )
        return tuple(decisions)

    async def dispatch_evidence(
        self,
        evidence: SharedSignalEvidence,
        bots: Sequence[BotDecisionContext],
        as_of: datetime,
    ) -> Sequence[BotDecision]:
        """Ghi evidence, fan-out và đánh giá từng bot; an toàn khi retry."""
        _validate_scope(evidence, bots)
        evidence.require_usable_at(as_of)
        recorded = await self._store.record_evidence(evidence, evaluated_at=as_of)
        # Retry cùng observation (chỉ khác mốc thời gian) dùng bản ghi đầu tiên đã lưu.
        jobs = await self.fan_out(recorded.evidence, bots, as_of)
        return tuple([await self.process_job(job) for job in jobs])

    async def _evaluate(self, evidence: SharedSignalEvidence, job: BotDecisionJob) -> BotDecision:
        return await evaluate_bot_decision(
            evidence=evidence, job=job, strategies=self._strategies, snapshots=self._snapshots
        )


class SignalEvidenceFanOutConsumer:
    """Consumer outbox: claim event evidence, fan-out job theo bot rồi acknowledge."""

    consumer_name = "bot-evidence-fanout"

    def __init__(
        self,
        store: SignalDecisionStore,
        dispatcher: BotEvidenceDispatcher,
        contexts: BotDecisionContextProvider,
        *,
        worker_id: str,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id không được để trống.")
        self._store = store
        self._dispatcher = dispatcher
        self._contexts = contexts
        self._worker_id = worker_id

    async def consume_once(
        self,
        *,
        workspace_id: UUID,
        now: datetime,
        lease_duration: timedelta = timedelta(seconds=60),
        max_attempts: int = 5,
        limit: int = 20,
    ) -> tuple[BotDecisionJob, ...]:
        """Một vòng claim → fan-out → ack; lỗi được ghi để retry/backoff, không nuốt im lặng."""
        events = await self._store.claim_evidence_events(
            workspace_id=workspace_id,
            worker_id=self._worker_id,
            now=now,
            lease_duration=lease_duration,
            max_attempts=max_attempts,
            limit=limit,
        )
        jobs: list[BotDecisionJob] = []
        for event in events:
            try:
                jobs.extend(await self._fan_out_event(event))
            except Exception as error:
                await self._store.record_event_failure(
                    workspace_id=workspace_id,
                    event_id=event.event_id,
                    worker_id=self._worker_id,
                    now=now,
                    error=error,
                    max_attempts=max_attempts,
                )
                continue
            await self._store.acknowledge_event(
                workspace_id=workspace_id,
                event_id=event.event_id,
                worker_id=self._worker_id,
                consumer_name=self.consumer_name,
                now=now,
            )
        return tuple(jobs)

    async def _fan_out_event(self, event: EvidenceRecordedEvent) -> tuple[BotDecisionJob, ...]:
        evidence = await self._store.load_evidence(
            workspace_id=event.workspace_id, evidence_id=event.evidence_id
        )
        if evidence is None or evidence.evidence_hash != event.evidence_hash:
            raise EvidenceScopeError("Evidence của outbox event không tồn tại hoặc sai hash.")
        bots = await self._contexts.contexts_for(
            workspace_id=event.workspace_id, as_of=event.evaluated_at
        )
        return await self._dispatcher.fan_out(evidence, bots, event.evaluated_at)


async def evaluate_bot_decision(
    *,
    evidence: SharedSignalEvidence,
    job: BotDecisionJob,
    strategies: StrategyRegistry,
    snapshots: DecisionSnapshotProvider,
) -> BotDecision:
    """Chạy cổng của bot rồi strategy riêng; không bao giờ tạo plan/intent/lệnh."""
    if (evidence.workspace_id, evidence.evidence_id, evidence.evidence_hash) != (
        job.workspace_id,
        job.evidence_id,
        job.evidence_hash,
    ):
        raise EvidenceScopeError("Evidence không khớp lineage đã ghim trong job.")
    context = job.context
    evaluated_at = job.evaluated_at
    draft = _DecisionDraft(evidence=evidence, job=job)
    unavailable = evidence.unavailable_reason(evaluated_at)
    if unavailable is not None:
        return draft.build(
            BotDecisionStatus.VETO,
            unavailable,
            "Evidence ngoài cửa sổ available_at <= evaluated_at < validity_end.",
        )
    if evaluated_at < context.effective_at:
        return draft.build(
            BotDecisionStatus.VETO,
            "BOT_CONFIG_NOT_EFFECTIVE",
            "Phiên bản cấu hình bot chưa có hiệu lực tại thời điểm đánh giá.",
        )
    if evidence.symbol not in context.symbols:
        return draft.build(
            BotDecisionStatus.ABSTAIN,
            "SYMBOL_OUT_OF_BOT_UNIVERSE",
            "Symbol của evidence nằm ngoài universe hiệu lực của bot.",
        )
    try:
        release = strategies.get_release(context.strategy_release_id)
    except UnknownStrategyReleaseError:
        return draft.build(
            BotDecisionStatus.VETO,
            "STRATEGY_RELEASE_UNAVAILABLE",
            "Strategy release của bot không có trong registry hiện hành.",
        )
    draft.strategy_release_hash = release.content_hash
    snapshot = await snapshots.snapshot_for(
        evidence=evidence, context=context, evaluated_at=evaluated_at
    )
    draft.decision_snapshot_id = snapshot.snapshot_id
    if snapshot.symbol != evidence.symbol:
        return draft.build(
            BotDecisionStatus.VETO,
            "DECISION_SNAPSHOT_SCOPE_MISMATCH",
            "DecisionSnapshot không cùng symbol với evidence.",
        )
    future_inputs = _future_inputs(snapshot, evaluated_at)
    if future_inputs:
        return draft.build(
            BotDecisionStatus.VETO,
            "FUTURE_INPUT_REJECTED",
            "Có dữ liệu market/feature/macro chỉ khả dụng sau thời điểm đánh giá.",
            gate_details=future_inputs,
        )
    selection = context.strategy_selection()
    result = strategies.resolve(context.strategy_release_id, selection).evaluate(
        snapshot, selection
    )
    if isinstance(result, NoTrade):
        return draft.build(
            BotDecisionStatus.ABSTAIN,
            result.code,
            result.detail_vi,
            gate_details=(*result.missing_features, *result.gate_details),
        )
    if (
        result.symbol != evidence.symbol
        or result.settings_version_id != selection.settings_version_id
        or result.strategy_release_id != context.strategy_release_id
        or result.release_content_hash != release.content_hash
    ):
        return draft.build(
            BotDecisionStatus.VETO,
            "CANDIDATE_SCOPE_MISMATCH",
            "Candidate không khớp symbol, cấu hình hoặc release đã chọn của bot.",
        )
    if result.signal_at > evaluated_at:
        return draft.build(
            BotDecisionStatus.VETO,
            "FUTURE_INPUT_REJECTED",
            "Candidate có thời điểm tín hiệu sau thời điểm đánh giá.",
        )
    return draft.build(
        BotDecisionStatus.CANDIDATE,
        result.reason_code,
        result.detail_vi,
        candidate=result,
    )


def decision_idempotency_key(
    *,
    workspace_id: UUID,
    bot_id: UUID,
    account_id: UUID,
    evidence_id: UUID,
    bot_config_version_id: UUID,
) -> str:
    """SHA-256 của scope quyết định; mỗi bot có namespace idempotency riêng."""
    raw = "|".join(
        (
            "bot-decision-v1",
            str(workspace_id),
            str(bot_id),
            str(account_id),
            str(evidence_id),
            str(bot_config_version_id),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _DecisionDraft:
    """Gom lineage chung để mọi nhánh trả quyết định nhất quán."""

    def __init__(self, *, evidence: SharedSignalEvidence, job: BotDecisionJob) -> None:
        self._evidence = evidence
        self._job = job
        self.strategy_release_hash: str | None = None
        self.decision_snapshot_id: str | None = None

    def build(
        self,
        status: BotDecisionStatus,
        reason_code: str,
        detail_vi: str,
        *,
        gate_details: Sequence[str] = (),
        candidate: StrategyCandidate | None = None,
    ) -> BotDecision:
        job = self._job
        context = job.context
        key = job.idempotency_key
        values: dict[str, Any] = {
            "decision_id": uuid5(BOT_DECISION_NAMESPACE, "decision:" + key),
            "job_id": job.job_id,
            "workspace_id": job.workspace_id,
            "bot_id": job.bot_id,
            "account_id": job.account_id,
            "evidence_id": self._evidence.evidence_id,
            "evidence_hash": self._evidence.evidence_hash,
            "feature_snapshot_id": self._evidence.feature_snapshot_id,
            "bot_config_version_id": context.bot_config_version_id,
            "bot_config_hash": context.bot_config_hash,
            "root_settings_version_id": context.root_settings_version_id,
            "root_settings_hash": context.root_settings_hash,
            "strategy_release_id": context.strategy_release_id,
            "strategy_release_hash": self.strategy_release_hash,
            "strategy_parameters": context.strategy_parameters,
            "model_release_id": self._evidence.model_release_id,
            "evaluated_at": job.evaluated_at,
            "status": status,
            "reason_code": reason_code,
            "detail_vi": detail_vi,
            "decision_snapshot_id": self.decision_snapshot_id,
            "candidate_json": None
            if candidate is None
            else _canonical_json(_candidate_payload(candidate)),
            "gate_details": tuple(gate_details),
            "idempotency_key": key,
        }
        return BotDecision(**values, decision_hash=_hash(_decision_payload(values)))


def _decision_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    """Payload canonical của quyết định từ giá trị trường (bỏ qua decision_hash)."""
    candidate_json = values["candidate_json"]
    model_release = values["model_release_id"]
    return {
        "account_id": str(values["account_id"]),
        "bot_config_hash": values["bot_config_hash"],
        "bot_config_version_id": str(values["bot_config_version_id"]),
        "bot_id": str(values["bot_id"]),
        "candidate": None if candidate_json is None else json.loads(candidate_json),
        "decision_id": str(values["decision_id"]),
        "decision_snapshot_id": values["decision_snapshot_id"],
        "detail_vi": values["detail_vi"],
        "evaluated_at": _utc_iso(values["evaluated_at"]),
        "evidence_hash": values["evidence_hash"],
        "evidence_id": str(values["evidence_id"]),
        "feature_snapshot_id": values["feature_snapshot_id"],
        "gate_details": list(values["gate_details"]),
        "idempotency_key": values["idempotency_key"],
        "job_id": str(values["job_id"]),
        "model_release_id": None if model_release is None else str(model_release),
        "reason_code": values["reason_code"],
        "root_settings_hash": values["root_settings_hash"],
        "root_settings_version_id": str(values["root_settings_version_id"]),
        "schema_version": BOT_DECISION_SCHEMA_VERSION,
        "status": BotDecisionStatus(values["status"]).value,
        "strategy_parameters": [[key, value] for key, value in values["strategy_parameters"]],
        "strategy_release_hash": values["strategy_release_hash"],
        "strategy_release_id": values["strategy_release_id"],
        "workspace_id": str(values["workspace_id"]),
    }


def _validate_scope(evidence: SharedSignalEvidence, bots: Sequence[BotDecisionContext]) -> None:
    if any(context.workspace_id != evidence.workspace_id for context in bots):
        raise EvidenceScopeError("Evidence không thuộc workspace của bot được dispatch.")
    bot_ids = [context.bot_id for context in bots]
    account_ids = [context.account_id for context in bots]
    if len(set(bot_ids)) != len(bot_ids) or len(set(account_ids)) != len(account_ids):
        raise EvidenceScopeError("Mỗi bot/account chỉ được nhận evidence một lần mỗi lượt.")


def _future_inputs(snapshot: DecisionSnapshot, evaluated_at: datetime) -> tuple[str, ...]:
    """Liệt kê input có mốc sau evaluated_at; độc lập với kiểm tra bên trong strategy."""
    found: list[str] = []
    if snapshot.decision_time > evaluated_at:
        found.append("decision_time")
    for series in snapshot.series:
        for bar in series.bars:
            if max(bar.close_time, bar.event_at, bar.available_at) > evaluated_at:
                found.append(f"bar:{series.timeframe}:{_utc_iso(bar.open_time)}")
    for feature in snapshot.features:
        if feature.available_at > evaluated_at:
            found.append(f"feature:{feature.feature_id}")
    macro = snapshot.macro_evidence_available_at
    if macro is not None and macro > evaluated_at:
        found.append("macro_evidence")
    return tuple(found)


def _candidate_payload(candidate: StrategyCandidate) -> dict[str, Any]:
    recipe = {
        name: getattr(candidate.recipe, name) for name in candidate.recipe.__dataclass_fields__
    }
    return {
        "atr_value": candidate.atr_value,
        "candidate_id": candidate.candidate_id,
        "detail_vi": candidate.detail_vi,
        "direction": candidate.direction,
        "eligible_after": _utc_iso(candidate.eligible_after),
        "evidence_bar_keys": sorted(candidate.evidence_bar_keys),
        "evidence_feature_ids": sorted(candidate.evidence_feature_ids),
        "expires_at": _utc_iso(candidate.expires_at),
        "geometry_version": candidate.geometry_version,
        "macro_evidence_ids": sorted(candidate.macro_evidence_ids),
        "reason_code": candidate.reason_code,
        "recipe": recipe,
        "reference_price": str(candidate.reference_price),
        "release_content_hash": candidate.release_content_hash,
        "settings_version_id": candidate.settings_version_id,
        "signal_at": _utc_iso(candidate.signal_at),
        "snapshot_id": candidate.snapshot_id,
        "strategy_id": candidate.strategy_id,
        "strategy_release_id": candidate.strategy_release_id,
        "symbol": candidate.symbol,
    }


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
