from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import trading_bot.application.learning_paper_performance as application
from trading_bot.adapters.database.repositories.learning_paper_performance import (
    PaperPerformanceAggregateUnavailable,
)
from trading_bot.auth.policy import Principal, Role

AS_OF = datetime(2026, 11, 14, 12, tzinfo=UTC)
WORKSPACE_ID = uuid4()
BOT_ID = uuid4()


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=AS_OF,
    )


class _Session:
    def __init__(self) -> None:
        self.executed: list[tuple[object, object]] = []

    async def execute(self, statement: object, params: object = None) -> object:
        self.executed.append((statement, params))
        return object()

    async def scalar(self, _statement: object) -> datetime:
        return AS_OF


def _install_uow(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    class Uow:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.session = session

        async def __aenter__(self) -> Uow:
            return self

        async def __aexit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
            return None

    class Grants:
        def __init__(self, _session: object) -> None:
            pass

        async def require(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(application, "UnitOfWork", Uow)
    monkeypatch.setattr(application, "BotGrantRepository", Grants)


def _report(*, report_id: object, bot_id: object = BOT_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=report_id,
        bot_id=bot_id,
        as_of=AS_OF,
        status="inconclusive",
        policy_version="wp06-paper-performance-v1",
        policy_hash="d" * 64,
        target_fingerprint="e" * 64,
        baseline_report_hash="f" * 64,
        report_hash="1" * 64,
        report={"scope": "PAPER_ONLY_DIAGNOSTIC"},
    )


@pytest.mark.asyncio
async def test_idempotent_prior_report_is_denied_after_current_rights_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = _report(report_id=uuid4())
    calls: list[str] = []

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_idempotency_reference(self, **_kwargs: object) -> SimpleNamespace:
            calls.append("metadata")
            return SimpleNamespace(report_id=prior.id, bot_id=prior.bot_id)

        async def report_current_rights(self, **_kwargs: object) -> bool:
            calls.append("recheck")
            return False

        async def get_report_by_id(self, **_kwargs: object) -> SimpleNamespace:
            calls.append("payload")
            return prior

    _install_uow(monkeypatch, _Session())
    monkeypatch.setattr(application, "LearningPaperPerformanceRepository", Repository)
    service = application.PrincipalLearningPaperPerformanceService(object(), _principal())

    with pytest.raises(PermissionError, match="Quyền aggregate hiện hành"):
        await service.assess(bot_id=BOT_ID, idempotency_key=uuid4())
    assert calls == ["metadata", "recheck"]


@pytest.mark.asyncio
async def test_list_recent_omits_reports_without_current_aggregate_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible = _report(report_id=uuid4())
    revoked = _report(report_id=uuid4())
    calls: list[tuple[str, object]] = []

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def list_recent_references(self, **_kwargs: object) -> tuple[SimpleNamespace, ...]:
            return tuple(
                SimpleNamespace(report_id=row.id, bot_id=row.bot_id) for row in (visible, revoked)
            )

        async def report_current_rights(self, *, report_id: object, **_kwargs: object) -> bool:
            calls.append(("recheck", report_id))
            return report_id == visible.id

        async def get_report_by_id(
            self, *, report_id: object, **_kwargs: object
        ) -> SimpleNamespace | None:
            calls.append(("payload", report_id))
            return visible if report_id == visible.id else revoked

    _install_uow(monkeypatch, _Session())
    monkeypatch.setattr(application, "LearningPaperPerformanceRepository", Repository)
    service = application.PrincipalLearningPaperPerformanceService(object(), _principal())

    reports = await service.list_recent()

    assert tuple(item.report_id for item in reports) == (visible.id,)
    assert calls == [("recheck", visible.id), ("payload", visible.id), ("recheck", revoked.id)]


def _aggregate_row(name: str, *, membership_digest: str) -> dict[str, object]:
    newer_end = AS_OF - timedelta(days=30)
    newer_start = newer_end - timedelta(days=30)
    start, end = (
        (newer_start - timedelta(days=30), newer_start)
        if name == "older"
        else (newer_start, newer_end)
    )
    return {
        "cohort_name": name,
        "window_start": start,
        "window_end": end,
        "eligible_decision_count": 0,
        "covered_outcome_count": 0,
        "class_minus_one_count": 0,
        "class_zero_count": 0,
        "class_plus_one_count": 0,
        "observed_brier": None,
        "observed_log_loss": None,
        "baseline_brier": "0.2",
        "baseline_log_loss": "1.1",
        "membership_digest": membership_digest,
    }


@pytest.mark.asyncio
async def test_current_rights_digest_is_bound_with_both_cohort_digests_before_snapshot_and_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    _install_uow(monkeypatch, session)
    rights_digest = "a" * 64
    older_digest = "b" * 64
    newer_digest = "c" * 64
    target = SimpleNamespace(
        target=SimpleNamespace(
            scorer_artifact_id=uuid4(),
            evaluation_report_id=uuid4(),
            evaluation_report_hash="f" * 64,
        ),
        source_rights_fingerprint="source-rights-fingerprint",
    )
    snapshot_queries: list[dict[str, object]] = []
    inserted: list[object] = []

    class RuntimeTargets:
        def __init__(self, _session: object) -> None:
            pass

        async def resolve_active(self, **_kwargs: object) -> SimpleNamespace:
            return target

    class DeploymentRights:
        def __init__(self, _session: object) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(fingerprint=target.source_rights_fingerprint)

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_idempotency_reference(self, **_kwargs: object) -> None:
            return None

        async def aggregate_active_target(self, **_kwargs: object) -> tuple[dict[str, object], ...]:
            return (
                {
                    **_aggregate_row("older", membership_digest=older_digest),
                    "rights_digest": rights_digest,
                },
                {
                    **_aggregate_row("newer", membership_digest=newer_digest),
                    "rights_digest": rights_digest,
                },
            )

        async def get_snapshot_reference(self, **kwargs: object) -> None:
            snapshot_queries.append(kwargs)
            return None

        async def insert(self, report: object) -> object:
            inserted.append(report)
            return report

        async def report_current_rights(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(application, "PostgresScorerRuntimeTargetRepository", RuntimeTargets)
    monkeypatch.setattr(application, "LearningScorerCurrentRightsRepository", DeploymentRights)
    monkeypatch.setattr(application, "LearningPaperPerformanceRepository", Repository)
    monkeypatch.setattr(application, "_target_fingerprint", lambda *_args: "e" * 64)
    service = application.PrincipalLearningPaperPerformanceService(object(), _principal())

    await service.assess(bot_id=BOT_ID, idempotency_key=uuid4())

    canonical = json.dumps(
        {
            "cohort_membership_digests": [older_digest, newer_digest],
            "rights_digest": rights_digest,
            "schema_version": "crypidian.wp06.paper-performance-membership.v2",
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert snapshot_queries[0]["membership_digest"] == expected_digest
    assert inserted[0].membership_digest == expected_digest


@pytest.mark.asyncio
async def test_snapshot_recheck_denial_never_loads_report_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    _install_uow(monkeypatch, session)
    target = SimpleNamespace(
        target=SimpleNamespace(
            scorer_artifact_id=uuid4(),
            evaluation_report_id=uuid4(),
            evaluation_report_hash="f" * 64,
        ),
        source_rights_fingerprint="source-rights-fingerprint",
    )
    snapshot_reference = SimpleNamespace(report_id=uuid4(), bot_id=BOT_ID)
    calls: list[str] = []

    class RuntimeTargets:
        def __init__(self, _session: object) -> None:
            pass

        async def resolve_active(self, **_kwargs: object) -> SimpleNamespace:
            return target

    class DeploymentRights:
        def __init__(self, _session: object) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(fingerprint=target.source_rights_fingerprint)

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_idempotency_reference(self, **_kwargs: object) -> None:
            calls.append("idempotency-metadata")
            return None

        async def aggregate_active_target(self, **_kwargs: object) -> tuple[dict[str, object], ...]:
            calls.append("aggregate")
            return (
                {
                    **_aggregate_row("older", membership_digest="b" * 64),
                    "rights_digest": "a" * 64,
                },
                {
                    **_aggregate_row("newer", membership_digest="c" * 64),
                    "rights_digest": "a" * 64,
                },
            )

        async def get_snapshot_reference(self, **_kwargs: object) -> SimpleNamespace:
            calls.append("snapshot-metadata")
            return snapshot_reference

        async def report_current_rights(self, **_kwargs: object) -> bool:
            calls.append("recheck")
            return False

        async def get_report_by_id(self, **_kwargs: object) -> object:
            calls.append("payload")
            raise AssertionError("Không được đọc payload sau khi quyền bị từ chối.")

    monkeypatch.setattr(application, "PostgresScorerRuntimeTargetRepository", RuntimeTargets)
    monkeypatch.setattr(application, "LearningScorerCurrentRightsRepository", DeploymentRights)
    monkeypatch.setattr(application, "LearningPaperPerformanceRepository", Repository)
    monkeypatch.setattr(application, "_target_fingerprint", lambda *_args: "e" * 64)
    service = application.PrincipalLearningPaperPerformanceService(object(), _principal())

    with pytest.raises(PermissionError, match="Quyền aggregate hiện hành"):
        await service.assess(bot_id=BOT_ID, idempotency_key=uuid4())

    assert calls == ["idempotency-metadata", "aggregate", "snapshot-metadata", "recheck"]


@pytest.mark.asyncio
async def test_denied_aggregate_does_not_insert_report(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session()
    _install_uow(monkeypatch, session)
    calls: list[str] = []
    target = SimpleNamespace(
        target=SimpleNamespace(
            scorer_artifact_id=uuid4(),
            evaluation_report_id=uuid4(),
            evaluation_report_hash="f" * 64,
        ),
        source_rights_fingerprint="source-rights-fingerprint",
    )
    inserted: list[object] = []

    class RuntimeTargets:
        def __init__(self, _session: object) -> None:
            pass

        async def resolve_active(self, **_kwargs: object) -> SimpleNamespace:
            return target

    class DeploymentRights:
        def __init__(self, _session: object) -> None:
            pass

        async def verify_deployment_rights(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(fingerprint=target.source_rights_fingerprint)

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_idempotency_reference(self, **_kwargs: object) -> None:
            calls.append("idempotency-reference")
            return None

        async def aggregate_active_target(self, **_kwargs: object) -> None:
            calls.append("aggregate")
            raise PaperPerformanceAggregateUnavailable("current rights denied")

        async def get_snapshot_reference(self, **_kwargs: object) -> None:
            calls.append("snapshot-reference")
            return None

        async def report_current_rights(self, **_kwargs: object) -> bool:
            calls.append("report-rights")
            return True

        async def get_report_by_id(self, **_kwargs: object) -> object:
            calls.append("report-payload")
            return object()

        async def insert(self, report: object) -> object:
            calls.append("insert")
            inserted.append(report)
            return report

    monkeypatch.setattr(application, "PostgresScorerRuntimeTargetRepository", RuntimeTargets)
    monkeypatch.setattr(application, "LearningScorerCurrentRightsRepository", DeploymentRights)
    monkeypatch.setattr(application, "LearningPaperPerformanceRepository", Repository)
    monkeypatch.setattr(application, "_target_fingerprint", lambda *_args: "e" * 64)
    service = application.PrincipalLearningPaperPerformanceService(object(), _principal())

    with pytest.raises(PaperPerformanceAggregateUnavailable):
        await service.assess(bot_id=BOT_ID, idempotency_key=uuid4())

    assert calls == ["idempotency-reference", "aggregate"]
    assert inserted == []
