"""Metadata bất biến cho mọi cơ hội quyết định của replay BASE đã khai báo."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

OPPORTUNITY_SCHEMA_VERSION = "crypidian-replay-decision-opportunity-v3"
OPPORTUNITY_QUERY_VERSION = "base-decision-opportunity-universe-v1"
InputState = Literal["EVALUATED", "INPUT_EXCLUDED"]
StrategyState = Literal["NO_CANDIDATE", "CANDIDATE", "NOT_RUN"]
DecisionOutcome = Literal[
    "NO_TRADE", "PLAN_REJECTED", "RISK_REJECTED", "RISK_APPROVED", "PAPER_INTENT_CREATED"
]


def metadata_hash(payload: object) -> str:
    """Băm JSON metadata canonical, không nhận hoặc dựng vector thay caller."""
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplayDecisionOpportunity:
    """Một nến nguồn, trạng thái gọi strategy và exact vector lineage không có values."""

    workspace_id: UUID
    manifest_hash: str
    bundle_hash: str
    scenario_id: str
    symbol: str
    strategy_release_id: str
    strategy_content_hash: str
    decision_timeframe: str
    evaluation_start: datetime
    range_end: datetime
    event_id: str
    decision_ordinal: int
    event_at: datetime
    available_at: datetime
    decision_at: datetime
    snapshot_id: str
    runtime_snapshot_id: str | None
    feature_snapshot_ids: tuple[tuple[str, str], ...]
    source_event_digests: tuple[tuple[str, str], ...]
    source_refs_digest: str
    feature_schema_hash: str | None
    vector_hash: str | None
    feature_values_hash: str | None
    input_state: InputState
    strategy_state: StrategyState
    decision_outcome: DecisionOutcome
    reason_code: str
    candidate_id: str | None
    schema_version: str = OPPORTUNITY_SCHEMA_VERSION
    query_version: str = OPPORTUNITY_QUERY_VERSION
    content_hash: str = field(init=False)
    idempotency_key: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if self.schema_version != OPPORTUNITY_SCHEMA_VERSION:
            raise ValueError("schema_version opportunity không được hỗ trợ.")
        if self.query_version != OPPORTUNITY_QUERY_VERSION:
            raise ValueError("query_version không được hỗ trợ.")
        for name in (
            "scenario_id",
            "symbol",
            "strategy_release_id",
            "decision_timeframe",
            "event_id",
            "reason_code",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        if type(self.decision_ordinal) is not int or self.decision_ordinal < 0:
            raise ValueError("decision_ordinal phải là số nguyên không âm.")
        for name in ("evaluation_start", "range_end", "event_at", "available_at", "decision_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} phải có múi giờ.")
            object.__setattr__(self, name, value.astimezone(UTC))
        if not self.evaluation_start <= self.decision_at < self.range_end:
            raise ValueError("decision_at nằm ngoài evaluation scope.")
        if not self.event_at <= self.available_at <= self.decision_at:
            raise ValueError("Thời gian source opportunity không nhân quả.")
        for name in (
            "manifest_hash",
            "bundle_hash",
            "strategy_content_hash",
            "snapshot_id",
            "source_refs_digest",
        ):
            self._require_hash(getattr(self, name), name)
        for name in (
            "runtime_snapshot_id",
            "feature_schema_hash",
            "vector_hash",
            "feature_values_hash",
            "candidate_id",
        ):
            if getattr(self, name) is not None:
                self._require_hash(getattr(self, name), name)
        if type(self.feature_snapshot_ids) is not tuple or not self.feature_snapshot_ids:
            raise ValueError("feature_snapshot_ids phải là tuple bất biến có source lineage.")
        timeframes = []
        for pair in self.feature_snapshot_ids:
            if type(pair) is not tuple or len(pair) != 2 or not pair[0].strip():
                raise ValueError("feature_snapshot_ids phải là tuple timeframe/hash.")
            self._require_hash(pair[1], "feature_snapshot_id")
            timeframes.append(pair[0])
        if len(set(timeframes)) != len(timeframes):
            raise ValueError("Timeframe feature snapshot không được trùng.")
        if type(self.source_event_digests) is not tuple or not self.source_event_digests:
            raise ValueError("source_event_digests phải là tuple metadata nguồn bất biến.")
        digest_timeframes = []
        for pair in self.source_event_digests:
            if type(pair) is not tuple or len(pair) != 2 or not isinstance(pair[0], str):
                raise ValueError("source_event_digests phải là tuple timeframe/hash.")
            self._require_hash(pair[1], "source_event_digest")
            digest_timeframes.append(pair[0])
        if digest_timeframes != timeframes:
            raise ValueError("source_event_digests không khớp ordered timeframe keys.")
        expected_refs = metadata_hash(
            [
                {"timeframe": timeframe, "source_event_digest": digest}
                for timeframe, digest in self.source_event_digests
            ]
        )
        if self.source_refs_digest != expected_refs:
            raise ValueError("source_refs_digest không khớp digest-map nguồn.")
        if self.input_state not in ("EVALUATED", "INPUT_EXCLUDED") or self.strategy_state not in (
            "CANDIDATE",
            "NO_CANDIDATE",
            "NOT_RUN",
        ):
            raise ValueError("Trạng thái input/strategy không hợp lệ.")
        if self.decision_outcome not in (
            "NO_TRADE",
            "PLAN_REJECTED",
            "RISK_REJECTED",
            "RISK_APPROVED",
            "PAPER_INTENT_CREATED",
        ):
            raise ValueError("decision_outcome không hợp lệ.")
        if self.input_state == "INPUT_EXCLUDED" and self.strategy_state != "NOT_RUN":
            raise ValueError("Input bị loại phải là NOT_RUN.")
        if self.input_state == "EVALUATED" and any(
            value is None
            for value in (
                self.runtime_snapshot_id,
                self.feature_schema_hash,
                self.vector_hash,
                self.feature_values_hash,
            )
        ):
            raise ValueError("Opportunity đã đánh giá phải có exact snapshot/schema/vector.")
        if self.input_state == "INPUT_EXCLUDED" and self.feature_values_hash is not None:
            raise ValueError("Opportunity bị loại không được có hash feature đã đánh giá.")
        if (self.feature_schema_hash is None) != (self.vector_hash is None):
            raise ValueError("Schema/vector hash phải cùng có hoặc cùng vắng.")
        if (self.strategy_state == "CANDIDATE") != (self.candidate_id is not None):
            raise ValueError("Trạng thái candidate phải khớp candidate_id.")
        if self.strategy_state != "CANDIDATE" and self.decision_outcome != "NO_TRADE":
            raise ValueError("Outcome planner/risk/intent đòi candidate.")
        object.__setattr__(self, "content_hash", metadata_hash(self.canonical_payload()))
        object.__setattr__(
            self,
            "idempotency_key",
            metadata_hash(
                {
                    "query_version": self.query_version,
                    "schema_version": self.schema_version,
                    "workspace_id": str(self.workspace_id),
                    "manifest_hash": self.manifest_hash,
                    "scenario_id": self.scenario_id,
                    "event_id": self.event_id,
                }
            ),
        )

    @staticmethod
    def _require_hash(value: object, name: str) -> None:
        if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
            raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")

    def canonical_payload(self) -> dict[str, object]:
        """Chỉ serialize metadata đã ghim; không có source/features/label payload."""
        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in ("content_hash", "idempotency_key")
        }
        payload["workspace_id"] = str(self.workspace_id)
        for name in ("evaluation_start", "range_end", "event_at", "available_at", "decision_at"):
            payload[name] = (
                getattr(self, name).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
        return payload

    def canonical_json(self) -> str:
        """Định dạng canonical thống nhất với content_hash."""
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
