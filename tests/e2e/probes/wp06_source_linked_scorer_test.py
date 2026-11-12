"""Hành trình TEST thật từ replay H03, qua các principal Compose riêng."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.application.outbox import UnitOfWork

STATE = json.loads(os.environ["WP06_SOURCE_LINKED_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
FUNCTION_SIGNATURES = (
    "public.wp06_claim_learning_training_lineage(uuid,uuid)",
    "public.validate_learning_scorer_test_opportunity_proof()",
    "public.wp06_open_scorer_test_access_receipt(uuid,uuid,uuid,uuid,uuid,text,text,text,text)",
    "public.wp06_consume_scorer_test_payload(uuid,uuid,text)",
)
CURRENT_RIGHTS_COLUMNS = (
    "scorer_artifact_id",
    "artifact_hash",
    "proof_hash",
    "bundle_hash",
    "source_scope_hash",
    "training_membership_digest",
    "validation_membership_digest",
    "training_scope_digest",
    "source_rights_digest",
    "fingerprint",
    "checked_at",
)
QA_MIGRATION_FUNCTIONS = (
    "public.wp06_lock_scorer_test_sources(uuid, uuid)",
    "public.wp06_recheck_scorer_current_rights(uuid, uuid)",
    "public.wp06_verify_scorer_deployment_rights(uuid, uuid, uuid)",
)


def principal():
    from trading_bot.auth.policy import Principal, Role

    return Principal(
        user_id=UUID(STATE["account_id"]),
        membership_id=UUID(STATE["membership_id"]),
        workspace_id=WORKSPACE,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )


def _load_migration(name, filename):
    path = Path("/app/migrations/versions") / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _verify_migration_body_guards(engine):
    f9 = _load_migration("c3_guard_f9", "f9c1e3a5b7d9_wp06_trainer_source_trial_lineage.py")
    a4 = _load_migration("c3_guard_a4", "a4c6e8f0b2d4_wp06_scorer_test_entry_hashes.py")
    b3 = _load_migration("c3_guard_b3", "b3d5f7a9c1e3_wp06_scorer_test_gateway_return_types.py")
    cases = (
        (
            f9,
            f9.FUNCTION_SIGNATURE,
            "WP06_TRAINER_LINEAGE_PREIMAGE_UNEXPECTED",
            (
                (f9.SOURCE_TRIAL_LINK, 2),
                *((replacement, 1) for _, replacement in f9.RETURN_CASTS),
            ),
        ),
        (
            a4,
            a4.FUNCTION_SIGNATURE,
            "WP06_SCORER_TEST_HASH_PREIMAGE_UNEXPECTED",
            ((a4.HASH_PATTERN, 7),),
        ),
        (
            b3,
            b3.CONSUME_SIGNATURE,
            "WP06_SCORER_TEST_RETURN_PREIMAGE_UNEXPECTED",
            tuple(
                (replacement, count)
                for signature, _, replacement, count in b3.PATCHES
                if signature == b3.CONSUME_SIGNATURE
            ),
        ),
    )
    async with engine.begin() as connection:
        for migration, signature, expected_error, snippets in cases:
            definition = await connection.scalar(
                text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": signature},
            )
            assert definition is not None
            altered, changed = re.subn(
                r"(?m)^([ \t]*BEGIN[ \t]*)$",
                r"\1\n            -- C3 review regression body change",
                definition,
                count=1,
            )
            assert changed == 1, signature
            for snippet, count in snippets:
                assert altered.count(snippet) == count
            await connection.execute(text(altered))
            try:
                try:
                    async with connection.begin_nested():

                        def run_downgrade(sync_connection, migration_module=migration):
                            migration_module.op = SimpleNamespace(
                                execute=lambda statement: sync_connection.execute(
                                    text(str(statement))
                                )
                            )
                            migration_module.downgrade()

                        await connection.run_sync(run_downgrade)
                except DBAPIError as error:
                    actual_error = getattr(error.orig.diag, "message_primary", str(error.orig))
                    assert expected_error in actual_error
                else:
                    raise AssertionError("Sửa body giữ snippets phải bị migration từ chối.")
            finally:
                await connection.execute(text(definition))
            restored = await connection.scalar(
                text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": signature},
            )
            assert restored == definition


def _qa_migration_downgrade_case(case_name: str):
    migration = _load_migration(
        "wp06_qa_migration_guard", "8ac4e2f6b0d1_wp06_first_party_qa_source.py"
    )
    if case_name == "test-lock":
        signature = migration.TEST_LOCK_SIGNATURE
        _, postimage, count = migration.TEST_LOCK_PATCHES[0]
        drift = postimage.replace("CRYPIDIAN_FIRST_PARTY_QA", "CRYPIDIAN_FIRST_PARTY_QA_DRIFT", 1)
        error_marker = "WP06_SCORER_TEST_QA_LOCK_DOWNGRADE_PREIMAGE_UNEXPECTED"
    elif case_name == "current-rights":
        signature = migration.CURRENT_RIGHTS_SIGNATURE
        _, postimage, count = next(
            patch for patch in migration.CURRENT_RIGHTS_PATCHES if patch[2] == 5
        )
        drift = postimage.replace("=", "<>", 1)
        error_marker = "WP06_SCORER_CURRENT_RIGHTS_QA_DOWNGRADE_PREIMAGE_UNEXPECTED"
    elif case_name == "deployment":
        signature = migration.DEPLOYMENT_SIGNATURE
        _, postimage, count = migration.DEPLOYMENT_PATCHES[0]
        drift = postimage.replace(
            "WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED",
            "WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED_DRIFT",
            1,
        )
        error_marker = "WP06_SCORER_DEPLOYMENT_QA_GUARD_DOWNGRADE_MARKER_PREIMAGE_UNEXPECTED"
    else:
        raise AssertionError(f"Migration guard case không hợp lệ: {case_name}")
    return signature, postimage, drift, count, error_marker


async def _qa_migration_function_hashes(session):
    return {
        signature: await session.scalar(
            text(
                "SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
                "pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure)), "
                "'UTF8')), 'hex')"
            ),
            {"signature": signature},
        )
        for signature in QA_MIGRATION_FUNCTIONS
    }


async def _prepare_qa_migration_downgrade_case(sessions, case_name: str):
    signature, postimage, drift, expected_count, error_marker = _qa_migration_downgrade_case(
        case_name
    )
    async with sessions() as session, session.begin():
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "c3f5a7b9d1e3", version
        function_hashes = await _qa_migration_function_hashes(session)
        definition = await session.scalar(
            text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        assert definition is not None
        assert definition.count(postimage) == expected_count, (case_name, expected_count)
        altered = definition.replace(postimage, drift, 1)
        assert altered != definition
        await (await session.connection()).exec_driver_sql(altered)
        drift_hash = hashlib.sha256(altered.encode("utf-8")).hexdigest()
    return {
        "migration_case": case_name,
        "migration_signature": signature,
        "migration_error_marker": error_marker,
        "migration_function_hashes_before": function_hashes,
        "migration_drift_hash": drift_hash,
    }


async def _check_and_restore_qa_migration_downgrade_case(sessions):
    case_name = STATE["migration_case"]
    signature, postimage, drift, _, expected_error = _qa_migration_downgrade_case(case_name)
    assert STATE["migration_signature"] == signature
    assert STATE["migration_error_marker"] == expected_error
    async with sessions() as session, session.begin():
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "c3f5a7b9d1e3", version
        definitions = {
            function_signature: await session.scalar(
                text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": function_signature},
            )
            for function_signature in QA_MIGRATION_FUNCTIONS
        }
        assert all(isinstance(value, str) for value in definitions.values())
        current_hashes = {
            function_signature: hashlib.sha256(definition.encode("utf-8")).hexdigest()
            for function_signature, definition in definitions.items()
        }
        assert current_hashes[signature] == STATE["migration_drift_hash"]
        assert {
            function_signature: current_hashes[function_signature]
            for function_signature in QA_MIGRATION_FUNCTIONS
            if function_signature != signature
        } == {
            function_signature: function_hash
            for function_signature, function_hash in STATE[
                "migration_function_hashes_before"
            ].items()
            if function_signature != signature
        }
        definition = definitions[signature]
        assert definition.count(drift) == 1, case_name
        restored_definition = definition.replace(drift, postimage, 1)
        await (await session.connection()).exec_driver_sql(restored_definition)
    async with sessions() as session:
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        restored_hashes = await _qa_migration_function_hashes(session)
    assert version == "c3f5a7b9d1e3", version
    assert restored_hashes == STATE["migration_function_hashes_before"]
    return {
        "migration_downgrade_rolled_back": True,
        "migration_revision_preserved": True,
        "migration_drift_survived_rollback": True,
        "migration_body_restored": True,
        "migration_case": case_name,
        "migration_error_marker": expected_error,
    }


async def counts(sessions):
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        return tuple(
            (
                await unit.session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM learning_scorer_test_access_receipt), "
                        "(SELECT count(*) FROM learning_scorer_test_payload_consumption), "
                        "(SELECT count(*) FROM learning_scorer_test_evaluation_report)"
                    )
                )
            ).one()
        )


async def run(phase):
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            role = await session.scalar(text("SELECT session_user"))
        if phase == "lineage-contract":
            contracts = {}
            functions = (
                (
                    "public.wp06_claim_learning_training_lineage(uuid,uuid)",
                    "crypidian_learning_batch_reader",
                    "trainer_worker",
                ),
                (
                    "public.wp06_open_scorer_test_access_receipt(uuid,uuid,uuid,uuid,uuid,text,text,text,text)",
                    "crypidian_learning_evaluation_reader",
                    "learning_evaluator",
                ),
                (
                    "public.wp06_consume_scorer_test_payload(uuid,uuid,text)",
                    "crypidian_learning_evaluation_reader",
                    "learning_evaluator",
                ),
            )
            async with sessions() as session:
                for signature, owner, allowed_role in functions:
                    row = (
                        (
                            await session.execute(
                                text(
                                    "SELECT pg_get_userbyid(proowner) AS owner, "
                                    "prosecdef AS security_definer, proconfig AS settings, "
                                    "ARRAY(SELECT acl::text FROM unnest(proacl) acl "
                                    "ORDER BY acl::text) AS acl FROM pg_proc "
                                    "WHERE oid=CAST(:signature AS regprocedure)"
                                ),
                                {"signature": signature},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    contract = dict(row)
                    assert contract["owner"] == owner
                    assert contract["security_definer"]
                    for target_role in (
                        "trainer_worker",
                        "candidate_builder",
                        "learning_evaluator",
                        "app",
                    ):
                        actual = await session.scalar(
                            text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                            {"role": f"crypidian_{target_role}", "signature": signature},
                        )
                        assert actual == (target_role == allowed_role)
                    contracts[signature] = contract
                return contracts
        if phase == "function-definition-hashes":
            async with sessions() as session:
                return {
                    signature: await session.scalar(
                        text(
                            "SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
                            "pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure)), "
                            "'UTF8')), 'hex')"
                        ),
                        {"signature": signature},
                    )
                    for signature in FUNCTION_SIGNATURES
                }
        if phase == "migration-body-guard":
            await _verify_migration_body_guards(engine)
            return {"altered_function_bodies_rejected": True}
        if phase == "qa-migration-downgrade-prepare":
            return await _prepare_qa_migration_downgrade_case(sessions, STATE["migration_case"])
        if phase == "qa-migration-downgrade-check-restore":
            return await _check_and_restore_qa_migration_downgrade_case(sessions)
        if phase == "stage":
            assert role == "crypidian_candidate_builder"
            from trading_bot.application.learning_split_candidate import (
                LearningSplitCandidateBuilderService,
            )
            from trading_bot.cli.learning_split_candidate import parse_config

            result = await LearningSplitCandidateBuilderService(sessions).stage(
                workspace_id=WORKSPACE,
                trial_record_id=UUID(STATE["trial_record_id"]),
                config=parse_config(json.dumps(STATE["config"])),
            )
            from trading_bot.adapters.database.models import LearningSplitCandidateRecord

            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
                candidate_row = await unit.session.scalar(
                    select(LearningSplitCandidateRecord).where(
                        LearningSplitCandidateRecord.id == result.candidate_id
                    )
                )
                approved_proof_hash = candidate_row.proof_hash
            return {
                "candidate_id": str(result.candidate_id),
                "manifest_hash": result.manifest_hash,
                "fold_plan_hash": result.fold_plan_hash,
                "approved_base_proof_id": str(result.proof_record_id),
                "approved_base_proof_hash": approved_proof_hash,
            }
        if phase == "approve":
            assert role == "crypidian_admin_web"
            from trading_bot.application.learning_split_admin import LearningSplitAdminService

            result = (
                await LearningSplitAdminService(sessions)
                .for_principal(principal())
                .approve(
                    candidate_id=UUID(STATE["candidate_id"]),
                    reason="Duyệt split thời gian của fixture H03 TEST.",
                    idempotency_key=uuid4(),
                )
            )
            return {"approval_hash": result.content_hash}
        if phase == "seal":
            assert role == "crypidian_candidate_builder"
            from trading_bot.application.learning_split_candidate import (
                LearningSplitCandidateBuilderService,
            )

            result = await LearningSplitCandidateBuilderService(sessions).seal(
                workspace_id=WORKSPACE,
                candidate_id=UUID(STATE["candidate_id"]),
            )
            return {"normalized_dataset_id": str(result.normalized_dataset_id)}
        if phase == "enqueue":
            assert role == "crypidian_admin_web"
            from trading_bot.application.learning_training_admin import LearningTrainingAdminService

            service = LearningTrainingAdminService(sessions).for_principal(principal())
            options = await service.list_eligible_folds()
            assert len(options) == 1
            option = options[0]
            result = await service.enqueue(
                normalized_dataset_id=option.normalized_dataset_id,
                fold_id=option.fold_id,
                idempotency_key=uuid4(),
            )
            return {"attempt_id": str(result.attempt_id), "fold_id": option.fold_id}
        if phase == "train":
            assert role == "crypidian_trainer_worker"
            from trading_bot.workers.learning_training_worker import LearningTrainingAttemptWorker

            result = await LearningTrainingAttemptWorker(sessions).run_once(
                workspace_id=WORKSPACE, attempt_id=UUID(STATE["attempt_id"])
            )
            from trading_bot.adapters.database.models import LearningTrainingArtifactRecord
            from trading_bot.adapters.database.repositories.learning_training_artifacts import (
                LearningTrainingArtifactRepository,
            )

            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
                base_row = await unit.session.scalar(
                    select(LearningTrainingArtifactRecord).where(
                        LearningTrainingArtifactRecord.id == result.artifact_id
                    )
                )
                LearningTrainingArtifactRepository.validate_record(base_row)
                canonical_artifact = base_row.canonical_artifact
                artifact_hash = base_row.artifact_hash
            return {
                "status": "succeeded",
                "artifact_id": str(result.artifact_id),
                "base_artifact_canonical": canonical_artifact,
                "base_artifact_hash": artifact_hash,
            }
        if phase == "proof":
            assert role == "crypidian_candidate_builder"
            from trading_bot.adapters.database.models import LearningScorerArtifactRecord
            from trading_bot.adapters.database.repositories.candidate_universe_proofs import (
                CandidateUniverseProofRepository,
            )
            from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (  # noqa: E501
                LearningScorerTestOpportunityProofRepository,
            )

            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
                for table in ("learning_training_attempt", "learning_scorer_artifact"):
                    privileges = (
                        await unit.session.execute(
                            text(
                                "SELECT has_table_privilege(current_user,:table,'SELECT'), "
                                "has_table_privilege(current_user,:table,'INSERT'), "
                                "has_table_privilege(current_user,:table,'UPDATE'), "
                                "has_table_privilege(current_user,:table,'DELETE')"
                            ),
                            {"table": table},
                        )
                    ).one()
                    assert tuple(privileges) == (True, False, False, False)
                    flags = (
                        await unit.session.execute(
                            text(
                                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                                "WHERE oid=CAST(:table AS regclass)"
                            ),
                            {"table": table},
                        )
                    ).one()
                    assert tuple(flags) == (True, True)
                for tenant in (str(uuid4()), ""):
                    await unit.session.execute(
                        text("SELECT set_config('app.workspace_id',:tenant,true)"),
                        {"tenant": tenant},
                    )
                    for table in ("learning_training_attempt", "learning_scorer_artifact"):
                        assert await unit.session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                await unit.session.execute(
                    text("SELECT set_config('app.workspace_id',:tenant,true)"),
                    {"tenant": str(WORKSPACE)},
                )
                definition = await unit.session.scalar(
                    text(
                        "SELECT pg_get_functiondef("
                        "'public.validate_learning_scorer_test_opportunity_proof()'::regprocedure)"
                    )
                )
                assert definition.count("^[0-9a-f]{64}$") == 7
                assert "^[0-9a-f]64$" not in definition
                scorer_row = await unit.session.scalar(
                    select(LearningScorerArtifactRecord).where(
                        LearningScorerArtifactRecord.attempt_id == UUID(STATE["attempt_id"])
                    )
                )
                assert scorer_row.state == "ready_for_evaluation", (
                    scorer_row.state,
                    scorer_row.reason_code,
                    scorer_row.training_sample_count,
                    scorer_row.validation_sample_count,
                )
                base = await CandidateUniverseProofRepository(
                    unit.session
                ).build_decision_opportunity(
                    workspace_id=WORKSPACE,
                    trial_record_id=UUID(STATE["trial_record_id"]),
                )
                assert base.proof.completeness_verified
                approved_base = await CandidateUniverseProofRepository(
                    unit.session
                ).build_and_append(
                    workspace_id=WORKSPACE, trial_record_id=UUID(STATE["trial_record_id"])
                )
                assert approved_base.proof.query_version == "base-closed-trade-universe-v1"
                assert str(approved_base.record_id) == STATE["approved_base_proof_id"]
                assert approved_base.proof.content_hash == STATE["approved_base_proof_hash"]
                assert base.record_id != approved_base.record_id
                assert base.proof.content_hash != approved_base.proof.content_hash
                result = await LearningScorerTestOpportunityProofRepository(
                    unit.session
                ).build_and_append(
                    workspace_id=WORKSPACE,
                    evaluation_run_id=UUID(STATE["evaluation_run_id"]),
                    attempt_id=UUID(STATE["attempt_id"]),
                )
                proof = result.proof
                assert proof.completeness_verified
                assert (
                    proof.source_gap_count
                    == proof.membership_mismatch_count
                    == proof.unlabeled_candidate_count
                    == 0
                )
                assert proof.candidate_count == 9
                assert proof.no_candidate_count == 81
                assert (
                    proof.expected_opportunity_count
                    == proof.candidate_count + proof.no_candidate_count
                )
                assert proof.bundle_hash == STATE["bundle_hash"]
                from sqlalchemy.exc import DBAPIError

                from trading_bot.adapters.database.models import (
                    LearningScorerTestOpportunityProofRecord,
                )

                for invalid_hash in ("a" * 63, "g" * 64):
                    invalid_row = LearningScorerTestOpportunityProofRecord.from_proof(proof)
                    invalid_row.id = uuid4()
                    invalid_payload = json.loads(invalid_row.canonical_proof)
                    invalid_payload["entries"][0]["stable_id_hash"] = invalid_hash
                    invalid_row.canonical_proof = json.dumps(
                        invalid_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    invalid_row.proof_hash = hashlib.sha256(
                        invalid_row.canonical_proof.encode()
                    ).hexdigest()
                    try:
                        async with unit.session.begin_nested():
                            unit.session.add(invalid_row)
                            await unit.session.flush()
                    except DBAPIError as error:
                        if (
                            error.orig.sqlstate != "P0001"
                            or error.orig.diag.message_primary
                            != "WP06_SCORER_TEST_PROOF_ENTRY_INVALID"
                        ):
                            raise
                    else:
                        raise AssertionError("Trigger phải từ chối stable ID hash không hợp lệ.")
                assert {
                    item.stable_id_hash: {
                        "candidate_id_hash": item.candidate_id_hash,
                        "vector_hash": item.vector_hash,
                        "status": item.status,
                    }
                    for item in proof.entries
                } == STATE["expected_test_inventory"]
                repeated = await LearningScorerTestOpportunityProofRepository(
                    unit.session
                ).build_and_append(
                    workspace_id=WORKSPACE,
                    evaluation_run_id=UUID(STATE["evaluation_run_id"]),
                    attempt_id=UUID(STATE["attempt_id"]),
                )
                assert repeated.is_duplicate
                assert repeated.record.id == result.record.id
                assert repeated.record.proof_hash == result.record.proof_hash
            return {
                "test_proof": json.loads(proof.canonical_json),
                "test_proof_id": str(result.record.id),
                "test_proof_hash": proof.content_hash,
                "scorer_artifact_id": str(scorer_row.id),
                "expected_test_events": proof.expected_opportunity_count,
                "test_candidates": proof.candidate_count,
                "test_no_candidates": proof.no_candidate_count,
            }
        if phase == "evaluate":
            assert role == "crypidian_learning_evaluator"
            return await evaluate(sessions)
        if phase == "current-rights":
            assert role == "crypidian_admin"
            return await verify_current_rights(sessions)
        if phase == "deployment-market-signal-worker":
            assert role == "crypidian_market_signal_worker"
            return await verify_market_signal_worker_deployment_rights(sessions)
        if phase == "deployment-inconclusive":
            assert role == "crypidian_admin_web"
            return await verify_inconclusive_denial(sessions)
        if phase == "deployment-audit":
            assert role == "crypidian_admin"
            return await verify_deployment_audit(sessions)
        if phase == "deployment-role-denied":
            assert role == "crypidian_learning_evaluator"
            return await verify_deployment_role_denial(sessions)
        raise ValueError("Phase không hợp lệ.")
    finally:
        await engine.dispose()


async def evaluate(sessions):
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.models import LearningScorerArtifactRecord
    from trading_bot.adapters.database.repositories.learning_scorer_artifacts import (
        LearningScorerArtifactRepository,
    )
    from trading_bot.adapters.database.repositories.learning_scorer_test_access import (
        LearningScorerTestAccessRepository,
        LearningScorerTestEvaluationReportRepository,
    )
    from trading_bot.application.learning_cpu_trainer import (
        LearningCpuArtifact,
        LearningCpuClassModel,
        LearningCpuValidationReport,
    )
    from trading_bot.application.learning_scorer_artifact import LearningScorerArtifact
    from trading_bot.application.learning_scorer_evaluation import (
        ScorerTestEvaluationPolicy,
        ScorerTestEvaluationScope,
        ScorerTestOpportunityEntry,
        ScorerTestOpportunityProof,
    )
    from trading_bot.application.learning_scorer_test_service import (
        ScorerTestEvaluationReportConflict,
        ScorerTestEvaluationRequest,
        ScorerTestEvaluationService,
        scorer_test_evaluation_request_fingerprint,
    )
    from trading_bot.domain.learning.resources import LearningResourcePlan, LearningResourceRequest

    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            await unit.session.execute(
                text("SELECT features FROM learning_normalized_sample LIMIT 1")
            )
    except DBAPIError as error:
        if error.orig.sqlstate != "42501":
            raise
    else:
        raise AssertionError("Evaluator không được đọc trực tiếp TEST payload.")

    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        definitions = {}
        for key, signature in (
            (
                "open",
                "public.wp06_open_scorer_test_access_receipt(uuid,uuid,uuid,uuid,uuid,text,text,text,text)",
            ),
            ("consume", "public.wp06_consume_scorer_test_payload(uuid,uuid,text)"),
        ):
            definitions[key] = await unit.session.scalar(
                text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": signature},
            )
        for prefix, columns, key in (
            (
                "receipt",
                (
                    "receipt_hash",
                    "request_fingerprint",
                    "scorer_artifact_hash",
                    "policy_hash",
                    "proof_hash",
                    "current_rights_receipt_hash",
                ),
                "open",
            ),
            (
                "consumption",
                (
                    "request_fingerprint",
                    "membership_digest",
                    "payload_commitment_hash",
                    "consumption_hash",
                    "current_rights_receipt_hash",
                ),
                "consume",
            ),
            (
                "sample",
                (
                    "outcome_hash",
                    "sample_hash",
                    "feature_snapshot_hash",
                    "feature_schema_hash",
                    "label_hash",
                ),
                "consume",
            ),
            (
                "outcome",
                ("outcome_hash", "trade_id", "entry_vector_hash", "entry_candidate_id"),
                "consume",
            ),
        ):
            for column in columns:
                assert f"{prefix}.{column}::text" in definitions[key]
        scorer_row = await unit.session.scalar(
            select(LearningScorerArtifactRecord).where(
                LearningScorerArtifactRecord.attempt_id == UUID(STATE["attempt_id"])
            )
        )
        LearningScorerArtifactRepository.validate_record(scorer_row)
        scorer_payload = json.loads(scorer_row.canonical_artifact)
        scorer_payload["validation_class_support"] = tuple(
            map(tuple, scorer_payload["validation_class_support"])
        )
        scorer = LearningScorerArtifact(**scorer_payload)
        assert scorer.state == "ready_for_evaluation"
        base_payload = json.loads(STATE["base_artifact_canonical"])
        base_payload["resource_request"] = LearningResourceRequest(
            **base_payload["resource_request"]
        )
        base_payload["resource_plan"] = LearningResourcePlan(**base_payload["resource_plan"])
        base_payload["models"] = tuple(
            LearningCpuClassModel(**(item | {"coefficients": tuple(item["coefficients"])}))
            for item in base_payload["models"]
        )
        base_payload["validation_report"] = LearningCpuValidationReport(
            **base_payload["validation_report"]
        )
        for name in ("feature_names", "feature_means", "feature_scales"):
            base_payload[name] = tuple(base_payload[name])
        base_payload["training_class_counts"] = tuple(
            map(tuple, base_payload["training_class_counts"])
        )
        base = LearningCpuArtifact(**base_payload)
        assert base.artifact_hash == scorer.base_artifact_hash == STATE["base_artifact_hash"]

    payload = STATE["test_proof"].copy()
    for name in (
        "schema_version",
        "candidate_count",
        "no_candidate_count",
        "input_excluded_count",
        "completeness_verified",
    ):
        payload.pop(name, None)
    for name in ("workspace_id", "evaluation_run_id", "attempt_id", "normalized_dataset_id"):
        payload[name] = UUID(payload[name])
    payload["record_id"] = UUID(STATE["test_proof_id"])
    payload["entries"] = tuple(ScorerTestOpportunityEntry(**item) for item in payload["entries"])
    proof = ScorerTestOpportunityProof(**payload)
    assert proof.content_hash == STATE["test_proof_hash"]
    assert proof.content_hash != STATE["approved_base_proof_hash"]
    assert str(proof.record_id) != STATE["approved_base_proof_id"]
    scope = ScorerTestEvaluationScope(
        **{
            name: getattr(proof, name)
            for name in (
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
        },
        opportunity_proof=proof,
    )
    # Ghim đầy đủ policy TEST v1 đang dùng bởi cấu hình runtime/evaluator.
    policy = ScorerTestEvaluationPolicy(
        policy_version="wp06-scorer-test-v1",
        minimum_sample_count=1000,
        minimum_class_support=100,
        maximum_calibration_error=0.05,
        maximum_log_loss_regression=0.01,
        maximum_brier_regression=0.01,
        minimum_coverage=0.80,
        minimum_candidate_retention=0.20,
        maximum_false_veto_rate=0.10,
        approve_probability_threshold=0.60,
        minimum_confidence=0.55,
        reliability_bin_count=10,
        maximum_standardized_feature_deviation=6.0,
        runtime_inference_timeout_ms=100,
    )
    request = ScorerTestEvaluationRequest(
        scope=scope,
        scorer_artifact=scorer,
        base_artifact=base,
        policy=policy,
        evaluated_at=datetime.now(UTC),
    )
    access = LearningScorerTestAccessRepository(sessions)
    fingerprint = scorer_test_evaluation_request_fingerprint(request)
    receipt = await access.open_committed_receipt(
        scope,
        scorer_artifact_hash=scorer.artifact_hash,
        policy_hash=policy.policy_hash,
        request_fingerprint=fingerprint,
    )
    assert await counts(sessions) == (1, 0, 0)
    assert receipt.committed_at is not None

    # Lỗi callback sau khi đọc phải rollback cả consumption/report, giữ receipt đã commit.
    def fail_after_payload(consumption):
        assert consumption.payload_count == 9
        raise RuntimeError("C3_FIXTURE_ROLLBACK_AFTER_CONSUME")

    try:
        await access.evaluate_and_persist(
            receipt, request_fingerprint=fingerprint, evaluate_payload=fail_after_payload
        )
    except RuntimeError as error:
        if str(error) != "C3_FIXTURE_ROLLBACK_AFTER_CONSUME":
            raise
    else:
        raise AssertionError("Callback lỗi phải rollback phase hai.")
    assert await counts(sessions) == (1, 0, 0)

    service = ScorerTestEvaluationService(
        access, LearningScorerTestEvaluationReportRepository(sessions)
    )
    first = await service.evaluate(request)
    assert not first.is_duplicate
    assert await counts(sessions) == (1, 1, 1)
    retry = await service.evaluate(request)
    assert retry.is_duplicate
    assert retry.record_id == first.record_id
    assert retry.report.canonical_json == first.report.canonical_json
    try:
        await service.evaluate(
            replace(request, evaluated_at=request.evaluated_at + timedelta(seconds=1))
        )
    except ScorerTestEvaluationReportConflict:
        pass
    else:
        raise AssertionError("Đổi fingerprint phải bị từ chối.")
    assert await counts(sessions) == (1, 1, 1)
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            await unit.session.execute(
                text(
                    "SELECT * FROM public.wp06_consume_scorer_test_payload("
                    "CAST(:workspace AS uuid),CAST(:receipt AS uuid),:digest)"
                ),
                {
                    "workspace": str(WORKSPACE),
                    "receipt": str(receipt.receipt_id),
                    "digest": "0" * 64,
                },
            )
    except DBAPIError as error:
        if (
            error.orig.sqlstate != "P0001"
            or error.orig.diag.message_primary != "WP06_SCORER_TEST_PAYLOAD_ALREADY_CONSUMED"
        ):
            raise
    else:
        raise AssertionError("Function TEST không được tiêu thụ receipt lần hai.")
    assert await counts(sessions) == (1, 1, 1)
    report = first.report
    assert report.test_current_rights_receipt_hash == receipt.current_rights_receipt_hash
    assert report.test_access_receipt_id == receipt.receipt_id
    assert report.opportunity_proof_id == proof.record_id
    assert report.proof_hash == proof.content_hash
    assert report.test_sample_count == 9
    assert report.class_support == ((-1, 3), (0, 3), (1, 3))
    assert report.opportunity_count == 90
    assert report.no_candidate_count == 81
    assert report.candidate_completeness_verified
    assert report.status == "INCONCLUSIVE"
    report_payload = json.loads(report.canonical_json)
    assert not ({"rows", "features", "labels", "samples", "test_payload"} & report_payload.keys())
    for name in (
        "attempt_id",
        "normalized_dataset_id",
        "manifest_hash",
        "fold_plan_hash",
        "fold_id",
        "approval_hash",
        "bundle_hash",
        "test_membership_digest",
    ):
        assert getattr(report, name) == getattr(proof, name)
    assert report.scorer_artifact_hash == scorer.artifact_hash
    return {
        "receipt_committed_before_payload": True,
        "atomic_rollback": True,
        "one_consumption_one_report": True,
        "safe_report_retry": True,
        "changed_request_rejected": True,
        "exact_scope": True,
        "current_training_rights_bound": True,
        "metadata_only_report": True,
        "report_status": report.status,
        "sample_support_block": {
            "sample_count": report.test_sample_count,
            "minimum_sample_count": policy.minimum_sample_count,
            "class_support": dict(report.class_support),
            "minimum_class_support": policy.minimum_class_support,
        },
        "evaluation_report_id": str(first.record_id),
        "test_access_receipt_id": str(report.test_access_receipt_id),
        "test_payload_consumption_id": str(report.test_payload_consumption_id),
        "scorer_artifact_hash": report.scorer_artifact_hash,
    }


async def verify_current_rights(sessions):
    """Kiểm gateway trong PostgreSQL bằng fixture TEST có source reference thật."""
    import hashlib

    from trading_bot.adapters.database.replay_training_rights import (
        DatabaseReplayTrainingRightsGate,
        training_scope_digest,
    )
    from trading_bot.adapters.database.repositories.source_registry import (
        SourceRegistryRepository,
    )
    from trading_bot.domain.research.source_registry import (
        normalize_source_cost,
        source_rights_hash,
    )

    artifact_id = UUID(STATE["scorer_artifact_id"])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        session = unit.session
        assert str(await session.scalar(text("SHOW transaction_isolation"))).lower() == (
            "read committed"
        )
        lineage_checks = (
            (
                await session.execute(
                    text(
                        """
                    SELECT access.proof_id IS NOT DISTINCT FROM proof.id AS proof_id,
                           access.normalized_dataset_id IS NOT DISTINCT FROM
                               artifact.normalized_dataset_id AS normalized_dataset_id,
                           access.manifest_hash IS NOT DISTINCT FROM
                               artifact.manifest_hash AS manifest_hash,
                           access.fold_plan_hash IS NOT DISTINCT FROM
                               artifact.fold_plan_hash AS fold_plan_hash,
                           access.fold_id IS NOT DISTINCT FROM artifact.fold_id AS fold_id,
                           access.approval_hash IS NOT DISTINCT FROM
                               artifact.approval_hash AS approval_hash,
                           access.bundle_hash IS NOT DISTINCT FROM proof.bundle_hash AS bundle_hash,
                           access.source_scope_hash IS NOT DISTINCT FROM
                               proof.source_scope_hash AS source_scope_hash,
                           access.proof_hash IS NOT DISTINCT FROM proof.proof_hash AS proof_hash,
                           access.scorer_artifact_hash IS NOT DISTINCT FROM
                               artifact.artifact_hash AS scorer_artifact_hash,
                           access.policy_hash IS NOT DISTINCT FROM
                               report.policy_hash AS policy_hash
                      FROM public.learning_scorer_test_access_receipt AS access
                      JOIN public.learning_scorer_test_opportunity_proof AS proof
                        ON proof.workspace_id=access.workspace_id
                       AND proof.id=access.proof_id
                      JOIN public.learning_scorer_artifact AS artifact
                        ON artifact.workspace_id=access.workspace_id
                       AND artifact.id=CAST(:artifact_id AS uuid)
                      JOIN public.learning_scorer_test_evaluation_report AS report
                        ON report.workspace_id=access.workspace_id
                       AND report.access_receipt_id=access.id
                     WHERE access.workspace_id=:workspace
                       AND access.evaluation_run_id=CAST(:evaluation_run_id AS uuid)
                       AND access.attempt_id=CAST(:attempt_id AS uuid)
                    """
                    ),
                    {
                        "workspace": WORKSPACE,
                        "artifact_id": STATE["scorer_artifact_id"],
                        "evaluation_run_id": STATE["evaluation_run_id"],
                        "attempt_id": STATE["attempt_id"],
                    },
                )
            )
            .mappings()
            .one()
        )
        mismatches = [name for name, matches in lineage_checks.items() if matches is not True]
        assert not mismatches, f"scorer access lineage mismatch fields: {mismatches}"
        source_ref_count = await session.scalar(
            text(
                "SELECT count(*) FROM public.dataset_manifest_source_ref AS reference "
                "JOIN public.dataset_manifest AS manifest "
                "ON manifest.workspace_id=reference.workspace_id "
                "AND manifest.id=reference.manifest_id "
                "WHERE reference.workspace_id=:workspace "
                "AND manifest.manifest_hash=:manifest_hash"
            ),
            {"workspace": WORKSPACE, "manifest_hash": STATE["manifest_hash"]},
        )
        assert source_ref_count == 0, "H03 v2 fixture phải được kiểm tra là source-free"
        first_source_lock = await session.scalar(
            text(
                "SELECT source_id FROM ("
                "SELECT DISTINCT reference.source_id "
                "FROM public.dataset_manifest_source_ref AS reference "
                "JOIN public.dataset_manifest AS manifest "
                "ON manifest.workspace_id=reference.workspace_id "
                "AND manifest.id=reference.manifest_id "
                "WHERE reference.workspace_id=:workspace "
                "AND manifest.manifest_hash=:manifest_hash "
                "UNION SELECT 'CRYPIDIAN_FIRST_PARTY_QA'::text) AS source_keys "
                "ORDER BY source_id LIMIT 1"
            ),
            {"workspace": WORKSPACE, "manifest_hash": STATE["manifest_hash"]},
        )

        await verify_manifest_source_ref_fence_v3(sessions)

        entry = await SourceRegistryRepository(session).get_entry(
            workspace_id=WORKSPACE, source_id="CRYPIDIAN_FIRST_PARTY_QA"
        )
        assert entry is not None
        revision = entry.revisions[-1]
        policy = revision.policy
        sql_policy_fingerprint = await session.scalar(
            text(
                "SELECT public.wp06_scorer_source_policy_fingerprint("
                ":source_id,:sequence,:policy_id,:state,CAST(:purposes AS jsonb),"
                ":evidence_ref,:reviewed_at,:valid_until,:approved_by,:publisher,"
                ":endpoint_url,:access_method,:auth_scope,:attribution,:retention_ref,"
                ":window_seconds,:max_requests,:max_bytes,CAST(:max_cost AS numeric),:cost_unit)"
            ),
            {
                "source_id": policy.source_id,
                "sequence": revision.sequence,
                "policy_id": policy.policy_id,
                "state": policy.state.value,
                "purposes": json.dumps(
                    sorted(purpose.value for purpose in policy.allowed_purposes)
                ),
                "evidence_ref": policy.evidence_ref,
                "reviewed_at": policy.reviewed_at,
                "valid_until": policy.valid_until,
                "approved_by": revision.approved_by,
                "publisher": revision.publisher,
                "endpoint_url": revision.endpoint_url,
                "access_method": revision.access_method,
                "auth_scope": revision.auth_scope,
                "attribution": revision.attribution,
                "retention_ref": revision.retention_ref,
                "window_seconds": int(revision.budget.window.total_seconds()),
                "max_requests": revision.budget.max_requests,
                "max_bytes": revision.budget.max_bytes,
                "max_cost": str(revision.budget.max_cost),
                "cost_unit": revision.budget.cost_unit,
            },
        )
        expected_policy_fingerprint = source_rights_hash(revision)
        assert sql_policy_fingerprint == expected_policy_fingerprint, (
            sql_policy_fingerprint,
            expected_policy_fingerprint,
        )

        decimal_fixtures = (
            "1230.0",
            "1E+3",
            "1E+6",
            "1E+7",
            "0.000001",
            "1E-7",
            "123.4500",
            "0.0000",
        )
        for value in decimal_fixtures:
            actual = await session.scalar(
                text("SELECT public.wp06_scorer_decimal_python_text(CAST(:value AS numeric))"),
                {"value": value},
            )
            assert actual == str(normalize_source_cost(value)), (value, actual)

        scope = {
            "workspace": WORKSPACE,
            "manifest_hash": STATE["manifest_hash"],
            "fold_plan_hash": STATE["fold_plan_hash"],
            "fold_id": STATE["fold_id"],
        }
        membership_rows = (
            (
                await session.execute(
                    text(
                        "SELECT assignment.role,sample.sample_id,sample.sample_hash,"
                        "outcome.entry_vector_hash AS vector_hash "
                        "FROM public.learning_fold_assignment AS assignment "
                        "JOIN public.learning_normalized_sample AS sample ON "
                        "sample.workspace_id=assignment.workspace_id AND "
                        "sample.manifest_hash=assignment.manifest_hash AND "
                        "sample.fold_plan_hash=assignment.fold_plan_hash AND "
                        "sample.sample_id=assignment.sample_id "
                        "JOIN public.backtest_trade_outcome AS outcome ON "
                        "outcome.workspace_id=sample.workspace_id AND "
                        "outcome.id::text=sample.outcome_id AND "
                        "outcome.outcome_hash=sample.outcome_hash "
                        "JOIN public.backtest_decision_opportunity AS opportunity ON "
                        "opportunity.workspace_id=outcome.workspace_id AND "
                        "opportunity.trial_record_id=outcome.trial_record_id AND "
                        "opportunity.run_id=outcome.run_id AND "
                        "opportunity.manifest_hash=outcome.manifest_hash AND "
                        "opportunity.event_id=outcome.entry_opportunity_event_id "
                        "JOIN public.backtest_run AS run ON "
                        "run.workspace_id=opportunity.workspace_id AND run.id=opportunity.run_id "
                        "WHERE assignment.workspace_id=:workspace AND "
                        "assignment.manifest_hash=:manifest_hash AND "
                        "assignment.fold_plan_hash=:fold_plan_hash AND "
                        "assignment.fold_id=:fold_id AND "
                        "assignment.role IN ('train','validation') AND "
                        "opportunity.schema_version='crypidian-replay-decision-opportunity-v3' AND "
                        "opportunity.input_state='EVALUATED' AND "
                        "opportunity.strategy_state='CANDIDATE' AND "
                        "opportunity.candidate_id=outcome.entry_candidate_id AND "
                        "opportunity.vector_hash=outcome.entry_vector_hash AND "
                        "opportunity.feature_schema_hash=outcome.entry_feature_schema_hash AND "
                        "opportunity.feature_schema_hash=sample.feature_schema_hash AND "
                        "run.manifest->>'strategy_release_id'=opportunity.strategy_release_id AND "
                        "run.manifest->>'strategy_content_hash'=opportunity.strategy_content_hash "
                        'ORDER BY assignment.role,sample.sample_id COLLATE "C"'
                    ),
                    scope,
                )
            )
            .mappings()
            .all()
        )
        membership_diagnostics = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        max(artifact.training_sample_count) AS training_sample_count,
                        max(artifact.validation_sample_count) AS validation_sample_count,
                        count(*) FILTER (WHERE assignment.role='train') AS train_assignments,
                        count(*) FILTER (WHERE assignment.role='validation')
                            AS validation_assignments,
                        count(sample.sample_id) FILTER (WHERE assignment.role='train')
                            AS train_samples,
                        count(sample.sample_id) FILTER (WHERE assignment.role='validation')
                            AS validation_samples,
                        count(outcome.id) FILTER (WHERE assignment.role='train')
                            AS train_outcome_id_matches,
                        count(outcome.id) FILTER (WHERE assignment.role='validation')
                            AS validation_outcome_id_matches,
                        count(outcome.id) FILTER (WHERE assignment.role='train' AND
                            outcome.manifest_hash=sample.manifest_hash)
                            AS train_outcome_manifest_matches,
                        count(outcome.id) FILTER (WHERE assignment.role='validation' AND
                            outcome.manifest_hash=sample.manifest_hash)
                            AS validation_outcome_manifest_matches,
                        count(outcome.id) FILTER (WHERE assignment.role='train' AND
                            outcome.outcome_hash=sample.outcome_hash)
                            AS train_outcome_id_hash_matches,
                        count(outcome.id) FILTER (WHERE assignment.role='validation' AND
                            outcome.outcome_hash=sample.outcome_hash)
                            AS validation_outcome_id_hash_matches,
                        count(opportunity.event_id) FILTER (WHERE assignment.role='train')
                            AS train_opportunities,
                        count(opportunity.event_id) FILTER (WHERE assignment.role='validation')
                            AS validation_opportunities,
                        count(run.id) FILTER (WHERE assignment.role='train') AS train_runs,
                        count(run.id) FILTER (WHERE assignment.role='validation')
                            AS validation_runs,
                        count(*) FILTER (WHERE assignment.role='train' AND
                            outcome.outcome_hash=sample.outcome_hash AND
                            opportunity.schema_version =
                                'crypidian-replay-decision-opportunity-v3' AND
                            opportunity.input_state = 'EVALUATED' AND
                            opportunity.strategy_state = 'CANDIDATE' AND
                            opportunity.candidate_id = outcome.entry_candidate_id AND
                            opportunity.vector_hash = outcome.entry_vector_hash AND
                            opportunity.feature_schema_hash =
                                outcome.entry_feature_schema_hash AND
                            opportunity.feature_schema_hash = sample.feature_schema_hash AND
                            run.manifest->>'strategy_release_id' =
                                opportunity.strategy_release_id AND
                            run.manifest->>'strategy_content_hash' =
                                opportunity.strategy_content_hash AND
                            opportunity.strategy_release_id = artifact.strategy_release_id AND
                            opportunity.strategy_content_hash =
                                artifact.strategy_release_content_hash AND
                            opportunity.feature_schema_hash = artifact.vector_schema_hash AND
                            opportunity.vector_hash IS NOT NULL) AS train_valid_lineage,
                        count(*) FILTER (WHERE assignment.role='validation' AND
                            outcome.outcome_hash=sample.outcome_hash AND
                            opportunity.schema_version =
                                'crypidian-replay-decision-opportunity-v3' AND
                            opportunity.input_state = 'EVALUATED' AND
                            opportunity.strategy_state = 'CANDIDATE' AND
                            opportunity.candidate_id = outcome.entry_candidate_id AND
                            opportunity.vector_hash = outcome.entry_vector_hash AND
                            opportunity.feature_schema_hash =
                                outcome.entry_feature_schema_hash AND
                            opportunity.feature_schema_hash = sample.feature_schema_hash AND
                            run.manifest->>'strategy_release_id' =
                                opportunity.strategy_release_id AND
                            run.manifest->>'strategy_content_hash' =
                                opportunity.strategy_content_hash AND
                            opportunity.strategy_release_id = artifact.strategy_release_id AND
                            opportunity.strategy_content_hash =
                                artifact.strategy_release_content_hash AND
                            opportunity.feature_schema_hash = artifact.vector_schema_hash AND
                            opportunity.vector_hash IS NOT NULL) AS validation_valid_lineage
                      FROM public.learning_scorer_artifact AS artifact
                      JOIN public.learning_fold_assignment AS assignment
                        ON assignment.workspace_id = artifact.workspace_id
                       AND assignment.manifest_hash = artifact.manifest_hash
                       AND assignment.fold_plan_hash = artifact.fold_plan_hash
                       AND assignment.fold_id = artifact.fold_id
                      LEFT JOIN public.learning_normalized_sample AS sample
                        ON sample.workspace_id = assignment.workspace_id
                       AND sample.manifest_hash = assignment.manifest_hash
                       AND sample.fold_plan_hash = assignment.fold_plan_hash
                       AND sample.sample_id = assignment.sample_id
                      LEFT JOIN public.backtest_trade_outcome AS outcome
                        ON outcome.workspace_id = sample.workspace_id
                       AND outcome.id::text = sample.outcome_id
                      LEFT JOIN public.backtest_decision_opportunity AS opportunity
                        ON opportunity.workspace_id = outcome.workspace_id
                       AND opportunity.trial_record_id = outcome.trial_record_id
                       AND opportunity.run_id = outcome.run_id
                       AND opportunity.manifest_hash = outcome.manifest_hash
                       AND opportunity.event_id = outcome.entry_opportunity_event_id
                      LEFT JOIN public.backtest_run AS run
                        ON run.workspace_id = opportunity.workspace_id
                       AND run.id = opportunity.run_id
                     WHERE artifact.workspace_id = :workspace
                       AND artifact.id = CAST(:artifact_id AS uuid)
                       AND assignment.role IN ('train','validation')
                    """
                    ),
                    {"workspace": WORKSPACE, "artifact_id": STATE["scorer_artifact_id"]},
                )
            )
            .mappings()
            .one()
        )
        if (
            membership_diagnostics["training_sample_count"]
            != membership_diagnostics["train_assignments"]
            or membership_diagnostics["validation_sample_count"]
            != membership_diagnostics["validation_assignments"]
            or membership_diagnostics["train_valid_lineage"]
            != membership_diagnostics["train_assignments"]
            or membership_diagnostics["validation_valid_lineage"]
            != membership_diagnostics["validation_assignments"]
        ):
            raise AssertionError(
                f"training membership lineage metadata: {dict(membership_diagnostics)}"
            )

        expected_digests = {}
        for fold_role in ("train", "validation"):
            members = [
                {
                    "sample_id": row["sample_id"],
                    "sample_hash": row["sample_hash"],
                    "vector_hash": row["vector_hash"],
                }
                for row in membership_rows
                if row["role"] == fold_role
            ]
            canonical = json.dumps(
                members, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            expected_digests[fold_role] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as training_rights_unit:
            receipts = await DatabaseReplayTrainingRightsGate(
                training_rights_unit.session
            ).authorize(workspace_id=WORKSPACE, bundle_hash=STATE["bundle_hash"])
            expected_training_scope_digest = training_scope_digest(receipts)

        counts_before = tuple(
            (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM public.learning_run_source_rights_receipt),"
                        "(SELECT count(*) FROM public.learning_scorer_test_access_receipt),"
                        "(SELECT count(*) FROM public.learning_scorer_test_payload_consumption),"
                        "(SELECT count(*) FROM public.learning_scorer_test_evaluation_report)"
                    )
                )
            ).one()
        )
        await session.execute(text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader"))
        permissions = (
            await session.execute(
                text(
                    "SELECT has_table_privilege(current_user,"
                    "'learning_scorer_test_payload_consumption','INSERT'),"
                    "has_function_privilege(current_user,"
                    "'public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid)','EXECUTE'),"
                    "has_column_privilege(current_user,'public.backtest_trade_outcome',"
                    "'run_id','SELECT'),"
                    "has_column_privilege(current_user,'public.backtest_trade_outcome',"
                    "'outcome','SELECT'),"
                    "has_column_privilege(current_user,'public.backtest_decision_opportunity',"
                    "'manifest_hash','SELECT'),"
                    "has_column_privilege(current_user,'public.backtest_decision_opportunity',"
                    "'source_event_digests','SELECT')"
                )
            )
        ).one()
        assert tuple(permissions) == (False, True, True, False, True, False), tuple(permissions)
        bundle_diagnostics = (
            (
                await session.execute(
                    text(
                        "SELECT bundle.bundle_hash=:bundle_hash AS proof_bundle_hash, "
                        "bundle.rights_provider='CRYPIDIAN_FIRST_PARTY_QA' AS provider, "
                        "bundle.rights_purpose='BACKTEST_RESEARCH' "
                        "AS bundle_backtest_research_purpose, "
                        "bundle.schema_version=1 AS schema_version, "
                        "bundle.canonical_bundle::jsonb IS NOT DISTINCT FROM bundle.bundle_json "
                        "AS jsonb_parity, "
                        "public.wp06_compact_canonical_json(bundle.canonical_bundle::json) "
                        "IS NOT DISTINCT FROM bundle.canonical_bundle AS compact_parity, "
                        "pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
                        "bundle.canonical_bundle,'UTF8')),'hex')=bundle.bundle_hash "
                        "AS hash_parity, "
                        "bundle.bundle_json->>'workspace_id'=:workspace AS json_workspace, "
                        "bundle.bundle_json->>'schema_version'=bundle.schema_version::text "
                        "AS json_schema, "
                        "bundle.bundle_json->>'symbol'=bundle.symbol AS json_symbol, "
                        "bundle.bundle_json->>'evaluation_start'="
                        "(public.wp06_canonical_utc_timestamp_json(bundle.evaluation_start)"
                        "#>>'{}') "
                        "AS json_evaluation_start, "
                        "bundle.bundle_json->>'range_end'="
                        "(public.wp06_canonical_utc_timestamp_json(bundle.range_end)#>>'{}') "
                        "AS json_range_end, "
                        "bundle.bundle_json->>'rights_provider'=bundle.rights_provider "
                        "AS json_provider, "
                        "bundle.bundle_json->>'rights_purpose'=bundle.rights_purpose "
                        "AS json_backtest_research_purpose, "
                        "bundle.bundle_json->>'policy_fingerprint'=bundle.policy_fingerprint "
                        "AS json_policy_fingerprint, "
                        "pg_catalog.jsonb_typeof(bundle.bundle_json->'components')='array' "
                        "AND pg_catalog.jsonb_array_length(bundle.bundle_json->'components')>0 "
                        "AS components FROM public.backtest_dataset_replay_bundle AS bundle "
                        "WHERE bundle.workspace_id=CAST(:workspace AS uuid) "
                        "AND bundle.bundle_hash=:bundle_hash"
                    ),
                    {"workspace": str(WORKSPACE), "bundle_hash": STATE["bundle_hash"]},
                )
            )
            .mappings()
            .one()
        )
        if not all(bundle_diagnostics.values()):
            raise AssertionError(f"bundle field mismatch flags: {dict(bundle_diagnostics)}")
        child_diagnostics = (
            (
                await session.execute(
                    text(
                        "SELECT count(child.dataset_hash) AS child_count, "
                        "count(child.dataset_hash)="
                        "pg_catalog.jsonb_array_length(bundle.bundle_json->'components') "
                        "AS component_count, "
                        "bool_and(COALESCE(child.status='READY',false)) AS child_ready, "
                        "bool_and(COALESCE(child.dataset_hash="
                        "component.value->>'dataset_hash',false)) "
                        "AS child_hash_link, "
                        "bool_and(COALESCE(child.symbol=bundle.symbol,false)) AS child_symbol, "
                        "bool_and(COALESCE(child.interval=component.value->>'interval',false)) "
                        "AS child_interval, "
                        "bool_and(COALESCE(child.evaluation_start=bundle.evaluation_start,false)) "
                        "AS child_evaluation_start, "
                        "bool_and(COALESCE(child.range_end=bundle.range_end,false)) "
                        "AS child_range_end, "
                        "bool_and(COALESCE(child.policy_fingerprint="
                        "bundle.policy_fingerprint,false)) "
                        "AS historical_policy_fingerprint_link, "
                        "bool_and(COALESCE(child.manifest_json->>'dataset_hash'="
                        "child.dataset_hash,false)) "
                        "AS json_dataset_hash, "
                        "bool_and(COALESCE(child.manifest_json->>'status'=child.status,false)) "
                        "AS json_status, "
                        "bool_and(COALESCE(child.manifest_json->>'symbol'=child.symbol,false)) "
                        "AS json_symbol, "
                        "bool_and(COALESCE(child.manifest_json->>'interval'=child.interval,false)) "
                        "AS json_interval, "
                        "bool_and(COALESCE(child.manifest_json->>'warmup_start'="
                        "(public.wp06_canonical_utc_timestamp_json(child.warmup_start)"
                        "#>>'{}'),false)) "
                        "AS json_warmup_start, "
                        "bool_and(COALESCE(child.manifest_json->>'prepared_at'="
                        "(public.wp06_canonical_utc_timestamp_json(child.prepared_at)"
                        "#>>'{}'),false)) "
                        "AS json_prepared_at, "
                        "bool_and(COALESCE(child.manifest_json->>'evaluation_start'="
                        "(public.wp06_canonical_utc_timestamp_json(child.evaluation_start)"
                        "#>>'{}'),false)) "
                        "AS json_evaluation_start, "
                        "bool_and(COALESCE(child.manifest_json->>'range_end'="
                        "(public.wp06_canonical_utc_timestamp_json(child.range_end)"
                        "#>>'{}'),false)) "
                        "AS json_range_end, "
                        "bool_and(COALESCE(child.manifest_json->>'bars_content_hash'="
                        "component.value->>'bars_content_hash',false)) AS json_bars_hash, "
                        "bool_and(COALESCE(child.manifest_json->>'rights_workspace_id'="
                        ":workspace,false)) "
                        "AS json_workspace, "
                        "bool_and(COALESCE(child.manifest_json->>'provider'="
                        "bundle.rights_provider,false)) "
                        "AS json_provider, "
                        "bool_and(COALESCE(child.manifest_json->>'purpose'="
                        "bundle.rights_purpose,false)) "
                        "AS json_backfill_purpose, "
                        "bool_and(COALESCE(child.manifest_json->>'policy_fingerprint'="
                        "child.policy_fingerprint,false)) AS json_policy_fingerprint, "
                        "bool_and(COALESCE(child.manifest_json="
                        "child.canonical_manifest::jsonb,false)) "
                        "AS jsonb_canonical, "
                        "bool_and(COALESCE(public.wp06_compact_canonical_json("
                        "child.canonical_manifest::json)=child.canonical_manifest,false)) "
                        "AS compact_canonical, "
                        "bool_and(COALESCE(pg_catalog.length(child.canonical_manifest)-"
                        "pg_catalog.length(pg_catalog.replace(child.canonical_manifest,"
                        "',\"dataset_hash\":\"'||child.dataset_hash||'\"',''))="
                        "pg_catalog.length(',\"dataset_hash\":\"'||child.dataset_hash||'\"')"
                        ",false)) "
                        "AS unique_hash_member, "
                        "bool_and(COALESCE(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
                        "pg_catalog.replace(child.canonical_manifest,"
                        "',\"dataset_hash\":\"'||child.dataset_hash||'\"',''),'UTF8')),'hex')"
                        "=child.dataset_hash,false)) AS child_hash_valid "
                        "FROM public.backtest_dataset_replay_bundle AS bundle "
                        "CROSS JOIN LATERAL pg_catalog.jsonb_array_elements("
                        "bundle.bundle_json->'components') AS component(value) "
                        "LEFT JOIN public.backtest_dataset_manifest AS child "
                        "ON child.workspace_id=bundle.workspace_id "
                        "AND child.dataset_hash=component.value->>'dataset_hash' "
                        "WHERE bundle.workspace_id=CAST(:workspace AS uuid) "
                        "AND bundle.bundle_hash=:bundle_hash "
                        "GROUP BY bundle.bundle_json"
                    ),
                    {"workspace": str(WORKSPACE), "bundle_hash": STATE["bundle_hash"]},
                )
            )
            .mappings()
            .one()
        )
        if child_diagnostics["child_count"] < 1 or not all(
            value for key, value in child_diagnostics.items() if key != "child_count"
        ):
            raise AssertionError(f"bundle child field mismatch flags: {dict(child_diagnostics)}")
        resolution_started = asyncio.Event()

        async def resolve_current_rights_in_new_transaction():
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as resolver_unit:
                await resolver_unit.session.execute(text("SET LOCAL lock_timeout = '5000ms'"))
                await resolver_unit.session.execute(
                    text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader")
                )
                resolution_started.set()
                result = await resolver_unit.session.execute(
                    text(
                        "SELECT * FROM public.wp06_recheck_scorer_current_rights("
                        "CAST(:workspace AS uuid),CAST(:artifact_id AS uuid))"
                    ),
                    {"workspace": str(WORKSPACE), "artifact_id": str(artifact_id)},
                )
                return dict(zip(CURRENT_RIGHTS_COLUMNS, result.one(), strict=True))

        # Writer first: the C4 gateway waits on the QA source lock.
        pending_resolution = None
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as source_writer:
            await source_writer.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key,0))"),
                {"lock_key": f"research-source:{WORKSPACE}:{first_source_lock}"},
            )
            pending_resolution = asyncio.create_task(resolve_current_rights_in_new_transaction())
            await asyncio.wait_for(resolution_started.wait(), timeout=5)
            done, _ = await asyncio.wait({pending_resolution}, timeout=0.2)
            assert not done, "C4 verifier phải chờ writer nhả cùng source lock"
        assert pending_resolution is not None
        writer_first_rights = await asyncio.wait_for(pending_resolution, timeout=10)
        assert writer_first_rights["training_scope_digest"] == expected_training_scope_digest
        rights_result = await session.execute(
            text(
                "SELECT * FROM public.wp06_recheck_scorer_current_rights("
                "CAST(:workspace AS uuid),CAST(:artifact_id AS uuid))"
            ),
            {"workspace": str(WORKSPACE), "artifact_id": str(artifact_id)},
        )
        current_rights_row = dict(zip(CURRENT_RIGHTS_COLUMNS, rights_result.one(), strict=True))
        assert current_rights_row["scorer_artifact_id"] == artifact_id
        assert current_rights_row["training_membership_digest"] == expected_digests["train"]
        assert current_rights_row["validation_membership_digest"] == expected_digests["validation"]
        assert current_rights_row["training_scope_digest"] == expected_training_scope_digest
        assert current_rights_row["source_rights_digest"] == hashlib.sha256(b"[]").hexdigest()
        assert current_rights_row["fingerprint"]
        assert len(current_rights_row["fingerprint"]) == 64
        assert current_rights_row["checked_at"].tzinfo is not None
        assert current_rights_row["fingerprint"] == writer_first_rights["fingerprint"]

        # C4 verifier first: QA source writer cannot pass the held lock before caller commit.
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as source_writer:
                await source_writer.session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await source_writer.session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key,0))"),
                    {"lock_key": f"research-source:{WORKSPACE}:CRYPIDIAN_FIRST_PARTY_QA"},
                )
        except DBAPIError as error:
            if error.orig.sqlstate != "55P03":
                raise AssertionError(
                    "source writer phải chờ advisory source lock của verifier"
                ) from error
        else:
            raise AssertionError("source writer không bị khóa khi verifier đang giữ source lock")

        try:
            async with session.begin_nested():
                await session.execute(
                    text("SELECT set_config('app.workspace_id',CAST(:workspace AS text),true)"),
                    {"workspace": str(WORKSPACE)},
                )
                await session.execute(
                    text(
                        "SELECT * FROM public.wp06_recheck_scorer_current_rights("
                        "CAST(:other_workspace AS uuid),CAST(:artifact_id AS uuid))"
                    ),
                    {"other_workspace": str(uuid4()), "artifact_id": str(artifact_id)},
                )
        except DBAPIError as error:
            if "WP06_SCORER_RIGHTS_WORKSPACE_DENIED" not in str(error.orig):
                raise AssertionError("Gateway từ chối vì lỗi không đúng scope.") from error
        else:
            raise AssertionError("Gateway phải từ chối workspace không khớp GUC.")
        await session.execute(text("RESET ROLE"))
        counts_after = tuple(
            (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM public.learning_run_source_rights_receipt),"
                        "(SELECT count(*) FROM public.learning_scorer_test_access_receipt),"
                        "(SELECT count(*) FROM public.learning_scorer_test_payload_consumption),"
                        "(SELECT count(*) FROM public.learning_scorer_test_evaluation_report)"
                    )
                )
            ).one()
        )
        assert counts_after == counts_before, "Rights verifier must not consume or append receipts."
    return {
        "current_rights_verified": True,
        "c4_writer_first_verified": True,
        "c4_resolver_first_verified": True,
        "exact_train_validation_membership_verified": True,
        "backfill_provenance_backtest_research_verified": bundle_diagnostics[
            "bundle_backtest_research_purpose"
        ],
        "current_training_scope_digest_matched": current_rights_row["training_scope_digest"]
        == expected_training_scope_digest,
        "no_test_receipt_mutation": True,
        "rights_integrity_counts": [int(value) for value in counts_after],
    }


async def verify_manifest_source_ref_fence_v3(sessions):
    """Kiểm riêng canonical v3 và khóa source_ref bằng metadata an toàn."""
    from trading_bot.adapters.database.models.learning_entities import (
        DatasetManifestRecord,
    )
    from trading_bot.domain.learning.dataset import (
        DatasetManifest,
        DatasetSample,
        DatasetSourceReference,
        DatasetSplit,
    )
    from trading_bot.domain.research.source_policy import SourceUsePurpose

    now = datetime.now(UTC)
    source_id = "WP06_SCORER_SOURCE_FENCE"
    reference = DatasetSourceReference(
        source_id=source_id,
        item_id="fence-item",
        revision_id="fence-revision-1",
        policy_id="fence-policy-1",
        policy_sequence=1,
        purpose=SourceUsePurpose.TRAINING,
        rights_hash="a" * 64,
    )
    sample = DatasetSample(
        sample_id="fence-sample",
        decision_at=now + timedelta(hours=1),
        feature_available_at=now,
        outcome_start=now + timedelta(hours=1),
        outcome_end=now + timedelta(hours=2),
        feature_snapshot_hash="b" * 64,
        outcome_id="fence-outcome",
        outcome_hash="c" * 64,
        label_hash="d" * 64,
        label_value=1,
        label_version="fence-label-v1",
        split=DatasetSplit.TRAIN,
        source_dependent=True,
        source_rights_hashes=(),
        source_revision_refs=(),
        source_references=(reference,),
    )
    manifest = DatasetManifest(
        workspace_id=WORKSPACE,
        query_hash="e" * 64,
        symbols=("BTCUSDT",),
        data_start=now,
        data_end=now + timedelta(hours=3),
        as_of=now + timedelta(hours=3),
        feature_schema_version="fence-feature-schema-v1",
        feature_version="fence-feature-v1",
        label_version="fence-label-v1",
        sample_schema_version="fence-sample-v1",
        split_policy_version="fence-split-v1",
        samples=(sample,),
        exclusions=(),
        holdout_sealed_at=None,
        manifest_version="dataset-manifest-v3",
    )

    async def insert_reference(
        manifest_id,
        *,
        lock_timeout: str | None = None,
        started: asyncio.Event | None = None,
    ):
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as writer_unit:
                if lock_timeout is not None:
                    await writer_unit.session.execute(
                        text(f"SET LOCAL lock_timeout = '{lock_timeout}'")
                    )
                if started is not None:
                    started.set()
                await writer_unit.session.execute(
                    text(
                        "INSERT INTO public.dataset_manifest_source_ref "
                        "(id,workspace_id,manifest_id,sample_id,source_id,item_id,revision_id,"
                        "policy_id,policy_sequence,purpose,rights_hash) VALUES "
                        "(CAST(:id AS uuid),:workspace,:manifest_id,:sample_id,:source_id,:item_id,"
                        ":revision_id,:policy_id,:policy_sequence,:purpose,:rights_hash)"
                    ),
                    {
                        "id": str(uuid4()),
                        "workspace": WORKSPACE,
                        "manifest_id": manifest_id,
                        "sample_id": sample.sample_id,
                        "source_id": reference.source_id,
                        "item_id": reference.item_id,
                        "revision_id": reference.revision_id,
                        "policy_id": reference.policy_id,
                        "policy_sequence": reference.policy_sequence,
                        "purpose": reference.purpose.value,
                        "rights_hash": reference.rights_hash,
                    },
                )
        except DBAPIError as error:
            return error.orig.sqlstate, str(error.orig)
        return None, None

    async def add_manifest_while_writer_first():
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as manifest_writer:
            manifest_record = DatasetManifestRecord.from_domain(manifest)
            manifest_writer.session.add(manifest_record)
            await manifest_writer.session.flush()
            manifest_id = manifest_record.id

        async with sessions() as metadata_session, metadata_session.begin():
            await metadata_session.execute(
                text("SELECT set_config('app.workspace_id',:workspace,true)"),
                {"workspace": str(WORKSPACE)},
            )
            await metadata_session.execute(
                text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader")
            )
            projected = (
                (
                    await metadata_session.execute(
                        text(
                            "SELECT * FROM public.wp06_scorer_manifest_source_refs("
                            "CAST(:workspace AS uuid),CAST(:manifest_id AS uuid))"
                        ),
                        {"workspace": WORKSPACE, "manifest_id": manifest_id},
                    )
                )
                .mappings()
                .all()
            )
            assert len(projected) == 1
            assert projected[0]["source_id"] == source_id

        # Tạo writer-first trên một index còn thiếu: manifest canonical đã commit,
        # writer giữ khóa source trong lúc thêm ref; ref thứ hai phải đợi rồi xung đột.
        insert_started = asyncio.Event()
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as source_writer:
            await source_writer.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key,0))"),
                {"lock_key": f"research-source:{WORKSPACE}:{source_id}"},
            )
            await source_writer.session.execute(
                text(
                    "INSERT INTO public.dataset_manifest_source_ref "
                    "(id,workspace_id,manifest_id,sample_id,source_id,item_id,revision_id,"
                    "policy_id,policy_sequence,purpose,rights_hash) VALUES "
                    "(CAST(:id AS uuid),:workspace,:manifest_id,:sample_id,:source_id,:item_id,"
                    ":revision_id,:policy_id,:policy_sequence,:purpose,:rights_hash)"
                ),
                {
                    "id": str(uuid4()),
                    "workspace": WORKSPACE,
                    "manifest_id": manifest_id,
                    "sample_id": sample.sample_id,
                    "source_id": reference.source_id,
                    "item_id": reference.item_id,
                    "revision_id": reference.revision_id,
                    "policy_id": reference.policy_id,
                    "policy_sequence": reference.policy_sequence,
                    "purpose": reference.purpose.value,
                    "rights_hash": reference.rights_hash,
                },
            )
            pending_insert = asyncio.create_task(
                insert_reference(manifest_id, started=insert_started)
            )
            await asyncio.wait_for(insert_started.wait(), timeout=5)
            done, _ = await asyncio.wait({pending_insert}, timeout=0.2)
            assert not done, (
                "source_ref INSERT phải chờ khóa của writer đầu tiên; "
                f"hoàn tất sớm với {pending_insert.result()!r}"
            )
        sqlstate, _ = await asyncio.wait_for(pending_insert, timeout=10)
        assert sqlstate == "23505", sqlstate
        return manifest_id

    manifest_id = await add_manifest_while_writer_first()

    # Verifier first: lock source ID, then ensure a duplicate canonical ref waits.
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as verifier:
        await verifier.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key,0))"),
            {"lock_key": f"research-source:{WORKSPACE}:{source_id}"},
        )
        sqlstate, _ = await insert_reference(manifest_id, lock_timeout="200ms")
        assert sqlstate == "55P03", sqlstate

    # Một reference không có trong canonical bị trigger bound từ chối trước khi khóa.
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            async with unit.session.begin_nested():
                await unit.session.execute(
                    text(
                        "INSERT INTO public.dataset_manifest_source_ref "
                        "(id,workspace_id,manifest_id,sample_id,source_id,item_id,revision_id,"
                        "policy_id,policy_sequence,purpose,rights_hash) VALUES "
                        "(CAST(:id AS uuid),:workspace,:manifest_id,:sample_id,'FORGED_SOURCE',"
                        "'forged-item','forged-revision','forged-policy',1,'training',:rights_hash)"
                    ),
                    {
                        "id": str(uuid4()),
                        "workspace": WORKSPACE,
                        "manifest_id": manifest_id,
                        "sample_id": sample.sample_id,
                        "rights_hash": "f" * 64,
                    },
                )
    except DBAPIError as error:
        if "WP06_SOURCE_REF_NOT_IN_MANIFEST" not in str(error.orig):
            raise AssertionError(
                "source_ref sai canonical phải bị trigger bound từ chối"
            ) from error
    else:
        raise AssertionError("source_ref sai canonical đã được ghi")


async def verify_inconclusive_denial(sessions):
    """Admin verifier rejects synthetic QA rights and preserves deployment pointer."""
    assert STATE["report_status"] == "INCONCLUSIVE"
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        before = tuple(
            (
                await unit.session.execute(
                    text(
                        "SELECT pointer.event_id,pointer.revision,pointer.active_pointer_version,"
                        "pointer.mode,(SELECT count(*) FROM "
                        "public.learning_scoring_deployment_event AS event "
                        "WHERE event.workspace_id=pointer.workspace_id) AS event_count "
                        "FROM public.learning_scoring_deployment_pointer AS pointer "
                        "WHERE pointer.workspace_id=:workspace"
                    ),
                    {"workspace": WORKSPACE},
                )
            ).one()
        )
        try:
            async with unit.session.begin_nested():
                await unit.session.execute(
                    text(
                        "SELECT * FROM public.wp06_verify_scorer_deployment_rights("
                        "CAST(:workspace AS uuid),CAST(:artifact_id AS uuid),"
                        "CAST(:report_id AS uuid))"
                    ),
                    {
                        "workspace": str(WORKSPACE),
                        "artifact_id": STATE["scorer_artifact_id"],
                        "report_id": STATE["evaluation_report_id"],
                    },
                )
        except DBAPIError as error:
            if "WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED" not in str(error.orig):
                raise AssertionError("Gateway phải từ chối nguồn QA với marker riêng.") from error
        else:
            raise AssertionError("Synthetic QA rights không được cấp deployment eligibility.")
        try:
            async with unit.session.begin_nested():
                await unit.session.execute(
                    text(
                        "SELECT * FROM public.wp06_verify_scorer_deployment_rights("
                        "CAST(:workspace AS uuid),CAST(:artifact_id AS uuid),"
                        "CAST(:report_id AS uuid))"
                    ),
                    {
                        "workspace": str(uuid4()),
                        "artifact_id": STATE["scorer_artifact_id"],
                        "report_id": STATE["evaluation_report_id"],
                    },
                )
        except DBAPIError as error:
            if "WP06_SCORER_DEPLOYMENT_RIGHTS_WORKSPACE_DENIED" not in str(error.orig):
                raise AssertionError("Outer verifier từ chối tenant với mã lỗi khác.") from error
        else:
            raise AssertionError("Outer verifier phải từ chối tenant khác workspace GUC.")
        after = tuple(
            (
                await unit.session.execute(
                    text(
                        "SELECT pointer.event_id,pointer.revision,pointer.active_pointer_version,"
                        "pointer.mode,(SELECT count(*) FROM "
                        "public.learning_scoring_deployment_event AS event "
                        "WHERE event.workspace_id=pointer.workspace_id) AS event_count "
                        "FROM public.learning_scoring_deployment_pointer AS pointer "
                        "WHERE pointer.workspace_id=:workspace"
                    ),
                    {"workspace": WORKSPACE},
                )
            ).one()
        )
        assert after == before, "QA deployment denial must not mutate event or pointer."
    return {
        "inconclusive_deployment_denied": True,
        "qa_source_deployment_denied": True,
        "qa_denial_side_effect_free": True,
    }


async def verify_deployment_audit(sessions):
    """Kiểm quyền đọc bất biến bằng vai trò kiểm toán sau lời gọi admin-web."""
    expected = tuple(STATE["rights_integrity_counts"])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        actual = tuple(
            (
                await unit.session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM public.learning_run_source_rights_receipt),"
                        "(SELECT count(*) FROM public.learning_scorer_test_access_receipt),"
                        "(SELECT count(*) FROM public.learning_scorer_test_payload_consumption),"
                        "(SELECT count(*) FROM public.learning_scorer_test_evaluation_report)"
                    )
                )
            ).one()
        )
    assert actual == expected, "Outer verifier must not consume or append receipts."
    return {"outer_gateway_no_test_payload_mutation": True}


async def verify_market_signal_worker_deployment_rights(sessions):
    """Cho worker gọi verifier qua EXECUTE hẹp, không mở quyền bảng khớp lệnh."""
    from sqlalchemy.exc import DBAPIError

    forbidden_tables = (
        "trade_plan",
        "risk_decision",
        "risk_reservation",
        "order_intent",
        "exchange_order",
        "fill",
        "position_event",
        "trade",
        "trade_cost",
        "venue_order_intent",
        "venue_order_transition",
    )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        privileges = (
            (
                await unit.session.execute(
                    text(
                        "SELECT table_name, "
                        "has_table_privilege(session_user, 'public.' || table_name, 'SELECT') "
                        "AS can_select, "
                        "has_table_privilege(session_user, 'public.' || table_name, 'INSERT') "
                        "AS can_insert, "
                        "has_table_privilege(session_user, 'public.' || table_name, 'UPDATE') "
                        "AS can_update, "
                        "has_table_privilege(session_user, 'public.' || table_name, 'DELETE') "
                        "AS can_delete FROM unnest(CAST(:tables AS text[])) AS table_name"
                    ),
                    {"tables": list(forbidden_tables)},
                )
            )
            .mappings()
            .all()
        )
        assert len(privileges) == len(forbidden_tables)
        assert all(
            not any(row[key] for key in ("can_select", "can_insert", "can_update", "can_delete"))
            for row in privileges
        ), [
            row["table_name"]
            for row in privileges
            if any(row[key] for key in ("can_select", "can_insert", "can_update", "can_delete"))
        ]

        try:
            await unit.session.execute(
                text(
                    "SELECT * FROM public.wp06_verify_scorer_deployment_rights("
                    "CAST(:workspace AS uuid),CAST(:artifact_id AS uuid),"
                    "CAST(:report_id AS uuid))"
                ),
                {
                    "workspace": str(WORKSPACE),
                    "artifact_id": STATE["scorer_artifact_id"],
                    "report_id": STATE["evaluation_report_id"],
                },
            )
        except DBAPIError as error:
            if error.orig.diag.message_primary != "WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED":
                raise AssertionError(
                    "Market signal worker phải bị chặn với marker QA deployment cụ thể"
                ) from error
        else:
            raise AssertionError("INCONCLUSIVE report không thể thành quyền deploy cho worker")
    return {
        "worker_execute_granted": True,
        "inconclusive_report_denied": True,
        "execution_table_privileges_absent": True,
    }


async def verify_deployment_role_denial(sessions):
    """Evaluator role không được gọi scorer deployment rights gateway."""
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        try:
            await unit.session.execute(
                text(
                    "SELECT * FROM public.wp06_verify_scorer_deployment_rights("
                    "CAST(:workspace AS uuid),CAST(:artifact_id AS uuid),"
                    "CAST(:report_id AS uuid))"
                ),
                {
                    "workspace": str(WORKSPACE),
                    "artifact_id": STATE["scorer_artifact_id"],
                    "report_id": STATE["evaluation_report_id"],
                },
            )
        except DBAPIError as error:
            if error.orig.sqlstate != "42501":
                raise AssertionError(
                    "Evaluator phải bị chặn ở EXECUTE ACL trước khi vào function body."
                ) from error
        else:
            raise AssertionError("Evaluator không được gọi deployment rights verifier.")
    return {"deployment_caller_denied": True}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(sys.argv[1])), sort_keys=True))
