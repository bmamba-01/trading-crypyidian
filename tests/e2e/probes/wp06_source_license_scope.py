"""Probe PostgreSQL cho scope license replay TRAINING WP06."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    BacktestDatasetManifestRecord,
    BacktestDatasetReplayBundleRecord,
    BacktestRunDatasetRightsRecord,
    CandidateUniverseProofRecord,
    DatasetManifestRecord,
    LearningAttemptEventRecord,
    LearningReplayTrainingReceiptRecord,
    LearningSplitApprovalRecord,
    LearningSplitCandidateRecord,
    LearningTrainingAccessReceiptRecord,
    LearningTrainingAttemptRecord,
    NormalizedLearningDatasetRecord,
)
from trading_bot.adapters.database.repositories.backtest_outcomes import BacktestOutcomeRepository
from trading_bot.adapters.database.repositories.learning_training_batches import (
    LearningTrainingBatchAccessConflict,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService
from trading_bot.application.learning_cpu_trainer import CPU_TRAINER_VERSION
from trading_bot.application.learning_split_candidate import SPLIT_POLICY_VERSION
from trading_bot.application.learning_training_input import (
    LearningTrainingInputRequest,
    LearningTrainingInputService,
)
from trading_bot.application.source_policy_admin import SourceLicenseEvidence
from trading_bot.domain.learning.accounting import (
    LearningAttemptActor,
    LearningAttemptEvent,
    LearningAttemptState,
    LearningTrainingAttempt,
)
from trading_bot.domain.learning.approval import LearningSplitApproval
from trading_bot.domain.learning.dataset import (
    DatasetManifest,
    DatasetSample,
    DatasetSplit,
)
from trading_bot.domain.learning.resources import (
    LearningResourceRequest,
    assess_learning_resource_request,
)
from trading_bot.domain.research.license_scope import (
    SourceLicenseScope,
    scope_sha256,
)
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceRegistryEntry

PHASE = os.environ.get("WP06_SOURCE_SCOPE_PHASE", "seed")
STATE = json.loads(os.environ.get("WP06_SOURCE_SCOPE_STATE", "{}"))
WORKSPACE_ID = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
QA_SOURCE = "CRYPIDIAN_FIRST_PARTY_QA_FIXTURE"
QA_PROVIDER = "CRYPIDIAN_FIRST_PARTY_QA"
QA_TERMS = (
    "https://github.com/bmamba-01/trading-crypyidian/blob/dev/"
    "docs/research/wp06-first-party-qa-fixture.md"
)
PRODUCT = "OHLCV"
VENUE = "CRYPIDIAN"
CONTRACT = "SPOT"
FOLD_HASH = hashlib.sha256(b"wp06-license-scope-e2e-fold-plan-v1").hexdigest()


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


def _scope(
    *,
    source_id: str = QA_PROVIDER,
    provider: str = QA_PROVIDER,
    product: str = PRODUCT,
    venue: str = VENUE,
    contract: str = CONTRACT,
    symbols: tuple[str, ...] = ("BTCUSDT",),
    intervals: tuple[str, ...] = ("1h",),
    start: datetime | None = None,
    end: datetime | None = None,
    allowed_purposes: tuple[SourceUsePurpose, ...] = (
        SourceUsePurpose.BACKTEST_RESEARCH,
        SourceUsePurpose.TRAINING,
    ),
    allowed_outputs: tuple[str, ...] = (
        "aggregate_report",
        "derived_features",
        "trained_model",
    ),
    derived_use: tuple[str, ...] = (
        "aggregate_reporting",
        "feature_extraction",
        "model_training",
    ),
    retention: int | None = None,
) -> SourceLicenseScope:
    return SourceLicenseScope(
        source_id=source_id,
        provider=provider,
        product=product,
        venue=venue,
        contract=contract,
        symbols=symbols,
        intervals=intervals,
        data_start=start or datetime(2010, 1, 1, tzinfo=UTC),
        data_end=end or datetime(2040, 1, 1, tzinfo=UTC),
        allowed_purposes=allowed_purposes,
        allowed_outputs=allowed_outputs,
        derived_use=derived_use,
        max_retention_days=retention,
        attribution="Dữ liệu synthetic first-party chỉ dùng cho kiểm thử tích hợp.",
    )


def _licensed_transition(original):
    def transition(self, **kwargs):
        state = kwargs.get("state")
        if self.source_id == QA_PROVIDER and state in {
            SourcePolicyState.RIGHTS_PENDING,
            SourcePolicyState.VERIFIED,
        }:
            scope = _scope()
            kwargs.setdefault("evidence_ref", QA_TERMS)
            kwargs.setdefault("provider", scope.provider)
            kwargs.setdefault("product", scope.product)
            kwargs.setdefault("venue", scope.venue)
            kwargs.setdefault("contract", scope.contract)
            kwargs.setdefault("license_scope", scope)
            kwargs.setdefault("attribution", scope.attribution)
            if state is SourcePolicyState.VERIFIED:
                kwargs.setdefault(
                    "license_evidence_id", UUID("b1c2d3e4-f5a6-4789-8abc-def012345678")
                )
                kwargs.setdefault(
                    "license_digest", hashlib.sha256(b"synthetic-e2e-license").hexdigest()
                )
                kwargs.setdefault("license_scope_hash", scope_sha256(scope))
        return original(self, **kwargs)

    return transition


async def _append_with_license_evidence(original, repository, **kwargs):
    result = await original(repository, **kwargs)
    revision = kwargs["revision"]
    if (
        revision.policy.source_id == QA_PROVIDER
        and revision.state is SourcePolicyState.RIGHTS_PENDING
    ):
        body = b"synthetic-e2e-license"
        await repository.append_license_evidence(
            workspace_id=kwargs["workspace_id"],
            evidence=SourceLicenseEvidence(
                UUID("b1c2d3e4-f5a6-4789-8abc-def012345678"),
                QA_PROVIDER,
                revision.policy.policy_id,
                UUID("c1d2e3f4-a5b6-4789-8abc-def012345678"),
                QA_TERMS,
                hashlib.sha256(body).hexdigest(),
                len(body),
                "text/plain",
                _scope(),
                "fixture-license-uploader",
                revision.created_at,
            ),
        )
    return result


async def _seed_replay() -> dict[str, Any]:
    """Lưu replay synthetic và typed current license bằng migrator fixture."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    original_transition = SourceRegistryEntry.transition
    original_append = SourceRegistryRepository.append_revision
    sys.path.insert(0, "/e2e-tests")
    from e2e.probes.wp06_source_pinned_replay_persistence import run_probe

    try:
        with (
            patch.object(
                SourceRegistryEntry,
                "transition",
                _licensed_transition(original_transition),
            ),
            patch.object(
                SourceRegistryRepository,
                "append_revision",
                lambda repository, **kwargs: _append_with_license_evidence(
                    original_append, repository, **kwargs
                ),
            ),
        ):
            seed_result = await run_probe(scorer_seed=True)
        workspace_id = UUID(seed_result["workspace_id"])
        async with sessions() as session, session.begin():
            await _set_workspace(session)
            bundle = await session.scalar(
                select(BacktestDatasetReplayBundleRecord).where(
                    BacktestDatasetReplayBundleRecord.workspace_id == workspace_id,
                    BacktestDatasetReplayBundleRecord.bundle_hash == seed_result["bundle_hash"],
                )
            )
            assert bundle is not None
            canonical_payload = json.loads(bundle.canonical_bundle)
            diagnostics = {
                "json_matches_canonical": bundle.bundle_json == canonical_payload,
                "key_count": len(bundle.bundle_json),
                "schema_matches": bundle.bundle_json.get("schema_version") == bundle.schema_version,
                "workspace_matches": bundle.bundle_json.get("workspace_id") == str(workspace_id),
                "symbol_matches": bundle.bundle_json.get("symbol") == bundle.symbol,
                "evaluation_start_matches": datetime.fromisoformat(
                    bundle.bundle_json["evaluation_start"]
                )
                == bundle.evaluation_start,
                "range_end_matches": datetime.fromisoformat(bundle.bundle_json["range_end"])
                == bundle.range_end,
                "provider_matches": bundle.bundle_json.get("rights_provider")
                == bundle.rights_provider,
                "purpose_matches": bundle.bundle_json.get("rights_purpose")
                == bundle.rights_purpose,
                "backtest_purpose_expected": bundle.rights_purpose == "BACKTEST_RESEARCH",
                "fingerprint_matches": bundle.bundle_json.get("policy_fingerprint")
                == bundle.policy_fingerprint,
                "hash_matches": hashlib.sha256(bundle.canonical_bundle.encode()).hexdigest()
                == bundle.bundle_hash,
                "component_array": isinstance(bundle.bundle_json.get("components"), list)
                and bool(bundle.bundle_json["components"]),
            }
            children = await session.scalars(
                select(BacktestDatasetManifestRecord).where(
                    BacktestDatasetManifestRecord.workspace_id == workspace_id,
                    BacktestDatasetManifestRecord.dataset_hash.in_(
                        [item["dataset_hash"] for item in bundle.bundle_json["components"]]
                    ),
                )
            )
            child_diagnostics = [
                {
                    "dataset_hash": child.dataset_hash,
                    "schema_version": child.manifest_json.get("schema_version"),
                    "key_count": len(child.manifest_json),
                    "json_matches_canonical": child.manifest_json
                    == json.loads(child.canonical_manifest),
                    "purpose": child.manifest_json.get("purpose"),
                    "provider": child.manifest_json.get("provider"),
                    "bar_identity_hash_present": bool(child.manifest_json.get("bar_identity_hash")),
                    "status_ready": child.status == "READY",
                    "symbol_matches": child.symbol
                    == bundle.symbol
                    == child.manifest_json.get("symbol"),
                    "interval_matches": child.interval
                    == next(
                        item["interval"]
                        for item in bundle.bundle_json["components"]
                        if item["dataset_hash"] == child.dataset_hash
                    )
                    == child.manifest_json.get("interval"),
                    "evaluation_start_matches": child.evaluation_start
                    == bundle.evaluation_start
                    == datetime.fromisoformat(child.manifest_json["evaluation_start"]),
                    "range_end_matches": child.range_end
                    == bundle.range_end
                    == datetime.fromisoformat(child.manifest_json["range_end"]),
                    "warmup_start_matches": child.warmup_start
                    == datetime.fromisoformat(child.manifest_json["warmup_start"]),
                    "prepared_at_matches": child.prepared_at
                    == datetime.fromisoformat(child.manifest_json["prepared_at"]),
                    "policy_matches": child.policy_fingerprint
                    == bundle.policy_fingerprint
                    == child.manifest_json.get("policy_fingerprint"),
                    "rights_workspace_matches": child.manifest_json.get("rights_workspace_id")
                    == str(workspace_id),
                    "policy_fingerprint_matches": child.manifest_json.get("policy_fingerprint")
                    == bundle.policy_fingerprint,
                    "bar_counts_match": child.manifest_json.get("bar_count")
                    == child.manifest_json.get("expected_bar_count"),
                    "bundle_component_hash_matches": next(
                        item["bars_content_hash"]
                        for item in bundle.bundle_json["components"]
                        if item["dataset_hash"] == child.dataset_hash
                    )
                    == child.manifest_json.get("bars_content_hash"),
                    "rights_rows": [
                        {
                            "provider": receipt.provider,
                            "purpose": receipt.purpose,
                            "interval": receipt.interval,
                            "run_id": str(receipt.run_id),
                            "child_hash_matches": receipt.child_dataset_hash == child.dataset_hash,
                            "bundle_hash_matches": receipt.bundle_hash == bundle.bundle_hash,
                            "policy_fingerprint_matches": receipt.policy_fingerprint
                            == bundle.policy_fingerprint,
                        }
                        for receipt in await session.scalars(
                            select(BacktestRunDatasetRightsRecord).where(
                                BacktestRunDatasetRightsRecord.workspace_id == workspace_id,
                                BacktestRunDatasetRightsRecord.bundle_hash == bundle.bundle_hash,
                                BacktestRunDatasetRightsRecord.child_dataset_hash
                                == child.dataset_hash,
                            )
                        )
                    ],
                }
                for child in children
            ]
            sql_result = await session.execute(
                text(
                    """
                    SELECT pg_catalog.jsonb_build_object(
                        'row_present', child.workspace_id IS NOT NULL,
                        'status_ready', child.status = 'READY',
                        'component_interval_matches', child.interval = component.value->>'interval',
                        'bundle_symbol_matches', child.symbol = bundle.symbol,
                        'evaluation_start_matches', child.evaluation_start = bundle.evaluation_start
                            AND (child.manifest_json->>'evaluation_start')::timestamptz
                                IS NOT DISTINCT FROM child.evaluation_start,
                        'range_end_matches', child.range_end = bundle.range_end
                            AND (child.manifest_json->>'range_end')::timestamptz
                                IS NOT DISTINCT FROM child.range_end,
                        'json_matches_canonical',
                            child.manifest_json IS NOT DISTINCT FROM
                                child.canonical_manifest::jsonb,
                        'manifest_key_count',
                            (SELECT count(*) FROM pg_catalog.jsonb_object_keys(
                                child.manifest_json)),
                        'manifest_hash_matches', pg_catalog.encode(pg_catalog.sha256(
                            pg_catalog.convert_to(public.wp06_license_canonical_json(
                                child.manifest_json - 'dataset_hash'), 'UTF8')), 'hex')
                            = child.dataset_hash,
                        'schema_version_matches', child.manifest_json->>'schema_version' = '4',
                        'dataset_hash_matches', child.manifest_json->>'dataset_hash'
                            = child.dataset_hash,
                        'status_matches', child.manifest_json->>'status' = child.status,
                        'symbol_matches', child.manifest_json->>'symbol' = child.symbol,
                        'interval_matches', child.manifest_json->>'interval' = child.interval,
                        'warmup_start_matches', (child.manifest_json->>'warmup_start')::timestamptz
                            IS NOT DISTINCT FROM child.warmup_start,
                        'prepared_at_matches', (child.manifest_json->>'prepared_at')::timestamptz
                            IS NOT DISTINCT FROM child.prepared_at,
                        'policy_fingerprint_matches', child.manifest_json->>'policy_fingerprint'
                            = child.policy_fingerprint
                            AND child.policy_fingerprint = bundle.policy_fingerprint,
                        'bar_counts_match', child.manifest_json->>'bar_count'
                            = child.manifest_json->>'expected_bar_count',
                        'provider_matches', child.manifest_json->>'provider'
                            = bundle.rights_provider,
                        'rights_workspace_matches', child.manifest_json->>'rights_workspace_id'
                            = bundle.workspace_id::text,
                        'purpose_matches', child.manifest_json->>'purpose' = 'BACKTEST_RESEARCH',
                        'bar_identity_hash_present',
                            child.manifest_json->>'bar_identity_hash' IS NOT NULL,
                        'component_bar_hash_matches', component.value->>'bars_content_hash'
                            = child.manifest_json->>'bars_content_hash',
                        'rights_row_matches', EXISTS (
                            SELECT 1 FROM public.backtest_run_dataset_rights AS rights
                             WHERE rights.workspace_id = bundle.workspace_id
                               AND rights.bundle_hash = bundle.bundle_hash
                               AND rights.child_dataset_hash = child.dataset_hash
                               AND rights.interval = component.value->>'interval'
                               AND rights.provider = bundle.rights_provider
                               AND rights.purpose = 'BACKTEST_RESEARCH'
                               AND rights.policy_fingerprint = bundle.policy_fingerprint
                        )
                    )
                      FROM public.backtest_dataset_replay_bundle AS bundle
                      CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                          bundle.bundle_json->'components') AS component(value)
                      LEFT JOIN public.backtest_dataset_manifest AS child
                        ON child.workspace_id = bundle.workspace_id
                       AND child.dataset_hash = component.value->>'dataset_hash'
                     WHERE bundle.workspace_id = :workspace_id
                       AND bundle.bundle_hash = :bundle_hash
                    """
                ),
                {"workspace_id": str(workspace_id), "bundle_hash": bundle.bundle_hash},
            )
            child_sql_diagnostics = sql_result.scalar_one()
            evidence_result = await session.execute(
                text(
                    """
                    SELECT pg_catalog.jsonb_build_object(
                        'row_present', evidence.id IS NOT NULL,
                        'digest_matches', evidence.digest = policy.license_digest,
                        'scope_hash_matches_policy',
                            evidence.scope_hash = policy.license_scope_hash,
                        'reference_matches', evidence.evidence_ref = policy.evidence_ref,
                        'scope_matches', evidence.license_scope = policy.license_scope,
                        'canonical_hash_matches', evidence.scope_hash = pg_catalog.encode(
                            pg_catalog.sha256(pg_catalog.convert_to(evidence.canonical_scope,'UTF8')),'hex'),
                        'canonical_scope_matches', evidence.canonical_scope =
                            public.wp06_license_canonical_json(evidence.license_scope),
                        'scope_valid', public.wp06_license_scope_valid(evidence.license_scope),
                        'policy_has_training', policy.allowed_purposes @> '["training"]'::jsonb,
                        'identity_complete', policy.provider IS NOT NULL
                            AND policy.product IS NOT NULL
                            AND policy.venue IS NOT NULL AND policy.contract IS NOT NULL,
                        'reviewed_before_now', policy.reviewed_at <= pg_catalog.clock_timestamp(),
                        'approved_by_distinct', policy.approved_by IS NOT NULL
                            AND policy.approved_by <> policy.actor_id,
                        'evidence_origin_is_pending', EXISTS (
                            SELECT 1 FROM public.source_policy_revision AS pending
                             WHERE pending.workspace_id=policy.workspace_id
                               AND pending.source_id=policy.source_id
                               AND pending.policy_id=evidence.policy_id
                               AND pending.state='rights_pending'
                               AND pending.evidence_ref=evidence.evidence_ref
                               AND pending.license_scope=evidence.license_scope
                        ),
                        'scope_keys', (SELECT pg_catalog.array_agg(key ORDER BY key COLLATE "C")
                            FROM pg_catalog.jsonb_object_keys(evidence.license_scope) AS item(key)),
                        'scope_json', evidence.canonical_scope,
                        'scope_json_db', public.wp06_license_canonical_json(evidence.license_scope)
                    )
                      FROM public.source_policy_revision AS policy
                      LEFT JOIN public.source_license_evidence AS evidence
                        ON evidence.workspace_id=policy.workspace_id
                       AND evidence.source_id=policy.source_id
                       AND evidence.id=policy.license_evidence_id
                     WHERE policy.workspace_id=:workspace_id AND policy.source_id=:source_id
                     ORDER BY policy.sequence DESC LIMIT 1
                    """
                ),
                {"workspace_id": str(workspace_id), "source_id": QA_PROVIDER},
            )
            evidence_diagnostics = evidence_result.scalar_one()
        return {
            "workspace_id": seed_result["workspace_id"],
            "bundle_hash": seed_result["bundle_hash"],
            "trial_record_id": seed_result["trial_record_id"],
            "bundle_diagnostics": diagnostics,
            "child_diagnostics": child_diagnostics,
            "child_sql_diagnostics": child_sql_diagnostics,
            "license_evidence_diagnostics": evidence_diagnostics,
        }
    finally:
        await engine.dispose()
        sys.path.remove("/e2e-tests")


async def _prepare_manifest() -> dict[str, Any]:
    """Tạo proof, manifest và lineage bằng đúng candidate-builder role."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        workspace_id = UUID(STATE["workspace_id"])
        trial_id = UUID(STATE["trial_record_id"])
        proof = await CandidateUniverseProofService(sessions).build_decision_opportunity(
            workspace_id=workspace_id,
            trial_record_id=trial_id,
        )

        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            bundle_row = await session.scalar(
                select(BacktestDatasetReplayBundleRecord).where(
                    BacktestDatasetReplayBundleRecord.workspace_id == workspace_id,
                    BacktestDatasetReplayBundleRecord.bundle_hash == STATE["bundle_hash"],
                )
            )
            assert bundle_row is not None
            outcomes = await BacktestOutcomeRepository(session).load_trade_outcome_rows(
                workspace_id=workspace_id,
                trial_record_id=trial_id,
            )
            outcome = next(row for row in outcomes.rows if row.trade.has_entry_provenance)
            lineage = await BacktestOutcomeRepository(session).load_training_bundle_lineage(
                workspace_id=workspace_id,
                trial_record_id=trial_id,
                outcomes=outcomes.rows,
            )
            trade = outcome.trade
            decision_at = trade.entry_decision_at
            assert decision_at is not None
            assert trade.entry_features_hash is not None
            data_start = decision_at - timedelta(days=1)
            data_end = trade.closed_at + timedelta(days=1)
            manifest = DatasetManifest(
                workspace_id=workspace_id,
                query_hash=hashlib.sha256(b"wp06-source-license-query").hexdigest(),
                symbols=("BTCUSDT",),
                data_start=data_start,
                data_end=data_end,
                as_of=data_end,
                feature_schema_version="wp06-e2e-feature-schema-v1",
                feature_version="wp06-e2e-feature-v1",
                label_version="wp06-e2e-label-v1",
                sample_schema_version="wp06-e2e-sample-v1",
                split_policy_version=SPLIT_POLICY_VERSION,
                samples=(
                    DatasetSample(
                        sample_id=str(outcome.record_id),
                        decision_at=decision_at,
                        feature_available_at=decision_at,
                        outcome_start=trade.opened_at,
                        outcome_end=trade.closed_at,
                        feature_snapshot_hash=trade.entry_features_hash,
                        outcome_id=str(outcome.record_id),
                        outcome_hash=outcome.outcome_hash,
                        label_hash=hashlib.sha256(b"wp06-e2e-label").hexdigest(),
                        label_value=(trade.net_pnl > 0) - (trade.net_pnl < 0),
                        label_version="wp06-e2e-label-v1",
                        split=DatasetSplit.TRAIN,
                        source_dependent=True,
                        source_rights_hashes=(),
                        source_revision_refs=(),
                        source_replay_bundle_hash=lineage.bundle_hash,
                        source_use_lineage=lineage.source_use_lineage,
                    ),
                ),
                exclusions=(),
                holdout_sealed_at=None,
                retention_policy_ref="fixture-retain-metadata-only",
                manifest_version="dataset-manifest-v4",
            )
            proof_row = await session.scalar(
                select(CandidateUniverseProofRecord).where(
                    CandidateUniverseProofRecord.workspace_id == workspace_id,
                    CandidateUniverseProofRecord.id == proof.record_id,
                )
            )
            assert proof_row is not None
            source_lineage = [item.canonical_payload for item in lineage.source_use_lineage]
            payload = json.loads(manifest.canonical_json)
            payload["manifest_version"] = "dataset-manifest-v5"
            payload["source_use_lineage"] = source_lineage
            lineage_set_hash = _source_lineage_set_hash(source_lineage)
            for item in payload["samples"]:
                item["source_replay_bundle_hash"] = lineage.bundle_hash
                item["source_use_lineage_set_hash"] = lineage_set_hash
                item.pop("source_use_lineage", None)
            manifest_hash, normalized_id = await _persist_manifest(
                session,
                payload,
                proof_id=proof_row.id,
                proof_hash=proof_row.proof_hash,
                trial_id=trial_id,
            )

        return STATE | {
            "proof_record_id": str(proof.record_id),
            "manifest_hash": manifest_hash,
            "normalized_dataset_id": str(normalized_id),
            "fold_plan_hash": FOLD_HASH,
            "outcome_id": str(outcome.record_id),
            "manifest": payload,
            "source_lineage_set_hash": lineage_set_hash,
            "expected_source_receipt_count": len(lineage.source_use_lineage),
        }
    finally:
        await engine.dispose()


async def _set_workspace(session) -> None:
    await session.execute(
        text("SELECT pg_catalog.set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(WORKSPACE_ID)},
    )


def _source_lineage_set_hash(lineage: list[dict[str, object]]) -> str:
    ordered = sorted(lineage, key=lambda item: str(item["child_dataset_hash"]))
    canonical = json.dumps(
        ordered, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _expect_denied(session, statement: str, params: dict[str, object]) -> str:
    try:
        async with session.begin_nested():
            await session.execute(text(statement), params)
    except DBAPIError as error:
        return str(error.orig)
    raise AssertionError(f"Cơ sở dữ liệu đáng lẽ phải từ chối: {statement}")


async def _authorize() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    bundle_hash = STATE["bundle_hash"]
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        rows = (
            await session.execute(
                text(
                    "SELECT child_dataset_hash, current_policy_fingerprint, scope_hash "
                    "FROM public.wp06_authorize_replay_training_bundle("
                    ":workspace_id, :bundle_hash, 'training')"
                ),
                {"workspace_id": str(WORKSPACE_ID), "bundle_hash": bundle_hash},
            )
        ).all()
        assert len(rows) == STATE["expected_source_receipt_count"]
        await _expect_denied(
            session,
            "SELECT * FROM public.wp06_authorize_replay_training_bundle("
            ":workspace_id, :bundle_hash, 'evaluation')",
            {"workspace_id": str(WORKSPACE_ID), "bundle_hash": bundle_hash},
        )
        await _expect_denied(
            session,
            "SELECT * FROM public.wp06_authorize_replay_training_bundle("
            ":workspace_id, :bundle_hash, 'training')",
            {"workspace_id": str(uuid4()), "bundle_hash": bundle_hash},
        )
    await engine.dispose()
    return {
        "exact_training_scope_allowed": True,
        "evaluation_operation_denied_to_builder": True,
        "cross_workspace_denied": True,
    }


def _manifest_payload(
    *,
    version: str,
    source_dependent: bool,
    outcome_id: str,
    source_use_lineage: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=2)
    end = now + timedelta(days=2)
    sample: dict[str, object] = {
        "sample_id": f"{version}-{uuid4().hex[:8]}",
        "decision_at": (now - timedelta(hours=1)).isoformat(),
        "feature_available_at": (now - timedelta(hours=1)).isoformat(),
        "outcome_start": now.isoformat(),
        "outcome_end": (now + timedelta(hours=1)).isoformat(),
        "feature_snapshot_hash": hashlib.sha256(b"feature").hexdigest(),
        "outcome_id": outcome_id,
        "outcome_hash": hashlib.sha256(b"outcome").hexdigest(),
        "label_hash": hashlib.sha256(b"label").hexdigest(),
        "label_value": 1,
        "label_version": "wp06-e2e-label-v1",
        "split": "development" if version == "dataset-manifest-v2" else "train",
        "source_dependent": source_dependent,
        "source_rights_hashes": [hashlib.sha256(b"legacy-rights").hexdigest()]
        if source_dependent and version != "dataset-manifest-v4"
        else [],
        "source_revision_refs": ["legacy-source-revision"]
        if source_dependent and version != "dataset-manifest-v4"
        else [],
    }
    if version == "dataset-manifest-v3":
        sample["source_references"] = []
    if version in {"dataset-manifest-v4", "dataset-manifest-v5"}:
        sample["source_replay_bundle_hash"] = STATE["bundle_hash"]
        if version == "dataset-manifest-v4":
            sample["source_use_lineage"] = source_use_lineage or []
        else:
            sample["source_use_lineage_set_hash"] = _source_lineage_set_hash(
                source_use_lineage or []
            )
    payload = {
        "manifest_version": version,
        "workspace_id": str(WORKSPACE_ID),
        "query_hash": hashlib.sha256(f"query:{uuid4()}".encode()).hexdigest(),
        "symbols": ["BTCUSDT"],
        "data_range": [start.isoformat(), end.isoformat()],
        "as_of": end.isoformat(),
        "feature_schema_version": "wp06-e2e-feature-schema-v1",
        "feature_version": "wp06-e2e-feature-v1",
        "label_version": "wp06-e2e-label-v1",
        "sample_schema_version": "wp06-e2e-sample-v1",
        "split_policy_version": SPLIT_POLICY_VERSION,
        "samples": [sample],
        "exclusions": [],
        "holdout_sealed_at": end.isoformat(),
        "retention_policy_ref": "fixture-retain-metadata-only",
    }
    if version == "dataset-manifest-v5":
        payload["source_use_lineage"] = source_use_lineage or []
    return payload


async def _persist_manifest(
    session,
    payload: dict[str, object],
    *,
    proof_id: UUID,
    proof_hash: str,
    trial_id: UUID,
    fold_hash: str = FOLD_HASH,
) -> tuple[str, UUID]:
    """Ghi manifest fixture, candidate và normalized header theo thứ tự FK/trigger."""
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    manifest_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    raw_range = payload["data_range"]
    assert isinstance(raw_range, list)
    assert len(raw_range) == 2
    data_start = datetime.fromisoformat(raw_range[0])
    data_end = datetime.fromisoformat(raw_range[1])
    samples = payload["samples"]
    assert isinstance(samples, list)
    version = payload["manifest_version"]
    assert isinstance(version, str)
    session.add(
        LearningSplitCandidateRecord(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            trial_record_id=trial_id,
            proof_record_id=proof_id,
            proof_hash=proof_hash,
            manifest_hash=manifest_hash,
            split_policy_version=SPLIT_POLICY_VERSION,
            fold_plan_hash=fold_hash,
            evaluation_cutoff=data_end,
            preview={"fixture": True},
            canonical_manifest=canonical,
            canonical_configuration="{}",
        )
    )
    await session.flush()
    approval_hash = _seed_synthetic_split_approval(manifest_hash)
    session.add(
        DatasetManifestRecord(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            manifest_hash=manifest_hash,
            query_hash=payload["query_hash"],
            manifest_version=version,
            data_start=data_start,
            data_end=data_end,
            as_of=datetime.fromisoformat(payload["as_of"]),
            feature_schema_version=payload["feature_schema_version"],
            feature_version=payload["feature_version"],
            label_version=payload["label_version"],
            sample_schema_version=payload["sample_schema_version"],
            split_policy_version=SPLIT_POLICY_VERSION,
            sample_count=len(samples),
            exclusion_count=0,
            holdout_sealed_at=(
                datetime.fromisoformat(payload["holdout_sealed_at"])
                if isinstance(payload["holdout_sealed_at"], str)
                else None
            ),
            retention_policy_ref=payload["retention_policy_ref"],
            manifest=payload,
            canonical_manifest=canonical,
        )
    )
    normalized_id = uuid4()
    session.add(
        NormalizedLearningDatasetRecord(
            id=normalized_id,
            workspace_id=WORKSPACE_ID,
            manifest_hash=manifest_hash,
            split_policy_version=SPLIT_POLICY_VERSION,
            fold_plan_hash=fold_hash,
            approval_hash=approval_hash,
            content_hash=hashlib.sha256(f"normalized:{manifest_hash}".encode()).hexdigest(),
            sample_count=max(len(samples), 1),
            assignment_count=1,
        )
    )
    await session.flush()
    return manifest_hash, normalized_id


def _seed_synthetic_split_approval(manifest_hash: str) -> str:
    """Nối approval fixture tới candidate; không đại diện cho duyệt sản phẩm thật."""
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_admin",
            password=Path(os.environ["WP06_E2E_SUPERUSER_PASSWORD_FILE"]).read_text().strip(),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    now = datetime.now(UTC)
    approval = LearningSplitApproval(
        workspace_id=WORKSPACE_ID,
        manifest_hash=manifest_hash,
        split_policy_version=SPLIT_POLICY_VERSION,
        fold_plan_hash=FOLD_HASH,
        actor_membership_id=uuid4(),
        idempotency_key=uuid4(),
        reason="Approval fixture chỉ phục vụ tích hợp E2E.",
        approved_at=now,
        mfa_verified_at=now,
    )
    record = LearningSplitApprovalRecord.from_domain(approval)
    record.id = uuid4()
    values = {
        column.name: getattr(record, column.name)
        for column in record.__table__.columns
        if column.name != "created_at"
    }
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(insert(record.__table__).values(**values))
    finally:
        engine.dispose()
    return record.approval_hash


async def _seed_negative_manifests(state: dict[str, Any]) -> dict[str, Any]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        proof_row = await session.scalar(
            select(CandidateUniverseProofRecord).where(
                CandidateUniverseProofRecord.workspace_id == WORKSPACE_ID,
                CandidateUniverseProofRecord.id == UUID(state["proof_record_id"]),
            )
        )
        assert proof_row is not None
        proof_id, proof_hash = proof_row.id, proof_row.proof_hash
        trial_id = UUID(state["trial_record_id"])

        source_lineage = state["manifest"]["source_use_lineage"]
        source_lineage = json.loads(json.dumps(source_lineage))
        source_v5 = json.loads(json.dumps(state["manifest"]))
        source_free = _manifest_payload(
            version="dataset-manifest-v2",
            source_dependent=False,
            outcome_id="wp02-source-free-outcome-not-backed-by-replay",
        )
        forged = _manifest_payload(
            version="dataset-manifest-v2",
            source_dependent=False,
            outcome_id=state["outcome_id"],
        )
        source_free_hash, source_free_id = await _persist_manifest(
            session,
            source_free,
            proof_id=proof_id,
            proof_hash=proof_hash,
            trial_id=trial_id,
        )
        forged_hash, forged_id = await _persist_manifest(
            session,
            forged,
            proof_id=proof_id,
            proof_hash=proof_hash,
            trial_id=trial_id,
        )

        legacy: dict[str, dict[str, str]] = {}
        for version in (
            "dataset-manifest-v1",
            "dataset-manifest-v2",
            "dataset-manifest-v3",
            "dataset-manifest-v4",
        ):
            payload = _manifest_payload(
                version=version,
                source_dependent=True,
                outcome_id="legacy-source-backed-outcome",
            )
            manifest_hash, normalized_id = await _persist_manifest(
                session,
                payload,
                proof_id=proof_id,
                proof_hash=proof_hash,
                trial_id=trial_id,
            )
            legacy[version] = {
                "manifest_hash": manifest_hash,
                "normalized_dataset_id": str(normalized_id),
            }

        variants: dict[str, dict[str, str]] = {}
        lineage = source_lineage[0]
        assert isinstance(lineage, dict)
        missing_payload = json.loads(json.dumps(source_v5))
        missing_payload["source_use_lineage"] = []
        missing_payload["samples"][0]["source_use_lineage_set_hash"] = _source_lineage_set_hash([])
        missing_hash, missing_id = await _persist_manifest(
            session,
            missing_payload,
            proof_id=proof_id,
            proof_hash=proof_hash,
            trial_id=trial_id,
        )
        variants["missing_child"] = {
            "manifest_hash": missing_hash,
            "normalized_dataset_id": str(missing_id),
            "payload": missing_payload,
        }

        extra_payload = json.loads(json.dumps(source_v5))
        extra_lineage = dict(lineage)
        extra_lineage["child_dataset_hash"] = hashlib.sha256(b"unexpected-child").hexdigest()
        extra_payload["source_use_lineage"] = [lineage, extra_lineage]
        extra_payload["samples"][0]["source_use_lineage_set_hash"] = _source_lineage_set_hash(
            [lineage, extra_lineage]
        )
        extra_hash, extra_id = await _persist_manifest(
            session,
            extra_payload,
            proof_id=proof_id,
            proof_hash=proof_hash,
            trial_id=trial_id,
        )
        variants["extra_child"] = {
            "manifest_hash": extra_hash,
            "normalized_dataset_id": str(extra_id),
            "payload": extra_payload,
        }

        substitute_payload = json.loads(json.dumps(source_v5))
        substitute_lineage = dict(lineage)
        supplied_ids = list(substitute_lineage["market_bar_ids"])
        supplied_ids[0] = f"market-bar-record:{uuid4()}"
        substitute_lineage["market_bar_ids"] = supplied_ids
        substitute_payload["source_use_lineage"] = [substitute_lineage]
        substitute_payload["samples"][0]["source_use_lineage_set_hash"] = _source_lineage_set_hash(
            [substitute_lineage]
        )
        substitute_hash, substitute_id = await _persist_manifest(
            session,
            substitute_payload,
            proof_id=proof_id,
            proof_hash=proof_hash,
            trial_id=trial_id,
        )
        variants["market_bar_substitution"] = {
            "manifest_hash": substitute_hash,
            "normalized_dataset_id": str(substitute_id),
            "payload": substitute_payload,
        }
    await engine.dispose()
    return state | {
        "source_free_manifest_hash": source_free_hash,
        "source_free_dataset_id": str(source_free_id),
        "forged_manifest_hash": forged_hash,
        "forged_dataset_id": str(forged_id),
        "legacy_manifests": legacy,
        "lineage_variants": variants,
    }


async def _call_normalize(
    session,
    *,
    manifest_hash: str,
    normalized_dataset_id: str,
    fold_plan_hash: str = FOLD_HASH,
) -> None:
    await session.execute(
        text(
            "SELECT public.wp06_record_replay_training_receipts("
            "CAST(:workspace AS uuid), CAST(:manifest AS text), "
            "CAST(:dataset AS uuid), CAST(:fold AS text))"
        ),
        {
            "workspace": str(WORKSPACE_ID),
            "manifest": manifest_hash,
            "dataset": normalized_dataset_id,
            "fold": fold_plan_hash,
        },
    )


async def _normalize() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        rights_diagnostics = _training_rights_diagnostics()
        assert rights_diagnostics["receipt_set_matches"] is True, rights_diagnostics
        await _call_normalize(
            session,
            manifest_hash=STATE["manifest_hash"],
            normalized_dataset_id=STATE["normalized_dataset_id"],
        )
        await _call_normalize(
            session,
            manifest_hash=STATE["manifest_hash"],
            normalized_dataset_id=STATE["normalized_dataset_id"],
        )
        await _call_normalize(
            session,
            manifest_hash=STATE["source_free_manifest_hash"],
            normalized_dataset_id=STATE["source_free_dataset_id"],
        )
        forged_error = await _expect_denied(
            session,
            "SELECT public.wp06_record_replay_training_receipts("
            "CAST(:workspace AS uuid), CAST(:manifest AS text), "
            "CAST(:dataset AS uuid), CAST(:fold AS text))",
            {
                "workspace": str(WORKSPACE_ID),
                "manifest": STATE["forged_manifest_hash"],
                "dataset": STATE["forged_dataset_id"],
                "fold": FOLD_HASH,
            },
        )
        legacy_denied = True
        for item in STATE["legacy_manifests"].values():
            error = await _expect_denied(
                session,
                "SELECT public.wp06_record_replay_training_receipts("
                "CAST(:workspace AS uuid), CAST(:manifest AS text), "
                "CAST(:dataset AS uuid), CAST(:fold AS text))",
                {
                    "workspace": str(WORKSPACE_ID),
                    "manifest": item["manifest_hash"],
                    "dataset": item["normalized_dataset_id"],
                    "fold": FOLD_HASH,
                },
            )
            legacy_denied = legacy_denied and "LEGACY_LINEAGE_DENIED" in error
        assert "SOURCE_FREE_FORGED" in forged_error
    await engine.dispose()
    attempt_id = await _seed_queued_attempt()
    return {
        "attempt_id": str(attempt_id),
        "rights_receipt_set_valid": True,
        "source_receipt_count": STATE["expected_source_receipt_count"],
        "retry_is_idempotent": True,
        "source_free_wp02_allowed": True,
        "forged_source_free_outcome_denied": True,
        "legacy_v1_v3_source_backed_denied": legacy_denied,
    }


async def _check_scope_variants_phase() -> dict[str, object]:
    """Gọi normalization gateway đúng quyền để kiểm các manifest tampered."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        result = await _check_scope_variants(session)
    await engine.dispose()
    return result


async def _check_evaluator_gate_acl_phase() -> dict[str, bool]:
    """Xác nhận evaluator không được gọi trực tiếp internal rights gate."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        error = await _expect_denied(
            session,
            "SELECT public.wp06_assert_training_source_rights("
            "CAST(:workspace AS uuid), CAST(:manifest_hash AS text), CAST(:manifest AS jsonb), "
            "CAST(:dataset AS uuid), CAST(:fold AS text), 'evaluation', CAST(:access AS uuid))",
            {
                "workspace": str(WORKSPACE_ID),
                "manifest_hash": STATE["manifest_hash"],
                "manifest": json.dumps(STATE["manifest"]),
                "dataset": STATE["normalized_dataset_id"],
                "fold": FOLD_HASH,
                "access": str(uuid4()),
            },
        )
    await engine.dispose()
    return {"direct_gate_denied_to_evaluator": "permission denied for function" in error}


def _training_rights_diagnostics() -> dict[str, Any]:
    """Đối chiếu từng receipt replay với đúng predicate của gate SQL trước khi normalize."""
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_admin",
            password=Path(os.environ["WP06_E2E_SUPERUSER_PASSWORD_FILE"]).read_text().strip(),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text(
                    """
            WITH target_trial AS (
                SELECT trial.run_id
                  FROM public.backtest_trial AS trial
                 WHERE trial.workspace_id = :workspace_id
                   AND trial.id = CAST(:trial_id AS uuid)
            ), target_bundle AS (
                SELECT bundle.bundle_hash, bundle.bundle_json
                  FROM public.backtest_dataset_replay_bundle AS bundle
                 WHERE bundle.workspace_id = :workspace_id
                   AND bundle.bundle_hash = :bundle_hash
            ), components AS (
                SELECT component.value->>'dataset_hash' AS child_hash,
                       component.value->>'interval' AS interval_name
                  FROM target_bundle AS bundle
                  CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                      bundle.bundle_json->'components') AS component(value)
            ), matching AS (
                SELECT component.child_hash,
                       (
                           SELECT pg_catalog.jsonb_build_object(
                               'receipt_present', receipt.id IS NOT NULL,
                               'canonical_json_matches', receipt.evidence_json IS NOT DISTINCT FROM
                                   receipt.canonical_evidence::jsonb,
                               'canonical_key_count_matches',
                                   (SELECT count(*) FROM pg_catalog.jsonb_object_keys(
                                       receipt.evidence_json)) = 13,
                               'canonical_text_matches', receipt.canonical_evidence =
                                   public.wp06_compact_canonical_json(
                                       receipt.canonical_evidence::json),
                               'evidence_digest_matches', pg_catalog.encode(pg_catalog.sha256(
                                   pg_catalog.convert_to(receipt.canonical_evidence, 'UTF8')),
                                   'hex') = receipt.evidence_hash,
                               'schema_matches', receipt.evidence_json->>'schema_version' = '1',
                               'workspace_matches', receipt.evidence_json->>'workspace_id' =
                                   :workspace_id_text,
                               'run_matches', receipt.evidence_json->>'run_id' =
                                   receipt.run_id::text,
                               'bundle_matches', receipt.evidence_json->>'bundle_hash' =
                                   :bundle_hash,
                               'child_matches', receipt.evidence_json->>'child_dataset_hash' =
                                   component.child_hash,
                               'interval_matches', receipt.evidence_json->>'interval' =
                                   component.interval_name,
                               'provider_matches', receipt.provider =
                                   'CRYPIDIAN_FIRST_PARTY_QA'
                                   AND receipt.evidence_json->>'provider' = receipt.provider,
                               'purpose_matches', receipt.purpose = 'BACKTEST_RESEARCH'
                                   AND receipt.evidence_json->>'purpose' = 'BACKTEST_RESEARCH',
                               'terms_match', receipt.evidence_json->>'terms_url' =
                                   receipt.terms_url,
                               'fingerprint_matches', receipt.policy_fingerprint =
                                   (SELECT bundle_json->>'policy_fingerprint' FROM target_bundle)
                                   AND receipt.evidence_json->>'policy_fingerprint' =
                                       receipt.policy_fingerprint,
                               'verified_at_matches',
                                   (receipt.evidence_json->>'verified_at')::timestamptz =
                                   receipt.verified_at,
                               'checked_at_matches',
                                   (receipt.evidence_json->>'checked_at')::timestamptz =
                                   receipt.checked_at,
                               'valid_until_matches',
                                   (receipt.evidence_json->>'valid_until')::timestamptz =
                                   receipt.valid_until,
                               'lease_order_matches', receipt.verified_at <= receipt.checked_at
                                   AND receipt.checked_at < receipt.valid_until
                           )
                             FROM target_trial AS trial
                             JOIN public.backtest_run_dataset_rights AS receipt
                               ON receipt.workspace_id = :workspace_id
                              AND receipt.run_id = trial.run_id
                              AND receipt.bundle_hash = :bundle_hash
                              AND receipt.child_dataset_hash = component.child_hash
                              AND receipt.interval = component.interval_name
                       ) AS checks
                  FROM components AS component
            )
            SELECT pg_catalog.jsonb_build_object(
                'component_count', (SELECT count(*) FROM components),
                'rights_count', (
                    SELECT count(*) FROM target_trial AS trial
                    JOIN public.backtest_run_dataset_rights AS receipt
                      ON receipt.workspace_id = :workspace_id AND receipt.run_id = trial.run_id
                ),
                'components', COALESCE((
                    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'child_hash', matching.child_hash, 'checks', matching.checks
                    )) FROM matching
                ), '[]'::jsonb)
            )
                    """
                ),
                {
                    "workspace_id": str(WORKSPACE_ID),
                    "workspace_id_text": str(WORKSPACE_ID),
                    "trial_id": STATE["trial_record_id"],
                    "bundle_hash": STATE["bundle_hash"],
                },
            )
            diagnostics = result.scalar_one()
    finally:
        engine.dispose()
    components = diagnostics["components"]
    component_checks = [item["checks"] for item in components]
    diagnostics["receipt_set_matches"] = (
        diagnostics["component_count"] == diagnostics["rights_count"]
        and bool(components)
        and all(
            isinstance(checks, dict)
            and bool(checks)
            and all(value is True for value in checks.values())
            for checks in component_checks
        )
    )
    for item in components:
        checks = item["checks"]
        item["matches"] = (
            isinstance(checks, dict)
            and bool(checks)
            and all(value is True for value in checks.values())
        )
    return diagnostics


async def _seed_queued_attempt() -> UUID:
    """Tạo attempt tổng hợp để đi qua claim gateway; không chạy trainer/model."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        approval = await session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == WORKSPACE_ID,
                LearningSplitApprovalRecord.manifest_hash == STATE["manifest_hash"],
                LearningSplitApprovalRecord.split_policy_version == SPLIT_POLICY_VERSION,
                LearningSplitApprovalRecord.fold_plan_hash == FOLD_HASH,
            )
        )
        assert approval is not None
        approval_hash = approval.approval_hash
    await engine.dispose()

    request = LearningResourceRequest(
        training_samples=1,
        validation_samples=1,
        feature_count=1,
        max_steps=1,
        max_runtime_seconds=10,
        memory_limit_bytes=16 * 1024 * 1024,
    )
    attempt = LearningTrainingAttempt(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        actor_membership_id=uuid4(),
        idempotency_key=uuid4(),
        manifest_hash=STATE["manifest_hash"],
        split_policy_version=SPLIT_POLICY_VERSION,
        fold_plan_hash=FOLD_HASH,
        approval_hash=approval_hash,
        parameter_family_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        trial_index=0,
        trial_count=1,
        fold_id=FOLD_HASH,
        seed=17,
        trainer_version=CPU_TRAINER_VERSION,
        dependency_fingerprint=hashlib.sha256(b"wp06-source-rights-gateway-e2e").hexdigest(),
        resource_policy_version="wp06-e2e-fixture-v1",
        resource_request=request,
        resource_plan=assess_learning_resource_request(request),
    )
    attempt_record = LearningTrainingAttemptRecord.from_domain(
        attempt,
        normalized_dataset_id=UUID(STATE["normalized_dataset_id"]),
    )
    initial_event = LearningAttemptEventRecord.from_domain(
        workspace_id=WORKSPACE_ID,
        event=LearningAttemptEvent(
            attempt_id=attempt.id,
            sequence=1,
            state=LearningAttemptState.QUEUED,
            occurred_at=datetime.now(UTC),
            actor_principal=LearningAttemptActor.ADMIN_WEB,
        ),
    )
    admin_engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="crypidian_admin",
            password=Path(os.environ["WP06_E2E_SUPERUSER_PASSWORD_FILE"]).read_text().strip(),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    try:
        with admin_engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            for record in (attempt_record, initial_event):
                values = {
                    column.name: getattr(record, column.name)
                    for column in record.__table__.columns
                    if column.name != "created_at"
                }
                connection.execute(insert(record.__table__).values(**values))
    finally:
        admin_engine.dispose()
    return attempt.id


async def _revoke_current_policy() -> dict[str, object]:
    """Thu hồi policy fixture trước lần claim tiếp theo của trainer-worker."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        repository = SourceRegistryRepository(session)
        entry = await repository.get_entry(workspace_id=WORKSPACE_ID, source_id=QA_PROVIDER)
        assert entry is not None
        assert entry.current.state is SourcePolicyState.VERIFIED
        key = uuid4()
        policy_id = "policy:" + key.hex + ":" + hashlib.sha256(key.bytes).hexdigest()
        revoked = entry.transition(
            policy_id=policy_id,
            state=SourcePolicyState.REVOKED,
            actor_id="wp06-e2e-rights-revoker",
            at=datetime.now(UTC),
            reason="E2E xác nhận claim trainer kiểm tra quyền hiện hành.",
        )
        result = await repository.append_revision(
            workspace_id=WORKSPACE_ID,
            revision=revoked.current,
        )
        assert result.revision.state is SourcePolicyState.REVOKED
    await engine.dispose()
    return {"current_policy_revoked": True}


async def _revoke_before_claim() -> dict[str, object]:
    """Gọi đúng application claim và chặn trước khi trả batch khi quyền đã thu hồi."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = LearningTrainingInputService(sessions)
    batch = None
    denial = ""
    try:
        batch = await service.claim_batch(
            LearningTrainingInputRequest(
                workspace_id=WORKSPACE_ID,
                attempt_id=UUID(STATE["attempt_id"]),
            )
        )
    except LearningTrainingBatchAccessConflict as error:
        denial = str(error)
    finally:
        await engine.dispose()
    return {
        "current_rights_denied_before_training_receipt": (
            "WP06_TRAINING_BATCH_SOURCE_RIGHTS_FAIL_CLOSED" in denial
            or "WP06_SOURCE_TRAINING_RIGHTS_POLICY_DENIED" in denial
            or "WP06_SOURCE_TRAINING_RIGHTS_INVALIDATED" in denial
        ),
        "no_training_payload_returned": batch is None,
        "denial_reason": denial,
        "payload_returned": batch is not None,
    }


async def _inspect_after_claim() -> dict[str, object]:
    """Xác minh claim bị từ chối không tạo receipt và không đổi receipt chuẩn hóa."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        training_receipt_count = await session.scalar(
            select(func.count())
            .select_from(LearningTrainingAccessReceiptRecord)
            .where(
                LearningTrainingAccessReceiptRecord.workspace_id == WORKSPACE_ID,
                LearningTrainingAccessReceiptRecord.attempt_id == UUID(STATE["attempt_id"]),
            )
        )
        normalization_receipt_count = await session.scalar(
            select(func.count())
            .select_from(LearningReplayTrainingReceiptRecord)
            .where(
                LearningReplayTrainingReceiptRecord.workspace_id == WORKSPACE_ID,
                LearningReplayTrainingReceiptRecord.access_kind == "normalization",
                LearningReplayTrainingReceiptRecord.access_id
                == UUID(STATE["normalized_dataset_id"]),
            )
        )
    await engine.dispose()
    return {
        "no_training_access_receipt": training_receipt_count == 0,
        "normalization_receipts_unchanged": (
            normalization_receipt_count == STATE["expected_source_receipt_count"]
        ),
    }


async def _check_scope_variants(session) -> dict[str, object]:
    """Gọi normalization gateway để xác minh child và MarketBar lineage."""
    checks: dict[str, object] = {}
    for name in ("missing_child", "extra_child", "market_bar_substitution"):
        variant = STATE["lineage_variants"][name]
        error = await _expect_denied(
            session,
            "SELECT public.wp06_record_replay_training_receipts("
            "CAST(:workspace AS uuid), CAST(:manifest_hash AS text), "
            "CAST(:dataset AS uuid), CAST(:fold AS text))",
            {
                "workspace": str(WORKSPACE_ID),
                "manifest_hash": variant["manifest_hash"],
                "dataset": variant["normalized_dataset_id"],
                "fold": FOLD_HASH,
            },
        )
        checks[f"{name}_error"] = error
        if name == "missing_child":
            checks[name] = any(
                reason in error
                for reason in (
                    "LEGACY_LINEAGE_DENIED",
                    "LINEAGE_INVALID",
                    "CHILD_COUNT_MISMATCH",
                )
            )
        elif name == "extra_child":
            checks[name] = "CHILD_COUNT_MISMATCH" in error
        else:
            checks[name] = "MARKET_BAR_SET_MISMATCH" in error
    return checks


async def _inspect_receipts() -> dict[str, object]:
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await _set_workspace(session)
        count = await session.scalar(
            select(func.count())
            .select_from(LearningReplayTrainingReceiptRecord)
            .where(
                LearningReplayTrainingReceiptRecord.workspace_id == WORKSPACE_ID,
                LearningReplayTrainingReceiptRecord.access_kind == "normalization",
                LearningReplayTrainingReceiptRecord.access_id
                == UUID(STATE["normalized_dataset_id"]),
            )
        )
        assert count == STATE["expected_source_receipt_count"]
        receipt = await session.scalar(
            select(LearningReplayTrainingReceiptRecord).where(
                LearningReplayTrainingReceiptRecord.workspace_id == WORKSPACE_ID,
                LearningReplayTrainingReceiptRecord.access_kind == "normalization",
                LearningReplayTrainingReceiptRecord.access_id
                == UUID(STATE["normalized_dataset_id"]),
            )
        )
        assert receipt is not None
        update_denied = await _expect_denied(
            session,
            "UPDATE public.learning_replay_training_receipt SET purpose='evaluation' "
            "WHERE id=:receipt_id",
            {"receipt_id": str(receipt.id)},
        )
        delete_denied = await _expect_denied(
            session,
            "DELETE FROM public.learning_replay_training_receipt WHERE id=:receipt_id",
            {"receipt_id": str(receipt.id)},
        )
    await engine.dispose()
    return {
        "metadata_only_receipts_persisted": True,
        "receipt_update_denied": "IMMUTABLE" in update_denied
        or "permission denied" in update_denied.lower(),
        "receipt_delete_denied": "IMMUTABLE" in delete_denied
        or "permission denied" in delete_denied.lower(),
    }


async def _rights_status() -> dict[str, Any]:
    """Trả số sự kiện rights QA chưa được ghi receipt consumer."""
    engine = _engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            await _set_workspace(session)
            pending = await session.scalar(
                text(
                    """
                    SELECT count(*)
                      FROM public.outbox_event AS event
                     WHERE event.workspace_id = :workspace_id
                       AND event.event_type = 'research.rights_changed.v1'
                       AND event.payload->>'source_id' = :source_id
                       AND NOT EXISTS (
                            SELECT 1 FROM public.consumer_receipt AS receipt
                             WHERE receipt.workspace_id = event.workspace_id
                               AND receipt.outbox_event_id = event.id
                               AND receipt.consumer_name =
                                   'wp06.dataset-rights-eligibility.v1'
                       )
                    """
                ),
                {"workspace_id": str(WORKSPACE_ID), "source_id": QA_PROVIDER},
            )
        return {"pending_rights_events": int(pending or 0)}
    finally:
        await engine.dispose()


async def main() -> None:
    if PHASE == "seed":
        result = await _seed_replay()
    elif PHASE == "prepare-manifest":
        result = await _prepare_manifest()
    elif PHASE == "seed-negative-manifests":
        result = await _seed_negative_manifests(STATE)
    elif PHASE == "authorize":
        result = await _authorize()
    elif PHASE == "normalize":
        result = await _normalize()
    elif PHASE == "inspect-receipts":
        result = await _inspect_receipts()
    elif PHASE == "check-scope-variants":
        result = await _check_scope_variants_phase()
    elif PHASE == "check-evaluator-gate-acl":
        result = await _check_evaluator_gate_acl_phase()
    elif PHASE == "revoke":
        result = await _revoke_current_policy()
    elif PHASE == "revoke-before-claim":
        result = await _revoke_before_claim()
    elif PHASE == "inspect-after-claim":
        result = await _inspect_after_claim()
    elif PHASE == "rights-status":
        result = await _rights_status()
    else:
        raise ValueError(f"Phase probe chưa được triển khai: {PHASE}")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    asyncio.run(main())
