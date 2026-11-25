"""Probe từ chối DB theo từng chiều scope của PAPER performance consumer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from wp06_paper_performance_source_scope import _seed_qa_decision_source, _url
from wp06_scorer_source_scope_near_miss import _scope_variant, _seed_synthetic_policy

_DIMENSIONS = (
    "source_id",
    "provider",
    "product",
    "venue",
    "contract",
    "symbols",
    "intervals",
    "data_start",
    "data_end",
    "purpose",
    "outputs",
    "derived_use",
    "retention",
)


def _engine(user: str, secret_path: str) -> Engine:
    password = Path(secret_path).read_text().strip()
    return create_engine(
        _url(user, password),
        connect_args={"options": "-c statement_timeout=30000"},
    )


def main() -> None:
    super_engine = _engine(
        os.environ["TRADING_BOT_DATABASE_USER"],
        os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"],
    )
    admin_engine = _engine("crypidian_admin_web", "/run/secrets/database_admin_web_password")
    results: dict[str, bool] = {}
    try:
        for dimension in _DIMENSIONS:
            workspace_id = uuid4()
            bot_id = uuid4()
            with super_engine.begin() as connection:
                connection.execute(text("SET LOCAL session_replication_role = replica"))
                connection.execute(
                    text(
                        "INSERT INTO public.workspace (id,name,slug,mode,status) VALUES ("
                        ":id,'WP06 synthetic scope fixture',:slug,'PAPER','ACTIVE')"
                    ),
                    {"id": workspace_id, "slug": f"wp06-scope-{workspace_id.hex}"},
                )
                decision_id, _ = _seed_qa_decision_source(
                    connection,
                    workspace_id,
                    bot_id,
                    include_trade=False,
                    source="BINANCE_USDM",
                )
                bar_open = connection.scalar(
                    text(
                        "SELECT bar.open_time FROM public.bot_decision_source_bar AS source "
                        "JOIN public.market_bar AS bar "
                        "ON bar.workspace_id=source.workspace_id "
                        "AND bar.id=source.market_bar_id "
                        "WHERE source.workspace_id=:workspace "
                        "AND source.bot_decision_id=:decision"
                    ),
                    {"workspace": workspace_id, "decision": decision_id},
                )
            assert bar_open is not None
            _seed_synthetic_policy(super_engine, workspace_id, _scope_variant(dimension, bar_open))
            with admin_engine.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(workspace_id)},
                )
                allowed = connection.scalar(
                    text(
                        "SELECT public.wp06_can_aggregate_paper_performance_decision_source("
                        "CAST(:workspace AS uuid),CAST(:decision AS uuid))"
                    ),
                    {"workspace": str(workspace_id), "decision": str(decision_id)},
                )
            with super_engine.connect() as connection:
                report_count = int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM public.learning_paper_performance_report "
                            "WHERE workspace_id=:workspace"
                        ),
                        {"workspace": workspace_id},
                    )
                    or 0
                )
            results[dimension] = allowed is False and report_count == 0

        outcome = {
            "synthetic_fixture_only": True,
            "checked_dimensions": sorted(results),
            "every_scope_near_miss_denied_without_report": len(results) == len(_DIMENSIONS)
            and all(results.values()),
        }
        if not outcome["every_scope_near_miss_denied_without_report"]:
            raise AssertionError(f"PAPER performance scope matrix không đạt: {results}")
        print(json.dumps(outcome, sort_keys=True))
    finally:
        admin_engine.dispose()
        super_engine.dispose()


if __name__ == "__main__":
    main()
