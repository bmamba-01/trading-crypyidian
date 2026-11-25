"""CLI đánh giá scorer TEST (AI-5, CRY-236): dựng đầu vào từ bản ghi, kiểm quyền và hash."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.test_learning_scorer_evaluation import _base_artifact, _scope, _scorer_artifact

from trading_bot.application.learning_evaluation_inputs import (
    evaluation_scope_from_proof,
    opportunity_proof_from_canonical,
)
from trading_bot.cli import learning_evaluation

ROOT = Path(__file__).resolve().parents[2]
_ROW = SimpleNamespace(canonical_artifact="{}", artifact_hash="0" * 64)


def _proof():
    return _scope(_base_artifact(), complete=True).opportunity_proof


def test_proof_roundtrip_rebuilds_identical_proof_and_scope() -> None:
    scope = _scope(_base_artifact(), complete=True)
    proof = scope.opportunity_proof
    rebuilt = opportunity_proof_from_canonical(
        record_id=proof.record_id,
        canonical_proof=proof.canonical_json,
        expected_hash=proof.content_hash,
    )
    assert rebuilt == proof
    assert evaluation_scope_from_proof(rebuilt) == scope


def test_v3_proof_roundtrip_preserves_not_run_reason_and_schema_version() -> None:
    from trading_bot.application.learning_scorer_evaluation import ScorerTestOpportunityEntry

    proof = _proof()
    not_run = ScorerTestOpportunityEntry(
        stable_id_hash=hashlib.sha256(b"event:not-run").hexdigest(),
        status="not_run",
        candidate_id_hash=None,
        vector_hash=hashlib.sha256(b"not-run-vector").hexdigest(),
        reason_code="STRATEGY_NOT_RUN",
    )
    proof = replace(
        proof,
        proof_schema_version="v3",
        expected_opportunity_count=proof.expected_opportunity_count + 1,
        entries=tuple(sorted((*proof.entries, not_run), key=lambda entry: entry.stable_id_hash)),
    )

    rebuilt = opportunity_proof_from_canonical(
        record_id=proof.record_id,
        canonical_proof=proof.canonical_json,
        expected_hash=proof.content_hash,
    )

    assert rebuilt == proof
    assert rebuilt.not_run_count == 1
    assert rebuilt.proof_schema_version == "v3"
    assert json.loads(rebuilt.canonical_json)["entries"][-1]["reason_code"] == "STRATEGY_NOT_RUN"


def test_proof_rejects_hash_mismatch_and_tampered_bytes() -> None:
    proof = _proof()
    with pytest.raises(ValueError, match="Hash proof"):
        opportunity_proof_from_canonical(
            record_id=proof.record_id, canonical_proof=proof.canonical_json, expected_hash="0" * 64
        )
    payload = json.loads(proof.canonical_json)
    payload["unexpected_field"] = 1  # trường lạ ngoài schema
    tampered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with pytest.raises(ValueError, match="schema"):
        opportunity_proof_from_canonical(
            record_id=proof.record_id,
            canonical_proof=tampered,
            expected_hash=hashlib.sha256(tampered.encode()).hexdigest(),
        )


def test_read_base_artifact_strips_one_newline_and_rejects_empty() -> None:
    assert learning_evaluation.read_base_artifact(io.StringIO('{"a":1}\n')) == '{"a":1}'
    with pytest.raises(ValueError, match="Stdin"):
        learning_evaluation.read_base_artifact(io.StringIO(""))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"process_role": "trainer-worker"}, "process_role"),
        ({"trading_mode": "LIVE"}, "PAPER"),
        ({"database_user": "crypidian_admin"}, "login"),
        ({"database_password_file": Path("/tmp/x")}, "secret"),
    ],
)
def test_evaluator_pool_requires_fixed_role_and_secret(override, message) -> None:
    settings = SimpleNamespace(
        process_role="learning-evaluator",
        trading_mode="PAPER",
        database_user=learning_evaluation.EVALUATOR_USER,
        database_password_file=learning_evaluation.EVALUATOR_PASSWORD_FILE,
        **{"database_host": "postgres", "database_name": "crypidian"},
    )
    for key, value in override.items():
        setattr(settings, key, value)
    with pytest.raises(PermissionError, match=message):
        learning_evaluation.create_evaluator_pool(settings)  # type: ignore[arg-type]


class _FakeUnit:
    def __init__(self, scorer_row) -> None:
        self.session = SimpleNamespace(scalar=self._scalar)
        self._row = scorer_row

    async def _scalar(self, _query):
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _patch_storage(monkeypatch, *, scorer, proof, scorer_row=_ROW) -> None:
    monkeypatch.setattr(learning_evaluation, "UnitOfWork", lambda *_a, **_k: _FakeUnit(scorer_row))
    monkeypatch.setattr(
        learning_evaluation.LearningScorerArtifactRepository,
        "validate_record",
        staticmethod(lambda _row: None),
    )
    monkeypatch.setattr(
        learning_evaluation,
        "learning_scorer_artifact_from_canonical_json",
        lambda *_a, **_k: scorer,
    )
    proof_row = (
        None
        if proof is None
        else SimpleNamespace(
            id=proof.record_id, canonical_proof=proof.canonical_json, proof_hash=proof.content_hash
        )
    )

    class _ProofRepo:
        def __init__(self, _session) -> None:
            pass

        async def get_for_attempt(self, **_kwargs):
            return proof_row

    monkeypatch.setattr(
        learning_evaluation, "LearningScorerTestOpportunityProofRepository", _ProofRepo
    )


def _settings():
    from tests.unit.test_learning_scorer_evaluation import _policy

    return SimpleNamespace(scorer_test_evaluation_policy=_policy)


async def _build(base_canonical: str):
    return await learning_evaluation.build_request(
        None,  # type: ignore[arg-type]
        workspace_id=uuid4(),
        attempt_id=uuid4(),
        base_canonical=base_canonical,
        settings=_settings(),  # type: ignore[arg-type]
        evaluated_at=datetime(2026, 11, 17, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_build_request_pins_base_artifact_hash(monkeypatch) -> None:
    base = _base_artifact()
    scorer = _scorer_artifact(base)
    proof = _proof()
    _patch_storage(monkeypatch, scorer=scorer, proof=proof)
    request = await _build(base.canonical_json)
    assert request.base_artifact == base
    assert request.scorer_artifact is scorer
    assert request.scope.opportunity_proof == proof

    with pytest.raises(ValueError, match="hash|JSON|canonical"):
        await _build(base.canonical_json.replace('"seed":', '"seed": '))


@pytest.mark.asyncio
async def test_build_request_rejects_abstain_scorer_and_missing_proof(monkeypatch) -> None:
    base = _base_artifact()
    abstain = replace(
        _scorer_artifact(base),
        state="abstain",
        reason_code="NOT_CALIBRATED",
        temperature=None,
        calibration_policy_version=None,
        calibration_policy_hash=None,
        validation_nll_before=None,
        validation_nll_after=None,
        calibration_iterations=None,
    )
    _patch_storage(monkeypatch, scorer=abstain, proof=_proof())
    with pytest.raises(ValueError, match="abstain"):
        await _build(base.canonical_json)

    _patch_storage(monkeypatch, scorer=_scorer_artifact(base), proof=None)
    with pytest.raises(LookupError, match="proof TEST"):
        await _build(base.canonical_json)

    _patch_storage(monkeypatch, scorer=_scorer_artifact(base), proof=_proof(), scorer_row=None)
    with pytest.raises(LookupError, match="scorer artifact"):
        await _build(base.canonical_json)


def test_main_hides_internal_error_detail(monkeypatch, capsys) -> None:
    async def fail(*_args, **_kwargs) -> int:
        raise RuntimeError("sensitive database detail")

    monkeypatch.setattr(learning_evaluation, "AppSettings", lambda: SimpleNamespace())
    monkeypatch.setattr(learning_evaluation, "_run", fail)
    code = learning_evaluation.main(
        [
            "run",
            "--workspace-id",
            str(uuid4()),
            "--attempt-id",
            str(uuid4()),
            "--base-artifact-stdin",
        ],
        stdin=io.StringIO("{}"),
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "RuntimeError" in captured.err
    assert "sensitive database detail" not in captured.err


def test_compose_learning_evaluator_points_to_existing_module() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    service = text[text.index("\n  learning-evaluator:\n") :]
    command = re.search(r'command: \["python", "-m", "([\w.]+)"', service)
    assert command is not None
    assert command.group(1) == "trading_bot.cli.learning_evaluation"
    assert importlib.util.find_spec(command.group(1)) is not None
