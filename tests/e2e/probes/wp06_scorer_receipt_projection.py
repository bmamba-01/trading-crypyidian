"""Kiểm projection receipt thật mà không tạo quyền hoặc report deployment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

SIGNATURE = "public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid)"
PARENT_REVISION = "c3f5a7b9d1e3"
FIXED_REVISION = "f4a6c8e0b2d5"


def _function_state(engine: Engine) -> tuple[str, str, tuple[object, ...]]:
    """Nạp body và thuộc tính cần giữ nguyên qua CREATE OR REPLACE."""
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        row = connection.execute(
            text(
                "SELECT pg_catalog.pg_get_functiondef(oid), prosrc, oid, proowner, "
                "proacl::text, prosecdef, proconfig::text, provolatile, proparallel, "
                "proargtypes::text, proallargtypes::text, proargmodes::text, proargnames::text "
                "FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": SIGNATURE},
        ).one()
    return row[0], row[1], tuple(row[2:])


def _check_projection(engine: Engine) -> None:
    """Thực thi projection đã cài trên composite schema thật, không có payload."""
    _, body, _ = _function_state(engine)
    match = re.search(
        r"SELECT\s+(receipt\.[\s\S]+?)\s+INTO receipt_row\s+"
        r"FROM public\.learning_scorer_test_access_receipt AS receipt",
        body,
    )
    assert match is not None
    projection = match.group(1)
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(
            text(
                "DO $probe$ DECLARE receipt_row record; BEGIN SELECT "
                + projection
                + " INTO receipt_row FROM pg_catalog.json_populate_record("
                "NULL::public.learning_scorer_test_access_receipt, "
                '\'{"test_membership_digest":"' + "a" * 64 + "\"}'::json) AS receipt; "
                "IF receipt_row.test_membership_digest IS DISTINCT FROM '"
                + "a"
                * 64
                + "' THEN RAISE EXCEPTION 'WP06_RECEIPT_PROJECTION_DIGEST_MISMATCH'; "
                "END IF; END $probe$"
            )
        )


def _migrate(direction: str, target: str, *, failure_marker: str | None = None) -> None:
    """Chạy Alembic thật; lỗi guard dự kiến phải rollback revision."""
    result = subprocess.run(
        ["alembic", direction, target], capture_output=True, text=True, timeout=120
    )
    output = result.stdout + result.stderr
    if failure_marker is None:
        assert result.returncode == 0, output[-6000:]
    else:
        assert result.returncode != 0, output[-6000:]
        assert failure_marker in output, output[-6000:]


def _assert_drift_denied(engine: Engine, *, forward: bool) -> None:
    """Đổi clause không liên quan trong project riêng để chứng minh full-body guard."""
    definition, source, metadata = _function_state(engine)
    drifted = definition.replace(
        "WP06_SCORER_DEPLOYMENT_REPORT_INVALID", "WP06_SCORER_DEPLOYMENT_REPORT_INVALID_DRIFT"
    )
    assert drifted != definition
    with engine.begin() as connection:
        connection.exec_driver_sql(drifted)
    direction = "UPGRADE" if forward else "DOWNGRADE"
    current_revision = PARENT_REVISION if forward else FIXED_REVISION
    try:
        _migrate(
            "upgrade" if forward else "downgrade",
            FIXED_REVISION if forward else PARENT_REVISION,
            failure_marker=f"WP06_SCORER_RECEIPT_PROJECTION_{direction}_PREIMAGE_UNEXPECTED",
        )
        _, current_source, current_metadata = _function_state(engine)
        assert current_source == source.replace(
            "WP06_SCORER_DEPLOYMENT_REPORT_INVALID", "WP06_SCORER_DEPLOYMENT_REPORT_INVALID_DRIFT"
        )
        assert current_metadata == metadata
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                current_revision
            )
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(definition)
    assert _function_state(engine) == (definition, source, metadata)


def main() -> None:
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    _check_projection(engine)
    original_fixed_state = _function_state(engine)
    _assert_drift_denied(engine, forward=False)
    _migrate("downgrade", PARENT_REVISION)
    _, previous_source, previous_metadata = _function_state(engine)
    assert hashlib.sha256(previous_source.encode()).hexdigest() == (
        "4cef8c81e58bcd8daf127d7216f0dc2be633dffcd3f3518f9cd1f393fe477ea6"
    )
    assert previous_metadata == original_fixed_state[2]
    negative_control = False
    try:
        _check_projection(engine)
    except DBAPIError as error:
        negative_control = getattr(
            error.orig, "sqlstate", None
        ) == "42703" and 'record "receipt_row" has no field "test_membership_digest"' in str(
            error.orig
        )
        if not negative_control:
            raise
    assert negative_control, "Projection trước migration phải tái hiện lỗi field record."
    _assert_drift_denied(engine, forward=True)
    _migrate("upgrade", FIXED_REVISION)
    _check_projection(engine)
    assert _function_state(engine) == original_fixed_state
    engine.dispose()
    print(
        json.dumps(
            {
                "installed_projection_preserves_test_membership_digest": True,
                "historical_projection_reproduces_missing_record_field": negative_control,
                "upgrade_downgrade_drift_guards": True,
                "roundtrip_preserves_function_metadata_and_body": True,
            }
        )
    )


if __name__ == "__main__":
    main()
