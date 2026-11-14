"""Probe quyền đọc ADMIN cho feed Shadow/Drift bằng gateway đã khóa quyền."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

TABLES = (
    "learning_shadow_observation",
    "learning_drift_assessment",
    "learning_drift_assessment_member",
    "learning_retrain_proposal",
)


def _url(password: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="crypidian_admin_web",
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def main() -> None:
    workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
    engine = create_engine(
        _url(Path("/run/secrets/database_admin_web_password").read_text().strip())
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            direct_select_denied = True
            for table in TABLES:
                try:
                    with connection.begin_nested():
                        connection.execute(text(f"SELECT * FROM public.{table} LIMIT 1")).all()
                except DBAPIError as error:
                    sqlstate = getattr(error.orig, "sqlstate", None)
                    direct_select_denied = direct_select_denied and sqlstate == "42501"
                else:
                    direct_select_denied = False

            feed_rows = (
                connection.execute(
                    text(
                        "SELECT kind,record_id,occurred_at,metadata "
                        "FROM public.wp06_read_learning_observations_admin_feed(:workspace,50)"
                    ),
                    {"workspace": workspace_id},
                )
                .mappings()
                .all()
            )

            context_denied = False
            try:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "SELECT public.wp06_read_learning_incident_context("
                            ":workspace,:proposal)"
                        ),
                        {"workspace": workspace_id, "proposal": uuid4()},
                    ).scalar_one()
            except DBAPIError:
                context_denied = True

        outcome = {
            "direct_select_denied": direct_select_denied,
            "empty_feed_gateway_returns_metadata_shape": feed_rows == [],
            "missing_incident_context_denied": context_denied,
        }
        if not all(outcome.values()):
            raise AssertionError(f"WP06 admin read gateway probe failed: {outcome!r}")
        print(json.dumps(outcome, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
