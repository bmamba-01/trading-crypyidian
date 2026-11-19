"""Kiểm đầu đọc nguồn chặn PAPER bằng PostgreSQL và scope thực của các role."""

import json
from uuid import uuid4

from tests.e2e.test_wp06_scorer_paper_entry_block_ddl import database as database
from tests.unit.test_learning_paper_entry_block_source_migration import (
    FUNCTION,
    GUARD,
    migration_sql,
)


def test_source_reader_role_returns_only_scoped_approve_tuple(database) -> None:
    workspace_id, foreign_workspace_id, bot_id, foreign_bot_id = [uuid4() for _ in range(4)]
    deployment_id, registry_id, artifact_id = [uuid4() for _ in range(3)]
    expected_columns = {
        "bot_id": str(bot_id),
        "bot_decision_hash": "a" * 64,
        "candidate_id": "b" * 64,
        "score_event_hash": "c" * 64,
        "deployment_event_id": str(deployment_id),
        "deployment_revision": 7,
        "registry_id": str(registry_id),
        "registry_revision": 4,
        "active_pointer_version": 3,
        "scorer_artifact_id": str(artifact_id),
        "scorer_artifact_hash": "d" * 64,
        "source_rights_fingerprint": "e" * 64,
    }
    database(
        """
        ALTER TABLE public.bot_decision
            ADD COLUMN status varchar(16), ADD COLUMN candidate_id varchar(64),
            ADD COLUMN decision_hash varchar(64), ADD COLUMN payload jsonb;
        ALTER TABLE public.learning_score_event
            ADD COLUMN bot_decision_hash varchar(64), ADD COLUMN candidate_id varchar(64),
            ADD COLUMN event_hash varchar(64), ADD COLUMN mode varchar(16),
            ADD COLUMN decision varchar(16), ADD COLUMN deployment_event_id uuid,
            ADD COLUMN deployment_revision integer, ADD COLUMN registry_id uuid,
            ADD COLUMN registry_revision integer, ADD COLUMN active_pointer_version integer,
            ADD COLUMN scorer_artifact_id uuid, ADD COLUMN scorer_artifact_hash varchar(64),
            ADD COLUMN source_rights_fingerprint varchar(64), ADD COLUMN canonical_event text;
        ALTER TABLE public.bot_decision ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.bot_decision FORCE ROW LEVEL SECURITY;
        ALTER TABLE public.learning_score_event ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.learning_score_event FORCE ROW LEVEL SECURITY;
        """,
        owner=False,
    )
    database("BEGIN;\n" + migration_sql() + "\nCOMMIT;", owner=False)
    sample_ids = {}
    for case in (
        "valid",
        "foreign-workspace",
        "foreign-bot",
        "wrong-candidate",
        "wrong-hash",
        "veto",
        "abstain",
        "decision-abstain",
        "shadow",
        "test",
    ):
        decision_id, event_id = uuid4(), uuid4()
        sample_ids[case] = (decision_id, event_id)
        scope = foreign_workspace_id if case == "foreign-workspace" else workspace_id
        score_bot = foreign_bot_id if case == "foreign-bot" else bot_id
        candidate_hash = "z" if case == "wrong-hash" else "a"
        candidate_id = "f" if case == "wrong-candidate" else "b"
        status = "ABSTAIN" if case == "decision-abstain" else "CANDIDATE"
        mode = case.upper() if case in {"shadow", "test"} else "PAPER_GATE"
        outcome = case.upper() if case in {"veto", "abstain"} else "APPROVE"
        database(
            f"""
            INSERT INTO public.bot_decision
                (workspace_id, bot_id, id, status, candidate_id, decision_hash, payload)
                VALUES ('{scope}', '{bot_id}', '{decision_id}', '{status}',
                        repeat('b', 64), repeat('a', 64), '{{}}');
            INSERT INTO public.learning_score_event (
                workspace_id, bot_id, bot_decision_id, id, bot_decision_hash, candidate_id,
                event_hash, mode, decision, deployment_event_id, deployment_revision, registry_id,
                registry_revision, active_pointer_version, scorer_artifact_id, scorer_artifact_hash,
                source_rights_fingerprint, canonical_event
            ) VALUES (
                '{scope}', '{score_bot}', '{decision_id}', '{event_id}',
                repeat('{candidate_hash}', 64), repeat('{candidate_id}', 64), repeat('c', 64),
                '{mode}', '{outcome}', '{deployment_id}', 7, '{registry_id}', 4, 3,
                '{artifact_id}', repeat('d', 64), repeat('e', 64), 'cấm đọc trực tiếp'
            );
            """,
            owner=False,
        )

    assert (
        database(
            f"""
        SELECT NOT (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit
                    OR rolcreatedb OR rolcreaterole OR rolreplication)
               AND NOT EXISTS (
                   SELECT 1 FROM pg_auth_members WHERE member = r.oid OR roleid = r.oid
               )
               AND NOT has_schema_privilege(r.oid, 'public', 'CREATE')
          FROM pg_roles AS r WHERE rolname = '{GUARD}';
        """,
            owner=False,
        )
        == "t"
    )
    assert (
        database(
            f"""
        SELECT pg_get_userbyid(proowner) = '{GUARD}' AND prosecdef
          FROM pg_proc WHERE oid = '{FUNCTION}'::regprocedure;
        """,
            owner=False,
        )
        == "t"
    )
    assert (
        database(
            f"""
        SELECT has_function_privilege('crypidian_admin_web', '{FUNCTION}', 'EXECUTE')
           AND NOT has_function_privilege('crypidian_trading_runtime', '{FUNCTION}', 'EXECUTE')
           AND NOT has_function_privilege(
               'crypidian_scorer_history', '{FUNCTION}', 'EXECUTE'
           )
           AND NOT has_column_privilege(
               'crypidian_admin_web', 'public.learning_score_event', 'deployment_event_id', 'SELECT'
           )
           AND NOT has_column_privilege(
               'crypidian_scorer_history', 'public.learning_score_event',
               'deployment_event_id', 'SELECT'
           )
           AND has_column_privilege(
               'crypidian_scorer_history', 'public.learning_score_event', 'bot_id', 'SELECT'
           )
           AND has_column_privilege(
               'crypidian_scorer_history', 'public.learning_score_event',
               'bot_decision_id', 'SELECT'
           )
           AND NOT has_table_privilege(
               'crypidian_scorer_history', 'public.learning_score_event', 'SELECT'
           )
           AND NOT has_column_privilege(
               '{GUARD}', 'public.learning_score_event', 'canonical_event', 'SELECT'
           )
           AND NOT has_column_privilege('{GUARD}', 'public.bot_decision', 'payload', 'SELECT')
           AND NOT has_any_column_privilege('{GUARD}', 'public.bot_decision', 'UPDATE');
        """,
            owner=False,
        )
        == "t"
    )

    valid_decision_id, valid_event_id = sample_ids["valid"]
    workspace_setting = f"SET app.workspace_id = '{workspace_id}';"
    admin_scope = "SET SESSION AUTHORIZATION crypidian_admin_web; " + workspace_setting
    call = "public.wp06_read_paper_entry_block_source"
    row = database(
        f"{admin_scope} SELECT to_jsonb(source) FROM {call}('{workspace_id}', "
        f"'{valid_decision_id}') AS source;",
        owner=False,
    )
    assert json.loads(row) == expected_columns | {"score_event_id": str(valid_event_id)}

    invalid_ids = ", ".join(
        f"('{decision}'::uuid)" for case, (decision, _) in sample_ids.items() if case != "valid"
    )
    assert (
        database(
            f"{admin_scope} SELECT count(*) FROM (VALUES {invalid_ids}) AS ids(id), "
            f"LATERAL {call}('{workspace_id}', ids.id);",
            owner=False,
        )
        == "0"
    )
    assert (
        database(
            f"{admin_scope} SELECT count(*) FROM {call}('{foreign_workspace_id}', "
            f"'{sample_ids['foreign-workspace'][0]}');",
            owner=False,
        )
        == "0"
    )
    assert (
        database(
            "SET SESSION AUTHORIZATION crypidian_admin_web; RESET app.workspace_id; "
            f"SELECT count(*) FROM {call}('{workspace_id}', '{valid_decision_id}');",
            owner=False,
        )
        == "0"
    )
    database(
        f"{admin_scope} SELECT deployment_event_id FROM public.learning_score_event;",
        owner=False,
        expected_error="permission denied",
    )
    database(
        f"SET SESSION AUTHORIZATION crypidian_trading_runtime; {workspace_setting}"
        f"SELECT * FROM {call}('{workspace_id}', '{valid_decision_id}');",
        owner=False,
        expected_error="permission denied",
    )

    # Một policy rộng được thêm vào không được nới scope của role sở hữu hàm.
    assert (
        database(
            f"""
        CREATE POLICY permissive_test_read ON public.learning_score_event FOR SELECT USING (true);
        SET ROLE {GUARD}; {workspace_setting}
        SELECT count(workspace_id) FROM public.learning_score_event
         WHERE workspace_id = '{foreign_workspace_id}';
        """,
            owner=False,
        )
        == "0"
    )

    database("BEGIN;\n" + migration_sql("downgrade") + "\nCOMMIT;", owner=False)
    assert (
        database(
            f"SELECT to_regprocedure('{FUNCTION}') IS NULL AND NOT EXISTS "
            f"(SELECT 1 FROM pg_roles WHERE rolname = '{GUARD}');",
            owner=False,
        )
        == "t"
    )
    assert database("SELECT count(*) FROM public.learning_score_event;", owner=False) == str(
        len(sample_ids)
    )
