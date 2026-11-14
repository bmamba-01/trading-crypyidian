"""Probe quyền current PAPER source, disclosure gateway và denial QA trên PostgreSQL."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError


def _url(user: str, password: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def _seed_qa_decision_source(connection, workspace_id: UUID, bot_id: UUID) -> tuple[UUID, UUID]:
    """Tạo lineage QA tối thiểu trong database kiểm thử tạm, không tạo policy/license."""
    now = datetime.now(UTC)
    bar_id = uuid4()
    decision_id = uuid4()
    trade_id = uuid4()
    account_id = uuid4()
    decision_snapshot_id = "d" * 64
    feature_snapshot_id = "e" * 64
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    connection.execute(
        text(
            """
            INSERT INTO public.market_bar (
                workspace_id,instrument_id,symbol,timeframe,source,logical_key,idempotency_key,
                revision,checksum,supersedes_id,raw_reference_id,source_cursor,open_time,
                close_time,event_at,received_at,available_at,open,high,low,close,volume,
                quote_volume,is_closed,id
            ) VALUES (
                :workspace,:instrument,'BTCUSDT','1h','CRYPIDIAN_FIRST_PARTY_QA_FIXTURE',
                :logical_key,:idempotency_key,1,:checksum,NULL,NULL,NULL,:open_time,
                :close_time,:close_time,:close_time,:close_time,100,101,99,100,1,NULL,true,:bar
            )
            """
        ),
        {
            "workspace": workspace_id,
            "instrument": uuid4(),
            "logical_key": f"qa:{bar_id}",
            "idempotency_key": f"qa:{bar_id}",
            "checksum": "a" * 64,
            "open_time": now - timedelta(hours=1),
            "close_time": now,
            "bar": bar_id,
        },
    )
    event_id = connection.execute(
        text(
            """
            SELECT 'market-bar:' || pg_catalog.encode(pg_catalog.sha256(
                pg_catalog.convert_to(public.wp06_license_canonical_json(
                    pg_catalog.jsonb_build_object(
                        'mode','market_bar_source_identity_v2',
                        'workspace_id',bar.workspace_id::text,
                        'record_id',bar.id::text,
                        'logical_key',bar.logical_key,
                        'revision',bar.revision,
                        'supersedes_record_id',bar.supersedes_id::text,
                        'checksum',bar.checksum,
                        'source',bar.source,
                        'raw_reference',bar.raw_reference_id::text,
                        'symbol',bar.symbol,
                        'timeframe',bar.timeframe,
                        'open_time',pg_catalog.to_char(
                            bar.open_time AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                    )), 'UTF8')), 'hex')
              FROM public.market_bar AS bar
             WHERE bar.workspace_id=:workspace AND bar.id=:bar
            """
        ),
        {"workspace": workspace_id, "bar": bar_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO public.bot_decision (
                workspace_id,bot_id,account_id,job_id,evidence_id,evidence_hash,
                bot_config_version_id,bot_config_hash,root_settings_version_id,
                root_settings_hash,strategy_release_id,strategy_release_hash,model_release_id,
                evaluated_at,status,reason_code,candidate_id,idempotency_key,decision_hash,
                payload,id
            ) VALUES (
                :workspace,:bot,:account,:job,:evidence,:evidence_hash,:bot_config,
                :bot_config_hash,:root_settings,:root_hash,'qa-source-gate-fixture',NULL,NULL,
                :evaluated_at,'CANDIDATE','qa-source-rights-fixture',:candidate,:idempotency,
                :decision_hash,CAST(:payload AS jsonb),:decision
            )
            """
        ),
        {
            "workspace": workspace_id,
            "bot": bot_id,
            "account": account_id,
            "job": uuid4(),
            "evidence": uuid4(),
            "evidence_hash": "1" * 64,
            "bot_config": uuid4(),
            "bot_config_hash": "2" * 64,
            "root_settings": uuid4(),
            "root_hash": "3" * 64,
            "evaluated_at": now,
            "candidate": "4" * 64,
            "idempotency": "5" * 64,
            "decision_hash": "6" * 64,
            "payload": json.dumps({"decision_snapshot_id": decision_snapshot_id}),
            "decision": decision_id,
        },
    )
    source_event_digest = connection.execute(
        text(
            """
            SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                public.wp06_license_canonical_json(pg_catalog.jsonb_build_object(
                    'timeframe','1h',
                    'source_event_ids',pg_catalog.jsonb_build_array(CAST(:event_id AS text)),
                    'source_bar_ids',pg_catalog.jsonb_build_array(
                        'market-bar-record:' || CAST(:bar AS text))
                )), 'UTF8')), 'hex')
            """
        ),
        {"event_id": event_id, "bar": bar_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO public.bot_decision_source_bar (
                id,workspace_id,bot_id,bot_decision_id,decision_snapshot_id,timeframe,
                feature_snapshot_id,market_bar_id,source_event_id,source_event_digest,
                ordinal,timeframe_bar_count,snapshot_timeframe_count
            ) VALUES (
                :id,:workspace,:bot,:decision,:snapshot,'1h',:feature,:bar,:event_id,
                :event_digest,0,1,1
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace": workspace_id,
            "bot": bot_id,
            "decision": decision_id,
            "snapshot": decision_snapshot_id,
            "feature": feature_snapshot_id,
            "bar": bar_id,
            "event_id": event_id,
            "event_digest": source_event_digest,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO public.trade (
                id,workspace_id,bot_id,account_id,mode,intent_id,status,symbol,quote_asset,
                side,settings_version_id,strategy_release_id,strategy_release_hash,
                feature_snapshot_id,candidate_id,risk_decision_id,risk_reservation_id,
                gross_pnl,fee_total,funding_total,slippage_total,total_cost,net_pnl,
                opened_at,closed_at,lineage
            ) VALUES (
                :id,:workspace,:bot,:account,'PAPER',:intent,'OPEN','BTCUSDT','USDT','LONG',
                :settings,:release,:release_hash,:feature,:candidate,:risk_decision,
                :reservation,0,0,0,0,0,0,:opened_at,NULL,'{}'::jsonb
            )
            """
        ),
        {
            "id": trade_id,
            "workspace": workspace_id,
            "bot": bot_id,
            "account": account_id,
            "intent": uuid4(),
            "settings": uuid4(),
            "release": uuid4(),
            "release_hash": "7" * 64,
            "feature": feature_snapshot_id,
            "candidate": "4" * 64,
            "risk_decision": uuid4(),
            "reservation": uuid4(),
            "opened_at": now,
        },
    )
    return decision_id, trade_id


def _probe_trade_member_fence(engine, workspace_id: UUID, trade_id: UUID) -> bool:
    """Chứng minh writer chờ cohort fence trước khi đổi trạng thái trade."""
    lock_key = f"wp06-learning-observation-member-set:{workspace_id}"
    application_name = f"wp06-member-fence-{uuid4()}"
    writer_started = Event()
    writer_done = Event()
    writer_pid: list[int] = []
    writer_errors: list[BaseException] = []

    def close_trade() -> None:
        try:
            with engine.begin() as writer:
                backend_pid = writer.execute(
                    text("SELECT pg_catalog.pg_backend_pid()")
                ).scalar_one()
                writer_pid.append(backend_pid)
                writer.execute(
                    text("SELECT pg_catalog.set_config('application_name', :name, true)"),
                    {"name": application_name},
                )
                writer_started.set()
                writer.execute(
                    text(
                        "UPDATE public.trade SET status='CLOSED',closed_at=:closed_at "
                        "WHERE workspace_id=:workspace AND id=:trade"
                    ),
                    {
                        "closed_at": datetime.now(UTC),
                        "workspace": workspace_id,
                        "trade": trade_id,
                    },
                )
            writer_done.set()
        except BaseException as error:
            writer_errors.append(error)
            writer_done.set()

    with engine.connect() as reader:
        transaction = reader.begin()
        reader.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key,0))"),
            {"key": lock_key},
        )
        writer = Thread(target=close_trade)
        writer.start()
        assert writer_started.wait(timeout=5), "Writer không khởi động."
        deadline = monotonic() + 5
        writer_waited = False
        while monotonic() < deadline and not writer_done.is_set():
            writer_waited = reader.execute(
                text("SELECT pg_catalog.pg_blocking_pids(:pid) <> ARRAY[]::integer[]"),
                {"pid": writer_pid[0]},
            ).scalar_one()
            if writer_waited:
                break
            sleep(0.05)
        visible_status = reader.execute(
            text("SELECT status FROM public.trade WHERE workspace_id=:workspace AND id=:trade"),
            {"workspace": workspace_id, "trade": trade_id},
        ).scalar_one()
        transaction.commit()
        writer.join(timeout=5)
        assert not writer.is_alive(), "Writer không tiếp tục sau khi cohort fence được mở."
    if writer_errors:
        raise AssertionError("Writer PAPER không thể hoàn tất sau khi nhả fence.") from (
            writer_errors[0]
        )
    with engine.connect() as connection:
        final_status = connection.execute(
            text("SELECT status FROM public.trade WHERE workspace_id=:workspace AND id=:trade"),
            {"workspace": workspace_id, "trade": trade_id},
        ).scalar_one()
    return writer_waited and visible_status == "OPEN" and final_status == "CLOSED"


def _assert_admin_payload_select_denied(connection, workspace_id: UUID) -> bool:
    try:
        with connection.begin_nested():
            connection.execute(
                text(
                    "SELECT report,canonical_report "
                    "FROM public.learning_paper_performance_report WHERE workspace_id=:workspace"
                ),
                {"workspace": workspace_id},
            ).all()
    except DBAPIError as error:
        return getattr(error.orig, "sqlstate", None) == "42501"
    return False


def main() -> None:
    workspace_id = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
    bot_id = uuid4()
    super_engine = create_engine(
        _url(
            os.environ["TRADING_BOT_DATABASE_USER"],
            Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip(),
        )
    )
    admin_engine = create_engine(
        _url(
            "crypidian_admin_web",
            Path("/run/secrets/database_admin_web_password").read_text().strip(),
        )
    )
    try:
        with super_engine.begin() as connection:
            decision_id, trade_id = _seed_qa_decision_source(connection, workspace_id, bot_id)

        writer_waits_before_mutation = _probe_trade_member_fence(
            super_engine, workspace_id, trade_id
        )

        with admin_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            roles = (
                connection.execute(
                    text(
                        """
                        SELECT NOT role.rolcanlogin AND NOT role.rolinherit
                                   AND NOT role.rolsuper AND NOT role.rolbypassrls
                                   AND NOT EXISTS (
                                       SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                                       WHERE membership.roleid=role.oid
                                          OR membership.member=role.oid
                                   ) AS evaluator_isolated,
                               pg_catalog.has_table_privilege(
                                   'crypidian_admin_web',
                                   'public.learning_paper_performance_report','SELECT'
                               ) AS admin_has_table_select,
                               pg_catalog.has_column_privilege(
                                   'crypidian_admin_web',
                                   'public.learning_paper_performance_report','id','SELECT'
                               ) AS admin_has_metadata_select,
                               pg_catalog.has_function_privilege(
                                   'crypidian_admin_web',
                                   'public.wp06_can_aggregate_paper_performance_decision_source'
                                   '(uuid,uuid)','EXECUTE'
                               ) AS can_call_fixed_gate,
                               pg_catalog.has_function_privilege(
                                   'crypidian_learning_paper_performance_evaluator',
                                   'public.wp06_can_aggregate_paper_performance_decision_source'
                                   '(uuid,uuid)','EXECUTE'
                               ) AS evaluator_can_call_fixed_gate,
                               report.relrowsecurity AND report.relforcerowsecurity AS report_rls
                          FROM pg_catalog.pg_roles AS role
                          JOIN pg_catalog.pg_class AS report
                            ON report.oid='public.learning_paper_performance_report'::regclass
                         WHERE role.rolname='crypidian_learning_paper_performance_evaluator'
                        """
                    )
                )
                .mappings()
                .one()
            )
            qa_allowed = connection.execute(
                text(
                    "SELECT public.wp06_can_aggregate_paper_performance_decision_source("
                    ":workspace,:decision)"
                ),
                {"workspace": workspace_id, "decision": decision_id},
            ).scalar_one()
            missing_decision_allowed = connection.execute(
                text(
                    "SELECT public.wp06_can_aggregate_paper_performance_decision_source("
                    ":workspace,:decision)"
                ),
                {"workspace": workspace_id, "decision": uuid4()},
            ).scalar_one()
            missing_report_rights = connection.execute(
                text(
                    "SELECT public.wp06_recheck_paper_performance_report_rights(:workspace,:report)"
                ),
                {"workspace": workspace_id, "report": uuid4()},
            ).scalar_one()
            missing_report_payload = connection.execute(
                text(
                    "SELECT count(*) FROM public.wp06_read_paper_performance_report("
                    ":workspace,:report)"
                ),
                {"workspace": workspace_id, "report": uuid4()},
            ).scalar_one()
            payload_select_denied = _assert_admin_payload_select_denied(connection, workspace_id)
            report_count = connection.execute(
                text(
                    "SELECT count(*) FROM public.learning_paper_performance_report "
                    "WHERE workspace_id=:workspace"
                ),
                {"workspace": workspace_id},
            ).scalar_one()

        assert roles["evaluator_isolated"] is True
        assert roles["admin_has_table_select"] is False
        assert roles["admin_has_metadata_select"] is True
        assert roles["can_call_fixed_gate"] is True
        assert roles["evaluator_can_call_fixed_gate"] is False
        assert roles["report_rls"] is True
        assert qa_allowed is False
        assert missing_decision_allowed is False
        assert missing_report_rights is False
        assert missing_report_payload == 0
        assert payload_select_denied is True
        assert report_count == 0
        assert writer_waits_before_mutation is True
        print(
            json.dumps(
                {
                    "admin_metadata_only": True,
                    "direct_payload_select_denied": True,
                    "evaluator_role_isolated": True,
                    "fixed_gate_execute_is_admin_only": True,
                    "qa_decision_source_denied": True,
                    "missing_decision_denied": True,
                    "missing_report_rights_false": True,
                    "missing_report_gateway_empty": True,
                    "no_paper_report_written": True,
                    "writer_waits_before_member_mutation": True,
                },
                sort_keys=True,
            )
        )
    finally:
        super_engine.dispose()
        admin_engine.dispose()


if __name__ == "__main__":
    main()
