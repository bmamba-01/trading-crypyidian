"""Probe PostgreSQL cho scorer deployment CAS, RLS và fail-closed rights gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from trading_bot.adapters.database.engine import create_session_factory
from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
    LearningScoringDeploymentEventRecord,
)
from trading_bot.application.learning_scoring_deployment import ScorerDeploymentService
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
    ScorerDeploymentTarget,
)


def _engine(username: str, password: str) -> AsyncEngine:
    return create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username=username,
            password=password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        ),
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


async def _seed_workspace(engine: AsyncEngine, workspace_id, suffix: str) -> tuple[Any, Any]:
    user_id = uuid4()
    membership_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO public.workspace(id,name,slug,mode,status) "
                "VALUES (:id,:name,:slug,'PAPER','ACTIVE')"
            ),
            {"id": workspace_id, "name": f"Scorer test {suffix}", "slug": f"scorer-{suffix}"},
        )
        await connection.execute(
            text(
                "INSERT INTO public.user_account(id,email_normalized,password_hash,status) "
                "VALUES (:id,:email,'test-only-password-hash','ACTIVE')"
            ),
            {"id": user_id, "email": f"scorer-{suffix}@example.test"},
        )
        await connection.execute(
            text(
                "INSERT INTO public.user_membership(id,workspace_id,user_id,status) "
                "VALUES (:id,:workspace_id,:user_id,'ACTIVE')"
            ),
            {"id": membership_id, "workspace_id": workspace_id, "user_id": user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO public.role_binding(id,workspace_id,membership_id,role) "
                "VALUES (:id,:workspace_id,:membership_id,'owner')"
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "membership_id": membership_id},
        )
    return user_id, membership_id


def _principal(workspace_id, membership_id, *, verified_at: datetime | None = None) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=membership_id,
        workspace_id=workspace_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=verified_at or datetime.now(UTC) - timedelta(seconds=1),
        mfa_method=MfaMethod.TOTP,
    )


async def _assert_permission_denied(
    connection, statement: str, parameters: dict[str, object]
) -> None:
    try:
        async with connection.begin_nested():
            await connection.execute(text(statement), parameters)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "42501":
            raise
        return
    raise AssertionError("La consulta debe bị từ chối bởi quyền cột/bảng PostgreSQL.")


def _invalid_score_event_payload(*, nested_extra: bool) -> dict[str, object]:
    digest = "a" * 64
    timeframe_snapshot_ids: list[list[object]] = [["1m", digest]]
    source_event_digests = [["1m", digest]]
    if nested_extra:
        timeframe_snapshot_ids = [["1m", {"extra": "must be rejected"}]]
    payload: dict[str, object] = {
        "active_pointer_version": 0,
        "base_artifact_hash": digest,
        "bot_decision_hash": digest,
        "bot_decision_id": str(uuid4()),
        "bot_id": str(uuid4()),
        "candidate_completeness_verified": False,
        "candidate_hash": digest,
        "candidate_id": digest,
        "class_probabilities": [],
        "confidence": None,
        "decision": "ABSTAIN",
        "decision_snapshot_id": digest,
        "deployment_event_id": str(uuid4()),
        "deployment_revision": 1,
        "evaluation_report_hash": digest,
        "evaluation_report_id": str(uuid4()),
        "evaluation_status": "ACCEPTED_SHADOW_ONLY",
        "feature_schema_hash": digest,
        "feature_schema_version": "test-v1",
        "final_status": "CANDIDATE",
        "idempotency_key": digest,
        "incumbent_decision_hash": digest,
        "incumbent_status": "CANDIDATE",
        "mode": "SHADOW",
        "model_latency_ms": 0,
        "policy_hash": digest,
        "policy_id": "test-policy",
        "probability": None,
        "proof_hash": digest,
        "proof_id": str(uuid4()),
        "proof_scope_hash": digest,
        "reason_code": "test-abstain",
        "registry_id": str(uuid4()),
        "registry_revision": 1,
        "report_scope_hash": digest,
        "schema_version": "crypidian.learning.score-event.v1",
        "scorer_artifact_hash": digest,
        "scorer_artifact_id": str(uuid4()),
        "source_event_digests": source_event_digests,
        "source_rights_fingerprint": digest,
        "timeframe_snapshot_ids": timeframe_snapshot_ids,
        "uncertainty": None,
        "vector_hash": digest,
        "workspace_id": str(uuid4()),
    }
    if not nested_extra:
        payload["unrecognized_metadata"] = {"nested": "extra keys are not canonical"}
    return payload


def _score_event_result_payload(
    *,
    mode: str = "SHADOW",
    decision: str = "APPROVE",
    final_status: str = "CANDIDATE",
    class_probabilities: list[list[float | int]] | None = None,
    probability: float | None = 0.5,
    confidence: float | None = 0.5,
    uncertainty: float | None = 0.5,
    evaluation_status: str = "ACCEPTED_SHADOW_ONLY",
    candidate_completeness_verified: bool = False,
    reason_code: str | None = None,
) -> dict[str, object]:
    """Fixture canonical, rồi để probe gửi event thẳng bằng role worker."""
    payload = _invalid_score_event_payload(nested_extra=True)
    payload["timeframe_snapshot_ids"] = [["1m", "a" * 64]]
    payload["mode"] = mode
    payload["decision"] = decision
    payload["final_status"] = final_status
    payload["class_probabilities"] = class_probabilities or [[-1, 0.2], [0, 0.3], [1, 0.5]]
    payload["probability"] = probability
    payload["confidence"] = confidence
    payload["uncertainty"] = uncertainty
    payload["evaluation_status"] = evaluation_status
    payload["candidate_completeness_verified"] = candidate_completeness_verified
    payload["reason_code"] = reason_code
    if decision == "ABSTAIN":
        payload["class_probabilities"] = []
        payload["probability"] = None
        payload["confidence"] = None
        payload["uncertainty"] = None
        payload["reason_code"] = reason_code or "test-abstain"
    elif decision == "VETO":
        payload["reason_code"] = reason_code or "test-veto"
    if mode == "PAPER_GATE":
        payload["evaluation_status"] = "ACCEPTED_PAPER_GATE_ELIGIBLE"
        payload["candidate_completeness_verified"] = True
    payload.pop("unrecognized_metadata", None)
    return payload


async def _score_event_insert_reaches_expected_guard(
    engine: AsyncEngine,
    *,
    workspace_id,
    bot_id,
    payload: dict[str, object],
    expected_marker: str = "WP06_SCORE_EVENT_CANONICAL_INVALID",
) -> bool:
    payload["workspace_id"] = str(workspace_id)
    payload["bot_id"] = str(bot_id)
    canonical = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    event_id = uuid4()
    decision_id = UUID(payload["bot_decision_id"])
    deployment_event_id = UUID(payload["deployment_event_id"])
    registry_id = UUID(payload["registry_id"])
    artifact_id = UUID(payload["scorer_artifact_id"])
    report_id = UUID(payload["evaluation_report_id"])
    proof_id = UUID(payload["proof_id"])
    columns = {
        "id": event_id,
        "workspace_id": workspace_id,
        "bot_id": bot_id,
        "bot_decision_id": decision_id,
        "bot_decision_hash": payload["bot_decision_hash"],
        "incumbent_decision_hash": payload["incumbent_decision_hash"],
        "incumbent_status": payload["incumbent_status"],
        "final_status": payload["final_status"],
        "candidate_id": payload["candidate_id"],
        "candidate_hash": payload["candidate_hash"],
        "decision_snapshot_id": payload["decision_snapshot_id"],
        "vector_hash": payload["vector_hash"],
        "feature_schema_version": payload["feature_schema_version"],
        "feature_schema_hash": payload["feature_schema_hash"],
        "timeframe_snapshot_ids": json.dumps(payload["timeframe_snapshot_ids"]),
        "source_event_digests": json.dumps(payload["source_event_digests"]),
        "source_rights_fingerprint": payload["source_rights_fingerprint"],
        "mode": payload["mode"],
        "deployment_event_id": deployment_event_id,
        "deployment_revision": payload["deployment_revision"],
        "registry_id": registry_id,
        "registry_revision": payload["registry_revision"],
        "active_pointer_version": payload["active_pointer_version"],
        "scorer_artifact_id": artifact_id,
        "scorer_artifact_hash": payload["scorer_artifact_hash"],
        "base_artifact_hash": payload["base_artifact_hash"],
        "evaluation_report_id": report_id,
        "evaluation_report_hash": payload["evaluation_report_hash"],
        "evaluation_status": payload["evaluation_status"],
        "report_scope_hash": payload["report_scope_hash"],
        "proof_id": proof_id,
        "proof_hash": payload["proof_hash"],
        "proof_scope_hash": payload["proof_scope_hash"],
        "candidate_completeness_verified": payload["candidate_completeness_verified"],
        "policy_id": payload["policy_id"],
        "policy_hash": payload["policy_hash"],
        "decision": payload["decision"],
        "reason_code": payload["reason_code"],
        "class_probabilities": json.dumps(payload["class_probabilities"]),
        "probability": payload["probability"],
        "confidence": payload["confidence"],
        "uncertainty": payload["uncertainty"],
        "model_latency_ms": payload["model_latency_ms"],
        "idempotency_key": payload["idempotency_key"],
        "event_hash": event_hash,
        "canonical_event": canonical,
        "event_payload": canonical,
    }
    expressions = {
        "timeframe_snapshot_ids": "CAST(:timeframe_snapshot_ids AS jsonb)",
        "source_event_digests": "CAST(:source_event_digests AS jsonb)",
        "class_probabilities": "CAST(:class_probabilities AS jsonb)",
        "event_payload": "CAST(:event_payload AS jsonb)",
    }
    statement = (
        "INSERT INTO public.learning_score_event ("
        + ",".join(columns)
        + ") VALUES ("
        + ",".join(expressions.get(column, f":{column}") for column in columns)
        + ")"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            await connection.execute(
                text("SELECT set_config('app.bot_id', :bot_id, true)"),
                {"bot_id": str(bot_id)},
            )
            await connection.execute(text(statement), columns)
    except DBAPIError as error:
        message = str(error.orig)
        if expected_marker in message:
            return True
        sqlstate = getattr(error.orig, "sqlstate", None)
        raise AssertionError(
            "Score event insert failed for an unexpected PostgreSQL reason "
            f"(expected {expected_marker}, SQLSTATE {sqlstate}): {message}"
        ) from error
    return False


async def _score_event_insert_waits_for_pointer_lock(
    seed_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    *,
    workspace_id,
    bot_id,
    lock_key: str,
) -> bool:
    async with seed_engine.connect() as lock_connection:
        transaction = await lock_connection.begin()
        await lock_connection.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))"),
            {"key": lock_key},
        )
        attempt = asyncio.create_task(
            _score_event_insert_reaches_expected_guard(
                worker_engine,
                workspace_id=workspace_id,
                bot_id=bot_id,
                payload=_invalid_score_event_payload(nested_extra=False),
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(attempt), timeout=0.2)
            waited = False
        except TimeoutError:
            waited = True
        await transaction.commit()
        return waited and await attempt


async def _insert_and_expect_error(
    sessions: async_sessionmaker,
    *,
    workspace_id,
    membership_id,
    event: ScorerDeploymentEvent,
    marker: str,
) -> bool:
    try:
        async with UnitOfWork(
            sessions, workspace_id=workspace_id, actor_membership_id=membership_id
        ) as unit:
            unit.session.add(LearningScoringDeploymentEventRecord.from_domain(event))
            await unit.session.flush()
    except DBAPIError as error:
        return marker in str(error.orig)
    return False


async def _probe() -> dict[str, object]:
    admin_password = Path(os.environ["WP06_ADMIN_PASSWORD_FILE"]).read_text().strip()
    admin_engine = _engine("crypidian_admin_web", admin_password)
    admin_sessions = create_session_factory(admin_engine)

    worker_password = Path(os.environ["WP06_WORKER_PASSWORD_FILE"]).read_text().strip()
    worker_engine = _engine("crypidian_market_signal_worker", worker_password)

    super_password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    seed_engine = _engine(os.environ["TRADING_BOT_DATABASE_USER"], super_password)
    workspace_id, second_workspace_id = uuid4(), uuid4()
    _user_id, membership_id = await _seed_workspace(seed_engine, workspace_id, uuid4().hex[:8])
    _second_user_id, second_membership_id = await _seed_workspace(
        seed_engine, second_workspace_id, uuid4().hex[:8]
    )

    async with admin_engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == "crypidian_admin_web"
        table_state = {}
        for table in (
            "learning_scoring_deployment_event",
            "learning_scoring_deployment_pointer",
        ):
            table_state[table] = await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = CAST(:name AS regclass)"
                ),
                {"name": f"public.{table}"},
            )
            assert table_state[table].one() == (True, True)
        privileges = {
            role: {
                table: {
                    action: await connection.scalar(
                        text("SELECT has_table_privilege(:role, :table_name, :privilege)"),
                        {
                            "role": role,
                            "table_name": f"public.{table}",
                            "privilege": action,
                        },
                    )
                    for action in ("SELECT", "INSERT", "UPDATE", "DELETE")
                }
                for table in (
                    "learning_scoring_deployment_event",
                    "learning_scoring_deployment_pointer",
                )
            }
            for role in ("crypidian_admin_web", "crypidian_trading_runtime")
        }
        event_policy = (
            await connection.execute(
                text(
                    "SELECT cmd, with_check FROM pg_policies WHERE schemaname='public' "
                    "AND tablename='learning_scoring_deployment_event'"
                )
            )
        ).all()
        trigger_names = (
            await connection.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgrelid='public.learning_scoring_deployment_event'::regclass"
                )
            )
        ).all()
    assert privileges["crypidian_admin_web"]["learning_scoring_deployment_event"] == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["crypidian_admin_web"]["learning_scoring_deployment_pointer"] == {
        "SELECT": True,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["crypidian_trading_runtime"]["learning_scoring_deployment_event"] == {
        # C4 runtime chỉ đọc deployment qua SECURITY DEFINER resolver.
        "SELECT": False,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["crypidian_trading_runtime"]["learning_scoring_deployment_pointer"] == {
        "SELECT": False,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
    }
    assert any(
        command == "INSERT" and "current_setting" in (check or "")
        for command, check in event_policy
    )
    assert {
        "learning_scoring_deployment_event_guard",
        "learning_scoring_deployment_event_apply",
        "learning_scoring_deployment_event_immutable",
    } <= set(trigger_names)

    scorer_event_read = {
        "workspace_id",
        "id",
        "created_at",
        "mode",
        "decision",
        "reason_code",
        "probability",
        "confidence",
        "uncertainty",
    }
    scorer_event_protected = {"canonical_event", "event_payload", "candidate_hash"}
    scorer_event_worker_read = {
        "workspace_id",
        "bot_id",
        "bot_decision_id",
        "idempotency_key",
        "event_hash",
        "canonical_event",
    }
    async with seed_engine.connect() as connection:
        for column in scorer_event_read:
            assert (
                await connection.scalar(
                    text(
                        "SELECT has_column_privilege(:role, 'public.learning_score_event', "
                        ":column_name, 'SELECT')"
                    ),
                    {"role": "crypidian_admin_web", "column_name": column},
                )
                is True
            )
        for column in scorer_event_protected:
            assert (
                await connection.scalar(
                    text(
                        "SELECT has_column_privilege(:role, 'public.learning_score_event', "
                        ":column_name, 'SELECT')"
                    ),
                    {"role": "crypidian_admin_web", "column_name": column},
                )
                is False
            )
        for column in scorer_event_worker_read:
            assert (
                await connection.scalar(
                    text(
                        "SELECT has_column_privilege(:role, 'public.learning_score_event', "
                        ":column_name, 'SELECT')"
                    ),
                    {"role": "crypidian_market_signal_worker", "column_name": column},
                )
                is True
            )
        for column in {"event_payload", "candidate_hash"}:
            assert (
                await connection.scalar(
                    text(
                        "SELECT has_column_privilege(:role, 'public.learning_score_event', "
                        ":column_name, 'SELECT')"
                    ),
                    {"role": "crypidian_market_signal_worker", "column_name": column},
                )
                is False
            )

        worker_forbidden_projections = {
            "learning_scoring_deployment_pointer": {
                "workspace_id",
                "event_id",
                "revision",
                "active_pointer_version",
                "mode",
            },
            "learning_scoring_deployment_event": {
                "workspace_id",
                "id",
                "mode",
                "revision",
                "scorer_artifact_id",
                "evaluation_report_id",
            },
            "learning_model_active_pointer": {"workspace_id", "version"},
        }
        for table, columns in worker_forbidden_projections.items():
            for column in columns:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT has_column_privilege(:role, :table_name, "
                            ":column_name, 'SELECT')"
                        ),
                        {
                            "role": "crypidian_market_signal_worker",
                            "table_name": f"public.{table}",
                            "column_name": column,
                        },
                    )
                    is False
                )
        for table, columns in {
            "learning_scoring_deployment_event": {"reason", "canonical_event", "event_payload"},
            "learning_scoring_deployment_pointer": {"changed_at"},
            "learning_model_active_pointer": {"registry_id", "updated_by_membership_id"},
        }.items():
            for column in columns:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT has_column_privilege(:role, :table_name, "
                            ":column_name, 'SELECT')"
                        ),
                        {
                            "role": "crypidian_market_signal_worker",
                            "table_name": f"public.{table}",
                            "column_name": column,
                        },
                    )
                    is False
                )

    async with seed_engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET LOCAL ROLE crypidian_market_signal_worker"))
            await connection.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            await connection.execute(
                text("SELECT set_config('app.bot_id', :bot_id, true)"),
                {"bot_id": str(uuid4())},
            )
            await _assert_permission_denied(
                connection,
                "SELECT event_id,mode FROM public.learning_scoring_deployment_pointer LIMIT 1",
                {},
            )
            await _assert_permission_denied(
                connection,
                "SELECT id,mode FROM public.learning_scoring_deployment_event LIMIT 1",
                {},
            )
            await _assert_permission_denied(
                connection,
                "SELECT version FROM public.learning_model_active_pointer LIMIT 1",
                {},
            )
            await connection.execute(
                text(
                    "SELECT workspace_id,bot_id,bot_decision_id,idempotency_key,event_hash,"
                    "canonical_event FROM public.learning_score_event "
                    "WHERE workspace_id=:workspace_id AND bot_id=:bot_id"
                ),
                {"workspace_id": workspace_id, "bot_id": uuid4()},
            )
            await _assert_permission_denied(
                connection,
                "SELECT report FROM public.learning_scorer_test_evaluation_report LIMIT 1",
                {},
            )
            await _assert_permission_denied(
                connection,
                "SELECT canonical_event FROM public.learning_scoring_deployment_event LIMIT 1",
                {},
            )
            await _assert_permission_denied(
                connection,
                "SELECT event_payload FROM public.learning_score_event LIMIT 1",
                {},
            )

    score_event_bot_id = uuid4()
    unknown_score_event_rejected = await _score_event_insert_reaches_expected_guard(
        worker_engine,
        workspace_id=workspace_id,
        bot_id=score_event_bot_id,
        payload=_invalid_score_event_payload(nested_extra=False),
    )
    nested_score_event_rejected = await _score_event_insert_reaches_expected_guard(
        worker_engine,
        workspace_id=workspace_id,
        bot_id=score_event_bot_id,
        payload=_invalid_score_event_payload(nested_extra=True),
    )
    invalid_result_payloads = (
        _score_event_result_payload(class_probabilities=[[-1, 0.2], [0, 0.3], [0, 0.5]]),
        _score_event_result_payload(
            class_probabilities=[[-1, -0.1], [0, 0.3], [1, 0.8]],
            probability=0.8,
            confidence=0.8,
            uncertainty=0.2,
        ),
        _score_event_result_payload(
            class_probabilities=[[-1, 0.2], [0, 0.3], [1, 0.4]],
            probability=0.4,
            confidence=0.4,
            uncertainty=0.6,
        ),
        _score_event_result_payload(probability=0.2),
        _score_event_result_payload(confidence=0.6, uncertainty=0.4),
        _score_event_result_payload(confidence=0.5, uncertainty=0.4),
    )
    invalid_result_rejected = all(
        [
            await _score_event_insert_reaches_expected_guard(
                worker_engine,
                workspace_id=workspace_id,
                bot_id=score_event_bot_id,
                payload=payload,
                expected_marker="WP06_SCORE_EVENT_RESULT_INVALID",
            )
            for payload in invalid_result_payloads
        ]
    )
    invalid_paper_gate_payloads = (
        _score_event_result_payload(mode="PAPER_GATE", decision="APPROVE", final_status="VETO"),
        _score_event_result_payload(mode="PAPER_GATE", decision="VETO", final_status="CANDIDATE"),
        _score_event_result_payload(mode="PAPER_GATE", decision="ABSTAIN", final_status="VETO"),
    )
    paper_gate_mapping_rejected = all(
        [
            await _score_event_insert_reaches_expected_guard(
                worker_engine,
                workspace_id=workspace_id,
                bot_id=score_event_bot_id,
                payload=payload,
                expected_marker="WP06_SCORE_EVENT_RESULT_INVALID",
            )
            for payload in invalid_paper_gate_payloads
        ]
    )
    valid_paper_gate_payloads = (
        _score_event_result_payload(
            mode="PAPER_GATE", decision="APPROVE", final_status="CANDIDATE"
        ),
        _score_event_result_payload(mode="PAPER_GATE", decision="VETO", final_status="VETO"),
        _score_event_result_payload(mode="PAPER_GATE", decision="ABSTAIN", final_status="ABSTAIN"),
    )
    valid_paper_gate_semantics_preserved = all(
        [
            await _score_event_insert_reaches_expected_guard(
                worker_engine,
                workspace_id=workspace_id,
                bot_id=score_event_bot_id,
                payload=payload,
                expected_marker="WP06_SCORE_EVENT_DECISION_MISMATCH",
            )
            for payload in valid_paper_gate_payloads
        ]
    )
    valid_shadow_veto_reaches_decision_check = await _score_event_insert_reaches_expected_guard(
        worker_engine,
        workspace_id=workspace_id,
        bot_id=score_event_bot_id,
        payload=_score_event_result_payload(decision="VETO", final_status="CANDIDATE"),
        expected_marker="WP06_SCORE_EVENT_DECISION_MISMATCH",
    )
    scoring_pointer_race_waits = await _score_event_insert_waits_for_pointer_lock(
        seed_engine,
        worker_engine,
        workspace_id=workspace_id,
        bot_id=score_event_bot_id,
        lock_key=f"learning-scoring-deployment:{workspace_id}:current-pointer",
    )
    model_pointer_race_waits = await _score_event_insert_waits_for_pointer_lock(
        seed_engine,
        worker_engine,
        workspace_id=workspace_id,
        bot_id=score_event_bot_id,
        lock_key=f"learning-model-registry:{workspace_id}:active-pointer",
    )

    async with seed_engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET LOCAL ROLE crypidian_admin_web"))
            await connection.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            await connection.execute(
                text(
                    "SELECT workspace_id,id,created_at,mode,decision,reason_code,probability,"
                    "confidence,uncertainty FROM public.learning_score_event "
                    "WHERE workspace_id=:workspace_id ORDER BY created_at DESC LIMIT 5"
                ),
                {"workspace_id": workspace_id},
            )
            await _assert_permission_denied(
                connection,
                "SELECT event_payload FROM public.learning_score_event LIMIT 1",
                {},
            )

    actor = _principal(workspace_id, membership_id)
    facade = ScorerDeploymentService(admin_sessions).for_principal(actor)
    idempotency_key = uuid4()
    first = await facade.set_mode(
        mode=ScorerDeploymentMode.DISABLED,
        expected_pointer_version=0,
        expected_deployment_revision=0,
        idempotency_key=idempotency_key,
        reason="Tắt scorer sau khi rà soát trạng thái.",
    )
    retry = (
        await ScorerDeploymentService(admin_sessions)
        .for_principal(
            _principal(
                workspace_id, membership_id, verified_at=datetime.now(UTC) - timedelta(seconds=2)
            )
        )
        .set_mode(
            mode=ScorerDeploymentMode.DISABLED,
            expected_pointer_version=0,
            expected_deployment_revision=0,
            idempotency_key=idempotency_key,
            reason="Tắt scorer sau khi rà soát trạng thái.",
        )
    )
    same_intent_retry = retry.event_id == first.event_id
    try:
        await facade.set_mode(
            mode=ScorerDeploymentMode.DISABLED,
            expected_pointer_version=0,
            expected_deployment_revision=0,
            idempotency_key=idempotency_key,
            reason="Tắt scorer sau khi quyền nguồn bị thu hồi.",
        )
    except Exception as error:
        changed_intent_conflict = "ý định deployment khác" in str(error)
    else:
        changed_intent_conflict = False

    state = await facade.load_admin_scoring_deployment()
    current_state_readable = (
        state.mode is ScorerDeploymentMode.DISABLED
        and state.deployment_revision == 1
        and state.active_pointer_version == 0
        and not state.runtime_usable
    )

    async def turn_off_with_key(key):
        try:
            return await facade.set_mode(
                mode=ScorerDeploymentMode.DISABLED,
                expected_pointer_version=0,
                expected_deployment_revision=1,
                idempotency_key=key,
                reason="Tắt scorer sau khi rà soát trạng thái.",
            )
        except Exception as error:  # kết quả race được phân loại bên dưới
            return error

    race_results = await asyncio.gather(turn_off_with_key(uuid4()), turn_off_with_key(uuid4()))
    race_has_one_winner = (
        sum(isinstance(result, ScorerDeploymentEvent) for result in race_results) == 1
    )
    race_has_one_conflict = sum(isinstance(result, Exception) for result in race_results) == 1
    race_diagnostics = [f"{type(result).__name__}: {result}" for result in race_results]
    assert race_has_one_winner, race_diagnostics
    assert race_has_one_conflict, race_diagnostics
    winner = next(result for result in race_results if isinstance(result, ScorerDeploymentEvent))

    # Mô phỏng pointer legacy active nhưng chưa có quyền hiện hành được xác minh;
    # runtime phải từ chối, còn admin đọc CAS hiện hành để ghi event DISABLED.
    async with seed_engine.begin() as connection:
        await connection.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await connection.execute(
            text(
                "UPDATE public.learning_scoring_deployment_pointer SET mode='SHADOW' "
                "WHERE workspace_id=:workspace_id"
            ),
            {"workspace_id": workspace_id},
        )
    stale_active_state = await facade.load_admin_scoring_deployment()
    stale_active_readable = (
        stale_active_state.mode is ScorerDeploymentMode.SHADOW
        and stale_active_state.deployment_revision == winner.revision
        and stale_active_state.active_pointer_version == 0
        and not stale_active_state.runtime_usable
    )
    runtime_rejects_unverified_active = False
    try:
        await facade.load_active_scoring_deployment()
    except Exception as error:
        runtime_rejects_unverified_active = "WP06_SCORER_RUNTIME_TARGET_EVENT_STALE" in str(error)
    disabled_after_stale_read = await facade.set_mode(
        mode=ScorerDeploymentMode.DISABLED,
        expected_pointer_version=stale_active_state.active_pointer_version,
        expected_deployment_revision=stale_active_state.deployment_revision,
        idempotency_key=uuid4(),
        reason="Tắt scorer vì cần rà soát quyền hiện hành.",
    )

    stale_cas_rejected = False
    try:
        await facade.set_mode(
            mode=ScorerDeploymentMode.DISABLED,
            expected_pointer_version=0,
            expected_deployment_revision=winner.revision,
            idempotency_key=uuid4(),
            reason="Tắt scorer bằng CAS cũ để xác minh lỗi.",
        )
    except Exception as error:
        stale_cas_rejected = "CAS đã cũ" in str(error)

    shadow_target = ScorerDeploymentTarget(
        registry_id=uuid4(),
        registry_revision=1,
        active_pointer_version=0,
        scorer_artifact_id=uuid4(),
        scorer_artifact_hash="a" * 64,
        evaluation_report_id=uuid4(),
        evaluation_report_hash="b" * 64,
        evaluation_status="ACCEPTED_SHADOW_ONLY",
        proof_id=uuid4(),
        proof_hash="c" * 64,
        candidate_completeness_verified=False,
        report_scope_hash="d" * 64,
        proof_scope_hash="d" * 64,
        policy_id="scorer-policy-v1",
        policy_hash="e" * 64,
        current_rights_receipt_hash="f" * 64,
    )
    shadow_event = ScorerDeploymentEvent(
        event_id=uuid4(),
        workspace_id=workspace_id,
        revision=disabled_after_stale_read.revision + 1,
        mode=ScorerDeploymentMode.SHADOW,
        target=shadow_target,
        actor_membership_id=membership_id,
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=1),
        mfa_method="TOTP",
        expected_pointer_version=0,
        expected_deployment_revision=disabled_after_stale_read.revision,
        reason="Bật SHADOW chỉ để test quyền gate.",
        idempotency_key=uuid4(),
        previous_event_id=disabled_after_stale_read.event_id,
        previous_event_hash=disabled_after_stale_read.event_hash,
        created_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    shadow_rejected = await _insert_and_expect_error(
        admin_sessions,
        workspace_id=workspace_id,
        membership_id=membership_id,
        event=shadow_event,
        marker="WP06_SCORER_RUNTIME_TARGET_POINTER_STALE",
    )

    bad_cas_event = ScorerDeploymentEvent(
        event_id=uuid4(),
        workspace_id=workspace_id,
        revision=disabled_after_stale_read.revision + 2,
        mode=ScorerDeploymentMode.DISABLED,
        target=None,
        actor_membership_id=membership_id,
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=1),
        mfa_method="TOTP",
        expected_pointer_version=0,
        expected_deployment_revision=disabled_after_stale_read.revision + 1,
        reason="Ghi event có CAS cũ để test rollback.",
        idempotency_key=uuid4(),
        previous_event_id=disabled_after_stale_read.event_id,
        previous_event_hash=disabled_after_stale_read.event_hash,
        created_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    rollback_after_cas_rejection = await _insert_and_expect_error(
        admin_sessions,
        workspace_id=workspace_id,
        membership_id=membership_id,
        event=bad_cas_event,
        marker="WP06_SCORING_DEPLOYMENT_STALE_REVISION",
    )

    async with admin_sessions() as database, database.begin():
        await database.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(workspace_id)},
        )
        event_count = await database.scalar(
            text("SELECT count(*) FROM public.learning_scoring_deployment_event")
        )
    async with admin_sessions() as database, database.begin():
        await database.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(second_workspace_id)},
        )
        cross_workspace_events = await database.scalar(
            text("SELECT count(*) FROM public.learning_scoring_deployment_event")
        )

    await admin_engine.dispose()
    await worker_engine.dispose()
    await seed_engine.dispose()
    result: dict[str, object] = {
        "admin_insert_policy": True,
        "cas_race_one_winner": race_has_one_winner,
        "cas_race_one_conflict": race_has_one_conflict,
        "changed_intent_conflict": changed_intent_conflict,
        "cross_workspace_rls": cross_workspace_events == 0,
        "current_state_readable": current_state_readable,
        "event_count": event_count,
        "least_privilege": True,
        "same_intent_retry": same_intent_retry,
        "shadow_rights_gate_rejected": shadow_rejected,
        "stale_active_admin_readable": stale_active_readable,
        "stale_active_disabled": disabled_after_stale_read.mode is ScorerDeploymentMode.DISABLED,
        "runtime_rejects_unverified_active": runtime_rejects_unverified_active,
        "stale_cas_rejected": stale_cas_rejected,
        "stale_cas_rollback": rollback_after_cas_rejection,
        "forced_rls": True,
        "score_event_unknown_key_rejected": unknown_score_event_rejected,
        "score_event_nested_value_rejected": nested_score_event_rejected,
        "score_event_invalid_result_rejected": invalid_result_rejected,
        "score_event_paper_gate_mapping_rejected": paper_gate_mapping_rejected,
        "score_event_valid_paper_gate_mappings_preserved": valid_paper_gate_semantics_preserved,
        "score_event_valid_shadow_veto_preserved": valid_shadow_veto_reaches_decision_check,
        "score_event_scoring_pointer_lock_wait": scoring_pointer_race_waits,
        "score_event_model_pointer_lock_wait": model_pointer_race_waits,
    }
    assert all(value is True for key, value in result.items() if key not in {"event_count"}), result
    assert event_count == 3, result
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_probe()), sort_keys=True))
