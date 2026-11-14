"""Probe source-bar append chỉ được phép cùng transaction tạo BotDecision."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from wp06_paper_performance_source_scope import _seed_qa_decision_source


def _url(user: str, password: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def main() -> None:
    workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
    admin_engine = create_engine(
        _url(
            os.environ["TRADING_BOT_DATABASE_USER"],
            Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        )
    )
    worker_engine = create_engine(
        _url(
            "crypidian_market_signal_worker",
            Path("/run/secrets/database_market_signal_worker_password").read_text().strip(),
        )
    )
    try:
        bot_id = uuid4()
        with admin_engine.begin() as connection:
            decision_id, _ = _seed_qa_decision_source(connection, workspace_id, bot_id)
            source = (
                connection.execute(
                    text(
                        "SELECT instrument_id,symbol FROM public.market_bar "
                        "WHERE workspace_id=:workspace AND id=("
                        "SELECT market_bar_id FROM public.bot_decision_source_bar "
                        "WHERE workspace_id=:workspace AND bot_decision_id=:decision LIMIT 1)"
                    ),
                    {"workspace": workspace_id, "decision": decision_id},
                )
                .mappings()
                .one()
            )
            late_bar_id = uuid4()
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(
                text(
                    """INSERT INTO public.market_bar (
                        workspace_id,instrument_id,symbol,timeframe,source,logical_key,
                        idempotency_key,revision,checksum,supersedes_id,raw_reference_id,
                        source_cursor,open_time,close_time,event_at,received_at,available_at,
                        open,high,low,close,volume,quote_volume,is_closed,id
                    ) VALUES (
                        :workspace,:instrument,:symbol,'1h',
                        'CRYPIDIAN_FIRST_PARTY_QA_FIXTURE',:logical_key,:idempotency_key,
                        1,:checksum,NULL,NULL,NULL,now()-interval '1 hour',now(),now(),
                        now(),now(),100,101,99,100,1,NULL,true,:bar
                    )"""
                ),
                {
                    "workspace": workspace_id,
                    "instrument": source["instrument_id"],
                    "symbol": source["symbol"],
                    "logical_key": f"qa-late:{late_bar_id}",
                    "idempotency_key": f"qa-late:{late_bar_id}",
                    "checksum": "b" * 64,
                    "bar": late_bar_id,
                },
            )
            before_count = connection.execute(
                text(
                    "SELECT count(*) FROM public.bot_decision_source_bar "
                    "WHERE workspace_id=:workspace AND bot_decision_id=:decision"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()

        parent_marker_is_null = False
        with admin_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id',:workspace,true)"),
                {"workspace": str(workspace_id)},
            )
            parent_marker_is_null = connection.execute(
                text(
                    "SELECT source_bar_created_xid IS NULL "
                    "FROM public.bot_decision WHERE workspace_id=:workspace AND id=:decision"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()

        denied_with_transaction_marker = False
        try:
            with worker_engine.begin() as connection:
                connection.execute(
                    text("SELECT pg_catalog.set_config('app.workspace_id',:workspace,true)"),
                    {"workspace": str(workspace_id)},
                )
                connection.execute(
                    text("SELECT pg_catalog.set_config('app.bot_id',:bot,true)"),
                    {"bot": str(bot_id)},
                )
                connection.execute(
                    text(
                        """INSERT INTO public.bot_decision_source_bar (
                            id,workspace_id,bot_id,bot_decision_id,decision_snapshot_id,
                            timeframe,feature_snapshot_id,market_bar_id,source_event_id,
                            source_event_digest,ordinal,timeframe_bar_count,
                            snapshot_timeframe_count
                        ) VALUES (
                            :id,:workspace,:bot,:decision,:snapshot,'1h',:feature,:bar,
                            :event_id,:event_digest,1,2,1
                        )"""
                    ),
                    {
                        "id": uuid4(),
                        "workspace": workspace_id,
                        "bot": bot_id,
                        "decision": decision_id,
                        "snapshot": "d" * 64,
                        "feature": "e" * 64,
                        "bar": late_bar_id,
                        "event_id": "market-bar:" + "f" * 64,
                        "event_digest": "a" * 64,
                    },
                )
        except DBAPIError as error:
            denied_with_transaction_marker = "WP06_SOURCE_BAR_PARENT_TRANSACTION_MISMATCH" in str(
                error.orig
            )

        with admin_engine.begin() as connection:
            after_count = connection.execute(
                text(
                    "SELECT count(*) FROM public.bot_decision_source_bar "
                    "WHERE workspace_id=:workspace AND bot_decision_id=:decision"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            registration = (
                connection.execute(
                    text(
                        """SELECT
                        pg_catalog.to_regclass('public.bot_decision') IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM pg_catalog.pg_attribute
                             WHERE attrelid='public.bot_decision'::regclass
                               AND attname='source_bar_created_xid'
                               AND atttypid='xid8'::regtype AND NOT attisdropped
                        ) AS marker_column,
                        EXISTS (
                            SELECT 1 FROM pg_catalog.pg_trigger
                             WHERE tgrelid='public.bot_decision'::regclass
                               AND tgname='zz_wp06_stamp_bot_decision_source_bar_xid'
                               AND NOT tgisinternal
                        ) AS decision_trigger,
                        EXISTS (
                            SELECT 1 FROM pg_catalog.pg_trigger
                             WHERE tgrelid='public.bot_decision_source_bar'::regclass
                               AND tgname='bot_decision_source_bar_parent_transaction'
                               AND NOT tgisinternal
                        ) AS source_bar_trigger"""
                    )
                )
                .mappings()
                .one()
            )

        result = {
            "source_lineage_unchanged": before_count == after_count,
            "late_append_denied_by_transaction_marker": denied_with_transaction_marker,
            "legacy_parent_has_no_stamp": parent_marker_is_null,
            "marker_xid8_column_registered": registration["marker_column"],
            "decision_stamp_trigger_registered": registration["decision_trigger"],
            "source_bar_gate_registered": registration["source_bar_trigger"],
        }
        if not all(result.values()):
            raise AssertionError(f"WP06 source-bar atomic-commit probe failed: {result!r}")
        print(json.dumps(result, sort_keys=True))
    finally:
        worker_engine.dispose()
        admin_engine.dispose()


if __name__ == "__main__":
    main()
