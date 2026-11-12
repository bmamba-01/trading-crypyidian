"""Kiểm tra WP06.7 trực tiếp trên PostgreSQL dùng Compose thật."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

STATE = json.loads(os.environ["WP06_SHADOW_DRIFT_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
OTHER_WORKSPACE = UUID(STATE["other_workspace_id"])
OBSERVER = "crypidian_learning_shadow_observer"
ADMIN_WEB = "crypidian_admin_web"
TABLE = "public.learning_drift_assessment"


def _connect(user: str, password_file: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=user,
        password=Path(password_file).read_text(encoding="utf-8").strip(),
    )


def _scope(connection: psycopg.Connection[Any], workspace_id: UUID | None) -> None:
    value = str(workspace_id) if workspace_id else ""
    connection.execute(
        "SELECT pg_catalog.set_config('app.workspace_id', %s, true)",
        (value,),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _assessment_values(
    connection: psycopg.Connection[Any], workspace_id: UUID, *, reasons: list[str]
) -> dict[str, Any]:
    created_at = connection.execute("SELECT transaction_timestamp()").fetchone()[0]
    values: dict[str, Any] = {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "policy_version": "coverage-v2-30d",
        "member_count": 0,
        "eligible_count": 0,
        "observation_window_days": 30,
        "minimum_observations": 3,
        "minimum_snapshot_coverage": 0.8,
        "minimum_quality_rate": 0.9,
        "data_quality_status": "inconclusive",
        "performance_status": "inconclusive",
        "reason_codes": json.dumps(reasons),
        "membership_digest": _digest("[]"),
        "created_at": created_at,
    }
    values["idempotency_key"] = _digest(
        _canonical(
            {
                "membership_digest": values["membership_digest"],
                "policy_version": values["policy_version"],
                "registry_id": None,
                "revision": None,
                "workspace_id": str(workspace_id),
            }
        )
    )
    values["content_hash"] = _digest(_canonical(_assessment_payload(values)))
    return values


def _assessment_payload(values: dict[str, Any]) -> dict[str, Any]:
    created_at = values["created_at"].astimezone(UTC)
    return {
        "assessment_id": str(values["id"]),
        "abstention_rate": None,
        "created_at": created_at.isoformat(),
        "cohort_registry_id": None,
        "cohort_revision": None,
        "data_quality_status": values["data_quality_status"],
        "eligible_count": values["eligible_count"],
        "member_count": values["member_count"],
        "member_ids": [],
        "membership_digest": values["membership_digest"],
        "performance_status": values["performance_status"],
        "policy_version": values["policy_version"],
        "quality_rate": None,
        "reason_codes": json.loads(values["reason_codes"]),
        "snapshot_coverage_rate": None,
        "window_end": None,
        "window_start": None,
        "workspace_id": str(values["workspace_id"]),
    }


def _proposal_values(
    assessment: dict[str, Any], *, reasons: list[str], created_at: datetime
) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "id": uuid4(),
        "workspace_id": assessment["workspace_id"],
        "assessment_id": assessment["id"],
        "assessment_hash": assessment["content_hash"],
        "policy_version": assessment["policy_version"],
        "membership_digest": assessment["membership_digest"],
        "reason_codes": reasons,
        "created_at": created_at.astimezone(UTC),
    }
    proposal["idempotency_key"] = _digest(
        _canonical(
            {
                "assessment_hash": proposal["assessment_hash"],
                "membership_digest": proposal["membership_digest"],
                "policy_version": proposal["policy_version"],
                "workspace_id": str(proposal["workspace_id"]),
            }
        )
    )
    proposal["content_hash"] = _digest(
        _canonical(
            {
                "assessment_hash": proposal["assessment_hash"],
                "assessment_id": str(proposal["assessment_id"]),
                "created_at": proposal["created_at"].isoformat(),
                "idempotency_key": proposal["idempotency_key"],
                "membership_digest": proposal["membership_digest"],
                "policy_version": proposal["policy_version"],
                "proposal_id": str(proposal["id"]),
                "reason_codes": proposal["reason_codes"],
                "workspace_id": str(proposal["workspace_id"]),
            }
        )
    )
    return proposal


def _insert_assessment(connection: psycopg.Connection[Any], values: dict[str, Any]) -> None:
    connection.execute(
        f"""
        INSERT INTO {TABLE} (
            id, workspace_id, policy_version, cohort_registry_id, cohort_revision,
            window_start, window_end, created_at,
            member_count, eligible_count, observation_window_days, minimum_observations,
            minimum_snapshot_coverage, minimum_quality_rate,
            snapshot_coverage_rate, quality_rate, abstention_rate,
            data_quality_status, performance_status, reason_codes,
            membership_digest, idempotency_key, content_hash
        ) VALUES (
            %(id)s, %(workspace_id)s, %(policy_version)s, NULL, NULL, NULL, NULL, %(created_at)s,
            %(member_count)s, %(eligible_count)s, %(observation_window_days)s,
            %(minimum_observations)s,
            %(minimum_snapshot_coverage)s, %(minimum_quality_rate)s,
            NULL, NULL, NULL, %(data_quality_status)s, %(performance_status)s,
            %(reason_codes)s::jsonb, %(membership_digest)s, %(idempotency_key)s,
            %(content_hash)s
        )
        """,
        values,
    )


def _expect_sqlstate(
    connection: psycopg.Connection[Any], sql: str, params: tuple[Any, ...], state: str
) -> bool:
    try:
        connection.execute(sql, params)
    except psycopg.Error as error:
        connection.rollback()
        if error.sqlstate != state:
            raise AssertionError(
                f"Mong đợi SQLSTATE {state}, nhận {error.sqlstate}: {error}"
            ) from error
        return True
    connection.rollback()
    raise AssertionError(f"Câu lệnh không bị từ chối với SQLSTATE {state}")


def _expect_marker(
    connection: psycopg.Connection[Any], sql: str, params: tuple[Any, ...], marker: str
) -> bool:
    try:
        connection.execute(sql, params)
        connection.commit()
    except psycopg.Error as error:
        message = str(error)
        connection.rollback()
        if marker not in message:
            raise AssertionError(f"Mong đợi marker {marker}, nhận lỗi: {message}") from error
        return True
    connection.rollback()
    raise AssertionError(f"Câu lệnh không bị trigger từ chối với marker {marker}")


def _expect_assessment_marker(
    connection: psycopg.Connection[Any], values: dict[str, Any], marker: str
) -> bool:
    try:
        _insert_assessment(connection, values)
        connection.commit()
    except psycopg.Error as error:
        message = str(error)
        connection.rollback()
        if marker not in message:
            raise AssertionError(f"Mong đợi marker {marker}, nhận lỗi: {message}") from error
        return True
    connection.rollback()
    raise AssertionError(f"Assessment không bị trigger từ chối với marker {marker}")


def _seed_workspaces() -> None:
    with _connect("crypidian_admin", "/run/secrets/postgres_password") as connection:
        connection.execute(
            """
            INSERT INTO public.workspace (id, name, slug, mode, status)
            VALUES (%s, 'WP06 drift E2E', %s, 'PAPER', 'ACTIVE'),
                   (%s, 'WP06 drift other E2E', %s, 'PAPER', 'ACTIVE')
            """,
            (
                WORKSPACE,
                f"wp06-drift-{WORKSPACE.hex[:12]}",
                OTHER_WORKSPACE,
                f"wp06-drift-other-{OTHER_WORKSPACE.hex[:12]}",
            ),
        )


def _assert_empty_assessment_rls(observer: psycopg.Connection[Any]) -> bool:
    for workspace_id in (WORKSPACE, OTHER_WORKSPACE):
        _scope(observer, workspace_id)
        assert observer.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
    _scope(observer, None)
    assert observer.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
    observer.commit()
    return True


def _assert_bad_observation_lineage(observer: psycopg.Connection[Any]) -> dict[str, bool]:
    sql = """
        INSERT INTO public.learning_shadow_observation (
            id, workspace_id, incumbent_decision_id, decision_hash,
            strategy_release_id, strategy_release_hash, incumbent_status,
            decision_evaluated_at, snapshot_id, snapshot_hash,
            snapshot_schema_hash, snapshot_as_of, challenger_registry_id,
            active_registry_id, active_registry_revision, active_artifact_hash,
            challenger_artifact_id, challenger_artifact_hash, challenger_manifest_hash,
            challenger_fold_plan_hash, training_approval_hash, challenger_revision,
            challenger_state, registry_approval_event_id, registry_approval_event_hash,
            registry_approval_event_sequence, active_pointer_version, snapshot_coverage,
            snapshot_quality_ok, schema_compatible, challenger_decision,
            challenger_reason, rights_eligible, rights_fingerprint, rights_source_id,
            rights_policy_id, rights_policy_sequence, rights_known_at, observed_at,
            idempotency_key, content_hash
        ) VALUES (
            %s, %s, %s, %s, 'baseline-v1', %s, 'CANDIDATE', %s,
            %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, 1,
            'evaluated', %s, %s, 1, 1, 1.0, true, true, 'abstain',
            'MODEL_UNAVAILABLE', false, NULL, NULL, NULL, NULL, NULL, %s, %s, %s
        )
    """
    _scope(observer, WORKSPACE)
    hash_parity_values = _new_invalid_observation_values(observer)
    hash_parity = _assert_observation_hash_parity(hash_parity_values)

    content_hash_rejected = False
    _scope(observer, WORKSPACE)
    forged_content = _new_invalid_observation_values(observer)
    forged_content["content_hash"] = _digest("forged-observation-content")
    content_hash_rejected = _expect_marker(
        observer,
        sql,
        _observation_insert_params(forged_content),
        "WP06_SHADOW_OBSERVATION_CONTENT_HASH_INVALID",
    )

    _scope(observer, WORKSPACE)
    forged_idempotency = _new_invalid_observation_values(observer)
    forged_idempotency["idempotency_key"] = _digest("forged-observation-idempotency")
    idempotency_rejected = _expect_marker(
        observer,
        sql,
        _observation_insert_params(forged_idempotency),
        "WP06_SHADOW_OBSERVATION_IDEMPOTENCY_INVALID",
    )

    _scope(observer, WORKSPACE)
    values = _new_invalid_observation_values(observer)
    lineage_rejected = _expect_marker(
        observer,
        sql,
        _observation_insert_params(values),
        "WP06_SHADOW_OBSERVATION_LINEAGE_INVALID",
    )
    assert content_hash_rejected
    assert idempotency_rejected
    assert lineage_rejected
    return {
        "observation_hash_parity": hash_parity,
        "observation_content_hash_rejected": content_hash_rejected,
        "observation_idempotency_rejected": idempotency_rejected,
        "observation_lineage_fails_closed": lineage_rejected,
    }


def _new_invalid_observation_values(
    observer: psycopg.Connection[Any],
) -> dict[str, Any]:
    now = observer.execute("SELECT transaction_timestamp()").fetchone()[0]
    fake_hash = _digest("wp06-invalid-observation")
    values = {
        "id": uuid4(),
        "workspace_id": WORKSPACE,
        "incumbent_decision_id": uuid4(),
        "decision_hash": fake_hash,
        "strategy_release_id": "baseline-v1",
        "strategy_release_hash": fake_hash,
        "incumbent_status": "CANDIDATE",
        "decision_evaluated_at": now,
        "snapshot_id": "s" * 64,
        "snapshot_hash": fake_hash,
        "snapshot_schema_hash": fake_hash,
        "snapshot_as_of": now,
        "challenger_registry_id": uuid4(),
        "active_registry_id": uuid4(),
        "active_registry_revision": 1,
        "active_artifact_hash": fake_hash,
        "challenger_artifact_id": uuid4(),
        "challenger_artifact_hash": fake_hash,
        "challenger_manifest_hash": fake_hash,
        "challenger_fold_plan_hash": fake_hash,
        "training_approval_hash": fake_hash,
        "challenger_revision": 1,
        "challenger_state": "evaluated",
        "registry_approval_event_id": uuid4(),
        "registry_approval_event_hash": fake_hash,
        "registry_approval_event_sequence": 1,
        "active_pointer_version": 1,
        "snapshot_coverage": 1.0,
        "snapshot_quality_ok": True,
        "schema_compatible": True,
        "rights_eligible": False,
        "rights_fingerprint": None,
        "rights_source_id": None,
        "rights_policy_id": None,
        "rights_policy_sequence": None,
        "rights_known_at": None,
        "challenger_decision": "abstain",
        "challenger_reason": "MODEL_UNAVAILABLE",
        "observed_at": now,
    }
    values["idempotency_key"] = _observation_idempotency_key(values)
    values["content_hash"] = _observation_content_hash(values)
    return values


def _observation_idempotency_key(values: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "active_artifact_hash": values["active_artifact_hash"],
            "active_registry_id": str(values["active_registry_id"]),
            "active_registry_revision": values["active_registry_revision"],
            "active_pointer_version": values["active_pointer_version"],
            "challenger_registry_id": str(values["challenger_registry_id"]),
            "challenger_revision": values["challenger_revision"],
            "registry_approval_event_hash": values["registry_approval_event_hash"],
            "registry_approval_event_id": str(values["registry_approval_event_id"]),
            "registry_approval_event_sequence": values["registry_approval_event_sequence"],
            "challenger_reason": values["challenger_reason"],
            "incumbent_decision_id": str(values["incumbent_decision_id"]),
            "incumbent_decision_hash": values["decision_hash"],
            "incumbent_strategy_release_hash": values["strategy_release_hash"],
            "snapshot_hash": values["snapshot_hash"],
            "snapshot_coverage": values["snapshot_coverage"],
            "snapshot_quality_ok": values["snapshot_quality_ok"],
            "schema_compatible": values["schema_compatible"],
            "rights_eligible": values["rights_eligible"],
            "rights_fingerprint": values["rights_fingerprint"],
            "rights_source_id": values["rights_source_id"],
            "rights_policy_id": values["rights_policy_id"],
            "rights_policy_sequence": values["rights_policy_sequence"],
            "rights_known_at": _iso(values["rights_known_at"]),
            "workspace_id": str(values["workspace_id"]),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _digest(canonical)


def _observation_content_hash(values: dict[str, Any]) -> str:
    canonical = json.dumps(
        _observation_content_payload(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _digest(canonical)


def _observation_content_payload(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_artifact_hash": values["active_artifact_hash"],
        "active_registry_id": str(values["active_registry_id"]),
        "active_registry_revision": values["active_registry_revision"],
        "active_pointer_version": values["active_pointer_version"],
        "challenger_model": {
            "artifact_hash": values["challenger_artifact_hash"],
            "artifact_id": str(values["challenger_artifact_id"]),
            "fold_plan_hash": values["challenger_fold_plan_hash"],
            "manifest_hash": values["challenger_manifest_hash"],
            "registry_approval_event_hash": values["registry_approval_event_hash"],
            "registry_approval_event_id": str(values["registry_approval_event_id"]),
            "registry_approval_event_sequence": values["registry_approval_event_sequence"],
            "registry_id": str(values["challenger_registry_id"]),
            "revision": values["challenger_revision"],
            "state": values["challenger_state"],
            "training_approval_hash": values["training_approval_hash"],
            "workspace_id": str(values["workspace_id"]),
        },
        "challenger_reason": values["challenger_reason"],
        "challenger_snapshot_state": {
            "coverage": values["snapshot_coverage"],
            "quality_ok": values["snapshot_quality_ok"],
            "schema_compatible": values["schema_compatible"],
        },
        "incumbent_decision": {
            "decision_hash": values["decision_hash"],
            "decision_id": str(values["incumbent_decision_id"]),
            "evaluated_at": _iso(values["decision_evaluated_at"]),
            "status": values["incumbent_status"],
            "strategy_release_hash": values["strategy_release_hash"],
            "strategy_release_id": values["strategy_release_id"],
        },
        "observation_id": str(values["id"]),
        "observed_at": _iso(values["observed_at"]),
        "source_rights": {
            "eligible": values["rights_eligible"],
            "fingerprint": values["rights_fingerprint"],
            "known_at": _iso(values["rights_known_at"]),
            "policy_id": values["rights_policy_id"],
            "policy_sequence": values["rights_policy_sequence"],
            "source_id": values["rights_source_id"],
        },
        "snapshot": {
            "as_of": _iso(values["snapshot_as_of"]),
            "schema_hash": values["snapshot_schema_hash"],
            "snapshot_hash": values["snapshot_hash"],
            "snapshot_id": values["snapshot_id"],
        },
        "workspace_id": str(values["workspace_id"]),
    }


def _assert_observation_hash_parity(values: dict[str, Any]) -> bool:
    row_values = {
        key: value
        for key, value in values.items()
        if key
        in {
            "id",
            "workspace_id",
            "incumbent_decision_id",
            "decision_hash",
            "strategy_release_id",
            "strategy_release_hash",
            "incumbent_status",
            "decision_evaluated_at",
            "snapshot_id",
            "snapshot_hash",
            "snapshot_schema_hash",
            "snapshot_as_of",
            "challenger_registry_id",
            "active_registry_id",
            "active_registry_revision",
            "active_artifact_hash",
            "challenger_artifact_id",
            "challenger_artifact_hash",
            "challenger_manifest_hash",
            "challenger_fold_plan_hash",
            "training_approval_hash",
            "challenger_revision",
            "challenger_state",
            "registry_approval_event_id",
            "registry_approval_event_hash",
            "registry_approval_event_sequence",
            "active_pointer_version",
            "snapshot_coverage",
            "snapshot_quality_ok",
            "schema_compatible",
            "rights_eligible",
            "rights_fingerprint",
            "rights_source_id",
            "rights_policy_id",
            "rights_policy_sequence",
            "rights_known_at",
            "challenger_decision",
            "challenger_reason",
            "observed_at",
            "idempotency_key",
            "content_hash",
        }
    }

    def encode_value(value: Any) -> Any:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return _iso(value)
        return value

    row_json = json.dumps(
        {key: encode_value(value) for key, value in row_values.items()},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expression = "pg_catalog.json_populate_record("
    expression += "NULL::public.learning_shadow_observation, %s::json)"
    with _connect("crypidian_admin", "/run/secrets/postgres_password") as owner:
        raw_document, compact_document, database_key, database_fields = owner.execute(
            "SELECT document.value::text, public.wp06_compact_canonical_json(document.value), "
            "public.wp06_shadow_observation_idempotency_key(populated.row_value), "
            "ROW((populated.row_value).challenger_reason, (populated.row_value).id, "
            "(populated.row_value).observed_at, (populated.row_value).snapshot_coverage)::text "
            "FROM (SELECT " + expression + " AS row_value) AS populated "
            "CROSS JOIN LATERAL (SELECT public.wp06_shadow_observation_canonical_document("
            "populated.row_value) AS value) AS document",
            (row_json,),
        ).fetchone()
    expected_document = json.dumps(
        _observation_content_payload(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if compact_document != expected_document:
        raise AssertionError(
            "Canonical JSON shadow lệch giữa Python/PostgreSQL. "
            f"Python={expected_document}; PostgreSQL={compact_document}; "
            f"raw={raw_document}; fields={database_fields}; input={row_json}"
        )
    if database_key != values["idempotency_key"]:
        raise AssertionError("Idempotency key shadow lệch giữa Python/PostgreSQL.")
    return True


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _observation_insert_params(values: dict[str, Any]) -> tuple[Any, ...]:
    return (
        values["id"],
        values["workspace_id"],
        values["incumbent_decision_id"],
        values["decision_hash"],
        values["strategy_release_hash"],
        values["decision_evaluated_at"],
        values["snapshot_id"],
        values["snapshot_hash"],
        values["snapshot_schema_hash"],
        values["snapshot_as_of"],
        values["challenger_registry_id"],
        values["active_registry_id"],
        values["active_artifact_hash"],
        values["challenger_artifact_id"],
        values["challenger_artifact_hash"],
        values["challenger_manifest_hash"],
        values["challenger_fold_plan_hash"],
        values["training_approval_hash"],
        values["registry_approval_event_id"],
        values["registry_approval_event_hash"],
        values["observed_at"],
        values["idempotency_key"],
        values["content_hash"],
    )


def _assert_bounded_query_cutoffs_and_expiry() -> bool:
    from psycopg.rows import dict_row
    from sqlalchemy.dialects import postgresql

    from trading_bot.adapters.database.repositories.learning_drift import (
        LearningDriftRepository,
        _member_from_mapping,
    )
    from trading_bot.domain.learning.drift import assess_learning_drift

    # Fixture tạm chỉ chứa metadata; truy vấn production chạy trên PostgreSQL thật.
    with _connect("crypidian_admin", "/run/secrets/postgres_password") as connection:
        connection.execute(
            "CREATE TEMP TABLE learning_shadow_observation "
            "(LIKE public.learning_shadow_observation INCLUDING DEFAULTS)"
        )
        connection.execute("CREATE TEMP TABLE bot_decision (LIKE public.bot_decision)")
        registry = uuid4()
        as_of = connection.execute("SELECT transaction_timestamp()").fetchone()[0]
        ids = [UUID(int=index + 1) for index in range(8)]
        times = [
            as_of - timedelta(days=30, microseconds=1),
            as_of - timedelta(days=30),
            as_of - timedelta(days=1),
            as_of,
            as_of + timedelta(microseconds=1),
            as_of,
            as_of,
            as_of,
        ]
        # Bỏ NOT NULL fixture cho các cột lineage không được projection đọc.
        columns = connection.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = "
            "'pg_temp.learning_shadow_observation'::regclass AND attnum > 0"
        ).fetchall()
        for (column,) in columns:
            connection.execute(
                psycopg.sql.SQL(
                    "ALTER TABLE pg_temp.learning_shadow_observation ALTER COLUMN {} DROP NOT NULL"
                ).format(psycopg.sql.Identifier(column))
            )
        for index, observed_at in enumerate(times):
            connection.execute(
                "INSERT INTO pg_temp.learning_shadow_observation "
                "(id, workspace_id, challenger_registry_id, challenger_revision, observed_at, "
                "snapshot_hash, snapshot_coverage, snapshot_quality_ok, schema_compatible, "
                "rights_eligible, challenger_decision, challenger_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,1,true,true,false,'abstain',"
                "'SOURCE_RIGHTS_INELIGIBLE')",
                (
                    ids[index],
                    OTHER_WORKSPACE if index == 5 else WORKSPACE,
                    uuid4() if index == 6 else registry,
                    2 if index == 7 else 1,
                    observed_at,
                    "1" * 64,
                ),
            )

        def read(end: datetime) -> tuple[Any, ...]:
            query = LearningDriftRepository.cohort_statement(
                workspace_id=WORKSPACE,
                registry_id=registry,
                revision=1,
                window_start=end - timedelta(days=30),
                window_end=end,
            ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(str(query))
                return tuple(_member_from_mapping(row) for row in cursor.fetchall())

        members = read(as_of)
        assert tuple(member.observation.observation_id for member in members) == tuple(ids[1:4])
        # Loại observation tương lai trước khi tiến mốc: kiểm việc hết hạn độc lập.
        connection.execute(
            "DELETE FROM pg_temp.learning_shadow_observation WHERE id = %s", (ids[4],)
        )
        expired = read(as_of + timedelta(microseconds=1))
        assert tuple(member.observation.observation_id for member in expired) == tuple(ids[2:4])
        first_digest = LearningDriftRepository.membership_digest(tuple(ids[1:4]))
        next_digest = LearningDriftRepository.membership_digest(tuple(ids[2:4]))
        assert first_digest != next_digest
        assessment = assess_learning_drift(
            workspace_id=WORKSPACE,
            assessment_id=uuid4(),
            policy=LearningDriftRepository.server_policy(),
            observations=tuple(member.observation for member in expired),
            created_at=as_of + timedelta(microseconds=1),
        )
        assert assessment.member_count == 2
        assert assessment.data_quality_status.value == "inconclusive"
        assert assessment.performance_status.value == "inconclusive"
        assert "low_sample" in [reason.value for reason in assessment.reason_codes]
        connection.rollback()
    return True


def _assert_bounded_policy_schema() -> bool:
    with _connect(
        OBSERVER, "/run/secrets/database_learning_shadow_observer_password"
    ) as connection:
        row = connection.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'learning_drift_assessment' "
            "AND column_name = 'observation_window_days'"
        ).fetchone()
        assert row is not None
        index = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = "
            "'ix_learning_shadow_observation_cohort_time'"
        ).fetchone()
        assert index is not None
        assert "observed_at" in index[0]
    return True


def main() -> None:
    _seed_workspaces()
    with _connect(OBSERVER, "/run/secrets/database_learning_shadow_observer_password") as observer:
        who = observer.execute("SELECT session_user, current_user").fetchone()
        assert who == (OBSERVER, OBSERVER)
        _scope(observer, WORKSPACE)
        forged_policy = _assessment_values(
            observer,
            WORKSPACE,
            reasons=["low_sample", "performance_not_evaluable"],
        )
        forged_policy["minimum_observations"] = 1
        drift_policy_rejected = _expect_assessment_marker(
            observer,
            forged_policy,
            "WP06_SHADOW_DRIFT_POLICY_INVALID",
        )

        _scope(observer, WORKSPACE)
        forged_duration = _assessment_values(
            observer, WORKSPACE, reasons=["low_sample", "performance_not_evaluable"]
        )
        forged_duration["observation_window_days"] = 31
        assert _expect_assessment_marker(
            observer, forged_duration, "WP06_SHADOW_DRIFT_POLICY_INVALID"
        )

        _scope(observer, WORKSPACE)
        target_assessment = _assessment_values(
            observer,
            WORKSPACE,
            reasons=["low_sample", "performance_not_evaluable"],
        )
        empty_assessment_rejected = _expect_assessment_marker(
            observer,
            target_assessment,
            "WP06_SHADOW_DRIFT_EMPTY_COHORT",
        )
        assessment_rows_scoped_per_workspace = _assert_empty_assessment_rls(observer)

        _scope(observer, OTHER_WORKSPACE)
        denied = _expect_sqlstate(
            observer,
            f"""
            INSERT INTO {TABLE} (
                id, workspace_id, policy_version, cohort_registry_id, cohort_revision,
                window_start, window_end,
                member_count, eligible_count,
                observation_window_days, minimum_observations,
                minimum_snapshot_coverage, minimum_quality_rate,
                data_quality_status, performance_status, reason_codes,
                membership_digest, idempotency_key, content_hash
            ) VALUES (%s, %s, 'coverage-v2-30d', NULL, NULL, transaction_timestamp(),
                      transaction_timestamp(), 1, 0, 30, 3, 0.8, 0.9,
                      'inconclusive', 'inconclusive',
                      '["low_sample","performance_not_evaluable"]'::jsonb,
                      %s, %s, %s)
            """,
            (
                uuid4(),
                WORKSPACE,
                _digest("[]"),
                _digest("cross-workspace-idempotency"),
                _digest("cross-workspace-content"),
            ),
            "42501",
        )
        cross_workspace_insert_denied = denied

        _scope(observer, WORKSPACE)
        rights_helpers = observer.execute(
            """
            SELECT pg_catalog.has_function_privilege(
                       current_user,
                       'public.wp06_shadow_current_paper_rights_hash('
                       'uuid,text,text,integer,timestamp with time zone)',
                       'EXECUTE'
                   ),
                   pg_catalog.has_function_privilege(
                       current_user,
                       'public.wp06_shadow_current_paper_rights_receipt(uuid)',
                       'EXECUTE'
                   ),
                   public.wp06_shadow_current_paper_rights_receipt(%s)
            """,
            (WORKSPACE,),
        ).fetchone()
        rights_hash_helper_execute_denied = rights_helpers[0] is False
        rights_receipt_helper_execute_allowed = rights_helpers[1] is True
        rights_receipt_fails_closed = rights_helpers[2] is None
        source_rights_mappings = observer.execute(
            """
            SELECT pg_catalog.has_function_privilege(
                       current_user,
                       'public.wp06_shadow_market_source_rights_matches(text,text)',
                       'EXECUTE'
                   ),
                   public.wp06_shadow_market_source_rights_matches(
                       'BINANCE_USDM', 'BINANCE_USDM_REST'
                   ),
                   public.wp06_shadow_market_source_rights_matches(
                       'BINANCE_USDM_REST_BACKFILL', 'BINANCE_USDM_REST'
                   ),
                   public.wp06_shadow_market_source_rights_matches(
                       'BINANCE_RB_' || pg_catalog.repeat('a', 64), 'BINANCE_USDM_REST'
                   ),
                   public.wp06_shadow_market_source_rights_matches(
                       'BINANCE_XRBX' || pg_catalog.repeat('a', 64), 'BINANCE_USDM_REST'
                   ),
                   public.wp06_shadow_market_source_rights_matches(
                       'BINANCE_USDM', 'BINANCE_USDM'
                   ),
                   public.wp06_shadow_market_source_rights_matches(
                       'BINANCE_USDM_REST_BACKFILL_EXTRA', 'BINANCE_USDM_REST'
                   )
            """
        ).fetchone()
        source_rights_mapping_execute_allowed = source_rights_mappings[0] is True
        source_rights_mapping_exact = tuple(source_rights_mappings[1:]) == (
            True,
            True,
            False,
            False,
            False,
            False,
        )

        source_policy_privileges = observer.execute(
            """
            SELECT pg_catalog.has_table_privilege(
                       current_user, 'public.source_policy_revision', 'SELECT'
                   ),
                   pg_catalog.has_table_privilege(
                       current_user, 'public.source_policy_observation', 'SELECT'
                   )
            """
        ).fetchone()
        direct_source_policy_select_denied = source_policy_privileges == (False, False)
        observer.commit()

        _expect_sqlstate(
            observer,
            "SELECT workspace_id FROM public.source_policy_revision LIMIT 0",
            (),
            "42501",
        )
        _expect_sqlstate(
            observer,
            "SELECT workspace_id FROM public.source_policy_observation LIMIT 0",
            (),
            "42501",
        )

        _scope(observer, WORKSPACE)
        canonical_source_cost_execute_allowed = observer.execute(
            "SELECT pg_catalog.has_function_privilege("
            "current_user, 'public.wp06_canonical_source_cost(numeric)', 'EXECUTE')"
        ).fetchone()[0]
        cost_cases = (
            (Decimal("0"), "0"),
            (Decimal("1000.000"), "1E+3"),
            (Decimal("0.000001"), "0.000001"),
            (Decimal("0.0000001"), "1E-7"),
            (Decimal("0.0000012"), "0.0000012"),
            (Decimal("123.4500"), "123.45"),
        )
        actual_costs = tuple(
            observer.execute(
                "SELECT public.wp06_canonical_source_cost(%s::numeric)",
                (value,),
            ).fetchone()[0]
            for value, _ in cost_cases
        )
        canonical_source_cost_parity = actual_costs == tuple(expected for _, expected in cost_cases)
        observer.commit()

    with _connect(OBSERVER, "/run/secrets/database_learning_shadow_observer_password") as observer:
        sensitive_columns_denied = _expect_sqlstate(
            observer,
            "SELECT features FROM public.feature_snapshot LIMIT 0",
            (),
            "42501",
        )
        observation_guard_results = _assert_bad_observation_lineage(observer)

        _scope(observer, WORKSPACE)
        proposal_sql = """
            INSERT INTO public.learning_retrain_proposal (
                id, workspace_id, assessment_id, assessment_hash, policy_version,
                membership_digest, idempotency_key, reason_codes, content_hash, created_at
            ) VALUES (%s, %s, %s, %s, 'coverage-v2-30d', %s, %s,
                      '["coverage_below_threshold"]'::jsonb, %s, %s)
        """
        correct_proposal = _proposal_values(
            target_assessment,
            reasons=["coverage_below_threshold"],
            created_at=observer.execute("SELECT transaction_timestamp()").fetchone()[0],
        )
        forged_proposal_hash = dict(correct_proposal)
        forged_proposal_hash["content_hash"] = _digest("forged-proposal-content")
        proposal_content_hash_rejected = _expect_marker(
            observer,
            proposal_sql,
            (
                forged_proposal_hash["id"],
                WORKSPACE,
                forged_proposal_hash["assessment_id"],
                forged_proposal_hash["assessment_hash"],
                forged_proposal_hash["membership_digest"],
                forged_proposal_hash["idempotency_key"],
                forged_proposal_hash["content_hash"],
                forged_proposal_hash["created_at"],
            ),
            "WP06_RETRAIN_PROPOSAL_CONTENT_HASH_INVALID",
        )
        _scope(observer, WORKSPACE)
        forged_proposal_key = _proposal_values(
            target_assessment,
            reasons=["coverage_below_threshold"],
            created_at=observer.execute("SELECT transaction_timestamp()").fetchone()[0],
        )
        forged_proposal_key["idempotency_key"] = _digest("forged-proposal-idempotency")
        forged_proposal_key["content_hash"] = _digest(
            _canonical(
                {
                    "assessment_hash": forged_proposal_key["assessment_hash"],
                    "assessment_id": str(forged_proposal_key["assessment_id"]),
                    "created_at": forged_proposal_key["created_at"].isoformat(),
                    "idempotency_key": forged_proposal_key["idempotency_key"],
                    "membership_digest": forged_proposal_key["membership_digest"],
                    "policy_version": forged_proposal_key["policy_version"],
                    "proposal_id": str(forged_proposal_key["id"]),
                    "reason_codes": forged_proposal_key["reason_codes"],
                    "workspace_id": str(forged_proposal_key["workspace_id"]),
                }
            )
        )
        proposal_idempotency_rejected = _expect_marker(
            observer,
            proposal_sql,
            (
                forged_proposal_key["id"],
                WORKSPACE,
                forged_proposal_key["assessment_id"],
                forged_proposal_key["assessment_hash"],
                forged_proposal_key["membership_digest"],
                forged_proposal_key["idempotency_key"],
                forged_proposal_key["content_hash"],
                forged_proposal_key["created_at"],
            ),
            "WP06_RETRAIN_PROPOSAL_IDEMPOTENCY_INVALID",
        )
        _scope(observer, WORKSPACE)
        correct_proposal = _proposal_values(
            target_assessment,
            reasons=["coverage_below_threshold"],
            created_at=observer.execute("SELECT transaction_timestamp()").fetchone()[0],
        )
        proposal_hash_parity = _expect_marker(
            observer,
            proposal_sql,
            (
                correct_proposal["id"],
                WORKSPACE,
                correct_proposal["assessment_id"],
                correct_proposal["assessment_hash"],
                correct_proposal["membership_digest"],
                correct_proposal["idempotency_key"],
                correct_proposal["content_hash"],
                correct_proposal["created_at"],
            ),
            "WP06_SHADOW_RETRAIN_PROPOSAL_ASSESSMENT_INVALID",
        )

        privileges = observer.execute(
            """
            SELECT pg_catalog.has_table_privilege(current_user, %s, 'SELECT'),
                   pg_catalog.has_table_privilege(current_user, %s, 'INSERT'),
                   pg_catalog.has_table_privilege(current_user, %s, 'UPDATE'),
                   pg_catalog.has_table_privilege(current_user, %s, 'DELETE')
            """,
            (TABLE, TABLE, TABLE, TABLE),
        ).fetchone()
        observer_role_least_privilege = privileges == (True, True, False, False)
        observer.commit()

    with _connect(ADMIN_WEB, "/run/secrets/database_admin_web_password") as admin_web:
        _scope(admin_web, WORKSPACE)
        select_only = admin_web.execute(
            """
            SELECT pg_catalog.has_table_privilege(current_user, %s, 'SELECT'),
                   pg_catalog.has_table_privilege(current_user, %s, 'INSERT'),
                   pg_catalog.has_table_privilege(current_user, %s, 'UPDATE'),
                   pg_catalog.has_table_privilege(current_user, %s, 'DELETE'),
                   count(*)
              FROM public.learning_drift_assessment
            """,
            (TABLE, TABLE, TABLE, TABLE),
        ).fetchone()
        admin_select_only = select_only == (True, False, False, False, 0)
        admin_web.rollback()

    with _connect("crypidian_admin", "/run/secrets/postgres_password") as owner:
        role = owner.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname = 'crypidian_admin'"
        ).fetchone()
        rls_flags = owner.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_catalog.pg_class "
            "WHERE oid = %s::regclass",
            (TABLE,),
        ).fetchone()
        observer_role = owner.execute(
            """
            SELECT rolsuper, rolbypassrls, rolinherit, rolcreatedb, rolcreaterole, rolreplication
              FROM pg_catalog.pg_roles WHERE rolname = %s
            """,
            (OBSERVER,),
        ).fetchone()
        memberships = owner.execute(
            """
            SELECT count(*) FROM pg_catalog.pg_auth_members AS membership
             WHERE membership.roleid = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s)
                OR membership.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s)
            """,
            (OBSERVER, OBSERVER),
        ).fetchone()[0]
        assert role is not None
        assert role[0]
        assert rls_flags == (True, True)
        assert observer_role == (False, False, False, False, False, False)
        assert memberships == 0

        immutable_triggers = owner.execute(
            """
            SELECT count(*) FROM pg_catalog.pg_trigger
             WHERE tgname IN (
                 'learning_drift_assessment_immutable',
                 'learning_drift_assessment_member_immutable',
                 'learning_retrain_proposal_immutable'
             ) AND NOT tgisinternal
            """
        ).fetchone()[0]
        immutable_trigger_definitions_present = immutable_triggers == 3

    print(
        json.dumps(
            {
                "bounded_policy_schema": _assert_bounded_policy_schema(),
                "bounded_cutoffs_and_expiry": _assert_bounded_query_cutoffs_and_expiry(),
                "empty_assessment_rejected": empty_assessment_rejected,
                "assessment_rows_scoped_per_workspace": assessment_rows_scoped_per_workspace,
                "drift_policy_rejected": drift_policy_rejected,
                "cross_workspace_insert_denied": cross_workspace_insert_denied,
                "unscoped_read_denied": True,
                "observer_role_least_privilege": observer_role_least_privilege,
                "observer_sensitive_columns_denied": sensitive_columns_denied,
                "admin_select_only": admin_select_only,
                "rights_hash_helper_execute_denied": rights_hash_helper_execute_denied,
                "rights_receipt_helper_execute_allowed": rights_receipt_helper_execute_allowed,
                "rights_receipt_fails_closed": rights_receipt_fails_closed,
                "source_rights_mapping_execute_allowed": source_rights_mapping_execute_allowed,
                "source_rights_mapping_exact": source_rights_mapping_exact,
                "source_policy_direct_select_denied": direct_source_policy_select_denied,
                "canonical_source_cost_execute_allowed": canonical_source_cost_execute_allowed,
                "canonical_source_cost_parity": canonical_source_cost_parity,
                **observation_guard_results,
                "proposal_content_hash_rejected": proposal_content_hash_rejected,
                "proposal_idempotency_rejected": proposal_idempotency_rejected,
                "proposal_hash_parity": proposal_hash_parity,
                "ledger_immutable_trigger_definitions_present": (
                    immutable_trigger_definitions_present
                ),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
