import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from test_learning_scorer_evaluation import (
    _base_artifact,
    _policy,
    _rows,
    _scope,
    _scorer_artifact,
)

from trading_bot.adapters.database.models.learning_fold_sample_entities import (
    _canonical_hash,
    _sample_payload,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import _trade_payload
from trading_bot.adapters.database.repositories.learning_scorer_test_access import (
    _consumption_from_rows,
    _scorer_test_row,
    scorer_test_evaluation_report_from_canonical_json,
)
from trading_bot.application.learning_fold_samples import NormalizedLearningSample
from trading_bot.application.learning_scorer_evaluation import (
    build_scorer_test_evaluation_report,
    scorer_test_payload_commitment_hash,
    scorer_test_payload_membership_digest,
)
from trading_bot.application.learning_scorer_test_service import (
    ScorerTestAccessReceipt,
    ScorerTestEvaluationAccessConflict,
)
from trading_bot.domain.learning.evaluation import (
    EvaluationTrade,
    calculate_entry_features_hash,
)


def _report():
    base = _base_artifact()
    return build_scorer_test_evaluation_report(
        scope=_scope(base, complete=True),
        scorer_artifact=_scorer_artifact(base),
        base_artifact=base,
        policy=_policy(),
        rows=_rows(),
    )


def test_scorer_test_report_repository_parser_round_trips_typed_report() -> None:
    report = _report()

    restored = scorer_test_evaluation_report_from_canonical_json(
        report.canonical_json,
        expected_report_hash=report.report_hash,
    )

    assert restored == report
    assert restored.canonical_json == report.canonical_json


def test_scorer_test_report_repository_parser_rejects_noncanonical_bytes() -> None:
    report = _report()
    noncanonical = report.canonical_json.replace(",", ", ", 1)

    with pytest.raises(ValueError, match="canonical"):
        scorer_test_evaluation_report_from_canonical_json(
            noncanonical,
            expected_report_hash=hashlib.sha256(noncanonical.encode()).hexdigest(),
        )


def test_scorer_test_report_repository_parser_rejects_nested_test_payload() -> None:
    report = _report()
    data = json.loads(report.canonical_json)
    data["metadata"] = {"sample_id": "secret"}
    payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()

    with pytest.raises(ValueError, match="payload"):
        scorer_test_evaluation_report_from_canonical_json(
            payload,
            expected_report_hash=payload_hash,
        )


def test_scorer_test_report_repository_parser_rejects_wrong_hash() -> None:
    report = _report()

    with pytest.raises(ValueError, match="hash"):
        scorer_test_evaluation_report_from_canonical_json(
            report.canonical_json,
            expected_report_hash="0" * 64,
        )


def _consumption_row() -> tuple[dict[str, object], ScorerTestAccessReceipt]:
    workspace_id = uuid4()
    receipt_id = uuid4()
    proof_id = uuid4()
    outcome_id = uuid4()
    candidate_hash = "c" * 64
    vector_hash = "d" * 64
    decision_at = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    features = (("momentum", 0.5), ("volatility", 0.2))
    schema_hash = "a" * 64
    snapshot_id = "snapshot-1"
    features_hash = calculate_entry_features_hash(
        snapshot_id,
        decision_at,
        schema_hash,
        features,
    )
    trade = EvaluationTrade(
        trade_id="trade-1",
        opened_at=decision_at,
        closed_at=datetime(2026, 11, 9, 0, 1, tzinfo=UTC),
        gross_pnl=Decimal("1.2"),
        fees=Decimal("0.01"),
        funding=Decimal("0"),
        slippage=Decimal("0.01"),
        exposure=Decimal("10"),
        entry_decision_snapshot_id=snapshot_id,
        entry_decision_at=decision_at,
        entry_feature_schema_hash=schema_hash,
        entry_features=features,
        entry_features_hash=features_hash,
        entry_symbol="BTCUSDT",
        entry_opportunity_event_id="event-1",
        entry_candidate_id=candidate_hash,
        entry_vector_hash=vector_hash,
    )
    trade_payload = _trade_payload(trade)
    canonical_trade = json.dumps(
        trade_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    outcome_hash = hashlib.sha256(canonical_trade.encode()).hexdigest()
    sample = NormalizedLearningSample(
        sample_id="sample-1",
        outcome_id=str(outcome_id),
        outcome_hash=outcome_hash,
        feature_snapshot_hash=features_hash,
        feature_schema_hash=schema_hash,
        label_hash="b" * 64,
        label_value=1,
        features=features,
    )
    sample_hash = _canonical_hash(_sample_payload(sample))
    raw: dict[str, object] = {
        "workspace_id": workspace_id,
        "receipt_id": receipt_id,
        "request_fingerprint": "e" * 64,
        "sample_id": sample.sample_id,
        "sample_outcome_id": outcome_id,
        "sample_outcome_hash": outcome_hash,
        "sample_hash": sample_hash,
        "feature_snapshot_hash": sample.feature_snapshot_hash,
        "feature_schema_hash": sample.feature_schema_hash,
        "label_hash": sample.label_hash,
        "features": dict(sample.features),
        "label_value": sample.label_value,
        "outcome_id": outcome_id,
        "outcome_hash": outcome_hash,
        "outcome_payload": trade_payload,
        "outcome_payload_canonical": canonical_trade,
        "trade_id": trade.trade_id,
        "vector_hash": vector_hash,
        "candidate_id_hash": candidate_hash,
    }
    expected_row = _scorer_test_row(
        raw,
        workspace_id=workspace_id,
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
    )
    membership_digest = scorer_test_payload_membership_digest((expected_row,))
    payload_commitment = scorer_test_payload_commitment_hash((expected_row,))
    raw.update(
        {
            "consumption_id": uuid4(),
            "payload_count": 1,
            "membership_digest": membership_digest,
            "payload_commitment_hash": payload_commitment,
            "consumption_hash": "f" * 64,
            "current_rights_receipt_hash": "9" * 64,
            "consumed_at": datetime(2026, 11, 9, 0, 2, tzinfo=UTC),
        }
    )
    receipt = ScorerTestAccessReceipt(
        receipt_id=receipt_id,
        workspace_id=workspace_id,
        receipt_hash="3" * 64,
        request_fingerprint="e" * 64,
        scorer_artifact_hash="4" * 64,
        policy_hash="5" * 64,
        proof_id=proof_id,
        proof_hash="6" * 64,
        current_rights_receipt_hash="9" * 64,
        committed_at=datetime(2026, 11, 9, tzinfo=UTC),
    )
    return raw, receipt


def test_scorer_test_payload_mapper_revalidates_sample_and_outcome_hashes() -> None:
    raw, receipt = _consumption_row()

    batch = _consumption_from_rows(
        [raw],
        receipt=receipt,
        request_fingerprint=receipt.request_fingerprint,
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
        expected_membership_digest=raw["membership_digest"],
        expected_current_rights_hash=receipt.current_rights_receipt_hash,
    )

    assert batch.payload_count == 1
    assert batch.rows[0].role == "test"
    assert batch.rows[0].trade.entry_candidate_id == raw["candidate_id_hash"]
    assert batch.rows[0].trade.entry_vector_hash == raw["vector_hash"]


def _replace_sample_payload(
    raw: dict[str, object],
    *,
    feature_snapshot_hash: str | None = None,
    features: dict[str, float] | None = None,
    feature_schema_hash: str | None = None,
    label_value: int | None = None,
) -> None:
    sample = NormalizedLearningSample(
        sample_id=str(raw["sample_id"]),
        outcome_id=str(raw["sample_outcome_id"]),
        outcome_hash=str(raw["sample_outcome_hash"]),
        feature_snapshot_hash=feature_snapshot_hash or str(raw["feature_snapshot_hash"]),
        feature_schema_hash=feature_schema_hash or str(raw["feature_schema_hash"]),
        label_hash=str(raw["label_hash"]),
        label_value=raw["label_value"] if label_value is None else label_value,
        features=tuple(sorted((features or raw["features"]).items())),
    )
    raw["features"] = dict(sample.features)
    raw["feature_snapshot_hash"] = sample.feature_snapshot_hash
    raw["feature_schema_hash"] = sample.feature_schema_hash
    raw["label_value"] = sample.label_value
    raw["sample_hash"] = _canonical_hash(_sample_payload(sample))


@pytest.mark.parametrize(
    ("sample_change", "message"),
    [
        ({"feature_snapshot_hash": "7" * 64}, "(?i)snapshot"),
        ({"features": {"momentum": 0.6, "volatility": 0.2}}, "(?i)feature"),
        ({"feature_schema_hash": "8" * 64}, "(?i)schema"),
        ({"label_value": -1}, "(?i)label"),
    ],
)
def test_scorer_test_payload_mapper_rejects_sample_outcome_semantic_mismatch(
    sample_change: dict[str, object], message: str
) -> None:
    raw, _ = _consumption_row()
    _replace_sample_payload(raw, **sample_change)

    with pytest.raises(ValueError, match=message):
        _scorer_test_row(
            raw,
            workspace_id=raw["workspace_id"],
            manifest_hash="1" * 64,
            fold_plan_hash="2" * 64,
        )


def test_scorer_test_payload_mapper_rejects_tampered_candidate_commitment() -> None:
    raw, receipt = _consumption_row()
    raw["candidate_id_hash"] = "7" * 64

    with pytest.raises(ScorerTestEvaluationAccessConflict, match="TEST payload"):
        _consumption_from_rows(
            [raw],
            receipt=receipt,
            request_fingerprint=receipt.request_fingerprint,
            manifest_hash="1" * 64,
            fold_plan_hash="2" * 64,
            expected_membership_digest=raw["membership_digest"],
            expected_current_rights_hash=receipt.current_rights_receipt_hash,
        )
