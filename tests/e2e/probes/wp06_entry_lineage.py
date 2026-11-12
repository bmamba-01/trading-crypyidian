"""Kiểm CHECK, RLS, quyền và trigger của provenance entry trên PostgreSQL thật."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()


def connect():
    return psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
    )


with connect() as connection:
    heads = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    assert heads == [("f6b8d0e2a4c7",)]
    connection.execute(
        "SELECT set_config('app.workspace_id', %s, true)",
        (str(workspace_id),),
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM backtest_trade_outcome WHERE workspace_id=%s "
            "AND entry_features_hash IS NOT NULL",
            (workspace_id,),
        ).fetchone()[0]
        >= 6
    )
    assert connection.execute(
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
        "WHERE oid='public.backtest_trade_outcome'::regclass"
    ).fetchone()[0]
    for role, expected in (
        ("crypidian_app", (False, True, False, False)),
        ("crypidian_trainer_worker", (False, False, False, False)),
        ("crypidian_replay_writer", (True, True, False, False)),
        ("crypidian_admin_web", (False, True, False, False)),
    ):
        privileges = connection.execute(
            "SELECT has_table_privilege(%s, 'backtest_trade_outcome', 'SELECT'), "
            "has_table_privilege(%s, 'backtest_trade_outcome', 'INSERT'), "
            "has_table_privilege(%s, 'backtest_trade_outcome', 'UPDATE'), "
            "has_table_privilege(%s, 'backtest_trade_outcome', 'DELETE')",
            (role,) * 4,
        ).fetchone()
        assert privileges == expected, (role, privileges)
    validator = connection.execute(
        "SELECT prosecdef, provolatile, proconfig FROM pg_proc "
        "WHERE oid='public.wp06_entry_features_valid(jsonb)'::regprocedure"
    ).fetchone()
    assert validator == (False, "i", ["search_path=pg_catalog, pg_temp"]), validator
    assert (
        connection.execute(
            "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) acl "
            "WHERE p.oid='public.wp06_entry_features_valid(jsonb)'::regprocedure "
            "AND acl.grantee=0"
        ).fetchone()[0]
        == 0
    )
    for invalid_features in (
        None,
        [],
        {},
        {"1h:atr": 1},
        [["1h:atr", True]],
        [[" 1h:atr", 1.0]],
        [["1h:atr"]],
        [["1h:atr", 1.0], ["1h:atr", 2.0]],
    ):
        valid = connection.execute(
            "SELECT public.wp06_entry_features_valid(%s)",
            (Jsonb(invalid_features),),
        ).fetchone()[0]
        assert valid is not True, invalid_features
    assert connection.execute(
        "SELECT public.wp06_entry_features_valid(%s)",
        (Jsonb([["1h:atr", 1.0], ["4h:trend", 2.0]]),),
    ).fetchone()[0]
    for invalid in (
        {"entry_features": None},
        {"entry_symbol": None},
        {"entry_symbol": "btcusdt"},
        {"entry_symbol": "BTC/USDT"},
        {"entry_decision_at": None},
        {"entry_features_hash": "A" * 64},
        {"entry_feature_schema_hash": "z" * 64},
        {"entry_features": []},
        {"entry_features": {"1h:atr": 1}},
        {"entry_features": [["1h:atr", True]]},
        {"entry_features": [[" 1h:atr", 1.0]]},
        {"entry_features": [["\t1h:atr", 1.0]]},
        {"entry_features": [["1h:atr", 1.0], ["1h:atr", 2.0]]},
        {"entry_features": [["4h:trend", 1.0], ["1h:atr", 2.0]]},
        {"entry_features": [["1h:atr"]]},
        {"entry_features": [[1, 1.0]]},
    ):
        try:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO backtest_trade_outcome "
                    "SELECT (jsonb_populate_record(NULL::backtest_trade_outcome, "
                    "to_jsonb(r) || %s::jsonb)).* FROM backtest_trade_outcome r "
                    "WHERE workspace_id=%s LIMIT 1",
                    (Jsonb({"id": str(uuid4()), **invalid}), workspace_id),
                )
        except psycopg.errors.CheckViolation:
            pass
        except psycopg.Error as error:
            feature_binding_fields = {
                "entry_decision_at",
                "entry_feature_schema_hash",
                "entry_features",
                "entry_features_hash",
            }
            if (
                feature_binding_fields.intersection(invalid)
                and error.sqlstate == "P0001"
                and any(
                    marker in str(error)
                    for marker in (
                        "WP06_ENTRY_FEATURES_INVALID",
                        "WP06_OUTCOME_ENTRY_FEATURES_HASH_MISMATCH",
                    )
                )
            ):
                pass
            else:
                raise AssertionError(
                    f"Outcome bị chặn sai guard DB cho payload: {invalid}"
                ) from error
        else:
            raise AssertionError(f"CHECK nhận provenance sai: {invalid}")
    for mutation in (
        "UPDATE backtest_trade_outcome SET entry_features_hash=entry_features_hash ",
        "DELETE FROM backtest_trade_outcome ",
    ):
        try:
            with connection.transaction():
                connection.execute(mutation + "WHERE workspace_id=%s", (workspace_id,))
        except psycopg.Error as error:
            if "backtest records are append-only" not in str(error):
                raise AssertionError("Mutation không chạm trigger bất biến.") from error
        else:
            raise AssertionError("Trigger cho phép sửa/xóa outcome đã có provenance.")
    for role in ("crypidian_app", "crypidian_trainer_worker", "crypidian_admin_web"):
        with connection.transaction():
            connection.execute("SET LOCAL ROLE " + role)
            connection.execute("SELECT set_config('app.workspace_id', %s, true)", (str(uuid4()),))
            try:
                with connection.transaction():
                    connection.execute("SELECT 1 FROM backtest_trade_outcome LIMIT 1")
            except psycopg.errors.InsufficientPrivilege:
                pass
            else:
                raise AssertionError(f"Role {role} không được đọc outcome chứa feature.")
    try:
        with connection.transaction():
            connection.execute("SET LOCAL ROLE crypidian_replay_writer")
            connection.execute(
                "SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),)
            )
            connection.execute(
                "INSERT INTO backtest_trade_outcome "
                "SELECT (jsonb_populate_record(NULL::backtest_trade_outcome, "
                "to_jsonb(r) || jsonb_build_object('id', gen_random_uuid(), "
                "'workspace_id', %s::uuid))).* FROM backtest_trade_outcome r "
                "WHERE workspace_id=%s LIMIT 1",
                (uuid4(), workspace_id),
            )
    except psycopg.Error as error:
        if not (
            "row-level security policy" in str(error)
            or (
                error.sqlstate == "P0001"
                and "WP06_OUTCOME_OPPORTUNITY_WORKSPACE_MISMATCH" in str(error)
            )
        ):
            raise AssertionError("INSERT khác workspace không chạm RLS WITH CHECK.") from error
    else:
        raise AssertionError("RLS WITH CHECK cho phép ghi outcome khác workspace.")
print(json.dumps({"entry_provenance_checks_roles_rls_immutable": True}))
