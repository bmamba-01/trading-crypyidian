"""Kiểm tra repository ledger incident và projection runtime fail-closed."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from tests.unit.test_learning_incident_entry_hold import WORKSPACE_ID, _hold

from trading_bot.adapters.database.models import LearningIncidentHoldEventRecord


def _repository(session):
    name = "trading_bot.adapters.database.repositories.learning_incident_entry_holds"
    assert importlib.util.find_spec(name) is not None, "Chưa có repository ledger incident"
    return importlib.import_module(name).LearningIncidentEntryHoldRepository(session)


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))

    async def scalar(self, statement, params=None):
        self.statements.append((str(statement), params))
        return self.results.pop(0)

    async def scalars(self, statement):
        self.statements.append((str(statement), None))
        return _Rows(self.results.pop(0))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flush_count += 1


class _ProjectionFailureSession(_Session):
    async def scalar(self, statement, params=None):
        raise DBAPIError(
            str(statement),
            params,
            RuntimeError("WP06_INCIDENT_READ_COMMITTED_REQUIRED"),
            False,
        )


def test_event_record_roundtrip_rejects_tampered_hash_or_payload() -> None:
    event = _hold()
    record = LearningIncidentHoldEventRecord.from_domain(event)
    assert record.to_domain() == event
    record.intent_hash = "9" * 64
    with pytest.raises(ValueError, match="canonical/hash"):
        record.to_domain()


@pytest.mark.asyncio
async def test_runtime_reads_only_scoped_boolean_function_and_rejects_invalid_result() -> None:
    session = _Session(True)
    assert await _repository(session).active_hold_exists(workspace_id=WORKSPACE_ID) is True
    assert (
        session.statements[-1][0]
        == "SELECT public.wp06_learning_incident_hold_active(CAST(:workspace_id AS uuid))"
    )
    assert session.statements[-1][1] == {"workspace_id": str(WORKSPACE_ID)}
    for value in (None, 1, "false"):
        with pytest.raises(RuntimeError, match="projection"):
            await _repository(_Session(value)).active_hold_exists(workspace_id=WORKSPACE_ID)


@pytest.mark.asyncio
async def test_runtime_projection_database_failure_maps_to_fail_closed_unavailable() -> None:
    module = importlib.import_module(
        "trading_bot.adapters.database.repositories.learning_incident_entry_holds"
    )
    with pytest.raises(module.LearningIncidentHoldUnavailable, match="projection"):
        await _repository(_ProjectionFailureSession()).active_hold_exists(workspace_id=WORKSPACE_ID)


@pytest.mark.asyncio
async def test_governance_lock_passes_workspace_and_incumbent_to_ordered_database_lock() -> None:
    session = _Session()
    registry_id = uuid4()
    await _repository(session).lock_hold_governance_scope(
        workspace_id=WORKSPACE_ID, registry_id=registry_id
    )
    assert "wp06_lock_learning_incident_scope" in session.statements[-1][0]
    assert session.statements[-1][1] == {
        "workspace_id": str(WORKSPACE_ID),
        "registry_id": str(registry_id),
    }


@pytest.mark.asyncio
async def test_runtime_entry_lock_is_existing_workspace_settings_l1() -> None:
    session = _Session()
    await _repository(session).lock_runtime_entry_scope(workspace_id=WORKSPACE_ID)
    assert session.statements[-1][1] == {"workspace_lock": f"workspace-settings:{WORKSPACE_ID}"}


@pytest.mark.asyncio
async def test_append_persists_canonical_event_without_committing() -> None:
    event = _hold()
    session = _Session(None)
    result = await _repository(session).append_event(workspace_id=WORKSPACE_ID, event=event)
    assert result.event == event
    assert result.is_duplicate is False
    assert session.flush_count == 1
    assert len(session.added) == 1
    assert session.added[0].to_domain() == event


@pytest.mark.asyncio
async def test_exact_retry_returns_original_before_revalidating_target() -> None:
    original = _hold()
    candidate = replace(original, event_id=uuid4(), request_id="retry")
    session = _Session(LearningIncidentHoldEventRecord.from_domain(original))
    result = await _repository(session).append_event(workspace_id=WORKSPACE_ID, event=candidate)
    assert result.event == original
    assert result.is_duplicate is True
    assert session.added == []
    assert session.flush_count == 0
    assert all(
        "eligible_target" not in sql and "resolve_scorer" not in sql
        for sql, _ in session.statements
    )


@pytest.mark.asyncio
async def test_conflicting_key_or_workspace_is_rejected_without_append() -> None:
    original = _hold()
    session = _Session(LearningIncidentHoldEventRecord.from_domain(original))
    with pytest.raises(RuntimeError, match="Idempotency"):
        await _repository(session).append_event(
            workspace_id=WORKSPACE_ID, event=replace(original, reason="Khác ý định")
        )
    assert not session.added
    with pytest.raises(PermissionError, match="workspace"):
        await _repository(_Session()).append_event(workspace_id=UUID(int=100), event=original)


@pytest.mark.asyncio
async def test_history_is_ordered_and_scoped_without_disclosing_other_workspace() -> None:
    event = _hold()
    session = _Session([LearningIncidentHoldEventRecord.from_domain(event)])
    history = await _repository(session).list_history(
        workspace_id=WORKSPACE_ID, proposal_id=event.proposal_id
    )
    assert history == (event,)
    assert "ORDER BY" in session.statements[-1][0]
    assert "workspace_id" in session.statements[-1][0]


@pytest.mark.asyncio
async def test_open_hold_projection_excludes_any_proposal_with_terminal_release() -> None:
    event = _hold()
    session = _Session([LearningIncidentHoldEventRecord.from_domain(event)])

    open_holds = await _repository(session).list_open_holds(workspace_id=WORKSPACE_ID)

    assert open_holds == (event,)
    statement, _params = session.statements[-1]
    assert "EXISTS" in statement
    assert "learning_incident_entry_hold_event_1.action = :action_2" in statement
    assert "workspace_id" in statement
    assert "LIMIT" in statement


@pytest.mark.asyncio
async def test_recent_proposal_projection_returns_only_scoped_ids() -> None:
    proposal_id = uuid4()
    session = _Session([proposal_id])
    result = await _repository(session).list_recent_proposal_ids(
        workspace_id=WORKSPACE_ID, limit=20
    )
    assert result == (proposal_id,)
    statement, _params = session.statements[-1]
    assert "learning_retrain_proposal" in statement
    assert "workspace_id" in statement
    assert "LIMIT" in statement


class _MappingResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _EligibilitySession(_Session):
    """Chỉ giả kết quả port SQL tại unit boundary, không tạo fixture DB/quyền nguồn."""

    def __init__(self, *, pointer_version=6):
        from tests.unit.test_learning_incident_entry_hold import _evidence, _target

        from trading_bot.adapters.database.repositories.learning_drift import (
            _assessment_record,
            _proposal_record,
        )
        from trading_bot.domain.learning.drift import DriftPolicy

        proposal, assessment = _evidence()
        super().__init__(
            _proposal_record(proposal),
            _assessment_record(
                assessment,
                policy=DriftPolicy("coverage-v2-30d", 1, 0.8, 0.9),
                idempotency_key="9" * 64,
            ),
            list(assessment.member_ids),
        )
        target = _target()
        self.expected_target = target
        self.eligible = {
            "incumbent_registry_id": target.target.registry_id,
            "incumbent_registry_revision": target.target.registry_revision,
            "incumbent_artifact_hash": "8" * 64,
            "active_pointer_version": target.active_pointer_version,
            "deployment_revision": target.deployment_revision,
        }
        self.metadata = {
            name: getattr(target.target, name) for name in target.target.__dataclass_fields__
        } | {
            "workspace_id": WORKSPACE_ID,
            "mode": target.mode.value,
            "deployment_event_id": target.deployment_event_id,
            "deployment_revision": target.deployment_revision,
            "active_pointer_version": pointer_version,
        }

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        if "eligible_target" in str(statement):
            return _MappingResult(self.eligible)
        if "resolve_scorer_runtime_target" in str(statement):
            return _MappingResult(self.metadata)
        return object()


@pytest.mark.asyncio
async def test_eligibility_maps_authoritative_sql_metadata_without_artifact_payloads() -> None:
    session = _EligibilitySession()
    result = await _repository(session).eligible_incident_target(
        workspace_id=WORKSPACE_ID, proposal_id=_hold().proposal_id
    )
    assert result.target == session.expected_target
    assert result.incumbent_artifact_hash == "8" * 64
    assert result.incumbent_artifact_hash != result.target.target.scorer_artifact_hash
    assert result.proposal.assessment_hash == result.assessment.content_hash
    gateway_sql = session.statements[-1][0]
    assert "wp06_resolve_scorer_runtime_target" in gateway_sql
    assert "base_artifact_canonical" not in gateway_sql
    assert "scorer_artifact_canonical" not in gateway_sql


@pytest.mark.asyncio
async def test_eligibility_rejects_target_change_between_authoritative_reads() -> None:
    with pytest.raises(RuntimeError, match="Tuple incumbent/deployment"):
        await _repository(_EligibilitySession(pointer_version=7)).eligible_incident_target(
            workspace_id=WORKSPACE_ID, proposal_id=_hold().proposal_id
        )
