from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.learning.scorer_report_summary import ScorerReportSummary


def _application_module():
    module_name = "trading_bot.application.learning_scorer_model_card"
    assert importlib.util.find_spec(module_name) is not None, "chưa có application model card"
    return importlib.import_module(module_name)


def _report() -> ScorerReportSummary:
    return ScorerReportSummary(
        evaluation_report_id=uuid4(),
        created_at=datetime(2026, 11, 21, 10, tzinfo=UTC),
        evaluated_at=datetime(2026, 11, 21, 9, tzinfo=UTC),
        status="INCONCLUSIVE",
        report_hash="a" * 64,
        proof_hash="b" * 64,
        scorer_artifact_hash="c" * 64,
        base_artifact_hash="d" * 64,
        policy_hash="e" * 64,
        policy_version="wp06-scorer-policy-v1",
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
        fold_id="fold-01",
        vector_schema_version="scorer-vector-v2",
        vector_schema_hash="3" * 64,
        candidate_completeness_verified=False,
        scored_candidate_coverage_verified=False,
        test_sample_count=9,
        opportunity_count=12,
        class_support=((-1, 3), (0, 3), (1, 3)),
        confusion_matrix=None,
        coverage=0.0,
        abstain_count=9,
        log_loss=0.0,
        brier_score=0.0,
        expected_calibration_error=0.0,
        false_veto_count=0,
        false_veto_rate=0.0,
        candidate_retention=None,
        candidate_count=0,
        scored_candidate_count=0,
        approved_candidate_count=0,
        no_candidate_count=0,
        input_excluded_count=0,
    )


@pytest.mark.asyncio
async def test_model_card_facade_passes_exact_report_hashes_in_workspace_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _application_module()

    workspace_id = uuid4()
    bot_id = uuid4()
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace_id,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    report = _report()
    expected_result = object()
    trace: list[str] = []
    session_factory = object()

    class Unit:
        def __init__(self, sessions, *, workspace_id, bot_id, actor_membership_id):
            assert sessions is session_factory
            assert workspace_id is workspace_id_expected
            assert bot_id is bot_id_expected
            assert actor_membership_id is membership_id_expected
            self.session = object()

        async def __aenter__(self):
            trace.append("enter")
            return self

        async def __aexit__(self, *_args):
            trace.append("exit")

    class Repository:
        def __init__(self, session):
            assert session is not None

        async def get_action_latency(self, **kwargs):
            assert kwargs == {
                "workspace_id": workspace_id_expected,
                "bot_id": bot_id_expected,
                "report_id": report.evaluation_report_id,
                "artifact_hash": report.scorer_artifact_hash,
                "report_hash": report.report_hash,
                "base_artifact_hash": report.base_artifact_hash,
                "policy_hash": report.policy_hash,
                "window_code": "7d",
            }
            trace.append("aggregate")
            return expected_result

    class Grants:
        def __init__(self, session):
            assert session is not None

        async def require(self, checked_principal, *, bot_id, permission):
            assert checked_principal is principal_expected
            assert bot_id is bot_id_expected
            assert permission is BotPermission.REPORT_READ
            trace.append("grant")

    workspace_id_expected = workspace_id
    bot_id_expected = bot_id
    principal_expected = principal
    membership_id_expected = principal.membership_id
    monkeypatch.setattr(module, "UnitOfWork", Unit)
    monkeypatch.setattr(module, "LearningScorerModelCardRepository", Repository)
    monkeypatch.setattr(module, "BotGrantRepository", Grants)

    result = await module.ScorerModelCardReadModel(session_factory).load_action_latency(
        principal=principal,
        bot_id=bot_id,
        report=report,
        window_code="7d",
    )

    assert result is expected_result
    assert trace == ["enter", "grant", "aggregate", "exit"]


@pytest.mark.asyncio
async def test_model_card_facade_rejects_untyped_report_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _application_module()

    def fail_if_opened(*_args, **_kwargs):
        pytest.fail("không được mở database khi report không typed")

    monkeypatch.setattr(module, "UnitOfWork", fail_if_opened)

    with pytest.raises(ValueError, match="report scorer"):
        await module.ScorerModelCardReadModel(object()).load_action_latency(
            principal=Principal(
                user_id=uuid4(),
                membership_id=uuid4(),
                workspace_id=uuid4(),
                roles=frozenset({Role.ANALYST_REVIEWER}),
                mfa_verified_at=datetime.now(UTC),
            ),
            bot_id=uuid4(),
            report=object(),  # type: ignore[arg-type]
            window_code="7d",
        )


@pytest.mark.asyncio
async def test_model_card_facade_denies_missing_report_read_grant_before_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _application_module()
    workspace_id = uuid4()
    principal = Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace_id,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    trace: list[str] = []

    class Unit:
        session = object()

        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Grants:
        def __init__(self, _session):
            pass

        async def require(self, *_args, **_kwargs):
            trace.append("grant")
            raise PermissionError("missing grant")

    def fail_if_called(*_args, **_kwargs):
        trace.append("aggregate")
        pytest.fail("aggregate must not run when REPORT_READ is missing")

    monkeypatch.setattr(module, "UnitOfWork", Unit)
    monkeypatch.setattr(module, "BotGrantRepository", Grants)
    monkeypatch.setattr(module, "LearningScorerModelCardRepository", fail_if_called)

    with pytest.raises(PermissionError, match="missing grant"):
        await module.ScorerModelCardReadModel(object()).load_action_latency(
            principal=principal,
            bot_id=uuid4(),
            report=_report(),
            window_code="7d",
        )

    assert trace == ["grant"]
