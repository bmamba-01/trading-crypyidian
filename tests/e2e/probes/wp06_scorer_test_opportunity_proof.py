"""Probe PostgreSQL cho boundary metadata proof TEST scorer C.3."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from trading_bot.adapters.database.models.learning_scorer_test_opportunity_entities import (
    LearningScorerTestOpportunityProofRecord,
)
from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
    LearningScorerTestOpportunityProofConflict,
    LearningScorerTestOpportunityProofRepository,
)
from trading_bot.application.learning_scorer_evaluation import (
    ScorerTestOpportunityEntry,
    ScorerTestOpportunityProof,
)

STATE = json.loads(os.environ["WP06_SCORER_TEST_PROOF_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
OTHER_WORKSPACE = UUID(STATE["other_workspace_id"])
TABLE = "learning_scorer_test_opportunity_proof"


def connect(role: str) -> psycopg.Connection:
    password_file = Path("/run/secrets") / f"database_{role}_password"
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=f"crypidian_{role}",
        password=password_file.read_text(encoding="utf-8").strip(),
        autocommit=True,
    )


def proof() -> ScorerTestOpportunityProof:
    candidate_hash = hashlib.sha256(b"candidate-id").hexdigest()
    entries = tuple(
        sorted(
            (
                ScorerTestOpportunityEntry(
                    stable_id_hash=hashlib.sha256(b"test-event-candidate").hexdigest(),
                    status="candidate",
                    candidate_id_hash=candidate_hash,
                    outcome_id_hash=hashlib.sha256(b"outcome-id").hexdigest(),
                    outcome_hash=hashlib.sha256(b"outcome-hash").hexdigest(),
                    sample_id_hash=hashlib.sha256(b"sample-id").hexdigest(),
                    sample_hash=hashlib.sha256(b"sample-hash").hexdigest(),
                    vector_hash=hashlib.sha256(b"vector-hash").hexdigest(),
                ),
                ScorerTestOpportunityEntry(
                    stable_id_hash=hashlib.sha256(b"test-event-no-candidate").hexdigest(),
                    status="no_candidate",
                    candidate_id_hash=None,
                ),
            ),
            key=lambda item: item.stable_id_hash,
        )
    )
    return ScorerTestOpportunityProof(
        record_id=uuid4(),
        workspace_id=WORKSPACE,
        evaluation_run_id=uuid4(),
        attempt_id=uuid4(),
        normalized_dataset_id=uuid4(),
        manifest_hash="1" * 64,
        fold_plan_hash="2" * 64,
        fold_id="3" * 64,
        approval_hash="4" * 64,
        bundle_hash="5" * 64,
        vector_schema_version="crypidian-learning-feature-vector-v1",
        vector_schema_hash="6" * 64,
        test_membership_digest="7" * 64,
        source_scope_hash="8" * 64,
        expected_opportunity_count=2,
        source_gap_count=0,
        membership_mismatch_count=0,
        unlabeled_candidate_count=0,
        entries=entries,
    )


def insert_as_migrator(record: LearningScorerTestOpportunityProofRecord) -> None:
    """Chèn row proof giả lập để kiểm database boundary, không dùng builder giả."""
    columns = [column.name for column in record.__table__.columns if column.name != "created_at"]
    query = sql.SQL("INSERT INTO public.{} ({}) VALUES ({})").format(
        sql.Identifier(TABLE),
        sql.SQL(",").join(sql.Identifier(column) for column in columns),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    values = [getattr(record, column) for column in columns]
    with psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
        .read_text(encoding="utf-8")
        .strip(),
    ) as connection:
        with connection.transaction():
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(query, values)


def insert_bad_hash_as_builder(record: LearningScorerTestOpportunityProofRecord) -> bool:
    record.id = uuid4()
    record.proof_hash = "0" * 64
    columns = [column.name for column in record.__table__.columns if column.name != "created_at"]
    query = sql.SQL("INSERT INTO public.{} ({}) VALUES ({})").format(
        sql.Identifier(TABLE),
        sql.SQL(",").join(sql.Identifier(column) for column in columns),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    values = [getattr(record, column) for column in columns]
    try:
        with connect("candidate_builder") as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.workspace_id', %s, true)", (str(WORKSPACE),)
                )
                connection.execute(query, values)
    except psycopg.Error as error:
        return (
            error.sqlstate == "P0001"
            and error.diag.message_primary == "WP06_SCORER_TEST_PROOF_CANONICAL_INVALID"
        )
    return False


def insert_noncanonical_as_builder(
    record: LearningScorerTestOpportunityProofRecord, canonical_proof: str
) -> bool:
    record.id = uuid4()
    record.canonical_proof = canonical_proof
    record.proof_hash = hashlib.sha256(canonical_proof.encode("utf-8")).hexdigest()
    columns = [column.name for column in record.__table__.columns if column.name != "created_at"]
    query = sql.SQL("INSERT INTO public.{} ({}) VALUES ({})").format(
        sql.Identifier(TABLE),
        sql.SQL(",").join(sql.Identifier(column) for column in columns),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    values = [getattr(record, column) for column in columns]
    try:
        with connect("candidate_builder") as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.workspace_id', %s, true)", (str(WORKSPACE),)
                )
                connection.execute(query, values)
    except psycopg.Error as error:
        return (
            error.sqlstate == "P0001"
            and error.diag.message_primary == "WP06_SCORER_TEST_PROOF_CANONICAL_INVALID"
        )
    return False


def verify_scorer_test_access_security() -> dict[str, bool]:
    """Xác minh quyền hẹp của receipt/report TEST và cấm evaluator đọc trực tiếp mẫu."""
    tables = (
        "learning_scorer_test_access_receipt",
        "learning_scorer_test_payload_consumption",
        "learning_scorer_test_evaluation_report",
    )
    roles = (
        "learning_evaluator",
        "learning_evaluation_reader",
        "admin_web",
        "trainer_worker",
        "replay_writer",
        "trading_runtime",
        "execution_gateway",
    )
    expected = {
        "learning_scorer_test_access_receipt": {
            "learning_evaluator": (True, False, False, False),
            "learning_evaluation_reader": (True, True, False, False),
            "admin_web": (True, False, False, False),
        },
        "learning_scorer_test_payload_consumption": {
            "learning_evaluator": (True, False, False, False),
            "learning_evaluation_reader": (True, True, False, False),
        },
        "learning_scorer_test_evaluation_report": {
            "learning_evaluator": (True, True, False, False),
            "learning_evaluation_reader": (True, True, False, False),
            "admin_web": (True, False, False, False),
        },
    }
    with psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
        .read_text(encoding="utf-8")
        .strip(),
    ) as admin:
        flags = admin.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
            "FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname = ANY(%s)",
            (list(tables),),
        ).fetchall()
        table_flags = {name: (enabled, forced) for name, enabled, forced in flags}
        assert table_flags == dict.fromkeys(tables, (True, True)), table_flags

        for table in tables:
            for role in roles:
                privileges = admin.execute(
                    "SELECT has_table_privilege(%s,%s,'SELECT'), "
                    "has_table_privilege(%s,%s,'INSERT'), "
                    "has_table_privilege(%s,%s,'UPDATE'), "
                    "has_table_privilege(%s,%s,'DELETE')",
                    tuple(value for _ in range(4) for value in (f"crypidian_{role}", table)),
                ).fetchone()
                assert tuple(privileges) == expected.get(table, {}).get(
                    role, (False, False, False, False)
                ), (table, role, privileges)

        functions = (
            "public.wp06_open_scorer_test_access_receipt(uuid,uuid,uuid,uuid,uuid,text,text,text,text)",
            "public.wp06_consume_scorer_test_payload(uuid,uuid,text)",
            "public.wp06_lock_scorer_test_sources(uuid,uuid)",
        )
        for signature in functions:
            assert admin.execute(
                "SELECT has_function_privilege('crypidian_learning_evaluator',%s,'EXECUTE'), "
                "has_function_privilege('crypidian_app',%s,'EXECUTE')",
                (signature, signature),
            ).fetchone() == (True, False), signature

        assert admin.execute(
            "SELECT has_table_privilege('crypidian_learning_evaluator',"
            "'public.learning_normalized_sample','SELECT'), "
            "has_table_privilege('crypidian_learning_evaluator',"
            "'public.backtest_trade_outcome','SELECT'), "
            "has_table_privilege('crypidian_learning_evaluator',"
            "'public.backtest_decision_opportunity','SELECT')"
        ).fetchone() == (False, False, False)

    return {
        "test_access_forced_rls": True,
        "test_access_grants_exact": True,
        "test_access_function_grants_exact": True,
        "evaluator_no_direct_test_select": True,
    }


def verify() -> dict[str, object]:
    source_proof = proof()
    record = LearningScorerTestOpportunityProofRecord.from_proof(source_proof)
    insert_as_migrator(record)

    table_security = {}
    roles = (
        "candidate_builder",
        "learning_evaluator",
        "admin_web",
        "trainer_worker",
        "learning_batch_reader",
        "learning_evaluation_reader",
        "replay_writer",
        "trading_runtime",
        "execution_gateway",
    )
    with psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
        .read_text(encoding="utf-8")
        .strip(),
    ) as admin:
        table_flags = admin.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
            (f"public.{TABLE}",),
        ).fetchone()
        table_security = {
            "relrowsecurity": table_flags[0],
            "relforcerowsecurity": table_flags[1],
        }
        grants = {}
        for role in roles:
            role_name = f"crypidian_{role}"
            grants[role] = admin.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT'), "
                "has_table_privilege(%s,%s,'INSERT'), "
                "has_table_privilege(%s,%s,'UPDATE'), "
                "has_table_privilege(%s,%s,'DELETE')",
                (role_name, TABLE, role_name, TABLE, role_name, TABLE, role_name, TABLE),
            ).fetchone()

    expected_grants = {
        "candidate_builder": (True, True, False, False),
        "learning_evaluator": (True, False, False, False),
        "learning_evaluation_reader": (True, False, False, False),
        "admin_web": (True, False, False, False),
    }
    for role, privileges in grants.items():
        assert tuple(privileges) == expected_grants.get(role, (False, False, False, False)), (
            role,
            privileges,
        )

    with connect("candidate_builder") as builder:
        with builder.transaction():
            builder.execute("SELECT set_config('app.workspace_id', %s, true)", (str(WORKSPACE),))
            own_count = builder.execute(
                sql.SQL("SELECT count(*) FROM public.{} WHERE workspace_id=%s").format(
                    sql.Identifier(TABLE)
                ),
                (WORKSPACE,),
            ).fetchone()[0]
            builder.execute(
                "SELECT set_config('app.workspace_id', %s, true)", (str(OTHER_WORKSPACE),)
            )
            other_count = builder.execute(
                sql.SQL("SELECT count(*) FROM public.{} WHERE workspace_id=%s").format(
                    sql.Identifier(TABLE)
                ),
                (WORKSPACE,),
            ).fetchone()[0]
    assert own_count == 1
    assert other_count == 0
    assert table_security == {"relrowsecurity": True, "relforcerowsecurity": True}

    with connect("candidate_builder") as builder:
        with builder.transaction():
            builder.execute("SELECT set_config('app.workspace_id', %s, true)", (str(WORKSPACE),))
            record_columns = [column.name for column in record.__table__.columns]
            persisted = builder.execute(
                sql.SQL("SELECT {} FROM public.{} WHERE id=%s").format(
                    sql.SQL(",").join(sql.Identifier(column) for column in record_columns),
                    sql.Identifier(TABLE),
                ),
                (record.id,),
            ).fetchone()
    persisted_record = LearningScorerTestOpportunityProofRecord(
        **dict(zip(record_columns, persisted, strict=True))
    )
    LearningScorerTestOpportunityProofRepository.validate_record(persisted_record)
    assert insert_bad_hash_as_builder(
        LearningScorerTestOpportunityProofRecord.from_proof(source_proof)
    )
    canonical_record = LearningScorerTestOpportunityProofRecord.from_proof(source_proof)
    proof_payload = json.loads(canonical_record.canonical_proof)
    whitespace_tamper_rejected = insert_noncanonical_as_builder(
        LearningScorerTestOpportunityProofRecord.from_proof(source_proof),
        json.dumps(proof_payload, ensure_ascii=False, indent=2),
    )
    reordered_payload = dict(reversed(tuple(proof_payload.items())))
    reordered_proof = json.dumps(
        reordered_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    key_order_tamper_rejected = insert_noncanonical_as_builder(
        LearningScorerTestOpportunityProofRecord.from_proof(source_proof), reordered_proof
    )
    assert whitespace_tamper_rejected
    assert key_order_tamper_rejected

    tampered = LearningScorerTestOpportunityProofRecord.from_proof(source_proof)
    tampered.bundle_hash = "9" * 64
    try:
        LearningScorerTestOpportunityProofRepository.validate_record(tampered)
    except LearningScorerTestOpportunityProofConflict:
        tamper_rejected = True
    else:
        tamper_rejected = False
    assert tamper_rejected

    trigger_checks = {}
    with psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
        .read_text(encoding="utf-8")
        .strip(),
        autocommit=True,
    ) as admin:
        for action in ("UPDATE", "DELETE"):
            try:
                if action == "UPDATE":
                    admin.execute(
                        sql.SQL("UPDATE public.{} SET proof_hash=%s WHERE id=%s").format(
                            sql.Identifier(TABLE)
                        ),
                        ("9" * 64, record.id),
                    )
                else:
                    admin.execute(
                        sql.SQL("DELETE FROM public.{} WHERE id=%s").format(sql.Identifier(TABLE)),
                        (record.id,),
                    )
            except psycopg.Error as error:
                trigger_checks[action.lower()] = (
                    error.sqlstate == "P0001"
                    and error.diag.message_primary == "WP06_SCORER_TEST_PROOF_IMMUTABLE"
                )
    assert trigger_checks == {"update": True, "delete": True}

    return {
        "forced_rls": True,
        "workspace_scope": True,
        "grants_exact": True,
        "canonical_hash_parity": True,
        "bad_direct_insert_rejected": True,
        "whitespace_tamper_rejected": whitespace_tamper_rejected,
        "key_order_tamper_rejected": key_order_tamper_rejected,
        "denormalized_tamper_rejected": True,
        "append_only": True,
        "populated_proof_id": str(record.id),
        **verify_scorer_test_access_security(),
    }


if __name__ == "__main__":
    print(json.dumps(verify(), sort_keys=True))
