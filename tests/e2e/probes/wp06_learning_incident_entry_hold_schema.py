"""Probe schema incident bằng metadata seed; không tạo rights/receipt/report eligible."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Uuid, create_engine, insert, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.models import (
    AuditEvent,
    LearningDriftAssessmentMemberRecord,
    LearningDriftAssessmentRecord,
    LearningIncidentHoldEventRecord,
    LearningModelRegistryRecord,
    LearningRetrainProposalRecord,
    LearningShadowObservationRecord,
)
from trading_bot.domain.learning.incident_entry_hold import (
    LearningIncidentHoldAction,
    LearningIncidentHoldEvent,
    create_release_event,
)

TABLE = "public.learning_incident_entry_hold_event"


def _engine(user, filename):
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=user,
            password=Path(filename).read_text().strip(),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )


def _scope(connection, workspace_id):
    connection.execute(
        text("SELECT set_config('app.workspace_id', :value, true)"),
        {"value": str(workspace_id) if workspace_id else ""},
    )


def _expect(connection, statement, params=None, *, marker=None, sqlstate=None):
    caught = None
    try:
        with connection.begin_nested():
            connection.execute(statement, params or {})
    except DBAPIError as error:
        caught = error
    assert caught is not None, "Thao tác phải bị PostgreSQL từ chối."
    if marker:
        assert marker in str(caught.orig), str(caught.orig)
    if sqlstate:
        assert getattr(caught.orig, "sqlstate", None) == sqlstate, str(caught.orig)


def _values(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def _insert_event(connection, event):
    record = LearningIncidentHoldEventRecord.from_domain(event)
    connection.execute(insert(record.__table__), _values(record))


def _metadata_defaults(table, now):
    values = {}
    for column in table.columns:
        if column.nullable:
            values[column.name] = None
        elif isinstance(column.type, Uuid):
            values[column.name] = uuid4()
        elif isinstance(column.type, DateTime):
            values[column.name] = now - timedelta(minutes=2)
        elif isinstance(column.type, Boolean):
            values[column.name] = False
        elif isinstance(column.type, Integer):
            values[column.name] = 1
        elif isinstance(column.type, Float):
            values[column.name] = 0.2
        elif isinstance(column.type, String):
            values[column.name] = "a" * 64 if (column.type.length or 0) >= 64 else "fixture"
        elif isinstance(column.type, JSONB):
            values[column.name] = []
        else:
            raise AssertionError(f"Chưa có giá trị metadata cho {column.name}")
    return values


def _seed_metadata(connection, workspace_id, actor_id, now, *, seed_hold=True, mixed=False):
    """Chỉ superuser seed metadata dưới replica; không dựng source rights hoặc deployment."""
    proposal_id, assessment_id, registry_id, challenger_id = uuid4(), uuid4(), uuid4(), uuid4()
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    registry = _metadata_defaults(LearningModelRegistryRecord.__table__, now)
    registry.update(
        id=registry_id,
        workspace_id=workspace_id,
        state="evaluated",
        revision=3,
        created_by_membership_id=actor_id,
    )
    connection.execute(insert(LearningModelRegistryRecord.__table__), registry)
    # Proposal/assessment chỉ là metadata fixture, không chứng minh đánh giá hay source rights.
    assessment = _metadata_defaults(LearningDriftAssessmentRecord.__table__, now)
    assessment.update(
        id=assessment_id,
        workspace_id=workspace_id,
        policy_version="coverage-v2-30d",
        cohort_registry_id=challenger_id,
        cohort_revision=1,
        window_start=now - timedelta(minutes=4),
        window_end=now - timedelta(minutes=2),
        member_count=2,
        eligible_count=0,
        observation_window_days=30,
        minimum_observations=3,
        minimum_snapshot_coverage=0.8,
        minimum_quality_rate=0.9,
        data_quality_status="detected",
        performance_status="inconclusive",
        reason_codes=["coverage_below_threshold", "performance_not_evaluable"],
        membership_digest="b" * 64,
        idempotency_key=uuid4().hex * 2,
        content_hash="c" * 64,
    )
    connection.execute(insert(LearningDriftAssessmentRecord.__table__), assessment)
    proposal = _metadata_defaults(LearningRetrainProposalRecord.__table__, now)
    proposal.update(
        id=proposal_id,
        workspace_id=workspace_id,
        assessment_id=assessment_id,
        assessment_hash="c" * 64,
        policy_version="coverage-v2-30d",
        membership_digest="b" * 64,
        idempotency_key=uuid4().hex * 2,
        content_hash="d" * 64,
        reason_codes=["coverage_below_threshold"],
    )
    connection.execute(insert(LearningRetrainProposalRecord.__table__), proposal)
    for index in range(2):
        observation = _metadata_defaults(LearningShadowObservationRecord.__table__, now)
        observation.update(
            workspace_id=workspace_id,
            challenger_registry_id=challenger_id,
            active_registry_id=registry_id,
            active_registry_revision=3,
            active_artifact_hash="a" * 64,
            active_pointer_version=1 + (index if mixed else 0),
            incumbent_status="ABSTAIN",
            challenger_state="evaluated",
            challenger_decision="abstain",
            challenger_reason="UNCALIBRATED_SCORE",
            strategy_release_id="schema-fixture",
            idempotency_key=uuid4().hex * 2,
        )
        connection.execute(insert(LearningShadowObservationRecord.__table__), observation)
        member = _metadata_defaults(LearningDriftAssessmentMemberRecord.__table__, now)
        member.update(
            workspace_id=workspace_id,
            assessment_id=assessment_id,
            observation_id=observation["id"],
            ordinal=index,
            challenger_decision="abstain",
        )
        connection.execute(insert(LearningDriftAssessmentMemberRecord.__table__), member)
    hold = LearningIncidentHoldEvent(
        event_id=uuid4(),
        workspace_id=workspace_id,
        proposal_id=proposal_id,
        proposal_hash="d" * 64,
        assessment_id=assessment_id,
        assessment_hash="c" * 64,
        action=LearningIncidentHoldAction.HOLD,
        revision=1,
        previous_event_id=None,
        previous_event_hash=None,
        incumbent_registry_id=registry_id,
        incumbent_registry_revision=3,
        incumbent_artifact_hash="a" * 64,
        active_pointer_version=1,
        deployment_revision=1,
        actor_membership_id=actor_id,
        mfa_verified_at=now - timedelta(seconds=1),
        mfa_method="TOTP",
        reason="Metadata fixture kiểm schema, không phải đánh giá model",
        idempotency_key=uuid4(),
        request_id=str(uuid4()),
        created_at=now,
    )
    if seed_hold:
        _insert_event(connection, hold)
    connection.execute(text("SET LOCAL session_replication_role = origin"))
    return hold


def _release(hold):
    now = datetime.now(UTC)
    return create_release_event(
        event_id=uuid4(),
        previous_event=hold,
        actor_membership_id=hold.actor_membership_id,
        mfa_verified_at=now - timedelta(seconds=1),
        mfa_method="TOTP",
        reason="Đã kiểm tra metadata fixture schema",
        idempotency_key=uuid4(),
        request_id=str(uuid4()),
        created_at=now,
    )


def main():
    root = _engine(
        os.environ["TRADING_BOT_DATABASE_USER"], os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]
    )
    admin = _engine("crypidian_admin_web", "/run/secrets/database_admin_web_password")
    runtime = _engine("crypidian_trading_runtime", "/run/secrets/database_trading_runtime_password")
    workspace, other, empty = uuid4(), uuid4(), uuid4()
    actors = {}
    now = datetime.now(UTC) - timedelta(seconds=1)
    with root.begin() as connection:
        for identifier in (workspace, other, empty):
            user_id, actor_id = uuid4(), uuid4()
            connection.execute(
                text(
                    "INSERT INTO public.workspace(id,name,slug,mode,status) VALUES "
                    "(:id,'Schema incident',:slug,'PAPER','ACTIVE')"
                ),
                {"id": identifier, "slug": f"incident-{identifier}"},
            )
            connection.execute(
                text(
                    "INSERT INTO "
                    "public.user_account(id,email_normalized,password_hash,status) "
                    "VALUES (:id,:email,'schema-test','ACTIVE')"
                ),
                {"id": user_id, "email": f"{identifier}@example.test"},
            )
            connection.execute(
                text(
                    "INSERT INTO "
                    "public.user_membership(id,workspace_id,user_id,status) VALUES "
                    "(:id,:workspace,:user,'ACTIVE')"
                ),
                {"id": actor_id, "workspace": identifier, "user": user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO "
                    "public.role_binding(id,workspace_id,membership_id,role) VALUES "
                    "(:id,:workspace,:actor,'risk_admin')"
                ),
                {"id": uuid4(), "workspace": identifier, "actor": actor_id},
            )
            actors[identifier] = actor_id
        holds = [_seed_metadata(connection, workspace, actors[workspace], now) for _ in range(2)]
        other_hold = _seed_metadata(connection, other, actors[other], now)
        disabled = _seed_metadata(connection, empty, actors[empty], now, seed_hold=False)
        mixed = _seed_metadata(connection, empty, actors[empty], now, seed_hold=False, mixed=True)
        security = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE "
                "oid=CAST(:table AS regclass)"
            ),
            {"table": TABLE},
        ).one()
        assert tuple(security) == (True, True)
        role = connection.execute(
            text(
                "SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit FROM pg_roles "
                "WHERE rolname='crypidian_learning_incident_reader'"
            )
        ).one()
        assert tuple(role) == (False, False, False, False)
        assert (
            connection.scalar(
                text(
                    "SELECT proconfig::text FROM pg_proc WHERE "
                    "oid='public.wp06_learning_incident_hold_active(uuid)'::regprocedure"
                )
            )
            == '{"search_path=pg_catalog, pg_temp"}'
        )
    with root.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        whitespace_only = LearningIncidentHoldEventRecord.from_domain(disabled)
        whitespace_only.reason = "\t"
        _expect(
            connection,
            insert(whitespace_only.__table__),
            _values(whitespace_only),
            sqlstate="23514",
        )
        request_id_whitespace_only = LearningIncidentHoldEventRecord.from_domain(disabled)
        request_id_whitespace_only.request_id = "\t"
        _expect(
            connection,
            insert(request_id_whitespace_only.__table__),
            _values(request_id_whitespace_only),
            sqlstate="23514",
        )
        connection.execute(text("SET LOCAL session_replication_role = origin"))
    for identifier, expected in ((workspace, True), (other, True), (empty, False)):
        with runtime.begin() as connection:
            _scope(connection, identifier)
            assert (
                connection.scalar(
                    text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
                    {"workspace": identifier},
                )
                is expected
            )
            _expect(connection, text(f"SELECT * FROM {TABLE}"), sqlstate="42501")
            _expect(
                connection,
                text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
                {"workspace": uuid4()},
                marker="WP06_INCIDENT_WORKSPACE_DENIED",
            )
    with runtime.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        _scope(connection, workspace)
        _expect(
            connection,
            text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
            {"workspace": workspace},
            marker="WP06_INCIDENT_READ_COMMITTED_REQUIRED",
        )
    with admin.begin() as connection:
        for identifier, expected in ((workspace, 2), (other, 1), (None, 0)):
            _scope(connection, identifier)
            assert connection.scalar(text(f"SELECT count(*) FROM {TABLE}")) == expected
        _scope(connection, workspace)
        _expect(connection, text(f"UPDATE {TABLE} SET reason='sai'"), sqlstate="42501")
        _expect(connection, text(f"DELETE FROM {TABLE}"), sqlstate="42501")
        _expect(
            connection,
            insert(LearningIncidentHoldEventRecord.__table__),
            _values(LearningIncidentHoldEventRecord.from_domain(_release(other_hold))),
            marker="WP06_INCIDENT_WORKSPACE_DENIED",
        )
        bad = LearningIncidentHoldEventRecord.from_domain(_release(holds[0]))
        bad.event_payload = dict(bad.event_payload, raw_source="cấm")
        _expect(
            connection,
            insert(bad.__table__),
            _values(bad),
            marker="WP06_INCIDENT_CANONICAL_INVALID",
        )
    with root.begin() as connection:
        _expect(
            connection, text(f"UPDATE {TABLE} SET reason='sai'"), marker="WP06_INCIDENT_IMMUTABLE"
        )
        _expect(connection, text(f"DELETE FROM {TABLE}"), marker="WP06_INCIDENT_IMMUTABLE")
    for role in ("trainer_worker", "learning_evaluator", "market_signal_worker"):
        engine = _engine(f"crypidian_{role}", f"/run/secrets/database_{role}_password")
        with engine.begin() as connection:
            _scope(connection, workspace)
            _expect(connection, text(f"SELECT * FROM {TABLE}"), sqlstate="42501")
            _expect(
                connection,
                text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
                {"workspace": workspace},
                sqlstate="42501",
            )
        engine.dispose()
    with admin.begin() as connection:
        _scope(connection, empty)
        for hold, marker in (
            (mixed, "WP06_INCIDENT_MIXED_INCUMBENT"),
            (disabled, "WP06_INCIDENT_TARGET_STALE"),
        ):
            _expect(
                connection,
                text(
                    "SELECT * FROM "
                    "public.wp06_learning_incident_eligible_target(:workspace,:proposal)"
                ),
                {"workspace": empty, "proposal": hold.proposal_id},
                marker=marker,
            )
            _expect(
                connection,
                insert(LearningIncidentHoldEventRecord.__table__),
                _values(LearningIncidentHoldEventRecord.from_domain(hold)),
                marker=marker,
            )
    # Target thực vẫn DISABLED; RELEASE metadata fixture chạy guard/canonical/actor/FK thật.
    with admin.begin() as connection:
        _scope(connection, workspace)
        release = _release(holds[0])
        _insert_event(connection, release)
        _expect(
            connection,
            insert(LearningIncidentHoldEventRecord.__table__),
            _values(LearningIncidentHoldEventRecord.from_domain(_release(holds[0]))),
            marker="WP06_INCIDENT_STALE_REVISION",
        )
        _expect(
            connection,
            insert(LearningIncidentHoldEventRecord.__table__),
            _values(
                LearningIncidentHoldEventRecord.from_domain(
                    replace(holds[0], event_id=uuid4(), idempotency_key=uuid4())
                )
            ),
            marker="WP06_INCIDENT_STALE_REVISION",
        )
        assert (
            connection.scalar(
                text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
                {"workspace": workspace},
            )
            is True
        )

    def compete():
        try:
            with admin.begin() as connection:
                _scope(connection, workspace)
                _insert_event(connection, _release(holds[1]))
            return "won"
        except DBAPIError as error:
            if "WP06_INCIDENT_STALE_REVISION" not in str(error.orig):
                raise
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: compete(), range(2))) == ["conflict", "won"]
    audit_id = uuid4()
    rollback_error = None
    try:
        with admin.begin() as connection:
            _scope(connection, other)
            _insert_event(connection, _release(other_hold))
            connection.execute(
                insert(AuditEvent.__table__),
                {
                    "id": audit_id,
                    "workspace_id": other,
                    "actor_membership_id": actors[other],
                    "action": "learning.incident.release",
                    "target_type": "learning_incident",
                    "target_id": other_hold.proposal_id,
                    "reason": "Rollback fixture",
                    "request_id": uuid4(),
                    "occurred_at": datetime.now(UTC),
                },
            )
            raise RuntimeError("rollback-fixture")
    except RuntimeError as error:
        rollback_error = str(error)
    assert rollback_error == "rollback-fixture"
    with runtime.begin() as connection:
        _scope(connection, workspace)
        assert (
            connection.scalar(
                text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
                {"workspace": workspace},
            )
            is False
        )
        _scope(connection, other)
        assert (
            connection.scalar(
                text("SELECT public.wp06_learning_incident_hold_active(:workspace)"),
                {"workspace": other},
            )
            is True
        )
    with root.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM audit_event WHERE id=:id"), {"id": audit_id}
            )
            == 0
        )
        # Unique key được kiểm độc lập, không tắt constraint; chỉ bỏ trigger dưới superuser.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        duplicate = replace(holds[0], event_id=uuid4(), proposal_id=uuid4())
        _expect(
            connection,
            insert(LearningIncidentHoldEventRecord.__table__),
            _values(LearningIncidentHoldEventRecord.from_domain(duplicate)),
            sqlstate="23505",
        )
        connection.execute(text("SET LOCAL session_replication_role = origin"))
    for engine in (root, admin, runtime):
        engine.dispose()
    print(
        json.dumps(
            dict.fromkeys(
                (
                    "synthetic_metadata_only",
                    "forced_rls_and_workspace_isolation",
                    "runtime_execute_only",
                    "immutable_canonical_chain",
                    "terminal_release_and_cas",
                    "idempotency_uniqueness",
                    "mixed_incumbent_denied",
                    "disabled_gateway_denied",
                    "release_after_disabled_target",
                    "multiple_holds_and_audit_rollback",
                    "whitespace_only_reason_denied",
                    "whitespace_only_request_id_denied",
                ),
                True,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
