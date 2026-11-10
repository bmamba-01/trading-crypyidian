"""Bằng chứng tập trade đóng đã lưu; không cấp quyền đọc mẫu hay huấn luyện."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

QUERY_VERSION = "base-closed-trade-universe-v1"


@dataclass(frozen=True, slots=True)
class CandidateUniverseProof:
    """Chỉ metadata và digest; eligible chỉ nghĩa là đủ provenance entry.

    Coverage giới hạn trong mọi closed trade outcome persisted của đúng trial
    BASE. Không chứng minh mọi quyết định/no-entry và không suy quyền TRAINING
    từ receipt BACKTEST_RESEARCH. Boundary cấp sample payload phải kiểm quyền
    TRAINING hiện hành riêng, cùng transaction, trước khi chuẩn hóa mẫu.
    """

    workspace_id: UUID
    run_id: UUID
    trial_record_id: UUID
    report_id: UUID
    scenario_id: UUID
    manifest_hash: str
    trial_hash: str
    report_hash: str
    scenario_hash: str
    outcomes_hash: str
    membership_digest: str
    training_rights_digest: str
    candidate_count: int
    eligible_count: int
    exclusion_count: int
    exclusion_reason_counts: tuple[tuple[str, int], ...]
    query_version: str = QUERY_VERSION
    coverage_scope: str = "persisted_closed_trade_outcomes"
    eligibility_scope: str = "entry_provenance_only"
    training_authorized: bool = False

    def __post_init__(self) -> None:
        if (
            self.query_version != QUERY_VERSION
            or self.coverage_scope != "persisted_closed_trade_outcomes"
            or self.eligibility_scope != "entry_provenance_only"
            or self.training_authorized is not False
        ):
            raise ValueError("Proof chỉ chứng minh coverage/provenance, không cấp quyền TRAINING.")
        for field in ("workspace_id", "run_id", "trial_record_id", "report_id", "scenario_id"):
            if type(getattr(self, field)) is not UUID:
                raise ValueError("Định danh proof phải là UUID.")
        for field in (
            "manifest_hash",
            "trial_hash",
            "report_hash",
            "scenario_hash",
            "outcomes_hash",
            "membership_digest",
            "training_rights_digest",
        ):
            if (
                not isinstance(getattr(self, field), str)
                or re.fullmatch(r"[0-9a-f]{64}", getattr(self, field)) is None
            ):
                raise ValueError("Digest proof phải là SHA-256 chữ thường.")
        counts = (self.candidate_count, self.eligible_count, self.exclusion_count)
        if (
            any(type(count) is not int or count < 0 for count in counts)
            or self.candidate_count != self.eligible_count + self.exclusion_count
        ):
            raise ValueError("Counts proof phải không âm và khớp tổng candidate.")
        expected = (
            (("entry_provenance_missing", self.exclusion_count),) if self.exclusion_count else ()
        )
        if (
            type(self.exclusion_reason_counts) is not tuple
            or self.exclusion_reason_counts != expected
        ):
            raise ValueError("Counts reason exclusion không khớp provenance thiếu.")

    @property
    def canonical_json(self) -> str:
        """Bytes chuẩn chỉ chứa metadata, không chứa vector hoặc nhãn."""
        payload = asdict(self)
        for key in ("workspace_id", "run_id", "trial_record_id", "report_id", "scenario_id"):
            payload[key] = str(payload[key])
        return canonical(payload)

    @property
    def content_hash(self) -> str:
        """SHA-256 của bytes chuẩn."""
        return digest(self.canonical_json)


@dataclass(frozen=True, slots=True)
class CandidateUniverseProofResult:
    """Kết quả ghi hoặc đọc lại proof bất biến."""

    record_id: UUID
    proof: CandidateUniverseProof
    duplicate: bool


DECISION_OPPORTUNITY_QUERY_VERSION = "base-decision-opportunity-universe-v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateDecisionOpportunityProof(CandidateUniverseProof):
    """Bằng chứng metadata BASE; không gắn fold/TEST hoặc mở quyền sample."""

    opportunity_count: int
    no_candidate_count: int
    not_run_count: int
    input_excluded_count: int
    unlabeled_candidate_count: int
    outcome_link_count: int
    source_scope_hash: str
    completeness_verified: bool
    query_version: str = DECISION_OPPORTUNITY_QUERY_VERSION
    coverage_scope: str = "declared_base_decision_opportunities"
    eligibility_scope: str = "linked_closed_outcome_entry_provenance"

    def __post_init__(self) -> None:
        if (
            self.query_version != DECISION_OPPORTUNITY_QUERY_VERSION
            or self.coverage_scope != "declared_base_decision_opportunities"
            or self.eligibility_scope != "linked_closed_outcome_entry_provenance"
        ):
            raise ValueError("Proof opportunity chỉ chứng minh scope BASE cố định.")
        common = {
            key: getattr(self, key)
            for key in (
                "workspace_id",
                "run_id",
                "trial_record_id",
                "report_id",
                "scenario_id",
                "manifest_hash",
                "trial_hash",
                "report_hash",
                "scenario_hash",
                "outcomes_hash",
                "membership_digest",
                "training_rights_digest",
                "candidate_count",
                "eligible_count",
                "exclusion_count",
                "training_authorized",
            )
        }
        CandidateUniverseProof(
            **common,
            exclusion_reason_counts=(
                (("entry_provenance_missing", self.exclusion_count),)
                if self.exclusion_count
                else ()
            ),
        )
        counts = (
            self.opportunity_count,
            self.no_candidate_count,
            self.not_run_count,
            self.input_excluded_count,
            self.unlabeled_candidate_count,
            self.outcome_link_count,
        )
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or self.opportunity_count
            != self.candidate_count + self.no_candidate_count + self.not_run_count
            or self.input_excluded_count > self.not_run_count
            or self.unlabeled_candidate_count != self.exclusion_count
            or self.outcome_link_count != self.eligible_count
            or type(self.completeness_verified) is not bool
            or (self.completeness_verified and self.input_excluded_count)
        ):
            raise ValueError("Counts/completeness proof opportunity không hợp lệ.")
        if (
            not isinstance(self.source_scope_hash, str)
            or re.fullmatch("[0-9a-f]{64}", self.source_scope_hash) is None
        ):
            raise ValueError("Digest nguồn phải là SHA-256 chữ thường.")
        expected = (
            (("closed_outcome_missing", self.unlabeled_candidate_count),)
            if self.unlabeled_candidate_count
            else ()
        )
        if self.exclusion_reason_counts != expected:
            raise ValueError("Counts reason không khớp candidate chưa có outcome.")


@dataclass(frozen=True, slots=True)
class CandidateDecisionOpportunityProofResult:
    """Kết quả proof opportunity versioned, chỉ chứa metadata."""

    record_id: UUID
    proof: CandidateDecisionOpportunityProof
    duplicate: bool


class CandidateUniverseProofService:
    """Tự quản lý transaction, khóa nguồn và trial cho scan/append nhất quán."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def build(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> CandidateUniverseProofResult:
        """Caller chỉ được chọn workspace và trial; không nhận claimed coverage."""
        from sqlalchemy import text

        from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
            CandidateUniverseProofRepository,
        )

        if type(workspace_id) is not UUID or type(trial_record_id) is not UUID:
            raise ValueError("workspace_id và trial_record_id phải là UUID.")
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE")
            )
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            session.info["workspace_id"] = workspace_id
            return await CandidateUniverseProofRepository(session).build_and_append(
                workspace_id=workspace_id, trial_record_id=trial_record_id
            )

    async def build_decision_opportunity(
        self, *, workspace_id: UUID, trial_record_id: UUID
    ) -> CandidateDecisionOpportunityProofResult:
        """Dựng proof BASE từ DB; caller không truyền inventory hay claimed count."""
        from sqlalchemy import text

        from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
            CandidateUniverseProofRepository,
        )

        if type(workspace_id) is not UUID or type(trial_record_id) is not UUID:
            raise ValueError("workspace_id và trial_record_id phải là UUID.")
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE")
            )
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            session.info["workspace_id"] = workspace_id
            return await CandidateUniverseProofRepository(session).build_decision_opportunity(
                workspace_id=workspace_id, trial_record_id=trial_record_id
            )


def canonical(value: object) -> str:
    """Chuẩn hóa JSON không chấp nhận số không hữu hạn."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: str) -> str:
    """Hash UTF-8 định trước, dùng chung cho proof và membership."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
