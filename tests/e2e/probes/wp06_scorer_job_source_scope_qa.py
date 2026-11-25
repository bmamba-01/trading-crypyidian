"""Probe denial QA của source-scope gate scorer trên PostgreSQL thật."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError

_QA_DENIAL = "WP06_SCORER_JOB_SOURCE_QA_DENIED"


def _engine() -> Engine:
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password=password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )


def _seed_pending_job(
    engine: Engine, *, source: str = "CRYPIDIAN_FIRST_PARTY_QA_FIXTURE"
) -> tuple[UUID, UUID, UUID]:
    workspace_id = uuid4()
    bot_id = uuid4()
    job_id = uuid4()
    evidence_id = uuid4()
    bar_id = uuid4()
    snapshot_id = "d" * 64
    feature_snapshot_id = "e" * 64
    evidence_hash = "1" * 64
    observed_at = datetime.now(UTC) - timedelta(seconds=2)
    bar_open = observed_at - timedelta(hours=1)
    bar_close = observed_at - timedelta(minutes=1)

    with engine.begin() as connection:
        # Fixture DB dùng một lần; replication role chỉ tắt trigger/FK lúc seed dữ liệu tổng hợp.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "INSERT INTO public.market_bar ("
                "workspace_id,instrument_id,symbol,timeframe,source,logical_key,"
                "idempotency_key,revision,checksum,supersedes_id,raw_reference_id,"
                "source_cursor,open_time,close_time,event_at,received_at,available_at,"
                "open,high,low,close,volume,quote_volume,is_closed,id) VALUES ("
                ":workspace_id,:instrument_id,'BTCUSDT','1h',"
                ":source,:logical_key,:idempotency_key,1,"
                ":checksum,NULL,NULL,NULL,:open_time,:close_time,:close_time,:close_time,"
                ":close_time,100,101,99,100,1,NULL,true,:bar_id)"
            ),
            {
                "workspace_id": workspace_id,
                "instrument_id": uuid4(),
                "source": source,
                "logical_key": f"scorer-source-scope:{source}:{bar_id}",
                "idempotency_key": f"scorer-source-scope:{source}:{bar_id}",
                "checksum": "a" * 64,
                "open_time": bar_open,
                "close_time": bar_close,
                "bar_id": bar_id,
            },
        )
        source_event_id = connection.scalar(
            text(
                "SELECT 'market-bar:' || pg_catalog.encode(pg_catalog.sha256("
                "pg_catalog.convert_to(public.wp06_license_canonical_json("
                "pg_catalog.jsonb_build_object("
                "'mode','market_bar_source_identity_v2',"
                "'workspace_id',bar.workspace_id::text,'record_id',bar.id::text,"
                "'logical_key',bar.logical_key,'revision',bar.revision,"
                "'supersedes_record_id',bar.supersedes_id::text,'checksum',bar.checksum,"
                "'source',bar.source,'raw_reference',bar.raw_reference_id::text,"
                "'symbol',bar.symbol,'timeframe',bar.timeframe,"
                "'open_time',pg_catalog.to_char(bar.open_time AT TIME ZONE 'UTC',"
                "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'))), 'UTF8')), 'hex') "
                "FROM public.market_bar AS bar "
                "WHERE bar.workspace_id=:workspace_id AND bar.id=:bar_id"
            ),
            {"workspace_id": workspace_id, "bar_id": bar_id},
        )
        assert isinstance(source_event_id, str)
        source_event_digest = connection.scalar(
            text(
                "SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to("
                "public.wp06_license_canonical_json(pg_catalog.jsonb_build_object("
                "'timeframe','1h','source_event_ids',"
                "pg_catalog.jsonb_build_array(CAST(:source_event_id AS text)),"
                "'source_bar_ids',pg_catalog.jsonb_build_array("
                "'market-bar-record:' || CAST(:bar_id AS text)))), 'UTF8')), 'hex')"
            ),
            {"source_event_id": source_event_id, "bar_id": bar_id},
        )
        assert isinstance(source_event_digest, str)
        connection.execute(
            text(
                "INSERT INTO public.shared_signal_evidence ("
                "id,workspace_id,symbol,setup_type,observed_at,available_at,validity_end,"
                "feature_snapshot_id,source_refs,content_hash,evidence_hash,schema_version,"
                "canonical_json) VALUES ("
                ":id,:workspace_id,'BTCUSDT','SCORER_QA_GATE',:observed_at,:observed_at,"
                ":validity_end,:feature_snapshot_id,"
                "CAST(:source_refs AS jsonb),:content_hash,:evidence_hash,"
                "'1','{}')"
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "observed_at": observed_at,
                "validity_end": observed_at + timedelta(hours=1),
                "feature_snapshot_id": feature_snapshot_id,
                "source_refs": json.dumps(
                    ["CRYPIDIAN_FIRST_PARTY_QA"]
                    if source == "CRYPIDIAN_FIRST_PARTY_QA_FIXTURE"
                    else ["BINANCE_USDM_REST"]
                ),
                "content_hash": "2" * 64,
                "evidence_hash": evidence_hash,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.bot_decision_job ("
                "id,workspace_id,bot_id,account_id,evidence_id,evidence_hash,"
                "bot_config_version_id,evaluated_at,context_hash,context_payload,status,"
                "completed_at,attempts) VALUES ("
                ":id,:workspace_id,:bot_id,:account_id,:evidence_id,:evidence_hash,"
                ":bot_config_version_id,:observed_at,:context_hash,'{}'::jsonb,'PENDING',"
                "NULL,0)"
            ),
            {
                "id": job_id,
                "workspace_id": workspace_id,
                "bot_id": bot_id,
                "account_id": uuid4(),
                "evidence_id": evidence_id,
                "evidence_hash": evidence_hash,
                "bot_config_version_id": uuid4(),
                "observed_at": observed_at,
                "context_hash": "3" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.bot_decision_job_source_bar ("
                "id,workspace_id,bot_id,job_id,evidence_id,evidence_hash,"
                "decision_snapshot_id,decision_time,timeframe,feature_snapshot_id,"
                "market_bar_id,source_event_id,source_event_digest,ordinal,"
                "timeframe_bar_count,snapshot_timeframe_count) VALUES ("
                ":id,:workspace_id,:bot_id,:job_id,:evidence_id,:evidence_hash,"
                ":snapshot_id,:decision_time,'1h',:feature_snapshot_id,:bar_id,"
                ":source_event_id,:source_event_digest,0,1,1)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "bot_id": bot_id,
                "job_id": job_id,
                "evidence_id": evidence_id,
                "evidence_hash": evidence_hash,
                "snapshot_id": snapshot_id,
                "decision_time": observed_at,
                "feature_snapshot_id": feature_snapshot_id,
                "bar_id": bar_id,
                "source_event_id": source_event_id,
                "source_event_digest": source_event_digest,
            },
        )
    return workspace_id, bot_id, job_id


def _counts(engine: Engine, workspace_id: UUID, job_id: UUID) -> dict[str, int]:
    queries = {
        "authorization": (
            "SELECT count(*) FROM public.bot_decision_job_source_rights_auth "
            "WHERE workspace_id=:workspace_id AND job_id=:job_id"
        ),
        "feature_snapshot": (
            "SELECT count(*) FROM public.feature_snapshot WHERE workspace_id=:workspace_id"
        ),
        "decision": (
            "SELECT count(*) FROM public.bot_decision "
            "WHERE workspace_id=:workspace_id AND job_id=:job_id"
        ),
        "score_event": (
            "SELECT count(*) FROM public.learning_score_event WHERE workspace_id=:workspace_id"
        ),
    }
    with engine.connect() as connection:
        return {
            name: int(
                connection.scalar(text(query), {"workspace_id": workspace_id, "job_id": job_id})
                or 0
            )
            for name, query in queries.items()
        }


def _has_source_lock_waiter(engine: Engine, gate_pid: int, lock_pid: int) -> bool:
    deadline = monotonic() + 12
    while monotonic() < deadline:
        with engine.connect() as connection:
            blockers = connection.scalar(
                text("SELECT pg_catalog.pg_blocking_pids(:gate_pid)"),
                {"gate_pid": gate_pid},
            )
        if lock_pid in (blockers or []):
            return True
        sleep(0.05)
    return False


def main() -> None:
    engine = _engine()
    try:
        workspace_id, bot_id, job_id = _seed_pending_job(engine)
        before = _counts(engine, workspace_id, job_id)
        source_lock_key = f"research-source:{workspace_id}:CRYPIDIAN_FIRST_PARTY_QA"
        gate_ready = Event()
        gate_state: dict[str, object] = {}

        lock_connection = engine.connect()
        lock_transaction = lock_connection.begin()
        lock_pid = int(lock_connection.scalar(text("SELECT pg_catalog.pg_backend_pid()")))
        lock_connection.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:lock_key, 0))"
            ),
            {"lock_key": source_lock_key},
        )

        def run_gate() -> None:
            try:
                with engine.connect() as connection:
                    with connection.begin():
                        connection.execute(
                            text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader")
                        )
                        connection.execute(
                            text("SELECT set_config('app.workspace_id', :value, true)"),
                            {"value": str(workspace_id)},
                        )
                        connection.execute(
                            text("SELECT set_config('app.bot_id', :value, true)"),
                            {"value": str(bot_id)},
                        )
                        gate_state["pid"] = int(
                            connection.scalar(text("SELECT pg_catalog.pg_backend_pid()"))
                        )
                        gate_ready.set()
                        connection.execute(
                            text(
                                "SELECT public.wp06_check_scorer_job_source_scope("
                                "CAST(:workspace_id AS uuid), CAST(:job_id AS uuid), true)"
                            ),
                            {"workspace_id": str(workspace_id), "job_id": str(job_id)},
                        ).scalar_one()
                        gate_state["marker"] = "RETURNED"
            except DBAPIError as error:
                diagnostic = getattr(error.orig, "diag", None)
                gate_state["marker"] = getattr(diagnostic, "message_primary", None)
                gate_state["error"] = error
            except Exception as error:  # pragma: no cover - surfaced on the controlling thread
                gate_state["error"] = error
            finally:
                gate_ready.set()

        gate_thread = Thread(target=run_gate, name="scorer-qa-source-gate", daemon=True)
        gate_thread.start()
        if not gate_ready.wait(timeout=10):
            lock_transaction.rollback()
            lock_connection.close()
            raise AssertionError("Scorer source gate không khởi tạo được.")
        gate_pid = int(gate_state["pid"])
        waited_for_source_lock = _has_source_lock_waiter(engine, gate_pid, lock_pid)
        lock_transaction.commit()
        lock_connection.close()
        gate_thread.join(timeout=15)
        if gate_thread.is_alive():
            raise AssertionError("Scorer source gate không hoàn tất sau khi nhả source lock.")
        if "error" in gate_state and gate_state.get("marker") != _QA_DENIAL:
            error = gate_state["error"]
            raise AssertionError(f"Scorer source gate lỗi khác QA denial: {error!r}") from error

        after = _counts(engine, workspace_id, job_id)
        with engine.connect() as connection:
            policy_count = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.source_policy_revision "
                        "WHERE workspace_id=:workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                )
                or 0
            )
            license_evidence_count = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM public.source_license_evidence "
                        "WHERE workspace_id=:workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                )
                or 0
            )

        outcome = {
            "qa_denial_exact": gate_state.get("marker") == _QA_DENIAL,
            "waited_for_source_lock": waited_for_source_lock,
            "no_authorization_write": after["authorization"] == before["authorization"] == 0,
            "no_feature_snapshot_write": (
                after["feature_snapshot"] == before["feature_snapshot"] == 0
            ),
            "no_decision_write": after["decision"] == before["decision"] == 0,
            "no_score_event_write": after["score_event"] == before["score_event"] == 0,
            "no_policy_or_license_rows_seeded": policy_count == license_evidence_count == 0,
        }
        if not all(outcome.values()):
            raise AssertionError(f"Scorer QA denial probe không đạt: {outcome}")
        print(json.dumps(outcome, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
