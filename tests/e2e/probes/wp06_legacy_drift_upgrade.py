"""Seed ledger v1 tổng hợp và chụp dữ liệu trước/sau migration PostgreSQL."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg

state = json.loads(os.environ["WP06_SHADOW_DRIFT_STATE"])
with psycopg.connect(
    host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
    dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    user="crypidian_admin",
    password=Path("/run/secrets/postgres_password").read_text(encoding="utf-8").strip(),
) as connection:
    if state["action"] == "seed":
        workspace, registry, observation, assessment = (str(uuid4()) for _ in range(4))
        digest = hashlib.sha256(
            json.dumps([observation], separators=(",", ":")).encode()
        ).hexdigest()
        h = "a" * 64
        now = "2026-01-01T00:00:00+00:00"
        connection.execute("SET LOCAL session_replication_role = replica")
        connection.execute(
            "INSERT INTO workspace (id, name, slug, mode, status) "
            "VALUES (%s, %s, %s, 'PAPER', 'ACTIVE')",
            (workspace, "WP06 legacy fixture", "legacy-" + workspace),
        )
        obs = {
            "id": observation,
            "workspace_id": workspace,
            "incumbent_decision_id": str(uuid4()),
            "decision_hash": h,
            "strategy_release_id": "legacy",
            "strategy_release_hash": h,
            "incumbent_status": "ABSTAIN",
            "decision_evaluated_at": now,
            "snapshot_id": "legacy",
            "snapshot_hash": h,
            "snapshot_schema_hash": h,
            "snapshot_as_of": now,
            "challenger_registry_id": registry,
            "active_registry_id": str(uuid4()),
            "active_registry_revision": 1,
            "active_artifact_hash": h,
            "challenger_artifact_id": str(uuid4()),
            "challenger_artifact_hash": h,
            "challenger_manifest_hash": h,
            "challenger_fold_plan_hash": h,
            "training_approval_hash": h,
            "challenger_revision": 1,
            "challenger_state": "evaluated",
            "registry_approval_event_id": str(uuid4()),
            "registry_approval_event_hash": h,
            "registry_approval_event_sequence": 1,
            "active_pointer_version": 1,
            "snapshot_coverage": 0.5,
            "snapshot_quality_ok": False,
            "schema_compatible": True,
            "rights_eligible": False,
            "challenger_decision": "abstain",
            "challenger_reason": "SOURCE_RIGHTS_INELIGIBLE",
            "observed_at": now,
            "idempotency_key": h,
            "content_hash": h,
            "created_at": now,
        }
        connection.execute(
            "INSERT INTO learning_shadow_observation SELECT * FROM "
            "jsonb_populate_record(NULL::learning_shadow_observation, %s::jsonb)",
            (json.dumps(obs),),
        )
        connection.execute(
            "UPDATE learning_shadow_observation AS o SET "
            "idempotency_key = public.wp06_shadow_observation_idempotency_key(o), "
            "content_hash = public.wp06_shadow_observation_content_hash(o) WHERE id = %s",
            (observation,),
        )
        row = {
            "id": assessment,
            "workspace_id": workspace,
            "policy_version": "coverage-v1",
            "cohort_registry_id": registry,
            "cohort_revision": 1,
            "window_start": now,
            "window_end": now,
            "member_count": 1,
            "eligible_count": 0,
            "minimum_observations": 3,
            "minimum_snapshot_coverage": 0.8,
            "minimum_quality_rate": 0.9,
            "abstention_rate": 1.0,
            "data_quality_status": "inconclusive",
            "performance_status": "inconclusive",
            "reason_codes": ["low_sample", "performance_not_evaluable", "rights_unverified"],
            "membership_digest": digest,
            "idempotency_key": h,
            "content_hash": h,
            "created_at": now,
        }
        connection.execute(
            "INSERT INTO learning_drift_assessment SELECT * FROM "
            "jsonb_populate_record(NULL::learning_drift_assessment, %s::jsonb)",
            (json.dumps(row),),
        )
        member = {
            "workspace_id": workspace,
            "assessment_id": assessment,
            "observation_id": observation,
            "ordinal": 0,
            "eligible": False,
            "observed_at": now,
            "snapshot_hash": h,
            "snapshot_coverage": 0.5,
            "snapshot_quality_ok": False,
            "baseline_available": True,
            "schema_compatible": True,
            "rights_eligible": False,
            "challenger_decision": "abstain",
        }
        connection.execute(
            "INSERT INTO learning_drift_assessment_member SELECT * FROM "
            "jsonb_populate_record(NULL::learning_drift_assessment_member, %s::jsonb)",
            (json.dumps(member),),
        )
        connection.execute(
            "UPDATE learning_drift_assessment AS a SET "
            "idempotency_key = public.wp06_drift_assessment_idempotency_key(a), "
            "content_hash = public.wp06_drift_assessment_content_hash(a, %s::json) WHERE id = %s",
            (json.dumps([observation]), assessment),
        )
    rows = {}
    for table in (
        "learning_shadow_observation",
        "learning_drift_assessment",
        "learning_drift_assessment_member",
    ):
        rows[table] = connection.execute(
            f"SELECT to_jsonb(t) FROM public.{table} AS t ORDER BY 1::text"
        ).fetchall()
    canonical = connection.execute(
        "SELECT a.content_hash = public.wp06_drift_assessment_content_hash(a, "
        "(SELECT json_agg(m.observation_id::text ORDER BY m.observation_id::text) "
        "FROM learning_drift_assessment_member m WHERE m.assessment_id = a.id)), "
        "a.idempotency_key = public.wp06_drift_assessment_idempotency_key(a), "
        "o.content_hash = public.wp06_shadow_observation_content_hash(o), "
        "o.idempotency_key = public.wp06_shadow_observation_idempotency_key(o) "
        "FROM learning_drift_assessment a JOIN learning_drift_assessment_member m "
        "ON m.assessment_id = a.id JOIN learning_shadow_observation o ON o.id = m.observation_id"
    ).fetchone()
    print(json.dumps({"rows": rows, "canonical": canonical}, sort_keys=True))
