"""Cài query có target theo sau dấu chấm phẩy để thử fail-closed guard."""

from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def main() -> None:
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
            .read_text(encoding="utf-8")
            .strip(),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ["TRADING_BOT_DATABASE_NAME"],
        )
    )
    try:
        with engine.begin() as connection:
            definition = connection.execute(
                text(
                    "SELECT pg_catalog.pg_get_functiondef("
                    "'public.wp06_learning_incident_eligible_target(uuid,uuid)'::regprocedure)"
                )
            ).scalar_one()
            end_marker = "END $function$"
            if definition.count(end_marker) != 1:
                raise RuntimeError("Không nhận dạng được điểm chèn function fixture.")
            injected = definition.replace(
                end_marker,
                "SELECT 1 INTO proposal;\n        END $function$",
                1,
            )
            if injected == definition:
                raise RuntimeError("Không chèn được query fixture.")
            connection.exec_driver_sql(injected)
        print(json.dumps({"semicolon_extra_target_installed": True}, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
