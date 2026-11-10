"""Compose probe cho schema/RLS/role matrix của WP06 model registry."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine


async def _verify_schema() -> dict[str, object]:
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    engine = create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    tables = (
        "learning_model_registry",
        "learning_model_registry_event",
        "learning_model_active_pointer",
    )
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == "crypidian_admin_web"
        rls_rows = {
            table: await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = CAST(:table_name AS regclass)"
                ),
                {"table_name": f"public.{table}"},
            )
            for table in tables
        }
        for result in rls_rows.values():
            assert result.fetchone() == (True, True)

        assert (
            await connection.scalar(text("SELECT count(*) FROM public.learning_model_registry"))
            == 0
        )
        privileges = {}
        for table in tables:
            privileges[table] = {
                role: {
                    action: await connection.scalar(
                        text("SELECT has_table_privilege(:role_name, :table_name, :privilege)"),
                        {
                            "role_name": role,
                            "table_name": f"public.{table}",
                            "privilege": action,
                        },
                    )
                    for action in ("SELECT", "INSERT", "UPDATE", "DELETE")
                }
                for role in (
                    "crypidian_admin_web",
                    "crypidian_trainer_worker",
                    "crypidian_app",
                )
            }
        assert privileges["learning_model_registry"]["crypidian_admin_web"] == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        }
        assert privileges["learning_model_registry_event"]["crypidian_admin_web"] == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": False,
            "DELETE": False,
        }
        assert privileges["learning_model_active_pointer"]["crypidian_admin_web"] == {
            "SELECT": True,
            "INSERT": True,
            "UPDATE": True,
            "DELETE": False,
        }
        for table in tables:
            assert not privileges[table]["crypidian_trainer_worker"]["SELECT"]
            assert not privileges[table]["crypidian_app"]["SELECT"]

        definitions = (
            await connection.scalars(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'public.learning_model_registry'::regclass AND contype = 'c'"
                )
            )
        ).all()
        state_allowlist = next(value for value in definitions if "paper_active" in value)
        assert "live" not in state_allowlist.lower()

        triggers = (
            await connection.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgrelid = 'public.learning_model_registry_event'::regclass"
                )
            )
        ).all()
        assert {
            "learning_model_registry_event_guard",
            "learning_model_registry_event_apply",
            "learning_model_registry_event_immutable",
        } <= set(triggers)
    await engine.dispose()
    return {"rls_force": True, "least_privilege": True, "event_triggers": True}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_verify_schema()), sort_keys=True))
