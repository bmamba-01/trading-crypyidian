"""Probe Compose xác thực lưu normalized samples chỉ bằng trusted builder."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    DatasetManifestRecord,
    LearningAttemptEventRecord,
    LearningFoldResourceProfileRecord,
    LearningModelRegistryEventRecord,
    LearningRunSourceRightsReceiptRecord,
    LearningSourceTrainingReceiptRecord,
    LearningSplitApprovalRecord,
    LearningTrainingAccessReceiptRecord,
    LearningTrainingArtifactRecord,
    LearningTrainingAttemptRecord,
    LearningTrainingJobRecord,
    NormalizedFoldAssignmentRecord,
    NormalizedLearningDatasetRecord,
    NormalizedLearningSampleRecord,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.dataset_manifest import DatasetManifestRepository
from trading_bot.adapters.database.repositories.learning_attempts import (
    LearningAttemptConflict,
    LearningAttemptRepository,
)
from trading_bot.adapters.database.repositories.learning_evaluation_access import (
    LearningEvaluationAccessRepository,
)
from trading_bot.adapters.database.repositories.learning_evaluation_reports import (
    LearningEvaluationReportRepository,
)
from trading_bot.adapters.database.repositories.learning_fold_samples import (
    LearningFoldSampleRepository,
)
from trading_bot.adapters.database.repositories.learning_model_registry import (
    LearningModelRegistryConflict,
    LearningModelRegistryRepository,
)
from trading_bot.adapters.database.repositories.learning_split_approvals import (
    LearningSplitApprovalRepository,
)
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactRepository,
)
from trading_bot.adapters.database.repositories.learning_training_batches import (
    LearningTrainingBatchRepository,
)
from trading_bot.adapters.database.repositories.learning_training_jobs import (
    LearningTrainingJobRepository,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.adapters.database.source_observer import PostCommitSourceObserver
from trading_bot.application.backtest_sensitivity import SensitivityScenario
from trading_bot.application.learning_cpu_trainer import CPU_TRAINER_VERSION
from trading_bot.application.learning_fold_evaluation import (
    FOLD_ROLES,
    FoldEvaluation,
    FoldRoleMetrics,
    WalkForwardEvaluationReport,
)
from trading_bot.application.learning_fold_plan import build_manifest_fold_plan
from trading_bot.application.learning_fold_samples import (
    NormalizedFoldAssignment,
    NormalizedLearningDataset,
    NormalizedLearningSample,
    normalized_learning_dataset_hash,
)
from trading_bot.application.learning_split_approval import (
    LearningSplitApprovalService,
    SplitApprovalRequest,
)
from trading_bot.application.learning_training_admin import LearningTrainingAdminService
from trading_bot.application.learning_training_batch import AuthorizedTrainerBatch
from trading_bot.application.learning_training_process import (
    LearningTrainingProcessFailure,
    LearningTrainingProcessFailureCode,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.learning.accounting import (
    LearningAttemptReasonCode,
    LearningAttemptState,
    LearningTrainingAttempt,
)
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSourceReference,
    DatasetSplit,
)
from trading_bot.domain.learning.evaluation import (
    TemporalWindow,
    WalkForwardSplit,
    build_evaluation_metrics,
)
from trading_bot.domain.learning.resources import (
    LearningResourceRejected,
    LearningResourceRequest,
    assess_learning_resource_request,
)
from trading_bot.domain.research.source_items import SourceItemRevision
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import (
    SourceBudget,
    SourceRegistryEntry,
    source_rights_hash,
)
from trading_bot.workers.learning_training_worker import LearningTrainingAttemptWorker
from trading_bot.workers.research_rights_worker import ResearchRightsMaintenanceWorker

PHASE = os.environ.get("WP06_NORMALIZED_PHASE", "admin")
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _engine():
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return create_async_engine(url)


def _at(hour: int) -> datetime:
    return BASE + timedelta(hours=hour)


def _manifest(workspace_id: UUID) -> DatasetManifest:
    samples = tuple(
        DatasetSample(
            sample_id=name,
            decision_at=_at(hour),
            feature_available_at=_at(hour) - timedelta(minutes=1),
            outcome_start=_at(hour),
            outcome_end=_at(hour) + timedelta(minutes=15),
            feature_snapshot_hash=feature_hash * 64,
            outcome_id=f"outcome-{name}",
            outcome_hash=outcome_hash * 64,
            label_hash=label_hash * 64,
            label_value=label,
            label_version="labels-v1",
            split=split,
            source_dependent=False,
            source_rights_hashes=(),
            source_revision_refs=(),
        )
        for name, hour, split, label, feature_hash, outcome_hash, label_hash in (
            ("dev-train", 1, DatasetSplit.DEVELOPMENT, 1, "1", "a", "b"),
            ("dev-validation", 4, DatasetSplit.DEVELOPMENT, -1, "2", "c", "d"),
            ("dev-test", 6, DatasetSplit.DEVELOPMENT, 0, "3", "e", "f"),
            ("final-holdout", 10, DatasetSplit.HOLDOUT, 1, "4", "1", "2"),
        )
    )
    return DatasetManifest(
        workspace_id=workspace_id,
        query_hash="9" * 64,
        symbols=("BTCUSDT",),
        data_start=_at(0),
        data_end=_at(12),
        as_of=_at(12),
        feature_schema_version="feature-schema-v1",
        feature_version="features-v1",
        label_version="labels-v1",
        sample_schema_version="samples-v1",
        split_policy_version="wp06-walk-forward-v1",
        samples=samples,
        exclusions=(),
        holdout_sealed_at=_at(12),
        manifest_version="dataset-manifest-v2",
    )


def _folds() -> tuple[WalkForwardSplit, ...]:
    return (
        WalkForwardSplit(
            train=TemporalWindow(_at(0), _at(3)),
            validation=TemporalWindow(_at(3), _at(5)),
            test=TemporalWindow(_at(5), _at(8)),
        ),
    )


async def _append_fixture_trial(session, workspace_id: UUID) -> UUID:
    """Tạo trial synthetic để neo approval downstream fixture."""
    sys.path.insert(0, "/e2e-tests")
    from fixtures.runtime_replay import build_h03_runtime_sensitivity

    replay = build_h03_runtime_sensitivity(
        workspace_id,
        (SensitivityScenario(scenario_id="normalized-anchor", kind="BASE"),),
        start_at=_at(20),
    )
    if not replay.is_complete or len(replay.runs) != 1:
        raise AssertionError("Fixture anchor cần đúng một replay BASE hoàn tất.")
    run = replay.runs[0]
    receipt = await BacktestOutcomeRepository(session).append_sensitivity_run(
        workspace_id=workspace_id,
        sensitivity_run=run.sensitivity_run,
        trade_outcomes=run.trade_outcomes,
    )
    return receipt.trial_record_id


async def _admin_seed() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id, user_id, membership_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(workspace_id)
    async with sessions() as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                name="WP06 normalized fixture",
                slug=f"wp06-normalized-{workspace_id.hex[:12]}",
            )
        )
        session.add(
            UserAccount(
                id=user_id,
                email_normalized=f"wp06-normalized-{user_id.hex}@example.invalid",
                password_hash="fixture-only-not-a-credential",
            )
        )
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        session.add(UserMembership(id=membership_id, workspace_id=workspace_id, user_id=user_id))
        trial_record_id = await _append_fixture_trial(session, workspace_id)
        sys.path.insert(0, "/e2e-tests")
        from e2e.probes.wp06_split_approval_anchor import append_split_approval_anchor

        await append_split_approval_anchor(
            session,
            manifest=manifest,
            folds=_folds(),
            evaluation_cutoff=_at(8),
            trial_record_id=trial_record_id,
        )
        identity = Principal(
            user_id=user_id,
            membership_id=membership_id,
            workspace_id=workspace_id,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=now,
        )
        request = SplitApprovalRequest(
            manifest=manifest,
            folds=_folds(),
            evaluation_cutoff=_at(8),
            reason="Duyệt split trước khi builder chuẩn hóa sample.",
            idempotency_key=uuid4(),
        )
        await (
            LearningSplitApprovalService(LearningSplitApprovalRepository(session))
            .for_principal(identity)
            .approve(request)
        )
        await DatasetManifestRepository(session).append_for_training(
            workspace_id=workspace_id,
            manifest=manifest,
            folds=_folds(),
            evaluation_cutoff=_at(8),
        )
    await engine.dispose()
    return {"workspace_id": str(workspace_id), "manifest_hash": manifest.content_hash}


async def _source_admin_seed() -> dict[str, object]:
    """Tạo workspace và policy TRAINING đã qua observer với metadata giả lập."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id, user_id, membership_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    async with sessions() as session, session.begin():
        session.add(
            Workspace(
                id=workspace_id,
                name="WP06 source rights fixture",
                slug=f"wp06-source-{workspace_id.hex[:12]}",
            )
        )
        session.add(
            UserAccount(
                id=user_id,
                email_normalized=f"wp06-source-{user_id.hex}@example.invalid",
                password_hash="fixture-only-not-a-credential",
            )
        )
        await session.flush()
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        session.add(UserMembership(id=membership_id, workspace_id=workspace_id, user_id=user_id))

    source_id = f"wp06-source-{workspace_id.hex[:12]}"
    entry = SourceRegistryEntry.discover(
        source_id=source_id,
        publisher="Nhà xuất bản giả lập",
        endpoint_url="https://example.invalid/wp06/source",
        access_method="GET",
        auth_scope="fixture-only",
        actor_id="fixture-policy-author",
        at=now,
        reason="Nguồn giả lập cho kiểm tra quyền WP06.",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=10,
            max_bytes=4096,
            max_cost=Decimal(0),
            cost_unit="USD",
        ),
    )
    entry = entry.transition(
        policy_id=f"{source_id}:policy:pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="fixture-policy-author",
        at=now,
        reason="Chờ reviewer xác minh quyền.",
    )
    entry = entry.transition(
        policy_id=f"{source_id}:policy:verified",
        state=SourcePolicyState.VERIFIED,
        actor_id="fixture-policy-author",
        approved_by="fixture-policy-reviewer",
        at=now,
        reviewed_at=now,
        evidence_ref="https://example.invalid/wp06/training-terms",
        allowed_purposes=frozenset({SourceUsePurpose.TRAINING}),
        reason="Reviewer fixture cấp riêng mục đích training.",
    )
    observer = PostCommitSourceObserver(sessions)
    current = None
    for revision in entry.revisions:
        result, progress = await observer.append_policy(
            workspace_id=workspace_id,
            revision=revision,
        )
        assert progress.complete
        if revision.state is SourcePolicyState.VERIFIED:
            current = result.revision
    assert current is not None
    await engine.dispose()
    return {
        "workspace_id": str(workspace_id),
        "membership_id": str(membership_id),
        "source_id": source_id,
        "item_id": "fixture-item",
        "revision_id": "fixture-revision-1",
        "policy_id": current.policy.policy_id,
        "policy_sequence": current.sequence,
        "rights_hash": source_rights_hash(current),
    }


async def _source_item_seed() -> dict[str, object]:
    """Ghi một item metadata bằng ingestion role và xử lý event rights trước manifest."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    source_id = os.environ["WP06_SOURCE_ID"]
    item_id = os.environ["WP06_SOURCE_ITEM_ID"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # Nội dung giả lập đã khả dụng trước timestamp của sample TRAIN.
    now = BASE + timedelta(minutes=55)
    retention_seconds = os.environ.get("WP06_SOURCE_RETENTION_SECONDS")
    retention_expires_at = (
        datetime.now(UTC) + timedelta(seconds=float(retention_seconds))
        if retention_seconds is not None
        else None
    )
    revision = SourceItemRevision(
        source_id=source_id,
        item_id=item_id,
        revision_id=os.environ["WP06_SOURCE_REVISION_ID"],
        canonical_url="https://example.invalid/wp06/item/fixture",
        observed_at=now,
        available_at=now,
        published_at=now,
        edited_at=now,
    )
    result, progress = await PostCommitSourceObserver(sessions).append_item(
        workspace_id=workspace_id,
        revision=revision,
        predecessor_revision_id=None,
        retention_expires_at=retention_expires_at,
    )
    assert progress.complete
    rights = await ResearchRightsMaintenanceWorker(
        session_factory=sessions,
        workspace_id=workspace_id,
        worker_id="wp06-source-fixture",
        tick_seconds=1,
    ).run_once()
    await engine.dispose()
    return {
        "item_revision_id": result.entry.revision.revision_id,
        "retention_expires_at": (
            retention_expires_at.isoformat() if retention_expires_at is not None else None
        ),
        "rights_events_processed": rights.rights_batch.processed,
        "rights_backlog_empty": (
            rights.rights_batch.processed == rights.rights_batch.claimed
            and rights.rights_batch.failed == 0
            and rights.rights_batch.dead_lettered == 0
        ),
    }


async def _source_manifest_seed() -> dict[str, object]:
    """Seal manifest v3 với một lineage TRAINING typed đã được chuẩn bị."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    reference = DatasetSourceReference(
        source_id=os.environ["WP06_SOURCE_ID"],
        item_id=os.environ["WP06_SOURCE_ITEM_ID"],
        revision_id=os.environ["WP06_SOURCE_REVISION_ID"],
        policy_id=os.environ["WP06_SOURCE_POLICY_ID"],
        policy_sequence=int(os.environ["WP06_SOURCE_POLICY_SEQUENCE"]),
        purpose=SourceUsePurpose.TRAINING,
        rights_hash=os.environ["WP06_SOURCE_RIGHTS_HASH"],
    )
    original = _manifest(workspace_id)
    split_by_sample = {
        "dev-train": DatasetSplit.TRAIN,
        "dev-validation": DatasetSplit.VALIDATION,
        "dev-test": DatasetSplit.TEST,
    }
    samples = tuple(
        replace(
            sample,
            split=split_by_sample.get(sample.sample_id, sample.split),
            source_dependent=sample.sample_id == "dev-train",
            source_rights_hashes=(),
            source_revision_refs=(),
            source_references=(reference,) if sample.sample_id == "dev-train" else (),
        )
        for sample in original.samples
    )
    manifest = replace(
        original,
        samples=samples,
        manifest_version="dataset-manifest-v3",
    )
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        membership = await session.scalar(
            select(UserMembership).where(UserMembership.workspace_id == workspace_id)
        )
        assert membership is not None
        trial_record_id = await _append_fixture_trial(session, workspace_id)
        sys.path.insert(0, "/e2e-tests")
        from e2e.probes.wp06_split_approval_anchor import append_split_approval_anchor

        await append_split_approval_anchor(
            session,
            manifest=manifest,
            folds=_folds(),
            evaluation_cutoff=_at(8),
            trial_record_id=trial_record_id,
        )
        principal = Principal(
            user_id=membership.user_id,
            membership_id=membership.id,
            workspace_id=workspace_id,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=datetime.now(UTC),
        )
        request = SplitApprovalRequest(
            manifest=manifest,
            folds=_folds(),
            evaluation_cutoff=_at(8),
            reason="Duyệt split cho fixture quyền nguồn TRAINING.",
            idempotency_key=uuid4(),
        )
        await (
            LearningSplitApprovalService(LearningSplitApprovalRepository(session))
            .for_principal(principal)
            .approve(request)
        )
        await DatasetManifestRepository(session).append_for_training(
            workspace_id=workspace_id,
            manifest=manifest,
            folds=_folds(),
            evaluation_cutoff=_at(8),
        )
    await engine.dispose()
    return {"workspace_id": str(workspace_id), "manifest_hash": manifest.content_hash}


async def _source_rights_revoke() -> dict[str, object]:
    """Thu hồi policy fixture qua cùng observer/advisory lock với builder."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    source_id = os.environ["WP06_SOURCE_ID"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        entry = await SourceRegistryRepository(session).get_entry(
            workspace_id=workspace_id,
            source_id=source_id,
        )
        assert entry is not None
        revoked = entry.transition(
            policy_id=f"{source_id}:policy:revoked",
            state=SourcePolicyState.REVOKED,
            actor_id="fixture-policy-author",
            at=datetime.now(UTC),
            reason="Thu hồi quyền fixture để kiểm tra retry stale.",
        ).current
    result, progress = await PostCommitSourceObserver(sessions).append_policy(
        workspace_id=workspace_id,
        revision=revoked,
    )
    await engine.dispose()
    return {
        "revoked": result.revision.state is SourcePolicyState.REVOKED,
        "observed": progress.complete,
    }


async def _source_evaluation_open() -> dict[str, object]:
    """Mở receipt evaluator; test có thể giữ transaction để kiểm serialize quyền."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_SOURCE_MANIFEST_HASH"]
    evaluation_run_id = UUID(os.environ["WP06_EVALUATION_RUN_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        access = await LearningEvaluationAccessRepository(unit.session).open(
            workspace_id=workspace_id,
            manifest_hash=manifest_hash,
            evaluation_run_id=evaluation_run_id,
            request_fingerprint=hashlib.sha256(evaluation_run_id.bytes).hexdigest(),
        )
        assert access.manifest.content_hash == manifest_hash
        assert any(sample.source_dependent for sample in access.manifest.samples)
        hold_seconds = float(os.environ.get("WP06_CONSUMER_HOLD_SOURCE_LOCK_SECONDS", "0"))
        if hold_seconds > 0:
            print("WP06_CONSUMER_SOURCE_LOCK_HELD", flush=True)
            await asyncio.sleep(min(hold_seconds, 10.0))
    await engine.dispose()
    return {"evaluation_access_opened": True, "receipt_committed": True}


async def _source_training_claim() -> dict[str, object]:
    """Gọi trainer gateway trực tiếp để kiểm deny rollback trước disclosure."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        batch = await LearningTrainingBatchRepository(unit.session).claim_batch(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        assert batch.workspace_id == workspace_id
        assert all(sample.role in {"train", "validation"} for sample in batch.samples)
    await engine.dispose()
    return {"training_access_opened": True, "batch_committed": True}


async def _source_run_rights_verify() -> dict[str, object]:
    """Đọc đúng metadata receipt theo từng lần chạy bằng quyền admin workspace."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    access_kind = os.environ["WP06_ACCESS_KIND"]
    access_id = UUID(os.environ["WP06_ACCESS_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        rows = (
            await unit.session.scalars(
                select(LearningRunSourceRightsReceiptRecord).where(
                    LearningRunSourceRightsReceiptRecord.workspace_id == workspace_id,
                    LearningRunSourceRightsReceiptRecord.access_kind == access_kind,
                    LearningRunSourceRightsReceiptRecord.access_id == access_id,
                )
            )
        ).all()
        assert len(rows) == int(os.environ.get("WP06_EXPECTED_SOURCE_RECEIPTS", "1"))
        assert all(row.purpose == "training" for row in rows)
        assert all(row.checked_at.tzinfo is not None for row in rows)
        assert all(row.rights_hash and len(row.rights_hash) == 64 for row in rows)
        workspace_isolated = await unit.session.scalar(
            select(func.count())
            .select_from(LearningRunSourceRightsReceiptRecord)
            .where(
                LearningRunSourceRightsReceiptRecord.workspace_id == uuid4(),
                LearningRunSourceRightsReceiptRecord.access_id == access_id,
            )
        )
        assert workspace_isolated == 0
        privileges = (
            await unit.session.execute(
                text(
                    "SELECT has_table_privilege('crypidian_admin_web', :table, 'SELECT'), "
                    "has_table_privilege('crypidian_trainer_worker', :table, 'SELECT'), "
                    "has_table_privilege('crypidian_learning_evaluator', :table, 'SELECT'), "
                    "has_table_privilege('crypidian_app', :table, 'SELECT')"
                ),
                {"table": f"public.{LearningRunSourceRightsReceiptRecord.__tablename__}"},
            )
        ).one()
        gate_function_signature = (
            "public.wp06_assert_training_source_rights(uuid,text,jsonb,uuid,text,text,uuid)"
        )
        gate_privileges = (
            await unit.session.execute(
                text(
                    "SELECT has_function_privilege('crypidian_learning_evaluation_reader', "
                    ":gate_function_signature, "
                    "'EXECUTE'), "
                    "has_function_privilege('crypidian_learning_batch_reader', "
                    ":gate_function_signature, "
                    "'EXECUTE'), "
                    "has_function_privilege('crypidian_learning_source_rights_reader', "
                    ":gate_function_signature, "
                    "'EXECUTE'), "
                    "has_function_privilege('crypidian_admin_web', "
                    ":gate_function_signature, "
                    "'EXECUTE'), "
                    "has_function_privilege('crypidian_app', "
                    ":gate_function_signature, "
                    "'EXECUTE'), "
                    "has_function_privilege('crypidian_learning_evaluator', "
                    ":gate_function_signature, "
                    "'EXECUTE'), "
                    "has_function_privilege('crypidian_trainer_worker', "
                    ":gate_function_signature, "
                    "'EXECUTE')"
                ),
                {"gate_function_signature": gate_function_signature},
            )
        ).one()
    await engine.dispose()
    assert privileges == (True, False, False, False)
    assert gate_privileges == (True, True, True, False, False, False, False)
    return {
        "gate_execute_scope": True,
        "run_source_rights_metadata_only": True,
        "workspace_isolation": True,
    }


async def _source_run_rights_immutable() -> dict[str, object]:
    """Database trigger từ chối sửa evidence rights đã ghi theo run."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    access_id = UUID(os.environ["WP06_ACCESS_ID"])
    engine = _engine()
    immutable = False
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        try:
            await connection.execute(
                text(
                    "UPDATE public.learning_run_source_rights_receipt "
                    "SET checked_at = checked_at WHERE workspace_id = :workspace_id "
                    "AND access_id = :access_id"
                ),
                {"workspace_id": str(workspace_id), "access_id": str(access_id)},
            )
        except DBAPIError as error:
            immutable = "WP06_RUN_SOURCE_RIGHTS_IMMUTABLE" in str(error.orig)
    await engine.dispose()
    assert immutable
    return {"run_source_rights_immutable": True}


async def _source_rights_consume() -> dict[str, object]:
    """Áp invalidation quyền nguồn sau khi manifest đã seal."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    result = await ResearchRightsMaintenanceWorker(
        session_factory=sessions,
        workspace_id=workspace_id,
        worker_id="wp06-source-revocation",
        tick_seconds=1,
    ).run_once()
    await engine.dispose()
    return {
        "rights_events_processed": result.rights_batch.processed,
        "rights_backlog_empty": (
            result.rights_batch.processed == result.rights_batch.claimed
            and result.rights_batch.failed == 0
            and result.rights_batch.dead_lettered == 0
        ),
    }


async def _source_rights_verify_unchanged() -> dict[str, object]:
    """Đảm bảo retry bị từ chối không sửa payload hoặc receipt đã ghi."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_SOURCE_MANIFEST_HASH"]
    engine = _engine()
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, false)"),
            {"workspace_id": str(workspace_id)},
        )
        receipt_count = await connection.scalar(
            select(func.count())
            .select_from(LearningSourceTrainingReceiptRecord)
            .where(
                LearningSourceTrainingReceiptRecord.workspace_id == workspace_id,
                LearningSourceTrainingReceiptRecord.manifest_hash == manifest_hash,
            )
        )
        normalized_count = await connection.scalar(
            select(func.count())
            .select_from(NormalizedLearningSampleRecord)
            .join(
                NormalizedLearningDatasetRecord,
                (
                    NormalizedLearningDatasetRecord.workspace_id
                    == NormalizedLearningSampleRecord.workspace_id
                )
                & (
                    NormalizedLearningDatasetRecord.manifest_hash
                    == NormalizedLearningSampleRecord.manifest_hash
                )
                & (
                    NormalizedLearningDatasetRecord.fold_plan_hash
                    == NormalizedLearningSampleRecord.fold_plan_hash
                ),
            )
            .where(
                NormalizedLearningDatasetRecord.workspace_id == workspace_id,
                NormalizedLearningDatasetRecord.manifest_hash == manifest_hash,
            )
        )
    await engine.dispose()
    assert receipt_count == 1
    assert normalized_count == 3
    return {"receipt_unchanged": True, "normalized_sample_count_unchanged": True}


async def _source_receipt_access_denied() -> dict[str, object]:
    """Kiểm tra admin/trainer không được đọc hoặc ghi receipt TRAINING."""
    engine = _engine()
    async with engine.connect() as connection:
        role, select_allowed, insert_allowed, update_allowed, delete_allowed = (
            await connection.execute(
                text(
                    "SELECT session_user, "
                    "has_table_privilege(session_user, :receipt, 'SELECT'), "
                    "has_table_privilege(session_user, :receipt, 'INSERT'), "
                    "has_table_privilege(session_user, :receipt, 'UPDATE'), "
                    "has_table_privilege(session_user, :receipt, 'DELETE')"
                ),
                {"receipt": "public.learning_source_training_receipt"},
            )
        ).one()
    assert role in {"crypidian_admin_web", "crypidian_trainer_worker"}
    assert not any((select_allowed, insert_allowed, update_allowed, delete_allowed))
    async with engine.connect() as connection:
        app_allowed = await connection.scalar(
            text(
                "SELECT has_table_privilege('crypidian_app', "
                "'public.learning_source_training_receipt', 'SELECT') OR "
                "has_table_privilege('crypidian_app', "
                "'public.learning_source_training_receipt', 'INSERT')"
            )
        )
    await engine.dispose()
    assert not app_allowed
    return {"receipt_access_denied_for": role, "app_receipt_access_denied": True}


async def _source_receipt_immutable() -> dict[str, object]:
    """DB trigger từ chối UPDATE kể cả khi E2E dùng migration owner principal."""
    workspace_id = UUID(os.environ["WP06_SOURCE_WORKSPACE_ID"])
    engine = _engine()
    immutable = False
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        try:
            await connection.execute(
                text(
                    "UPDATE public.learning_source_training_receipt "
                    "SET checked_at = checked_at WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": str(workspace_id)},
            )
        except DBAPIError as error:
            immutable = "WP06_NORMALIZED_SAMPLE_IMMUTABLE" in str(error.orig)
    await engine.dispose()
    assert immutable
    return {"receipt_update_trigger_immutable": True}


async def _builder() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_MANIFEST_HASH"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        manifest_record = await session.scalar(
            select(DatasetManifestRecord).where(
                DatasetManifestRecord.workspace_id == workspace_id,
                DatasetManifestRecord.manifest_hash == manifest_hash,
            )
        )
        approval_record = await session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == workspace_id,
                LearningSplitApprovalRecord.manifest_hash == manifest_hash,
            )
        )
        assert manifest_record is not None
        assert approval_record is not None
        manifest = manifest_record.to_domain()
        approval = approval_record.to_domain()
        plan = build_manifest_fold_plan(
            manifest,
            folds=_folds(),
            expected_split_policy_version=manifest.split_policy_version,
            evaluation_cutoff=_at(8),
        )
        assigned = {
            sample_id
            for fold in plan.folds
            for sample_id in (
                *fold.train_sample_ids,
                *fold.validation_sample_ids,
                *fold.test_sample_ids,
            )
        }
        manifest_samples = {sample.sample_id: sample for sample in manifest.samples}
        samples = tuple(
            NormalizedLearningSample(
                sample_id=sample_id,
                outcome_id=manifest_samples[sample_id].outcome_id,
                outcome_hash=manifest_samples[sample_id].outcome_hash,
                feature_snapshot_hash=manifest_samples[sample_id].feature_snapshot_hash,
                feature_schema_hash="e" * 64,
                label_hash=manifest_samples[sample_id].label_hash,
                label_value=manifest_samples[sample_id].label_value,
                features=(("fixture_feature", float(index + 1)),),
            )
            for index, sample_id in enumerate(sorted(assigned))
        )
        assignments = tuple(
            NormalizedFoldAssignment(fold.fold_id, sample_id, role)
            for fold in plan.folds
            for role, sample_ids in (
                ("train", fold.train_sample_ids),
                ("validation", fold.validation_sample_ids),
                ("test", fold.test_sample_ids),
            )
            for sample_id in sample_ids
        )
        dataset = NormalizedLearningDataset(
            workspace_id=workspace_id,
            manifest_hash=manifest.content_hash,
            split_policy_version=manifest.split_policy_version,
            fold_plan_hash=plan.content_hash,
            samples=samples,
            assignments=assignments,
            content_hash="",
        )
        dataset = replace(dataset, content_hash=normalized_learning_dataset_hash(dataset))
        repository = LearningFoldSampleRepository(session)
        first = await repository.append_normalized_dataset(
            manifest=manifest,
            fold_plan=plan,
            approval=approval,
            dataset=dataset,
        )
        retry = await repository.append_normalized_dataset(
            manifest=manifest,
            fold_plan=plan,
            approval=approval,
            dataset=dataset,
        )
        sample_count = await session.scalar(
            select(func.count()).select_from(NormalizedLearningSampleRecord)
        )
        assignment_count = await session.scalar(
            select(func.count()).select_from(NormalizedFoldAssignmentRecord)
        )
        assert first.is_duplicate is False
        assert retry.is_duplicate is True
        assert sample_count == 3
        assert assignment_count == 3
        forged_profile_rejected = False
        try:
            async with session.begin_nested():
                session.add(
                    LearningFoldResourceProfileRecord(
                        workspace_id=workspace_id,
                        normalized_dataset_id=first.record.id,
                        manifest_hash=manifest.content_hash,
                        fold_plan_hash=plan.content_hash,
                        fold_id=plan.folds[0].fold_id,
                        training_samples=2,
                        validation_samples=1,
                        feature_count=1,
                        feature_schema_hash="e" * 64,
                        profile_hash="a" * 64,
                    )
                )
                await session.flush()
        except DBAPIError as error:
            forged_profile_rejected = "WP06_LEARNING_RESOURCE_PROFILE_NOT_DERIVED_FROM_ROWS" in str(
                error.orig
            )
        assert forged_profile_rejected
        profiles = await session.scalars(select(LearningFoldResourceProfileRecord))
        profile_rows = profiles.all()
        assert len(profile_rows) == 1
        assert (
            profile_rows[0].training_samples,
            profile_rows[0].validation_samples,
            profile_rows[0].feature_count,
            profile_rows[0].feature_schema_hash,
        ) == (1, 1, 1, "e" * 64)
        rows = await session.scalars(select(NormalizedLearningSampleRecord))
        assert all(row.to_domain() in samples for row in rows.all())
        source_dependent = any(sample.source_dependent for sample in manifest.samples)
        source_receipts = (
            await session.scalars(
                select(LearningSourceTrainingReceiptRecord).where(
                    LearningSourceTrainingReceiptRecord.workspace_id == workspace_id,
                    LearningSourceTrainingReceiptRecord.normalized_dataset_id == first.record.id,
                )
            )
        ).all()
        if source_dependent:
            assert len(source_receipts) == 1
            receipt = source_receipts[0]
            canonical = json.loads(receipt.canonical_evidence)
            assert set(canonical) == {
                "checked_at",
                "manifest_hash",
                "reference",
                "sample_id",
                "workspace_id",
            }
            assert canonical["reference"]["purpose"] == "training"
            assert receipt.checked_at.tzinfo is not None
        else:
            assert source_receipts == []

        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(uuid4())},
        )
        assert (
            await session.scalar(select(func.count()).select_from(NormalizedLearningSampleRecord))
            == 0
        )
        if source_dependent:
            assert (
                await session.scalar(
                    select(func.count()).select_from(LearningSourceTrainingReceiptRecord)
                )
                == 0
            )
        hold_seconds = float(os.environ.get("WP06_BUILDER_HOLD_SOURCE_LOCK_SECONDS", "0"))
        if source_dependent and hold_seconds > 0:
            print("WP06_SOURCE_LOCK_HELD", flush=True)
            await asyncio.sleep(min(hold_seconds, 10.0))
    async with engine.connect() as connection:
        await connection.execute(text("RESET app.workspace_id"))
        assert (
            await connection.scalar(text("SELECT count(*) FROM public.learning_normalized_sample"))
            == 0
        )
        for table in (
            "learning_normalized_dataset",
            "learning_normalized_sample",
            "learning_fold_assignment",
        ):
            assert await connection.scalar(
                text("SELECT has_table_privilege('crypidian_candidate_builder', :table, 'SELECT')"),
                {"table": f"public.{table}"},
            )
            assert await connection.scalar(
                text("SELECT has_table_privilege('crypidian_candidate_builder', :table, 'INSERT')"),
                {"table": f"public.{table}"},
            )
            for role in ("crypidian_admin_web", "crypidian_trainer_worker", "crypidian_app"):
                assert not await connection.scalar(
                    text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                    {"role": role, "table": f"public.{table}"},
                )
                assert not await connection.scalar(
                    text("SELECT has_table_privilege(:role, :table, 'INSERT')"),
                    {"role": role, "table": f"public.{table}"},
                )
        profile_table = "public.learning_fold_resource_profile"
        assert await connection.scalar(
            text("SELECT has_table_privilege('crypidian_candidate_builder', :table, 'INSERT')"),
            {"table": profile_table},
        )
        for role in ("crypidian_admin_web", "crypidian_trainer_worker"):
            assert await connection.scalar(
                text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                {"role": role, "table": profile_table},
            )
            assert not await connection.scalar(
                text("SELECT has_table_privilege(:role, :table, 'INSERT')"),
                {"role": role, "table": profile_table},
            )
        denied = False
        try:
            await connection.execute(
                text("UPDATE public.learning_normalized_sample SET label_value = 0")
            )
        except DBAPIError as error:
            denied = "permission denied" in str(error.orig).lower()
        else:
            raise AssertionError("Candidate builder có quyền UPDATE normalized payload.")
        assert denied
    await engine.dispose()
    result = {
        "normalized_payloads": 3,
        "fold_assignments": 3,
        "holdout_excluded": True,
        "idempotent_retry": True,
        "workspace_rls": True,
        "builder_only_acl": True,
        "derived_profile_trigger": True,
        "unscoped_zero_rows": True,
    }
    if source_dependent:
        result["source_training_receipt_count"] = len(source_receipts)
        result["source_receipt_metadata_only"] = True
    return result


async def _training_admin_lifecycle() -> dict[str, object]:
    """Admin-web đọc metadata, enqueue idempotent và ghi yêu cầu hủy có actor."""
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_MANIFEST_HASH"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        membership = await session.scalar(
            select(UserMembership).where(UserMembership.workspace_id == workspace_id)
        )
        assert membership is not None
        principal = Principal(
            user_id=membership.user_id,
            membership_id=membership.id,
            workspace_id=workspace_id,
            roles=frozenset({Role.ANALYST_REVIEWER}),
            mfa_verified_at=datetime.now(UTC),
        )
    facade = LearningTrainingAdminService(sessions).for_principal(principal)
    options = await facade.list_eligible_folds()
    assert len(options) == 1
    option = options[0]
    assert option.manifest_hash == manifest_hash
    assert (option.training_samples, option.validation_samples, option.feature_count) == (1, 1, 1)
    idempotency_key = uuid4()
    first = await facade.enqueue(
        normalized_dataset_id=option.normalized_dataset_id,
        fold_id=option.fold_id,
        idempotency_key=idempotency_key,
    )
    retry = await facade.enqueue(
        normalized_dataset_id=option.normalized_dataset_id,
        fold_id=option.fold_id,
        idempotency_key=idempotency_key,
    )
    assert first.status == "queued"
    assert first.job_id is not None
    assert retry.duplicate
    assert retry.attempt_id == first.attempt_id
    assert retry.job_id == first.job_id
    cancelled = await facade.cancel(job_id=first.job_id)
    assert cancelled.status == "queued"
    assert cancelled.cancel_requested
    async with sessions() as session:
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        job = await session.scalar(
            select(LearningTrainingJobRecord).where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.id == first.job_id,
            )
        )
        assert job is not None
        assert job.cancel_requested_by == membership.id
    async with engine.connect() as connection:
        assert not await connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_admin_web', "
                "'public.learning_normalized_sample', 'features', 'SELECT')"
            )
        )
        assert not await connection.scalar(
            text(
                "SELECT has_column_privilege('crypidian_admin_web', "
                "'public.learning_normalized_sample', 'label_value', 'SELECT')"
            )
        )
    await engine.dispose()
    return {
        "fold_exactly_approved": True,
        "metadata_only_admin_query": True,
        "admin_payload_column_denied": True,
        "enqueue_idempotent": True,
        "cancel_actor_verified": True,
    }


async def _denied() -> dict[str, object]:
    engine = _engine()
    denied_tables = []
    metadata_header_available = False
    for table in (
        "learning_normalized_dataset",
        "learning_normalized_sample",
        "learning_fold_assignment",
    ):
        async with engine.connect() as connection:
            try:
                if (
                    os.environ["TRADING_BOT_DATABASE_USER"] == "crypidian_admin_web"
                    and table == "learning_normalized_dataset"
                ):
                    await connection.execute(
                        text(
                            "SELECT workspace_id, id, manifest_hash, split_policy_version, "
                            "fold_plan_hash, approval_hash, content_hash, sample_count, "
                            "assignment_count, created_at "
                            "FROM public.learning_normalized_dataset LIMIT 1"
                        )
                    )
                    metadata_header_available = True
                else:
                    await connection.execute(text(f"SELECT * FROM public.{table} LIMIT 1"))
            except DBAPIError as error:
                if "permission denied" not in str(error.orig).lower():
                    raise
                denied_tables.append(table)
    await engine.dispose()
    if os.environ["TRADING_BOT_DATABASE_USER"] == "crypidian_admin_web":
        assert metadata_header_available
        assert set(denied_tables) == {"learning_normalized_sample", "learning_fold_assignment"}
    else:
        assert set(denied_tables) == {
            "learning_normalized_dataset",
            "learning_normalized_sample",
            "learning_fold_assignment",
        }
    return {"trainer_payload_select_denied": True}


def _training_attempt(
    *,
    workspace_id: UUID,
    membership_id: UUID,
    profile: LearningFoldResourceProfileRecord,
    approval: LearningSplitApprovalRecord,
    request: LearningResourceRequest,
    rejected: bool = False,
    trial_index: int = 0,
    trial_count: int = 2,
) -> LearningTrainingAttempt:
    plan = None
    reason = None
    if rejected:
        try:
            assess_learning_resource_request(request)
        except LearningResourceRejected:
            reason = LearningAttemptReasonCode.RESOURCE_LIMIT
        else:
            raise AssertionError("Fixture rejection phải vượt resource budget.")
    else:
        plan = assess_learning_resource_request(request)
    return LearningTrainingAttempt(
        id=uuid4(),
        workspace_id=workspace_id,
        actor_membership_id=membership_id,
        idempotency_key=uuid4(),
        manifest_hash=profile.manifest_hash,
        split_policy_version=approval.split_policy_version,
        fold_plan_hash=profile.fold_plan_hash,
        approval_hash=approval.approval_hash,
        parameter_family_hash="d" * 64,
        trial_index=trial_index,
        trial_count=trial_count,
        fold_id=profile.fold_id,
        seed=7,
        trainer_version=CPU_TRAINER_VERSION,
        dependency_fingerprint="f" * 64,
        resource_policy_version="budget-v1",
        resource_request=request,
        resource_plan=plan,
        rejection_reason_code=reason,
    )


async def _create_attempts() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_MANIFEST_HASH"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        profile = await session.scalar(
            select(LearningFoldResourceProfileRecord).where(
                LearningFoldResourceProfileRecord.workspace_id == workspace_id,
                LearningFoldResourceProfileRecord.manifest_hash == manifest_hash,
            )
        )
        approval = await session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == workspace_id,
                LearningSplitApprovalRecord.manifest_hash == manifest_hash,
            )
        )
        membership = await session.scalar(
            select(UserMembership).where(UserMembership.workspace_id == workspace_id)
        )
        assert profile is not None
        assert approval is not None
        assert membership is not None
        repository = LearningAttemptRepository(session)
        request = LearningResourceRequest(
            profile.training_samples,
            profile.validation_samples,
            profile.feature_count,
            100,
            30,
            512 * 1024 * 1024,
        )
        forged_request = LearningResourceRequest(
            profile.training_samples + 1,
            profile.validation_samples,
            profile.feature_count,
            100,
            30,
            2 * 1024 * 1024,
        )
        forged_attempt = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=forged_request,
        )
        database_count_guard = False
        try:
            async with session.begin_nested():
                session.add(
                    LearningTrainingAttemptRecord.from_domain(
                        forged_attempt,
                        normalized_dataset_id=profile.normalized_dataset_id,
                    )
                )
                await session.flush()
        except DBAPIError as error:
            database_count_guard = "WP06_LEARNING_ATTEMPT_COUNTS_NOT_AUTHORITATIVE" in str(
                error.orig
            )
        assert database_count_guard
        attempt = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=request,
        )
        first = await repository.append_attempt(
            attempt=attempt,
            normalized_dataset_id=profile.normalized_dataset_id,
        )
        retry = await repository.append_attempt(
            attempt=attempt,
            normalized_dataset_id=profile.normalized_dataset_id,
        )
        conflict = False
        try:
            await repository.append_attempt(
                attempt=replace(attempt, seed=attempt.seed + 1),
                normalized_dataset_id=profile.normalized_dataset_id,
            )
        except LearningAttemptConflict:
            conflict = True
        trial_reuse_rejected = False
        try:
            await repository.append_attempt(
                attempt=replace(
                    attempt,
                    id=uuid4(),
                    idempotency_key=uuid4(),
                ),
                normalized_dataset_id=profile.normalized_dataset_id,
            )
        except LearningAttemptConflict:
            trial_reuse_rejected = True
        rejected_request = LearningResourceRequest(
            profile.training_samples,
            profile.validation_samples,
            profile.feature_count,
            2_001,
            30,
            2 * 1024 * 1024,
        )
        rejected = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=rejected_request,
            rejected=True,
            trial_index=1,
        )
        rejected_result = await repository.append_attempt(
            attempt=rejected,
            normalized_dataset_id=profile.normalized_dataset_id,
        )
        recovery_attempt = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=request,
            trial_index=2,
            trial_count=3,
        )
        await repository.append_attempt(
            attempt=recovery_attempt,
            normalized_dataset_id=profile.normalized_dataset_id,
        )
        cancel_attempt = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=request,
            trial_index=3,
            trial_count=4,
        )
        await repository.append_attempt(
            attempt=cancel_attempt,
            normalized_dataset_id=profile.normalized_dataset_id,
        )
        assert first.is_duplicate is False
        assert retry.is_duplicate is True
        assert conflict
        assert trial_reuse_rejected
        assert rejected_result.attempt.initial_state is LearningAttemptState.REJECTED
        missing_event_attempt = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=request,
            trial_index=4,
            trial_count=5,
        )

    missing_event_required = False
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            session.add(
                LearningTrainingAttemptRecord.from_domain(
                    missing_event_attempt,
                    normalized_dataset_id=profile.normalized_dataset_id,
                )
            )
            await session.flush()
    except DBAPIError as error:
        missing_event_required = "WP06_LEARNING_ATTEMPT_INITIAL_EVENT_REQUIRED" in str(error.orig)
    assert missing_event_required
    await engine.dispose()
    return {
        "attempt_id": str(attempt.id),
        "rejected_attempt_id": str(rejected.id),
        "recovery_attempt_id": str(recovery_attempt.id),
        "cancel_attempt_id": str(cancel_attempt.id),
        "membership_id": str(membership.id),
        "idempotent_retry": retry.is_duplicate,
        "conflict_rejected": conflict,
        "trial_reuse_rejected": trial_reuse_rejected,
        "budget_rejected": rejected_result.attempt.initial_state.value == "rejected",
        "database_count_guard": database_count_guard,
        "initial_event_required": missing_event_required,
    }


async def _run_attempt_lifecycle() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    rejected_id = UUID(os.environ["WP06_REJECTED_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        artifact = await session.scalar(
            select(LearningTrainingArtifactRecord).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.attempt_id == attempt_id,
            )
        )
        assert artifact is not None
        job = await session.scalar(
            select(LearningTrainingJobRecord).where(
                LearningTrainingJobRecord.workspace_id == workspace_id,
                LearningTrainingJobRecord.attempt_id == attempt_id,
            )
        )
        assert job is not None
        assert job.status == "succeeded"
        assert job.stage == "complete"
        assert job.attempt_count == 2
        assert job.lease_generation == 2
        assert job.lease_owner is None
        assert job.lease_until is None
        LearningTrainingArtifactRepository.validate_record(artifact)
        events = (
            await session.scalars(
                select(LearningAttemptEventRecord)
                .where(
                    LearningAttemptEventRecord.workspace_id == workspace_id,
                    LearningAttemptEventRecord.attempt_id == attempt_id,
                )
                .order_by(LearningAttemptEventRecord.sequence)
            )
        ).all()
        rejected_event = await session.scalar(
            select(LearningAttemptEventRecord).where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == rejected_id,
                LearningAttemptEventRecord.sequence == 1,
            )
        )
        assert [event.state for event in events] == ["queued", "running", "succeeded"]
        terminal = events[-1]
        assert terminal.elapsed_milliseconds == artifact.elapsed_milliseconds
        assert terminal.peak_memory_bytes == artifact.peak_memory_bytes
        assert terminal.actual_operation_units == artifact.actual_operation_units
        assert artifact.artifact["scorer_state"] == "abstain"
        assert artifact.artifact["calibrated"] is False
        assert "samples" not in artifact.artifact
        assert rejected_event is not None
        assert rejected_event.state == "rejected"
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(uuid4())},
        )
        assert (
            await session.scalar(
                select(LearningTrainingAttemptRecord.id).where(
                    LearningTrainingAttemptRecord.id == attempt_id
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(LearningTrainingArtifactRecord.id).where(
                    LearningTrainingArtifactRecord.attempt_id == attempt_id
                )
            )
            is None
        )
    async with engine.connect() as connection:
        for table in (
            "learning_training_attempt",
            "learning_attempt_event",
            "learning_training_artifact",
        ):
            assert await connection.scalar(
                text("SELECT has_table_privilege(current_user, :table_name, 'SELECT')"),
                {"table_name": f"public.{table}"},
            )
        assert await connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'public.learning_training_artifact', 'INSERT')"
            )
        )
        assert not await connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'public.learning_training_artifact', 'UPDATE')"
            )
        )
        assert not await connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'public.learning_training_artifact', 'DELETE')"
            )
        )
        assert await connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'public.learning_attempt_event', 'INSERT')"
            )
        )
    await engine.dispose()
    return {
        "event_sequence_valid": True,
        "terminal_cost_persisted": True,
        "elapsed_milliseconds": terminal.elapsed_milliseconds,
        "cpu_time_milliseconds": artifact.cpu_time_milliseconds,
        "peak_memory_bytes": terminal.peak_memory_bytes,
        "actual_operation_units": terminal.actual_operation_units,
        "artifact_hash": artifact.artifact_hash,
        "artifact_persisted": True,
        "artifact_checksum_valid": True,
        "artifact_scorer_abstains": True,
        "resource_limit_recorded": True,
        "workspace_rls": True,
        "trainer_artifact_insert_only_acl": True,
        "job_success_fenced_and_persisted": True,
    }


async def _verify_training_receipt() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        receipt = await session.scalar(
            select(LearningTrainingAccessReceiptRecord).where(
                LearningTrainingAccessReceiptRecord.workspace_id == workspace_id,
                LearningTrainingAccessReceiptRecord.attempt_id == attempt_id,
            )
        )
        assert receipt is not None
        assert receipt.purpose == "trainer-input"
        assert receipt.actor_principal == "crypidian_trainer_worker"
        assert receipt.training_samples == 1
        assert receipt.validation_samples == 1
        assert receipt.sample_count == 2
        assert receipt.request_fingerprint == (
            await session.scalar(
                select(LearningTrainingAttemptRecord.content_hash).where(
                    LearningTrainingAttemptRecord.workspace_id == workspace_id,
                    LearningTrainingAttemptRecord.id == attempt_id,
                )
            )
        )
        assert not await session.scalar(
            text(
                "SELECT has_table_privilege(current_user, "
                "'public.learning_training_access_receipt', 'INSERT')"
            )
        )
    await engine.dispose()
    return {"access_receipt_auditable": True, "holdout_not_recorded": True}


async def _recover_before_receipt() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    now = datetime.now(UTC)
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        jobs = LearningTrainingJobRepository(session)
        claim = await jobs.claim_next(
            workspace_id=workspace_id,
            worker_id="e2e-crash-before-receipt",
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        assert claim is not None
        attempt_id = claim.attempt_id
        assert (
            await jobs.reclaim_expired(
                workspace_id=workspace_id,
                now=claim.lease_until + timedelta(seconds=1),
            )
            == 1
        )
        recovered = await jobs.get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.lease_generation == 1
        assert recovered.attempt_count == 1
    await engine.dispose()
    return {
        "attempt_id": str(attempt_id),
        "lease_requeued_before_receipt": True,
        "generation_advanced": True,
    }


async def _recover_after_receipt() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    now = datetime.now(UTC)
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        jobs = LearningTrainingJobRepository(session)
        claim = await jobs.claim_next(
            workspace_id=workspace_id,
            worker_id="e2e-crash-after-receipt",
            now=now,
            lease_duration=timedelta(seconds=30),
        )
        assert claim is not None
        attempt_id = claim.attempt_id
        await LearningTrainingBatchRepository(session).claim_batch(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )

    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        jobs = LearningTrainingJobRepository(session)
        assert (
            await jobs.reclaim_expired(
                workspace_id=workspace_id,
                now=claim.lease_until + timedelta(seconds=1),
            )
            == 1
        )
        failed = await jobs.get_by_attempt(workspace_id=workspace_id, attempt_id=attempt_id)
        latest = await session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        assert failed is not None
        assert failed.status == "failed"
        assert latest is not None
        assert latest.state == "failed"
        assert (
            await session.scalar(
                select(LearningTrainingArtifactRecord.id).where(
                    LearningTrainingArtifactRecord.workspace_id == workspace_id,
                    LearningTrainingArtifactRecord.attempt_id == attempt_id,
                )
            )
            is None
        )
    await engine.dispose()
    return {
        "attempt_id": str(attempt_id),
        "receipt_consumed_attempt_failed": True,
        "consumed_batch_not_reissued": True,
    }


async def _request_queued_cancel() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    membership_id = UUID(os.environ["WP06_MEMBERSHIP_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        job = await LearningTrainingJobRepository(session).get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        assert job is not None
        assert job.status == "queued"
        result = await LearningTrainingJobRepository(session).request_cancel(
            workspace_id=workspace_id,
            job_id=job.id,
            requested_by=membership_id,
            now=datetime.now(UTC),
        )
        assert result is not None
        assert result.cancel_requested
        assert result.status == "queued"
    await engine.dispose()
    return {"cancel_request_recorded": True}


async def _create_training_attempt() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_MANIFEST_HASH"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        profile = await session.scalar(
            select(LearningFoldResourceProfileRecord).where(
                LearningFoldResourceProfileRecord.workspace_id == workspace_id,
                LearningFoldResourceProfileRecord.manifest_hash == manifest_hash,
            )
        )
        approval = await session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == workspace_id,
                LearningSplitApprovalRecord.manifest_hash == manifest_hash,
            )
        )
        membership = await session.scalar(
            select(UserMembership).where(UserMembership.workspace_id == workspace_id)
        )
        assert profile is not None
        assert approval is not None
        assert membership is not None
        request = LearningResourceRequest(
            profile.training_samples,
            profile.validation_samples,
            profile.feature_count,
            100,
            30,
            512 * 1024 * 1024,
        )
        attempt = _training_attempt(
            workspace_id=workspace_id,
            membership_id=membership.id,
            profile=profile,
            approval=approval,
            request=request,
            trial_index=int(os.environ.get("WP06_TRIAL_INDEX", "5")),
            trial_count=int(os.environ.get("WP06_TRIAL_INDEX", "5")) + 1,
        )
        await LearningAttemptRepository(session).append_attempt(
            attempt=attempt,
            normalized_dataset_id=profile.normalized_dataset_id,
        )
    await engine.dispose()
    return {"attempt_id": str(attempt.id)}


async def _training_job_status() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        job = await LearningTrainingJobRepository(session).get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        latest = await session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        artifact = await session.scalar(
            select(LearningTrainingArtifactRecord.id).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.attempt_id == attempt_id,
            )
        )
        assert job is not None
        assert latest is not None
        result = {
            "status": job.status,
            "event_state": latest.state,
            "artifact_created": artifact is not None,
            "artifact_id": str(artifact) if artifact is not None else None,
        }
    await engine.dispose()
    return result


async def _runtime_resource_limits() -> dict[str, object]:
    """Đọc giới hạn cgroup áp dụng trong container trainer hiện hành."""
    root = Path("/sys/fs/cgroup")
    cpu_max = root / "cpu.max"
    if cpu_max.is_file():
        quota_raw, period_raw = cpu_max.read_text(encoding="ascii").split()
        memory_raw = (root / "memory.max").read_text(encoding="ascii").strip()
        pids_raw = (root / "pids.max").read_text(encoding="ascii").strip()
        cpu_millicores = (
            None if quota_raw == "max" else round(int(quota_raw) * 1000 / int(period_raw))
        )
    else:
        quota_raw = (root / "cpu/cpu.cfs_quota_us").read_text(encoding="ascii").strip()
        period_raw = (root / "cpu/cpu.cfs_period_us").read_text(encoding="ascii").strip()
        memory_raw = (root / "memory/memory.limit_in_bytes").read_text(encoding="ascii").strip()
        pids_raw = (root / "pids/pids.max").read_text(encoding="ascii").strip()
        cpu_millicores = (
            None if int(quota_raw) < 0 else round(int(quota_raw) * 1000 / int(period_raw))
        )
    return {
        "cpu_millicores": cpu_millicores,
        "memory_limit_bytes": None if memory_raw == "max" else int(memory_raw),
        "pids_limit": None if pids_raw == "max" else int(pids_raw),
    }


async def _verify_cancelled_job() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        job = await LearningTrainingJobRepository(session).get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        assert job is not None
        assert job.status == "cancelled"
        assert job.lease_generation == 0
        assert job.attempt_count == 0
        event = await session.scalar(
            select(LearningAttemptEventRecord).where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == attempt_id,
                LearningAttemptEventRecord.sequence == 2,
            )
        )
        assert event is not None
        assert event.state == "cancelled"
        receipt = await session.scalar(
            select(LearningTrainingAccessReceiptRecord.id).where(
                LearningTrainingAccessReceiptRecord.workspace_id == workspace_id,
                LearningTrainingAccessReceiptRecord.attempt_id == attempt_id,
            )
        )
        assert receipt is None
    await engine.dispose()
    return {"cancelled_event_persisted": True, "receipt_not_created": True}


async def _running_cancel_status() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        job = await LearningTrainingJobRepository(session).get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        receipt = await session.scalar(
            select(LearningTrainingAccessReceiptRecord.id).where(
                LearningTrainingAccessReceiptRecord.workspace_id == workspace_id,
                LearningTrainingAccessReceiptRecord.attempt_id == attempt_id,
            )
        )
        assert job is not None
        result = {"status": job.status, "receipt_created": receipt is not None}
    await engine.dispose()
    return result


async def _request_running_cancel() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    membership_id = UUID(os.environ["WP06_MEMBERSHIP_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        repository = LearningTrainingJobRepository(session)
        job = await repository.get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        assert job is not None
        assert job.status == "running"
        result = await repository.request_cancel(
            workspace_id=workspace_id,
            job_id=job.id,
            requested_by=membership_id,
            now=datetime.now(UTC),
        )
        assert result is not None
        assert result.cancel_requested
    await engine.dispose()
    return {"cancel_request_recorded": True}


async def _verify_running_cancelled_job() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        job = await LearningTrainingJobRepository(session).get_by_attempt(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
        )
        latest = await session.scalar(
            select(LearningAttemptEventRecord)
            .where(
                LearningAttemptEventRecord.workspace_id == workspace_id,
                LearningAttemptEventRecord.attempt_id == attempt_id,
            )
            .order_by(LearningAttemptEventRecord.sequence.desc())
            .limit(1)
        )
        receipt = await session.scalar(
            select(LearningTrainingAccessReceiptRecord.id).where(
                LearningTrainingAccessReceiptRecord.workspace_id == workspace_id,
                LearningTrainingAccessReceiptRecord.attempt_id == attempt_id,
            )
        )
        artifact = await session.scalar(
            select(LearningTrainingArtifactRecord.id).where(
                LearningTrainingArtifactRecord.workspace_id == workspace_id,
                LearningTrainingArtifactRecord.attempt_id == attempt_id,
            )
        )
        assert job is not None
        assert job.status == "cancelled"
        assert job.cancel_requested
        assert job.attempt_count == 1
        assert job.lease_generation == 1
        assert latest is not None
        assert latest.state == "cancelled"
        assert receipt is not None
        assert artifact is None
    await engine.dispose()
    return {
        "cancelled_event_persisted": True,
        "receipt_preserved": True,
        "artifact_not_created": True,
    }


class _WaitForRunningCancellation:
    def train(self, _batch: AuthorizedTrainerBatch, *, cancel_event: Event | None = None) -> object:
        if cancel_event is None or not cancel_event.wait(timeout=30):
            raise TimeoutError("Worker không nhận được yêu cầu hủy trong E2E.")
        raise LearningTrainingProcessFailure(LearningTrainingProcessFailureCode.CANCELLED)


async def _run_running_cancel_worker() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    result = await LearningTrainingAttemptWorker(
        sessions,
        process_runner=_WaitForRunningCancellation(),
    ).work_once(
        workspace_id=workspace_id,
        worker_id="e2e-running-cancel-worker",
    )
    await engine.dispose()
    return {
        "status": result.status,
        "attempt_id": str(result.attempt_id) if result.attempt_id is not None else None,
    }


def _registry_report(
    *,
    manifest_hash: str,
    fold_plan_hash: str,
    split_policy_version: str,
    cutoff: datetime,
    fold_id: str,
) -> WalkForwardEvaluationReport:
    roles = {
        role: FoldRoleMetrics(
            role=role,
            sample_ids=(),
            trade_lineage_hash="1" * 64,
            metrics=build_evaluation_metrics((), observation_count=0, seed=17),
        )
        for role in FOLD_ROLES
    }
    report = WalkForwardEvaluationReport(
        manifest_hash=manifest_hash,
        fold_plan_hash=fold_plan_hash,
        split_policy_version=split_policy_version,
        evaluation_cutoff=cutoff,
        seed=17,
        fold_evaluations=(
            FoldEvaluation(
                fold_id=fold_id,
                train=roles["train"],
                validation=roles["validation"],
                test=roles["test"],
                exclusions=(),
            ),
        ),
        candidate_completeness_verified=False,
        report_hash="",
        caveat_vi="Fixture diagnostic rỗng chỉ kiểm tra khóa liên kết của registry.",
    )
    return replace(
        report,
        report_hash=hashlib.sha256(report.canonical_json.encode()).hexdigest(),
    )


async def _create_registry_evaluation_report() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    manifest_hash = os.environ["WP06_MANIFEST_HASH"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        # Fixture xác định trước manifest seed; evaluator không SELECT payload/holdout.
        manifest = _manifest(workspace_id)
        assert manifest.content_hash == manifest_hash
        plan = build_manifest_fold_plan(
            manifest,
            folds=_folds(),
            expected_split_policy_version=manifest.split_policy_version,
            evaluation_cutoff=_at(8),
        )
        report = _registry_report(
            manifest_hash=manifest.content_hash,
            fold_plan_hash=plan.content_hash,
            split_policy_version=manifest.split_policy_version,
            cutoff=_at(8),
            fold_id=plan.folds[0].fold_id,
        )
        result = await LearningEvaluationReportRepository(unit.session).append(
            workspace_id=workspace_id,
            idempotency_key=uuid4(),
            report=report,
        )
        report_id = result.record.id
    await engine.dispose()
    return {"report_id": str(report_id), "report_hash": report.report_hash}


async def _prepare_model_registry_shadow() -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    artifact_ids = tuple(UUID(value) for value in os.environ["WP06_ARTIFACT_IDS"].split(","))
    if len(artifact_ids) != 3:
        raise ValueError("Shadow registry yêu cầu đúng ba artifact kiểm thử.")
    report_id = UUID(os.environ["WP06_REPORT_ID"])
    report_hash = os.environ["WP06_REPORT_HASH"]
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        membership_id = await unit.session.scalar(
            select(UserMembership.id)
            .where(UserMembership.workspace_id == workspace_id)
            .order_by(UserMembership.id)
            .limit(1)
        )
        assert membership_id is not None

    registry_ids: list[UUID] = []
    registration_keys = tuple(uuid4() for _ in artifact_ids)
    async with UnitOfWork(
        sessions, workspace_id=workspace_id, actor_membership_id=membership_id
    ) as unit:
        repository = LearningModelRegistryRepository(unit.session)
        first = await repository.register_artifact(
            workspace_id=workspace_id,
            artifact_id=artifact_ids[0],
            actor_membership_id=membership_id,
            idempotency_key=registration_keys[0],
            policy_version="learning-model-governance-v1",
            reason="Đăng ký artifact PAPER sau khi kiểm tra lineage.",
        )
        retry = await repository.register_artifact(
            workspace_id=workspace_id,
            artifact_id=artifact_ids[0],
            actor_membership_id=membership_id,
            idempotency_key=registration_keys[0],
            policy_version="learning-model-governance-v1",
            reason="Đăng ký artifact PAPER sau khi kiểm tra lineage.",
        )
        assert first.id == retry.id
        registry_ids.append(first.id)
        for artifact_id, key in zip(artifact_ids[1:], registration_keys[1:], strict=True):
            entry = await repository.register_artifact(
                workspace_id=workspace_id,
                artifact_id=artifact_id,
                actor_membership_id=membership_id,
                idempotency_key=key,
                policy_version="learning-model-governance-v1",
                reason="Đăng ký artifact PAPER đã hoàn tất huấn luyện.",
            )
            registry_ids.append(entry.id)
        conflicting_retry_rejected = False
        try:
            await repository.register_artifact(
                workspace_id=workspace_id,
                artifact_id=artifact_ids[0],
                actor_membership_id=membership_id,
                idempotency_key=registration_keys[0],
                policy_version="learning-model-governance-v1",
                reason="Khóa đăng ký không được dùng cho nội dung khác.",
            )
        except LearningModelRegistryConflict:
            conflicting_retry_rejected = True
        assert conflicting_retry_rejected

    async with UnitOfWork(
        sessions, workspace_id=workspace_id, actor_membership_id=membership_id
    ) as unit:
        repository = LearningModelRegistryRepository(unit.session)
        wrong_report_rejected = False
        try:
            await repository.attach_evaluation(
                workspace_id=workspace_id,
                registry_id=registry_ids[0],
                evaluation_report_id=report_id,
                evaluation_report_hash="0" * 64,
                actor_membership_id=membership_id,
                expected_revision=1,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Report phải khớp đúng hash đã lưu.",
            )
        except LearningModelRegistryConflict:
            wrong_report_rejected = True
        stale_revision_rejected = False
        try:
            await repository.attach_evaluation(
                workspace_id=workspace_id,
                registry_id=registry_ids[0],
                evaluation_report_id=report_id,
                evaluation_report_hash=report_hash,
                actor_membership_id=membership_id,
                expected_revision=2,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Revision cũ phải bị từ chối.",
            )
        except LearningModelRegistryConflict:
            stale_revision_rejected = True
        assert wrong_report_rejected
        assert stale_revision_rejected
        for registry_id in registry_ids[:2]:
            evaluated = await repository.attach_evaluation(
                workspace_id=workspace_id,
                registry_id=registry_id,
                evaluation_report_id=report_id,
                evaluation_report_hash=report_hash,
                actor_membership_id=membership_id,
                expected_revision=1,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Gắn report diagnostic khớp lineage artifact.",
            )
            assert evaluated.state.value == "evaluated"
        for registry_id in registry_ids[:2]:
            approved = await repository.approve(
                workspace_id=workspace_id,
                registry_id=registry_id,
                actor_membership_id=membership_id,
                mfa_verified_at=datetime.now(UTC),
                expected_revision=2,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Reviewer duyệt metadata cho model PAPER.",
            )
            assert approved.state.value == "evaluated"
            assert approved.revision == 3
        rejected = await repository.reject(
            workspace_id=workspace_id,
            registry_id=registry_ids[2],
            actor_membership_id=membership_id,
            mfa_verified_at=datetime.now(UTC),
            expected_revision=1,
            idempotency_key=uuid4(),
            policy_version="learning-model-governance-v1",
            reason_code="insufficient_evidence",
            reason="Bằng chứng đánh giá chưa đủ để tiếp tục.",
        )
        assert rejected.state.value == "rejected"
        assert rejected.revision == 2
        rejected_activation = False
        try:
            await repository.activate(
                workspace_id=workspace_id,
                registry_id=registry_ids[2],
                actor_membership_id=membership_id,
                mfa_verified_at=datetime.now(UTC),
                expected_revision=2,
                expected_pointer_version=0,
                expected_current_registry_id=None,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Model bị reject không thể active.",
            )
        except LearningModelRegistryConflict:
            rejected_activation = True
        assert rejected_activation

    async with UnitOfWork(
        sessions, workspace_id=workspace_id, actor_membership_id=membership_id
    ) as unit:
        repository = LearningModelRegistryRepository(unit.session)
        first_active, pointer = await repository.activate(
            workspace_id=workspace_id,
            registry_id=registry_ids[0],
            actor_membership_id=membership_id,
            mfa_verified_at=datetime.now(UTC),
            expected_revision=3,
            expected_pointer_version=0,
            expected_current_registry_id=None,
            idempotency_key=uuid4(),
            policy_version="learning-model-governance-v1",
            reason="Kích hoạt tham chiếu model đã được reviewer duyệt.",
        )
        assert first_active.state.value == "paper_active"
        assert pointer.version == 1

    await engine.dispose()
    return {
        "active_pointer_version": pointer.version,
        "active_registry_id": str(registry_ids[0]),
        "challenger_registry_id": str(registry_ids[1]),
        "registry_ids": [str(registry_id) for registry_id in registry_ids],
    }


async def _finish_model_registry_shadow(registry_ids: tuple[UUID, ...]) -> dict[str, object]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    if len(registry_ids) != 3:
        raise ValueError("Shadow registry yêu cầu đúng ba registry đã chuẩn bị.")
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        membership_id = await unit.session.scalar(
            select(UserMembership.id)
            .where(UserMembership.workspace_id == workspace_id)
            .order_by(UserMembership.id)
            .limit(1)
        )
        assert membership_id is not None

    async with UnitOfWork(
        sessions, workspace_id=workspace_id, actor_membership_id=membership_id
    ) as unit:
        repository = LearningModelRegistryRepository(unit.session)
        active_pointer = await repository.get_active_pointer(workspace_id=workspace_id)
        assert active_pointer.registry_id == registry_ids[0]
        assert active_pointer.version == 1
        challenger = await repository.get_entry(
            workspace_id=workspace_id, registry_id=registry_ids[1]
        )
        assert challenger.state.value == "evaluated"
        stale_pointer_rejected = False
        try:
            await repository.activate(
                workspace_id=workspace_id,
                registry_id=registry_ids[1],
                actor_membership_id=membership_id,
                mfa_verified_at=datetime.now(UTC),
                expected_revision=challenger.revision,
                expected_pointer_version=0,
                expected_current_registry_id=None,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Version pointer cũ phải bị từ chối.",
            )
        except LearningModelRegistryConflict:
            stale_pointer_rejected = True
        assert stale_pointer_rejected
        second_active, pointer_two = await repository.activate(
            workspace_id=workspace_id,
            registry_id=registry_ids[1],
            actor_membership_id=membership_id,
            mfa_verified_at=datetime.now(UTC),
            expected_revision=challenger.revision,
            expected_pointer_version=1,
            expected_current_registry_id=registry_ids[0],
            idempotency_key=uuid4(),
            policy_version="learning-model-governance-v1",
            reason="Thay model PAPER theo version con trỏ hiện hành.",
        )
        assert second_active.state.value == "paper_active"
        assert pointer_two.version == 2
        first_retired = await repository.get_entry(
            workspace_id=workspace_id, registry_id=registry_ids[0]
        )
        assert first_retired.state.value == "retired"
        rolled_back, pointer_three = await repository.rollback(
            workspace_id=workspace_id,
            registry_id=registry_ids[0],
            actor_membership_id=membership_id,
            mfa_verified_at=datetime.now(UTC),
            expected_revision=first_retired.revision,
            expected_pointer_version=2,
            expected_current_registry_id=registry_ids[1],
            idempotency_key=uuid4(),
            policy_version="learning-model-governance-v1",
            reason="Khôi phục model PAPER đã active và được duyệt trước đó.",
        )
        assert rolled_back.state.value == "paper_active"
        assert pointer_three.version == 3
        active_pointer = await repository.get_active_pointer(workspace_id=workspace_id)
        assert active_pointer.registry_id == registry_ids[0]
        direct_retirement_rejected = False
        try:
            await repository.retire(
                workspace_id=workspace_id,
                registry_id=registry_ids[0],
                actor_membership_id=membership_id,
                mfa_verified_at=datetime.now(UTC),
                expected_revision=rolled_back.revision,
                idempotency_key=uuid4(),
                policy_version="learning-model-governance-v1",
                reason="Active model chỉ retire cùng pointer CAS.",
            )
        except LearningModelRegistryConflict:
            direct_retirement_rejected = True
        assert direct_retirement_rejected
        first_events = await repository.list_events(
            workspace_id=workspace_id, registry_id=registry_ids[0]
        )
        assert [event.sequence for event in first_events] == [1, 2, 3, 4, 5, 6]
        assert [event.action.value for event in first_events] == [
            "register",
            "evaluate",
            "approve",
            "activate",
            "retire",
            "rollback",
        ]
        all_entries = await repository.list_entries(workspace_id=workspace_id)
        assert len(all_entries) == 3

    other_workspace_id = uuid4()
    async with UnitOfWork(sessions, workspace_id=other_workspace_id) as unit:
        isolated = await LearningModelRegistryRepository(unit.session).list_entries(
            workspace_id=other_workspace_id
        )
        assert isolated == ()

    async def mutation_denied(sql: str, row_id: UUID) -> bool:
        try:
            async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                await unit.session.execute(text(sql), {"row_id": str(row_id)})
        except DBAPIError:
            return True
        return False

    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        first_event_id = await unit.session.scalar(
            select(LearningModelRegistryEventRecord.id)
            .where(LearningModelRegistryEventRecord.registry_id == registry_ids[0])
            .order_by(LearningModelRegistryEventRecord.sequence)
            .limit(1)
        )
        assert first_event_id is not None
        privileges = await unit.session.execute(
            text(
                "SELECT has_table_privilege(current_user, 'learning_model_registry', 'UPDATE'), "
                "has_table_privilege(current_user, 'learning_model_registry_event', 'DELETE')"
            )
        )
        registry_update, event_delete = privileges.one()
    assert not registry_update
    assert not event_delete
    assert await mutation_denied(
        "UPDATE learning_model_registry SET revision = revision WHERE id = :row_id",
        registry_ids[0],
    )
    assert await mutation_denied(
        "DELETE FROM learning_model_registry_event WHERE id = :row_id", first_event_id
    )
    await engine.dispose()
    return {
        "register_idempotency_and_conflict": True,
        "diagnostic_report_lineage_checked": True,
        "stale_revision_and_pointer_rejected": True,
        "approval_rejection_and_no_live_state": True,
        "activation_replacement_rollback_cas": True,
        "active_retire_rejected": True,
        "append_only_events_and_row_immutability": True,
        "workspace_isolation": True,
    }


async def _exercise_model_registry() -> dict[str, object]:
    prepared = await _prepare_model_registry_shadow()
    registry_ids = tuple(UUID(str(value)) for value in prepared["registry_ids"])
    return await _finish_model_registry_shadow(registry_ids)


async def main() -> None:
    if PHASE == "admin":
        result = await _admin_seed()
    elif PHASE == "source-admin-seed":
        result = await _source_admin_seed()
    elif PHASE == "source-item-seed":
        result = await _source_item_seed()
    elif PHASE == "source-manifest-seed":
        result = await _source_manifest_seed()
    elif PHASE == "source-rights-revoke":
        result = await _source_rights_revoke()
    elif PHASE == "source-evaluation-open":
        result = await _source_evaluation_open()
    elif PHASE == "source-training-claim":
        result = await _source_training_claim()
    elif PHASE == "source-run-rights-verify":
        result = await _source_run_rights_verify()
    elif PHASE == "source-run-rights-immutable":
        result = await _source_run_rights_immutable()
    elif PHASE == "source-rights-consume":
        result = await _source_rights_consume()
    elif PHASE == "source-rights-verify-unchanged":
        result = await _source_rights_verify_unchanged()
    elif PHASE == "source-receipt-denied":
        result = await _source_receipt_access_denied()
    elif PHASE == "source-receipt-immutable":
        result = await _source_receipt_immutable()
    elif PHASE == "builder":
        result = await _builder()
    elif PHASE == "training-admin-lifecycle":
        result = await _training_admin_lifecycle()
    elif PHASE == "denied":
        result = await _denied()
    elif PHASE == "attempt-admin":
        result = await _create_attempts()
    elif PHASE == "attempt-trainer":
        result = await _run_attempt_lifecycle()
    elif PHASE == "training-receipt":
        result = await _verify_training_receipt()
    elif PHASE == "pre-receipt-recovery":
        result = await _recover_before_receipt()
    elif PHASE == "post-receipt-recovery":
        result = await _recover_after_receipt()
    elif PHASE == "cancel-queued-job":
        result = await _request_queued_cancel()
    elif PHASE == "create-training-attempt":
        result = await _create_training_attempt()
    elif PHASE == "training-job-status":
        result = await _training_job_status()
    elif PHASE == "runtime-resource-limits":
        result = await _runtime_resource_limits()
    elif PHASE == "verify-cancelled-job":
        result = await _verify_cancelled_job()
    elif PHASE == "running-cancel-status":
        result = await _running_cancel_status()
    elif PHASE == "cancel-running-job":
        result = await _request_running_cancel()
    elif PHASE == "verify-running-cancelled-job":
        result = await _verify_running_cancelled_job()
    elif PHASE == "running-worker":
        result = await _run_running_cancel_worker()
    elif PHASE == "registry-report":
        result = await _create_registry_evaluation_report()
    elif PHASE == "model-registry-lifecycle":
        result = await _exercise_model_registry()
    else:
        raise ValueError(f"Probe phase không hỗ trợ: {PHASE}")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
