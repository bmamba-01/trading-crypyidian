"""Dựng đầu vào đánh giá scorer TEST từ bản ghi đã lưu (AI-5, CRY-236).

CLI ``learning_evaluation`` dùng module này để khôi phục proof TEST và scope từ đúng
canonical bytes đã lưu. Không có payload TEST ở đây: hàng TEST chỉ được gateway DB mở
một lần trong phase hai của ``ScorerTestEvaluationService``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from typing import Any
from uuid import UUID

from trading_bot.application.learning_scorer_evaluation import (
    ScorerTestEvaluationScope,
    ScorerTestOpportunityEntry,
    ScorerTestOpportunityProof,
)

# Trường suy ra (đếm/cờ) có trong canonical nhưng không phải tham số dựng proof.
_DERIVED_PROOF_FIELDS = (
    "schema_version",
    "candidate_count",
    "no_candidate_count",
    "input_excluded_count",
    "completeness_verified",
)
_SCOPE_FIELDS = (
    "workspace_id",
    "evaluation_run_id",
    "attempt_id",
    "normalized_dataset_id",
    "manifest_hash",
    "fold_plan_hash",
    "fold_id",
    "approval_hash",
    "vector_schema_version",
    "vector_schema_hash",
    "test_membership_digest",
    "bundle_hash",
    "source_scope_hash",
)
_ENTRY_FIELDS = frozenset(
    {
        "stable_id_hash",
        "status",
        "candidate_id_hash",
        "outcome_id_hash",
        "outcome_hash",
        "sample_id_hash",
        "sample_hash",
        "vector_hash",
    }
)

_PROOF_FIELDS = frozenset(
    field.name for field in fields(ScorerTestOpportunityProof) if field.name != "record_id"
)


def opportunity_proof_from_canonical(
    *, record_id: UUID, canonical_proof: str, expected_hash: str
) -> ScorerTestOpportunityProof:
    """Khôi phục proof TEST; từ chối nếu hash hoặc canonical bytes không khớp tuyệt đối."""
    if type(record_id) is not UUID or not isinstance(canonical_proof, str):
        raise ValueError("Proof TEST cần record UUID và canonical text.")
    if hashlib.sha256(canonical_proof.encode("utf-8")).hexdigest() != expected_hash:
        raise ValueError("Hash proof TEST không khớp canonical bytes.")
    payload: dict[str, Any] = json.loads(canonical_proof)
    if not isinstance(payload, dict):
        raise ValueError("Proof TEST phải là JSON object.")
    values = {key: value for key, value in payload.items() if key not in _DERIVED_PROOF_FIELDS}
    if set(values) != _PROOF_FIELDS:
        raise ValueError("Proof TEST sai schema.")
    try:
        for name in ("workspace_id", "evaluation_run_id", "attempt_id", "normalized_dataset_id"):
            values[name] = UUID(str(values[name]))
        entries = []
        for item in values.pop("entries"):
            if not isinstance(item, dict) or set(item) != _ENTRY_FIELDS:
                raise ValueError("Entry proof TEST sai schema.")
            entries.append(ScorerTestOpportunityEntry(**item))
        proof = ScorerTestOpportunityProof(record_id=record_id, entries=tuple(entries), **values)
    except (KeyError, TypeError) as error:
        raise ValueError("Proof TEST sai kiểu dữ liệu.") from error
    if proof.canonical_json != canonical_proof or proof.content_hash != expected_hash:
        raise ValueError("Dựng lại proof TEST không ra đúng canonical bytes.")
    return proof


def evaluation_scope_from_proof(proof: ScorerTestOpportunityProof) -> ScorerTestEvaluationScope:
    """Scope đánh giá lấy nguyên lineage từ proof đã lưu (không nhận từ argv)."""
    if type(proof) is not ScorerTestOpportunityProof:
        raise ValueError("Cần ScorerTestOpportunityProof typed.")
    return ScorerTestEvaluationScope(
        **{name: getattr(proof, name) for name in _SCOPE_FIELDS},
        opportunity_proof=proof,
    )


__all__ = ["evaluation_scope_from_proof", "opportunity_proof_from_canonical"]
