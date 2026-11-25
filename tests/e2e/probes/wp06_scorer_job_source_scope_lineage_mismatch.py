"""Probe từ chối lineage scorer sai trước khi lấy source lock."""

from __future__ import annotations

import json
from threading import Event, Thread
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from wp06_scorer_job_source_scope_qa import _counts, _engine, _seed_qa_pending_job

_LINEAGE_DENIAL = "WP06_SCORER_JOB_SOURCE_LINEAGE_MISSING"
_MismatchDimension = Literal["timeframe", "symbol", "provider"]
_StageGuardDimension = Literal["revision", "checksum", "source_event", "source", "missing_bar"]
_UNKNOWN_PROVIDER_DENIAL = "WP06_SCORER_JOB_SOURCE_UNKNOWN_PROVIDER"


def _receipt_count(engine: Engine, workspace_id: UUID, job_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.bot_decision_source_rights_receipt "
                    "WHERE workspace_id=:workspace_id AND job_id=:job_id"
                ),
                {"workspace_id": workspace_id, "job_id": job_id},
            )
            or 0
        )


def _run_lineage_mismatch(
    engine: Engine,
    workspace_id: UUID,
    bot_id: UUID,
    job_id: UUID,
    *,
    dimension: _MismatchDimension,
) -> tuple[dict[str, bool], bool]:
    before = _counts(engine, workspace_id, job_id)
    receipt_before = _receipt_count(engine, workspace_id, job_id)
    with engine.begin() as connection:
        # Bỏ qua trigger/FK chỉ trong DB tạm để tạo mismatch có chủ đích.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        if dimension == "timeframe":
            connection.execute(
                text(
                    "UPDATE public.market_bar SET timeframe='4h' "
                    "WHERE workspace_id=:workspace_id AND id=("
                    "SELECT market_bar_id FROM public.bot_decision_job_source_bar "
                    "WHERE workspace_id=:workspace_id AND job_id=:job_id)"
                ),
                {"workspace_id": workspace_id, "job_id": job_id},
            )
        elif dimension == "symbol":
            connection.execute(
                text(
                    "UPDATE public.market_bar SET timeframe='1h', symbol='ETHUSDT' "
                    "WHERE workspace_id=:workspace_id AND id=("
                    "SELECT market_bar_id FROM public.bot_decision_job_source_bar "
                    "WHERE workspace_id=:workspace_id AND job_id=:job_id)"
                ),
                {"workspace_id": workspace_id, "job_id": job_id},
            )
        else:
            connection.execute(
                text(
                    "UPDATE public.market_bar SET source='UNEXPECTED_SOURCE' "
                    "WHERE workspace_id=:workspace_id AND id=("
                    "SELECT market_bar_id FROM public.bot_decision_job_source_bar "
                    "WHERE workspace_id=:workspace_id AND job_id=:job_id)"
                ),
                {"workspace_id": workspace_id, "job_id": job_id},
            )

    source_lock_key = f"research-source:{workspace_id}:CRYPIDIAN_FIRST_PARTY_QA"
    gate_ready = Event()
    gate_finished = Event()
    gate_state: dict[str, object] = {}
    lock_connection = engine.connect()
    lock_transaction = lock_connection.begin()
    lock_pid = int(lock_connection.scalar(text("SELECT pg_catalog.pg_backend_pid()")))
    lock_connection.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:lock_key, 0))"),
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
            gate_finished.set()
            gate_ready.set()

    gate_thread = Thread(
        target=run_gate,
        name=f"scorer-{dimension}-lineage-mismatch-gate",
        daemon=True,
    )
    gate_thread.start()
    completed_before_lock_release = False
    not_blocked_by_source_lock = False
    try:
        if not gate_ready.wait(timeout=10):
            raise AssertionError("Scorer source gate không khởi tạo được.")
        if "pid" not in gate_state:
            error = gate_state.get("error")
            raise AssertionError(f"Scorer source gate không có PID: {error!r}") from (
                error if isinstance(error, Exception) else None
            )
        gate_pid = int(gate_state["pid"])
        completed_before_lock_release = gate_finished.wait(timeout=8)
        with engine.connect() as connection:
            blockers = connection.scalar(
                text("SELECT pg_catalog.pg_blocking_pids(:gate_pid)"),
                {"gate_pid": gate_pid},
            )
        not_blocked_by_source_lock = lock_pid not in (blockers or [])
    finally:
        lock_transaction.commit()
        lock_connection.close()
        gate_thread.join(timeout=15)
    if gate_thread.is_alive():
        raise AssertionError("Scorer source gate không dừng sau khi nhả source lock.")
    expected_marker = _UNKNOWN_PROVIDER_DENIAL if dimension == "provider" else _LINEAGE_DENIAL
    if gate_state.get("marker") != expected_marker:
        error = gate_state.get("error")
        raise AssertionError(f"Scorer source gate trả denial sai cho {dimension}: {error!r}") from (
            error if isinstance(error, Exception) else None
        )

    after = _counts(engine, workspace_id, job_id)
    receipt_after = _receipt_count(engine, workspace_id, job_id)
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

    result = {
        f"{dimension}_lineage_denial_exact": gate_state.get("marker") == expected_marker,
        f"{dimension}_completed_before_lock_release": completed_before_lock_release,
        f"{dimension}_not_blocked_by_source_lock": not_blocked_by_source_lock,
        f"{dimension}_no_rights_or_decision_writes": (
            before == after and receipt_before == receipt_after
        ),
    }
    no_policy_or_license_rows = policy_count == license_evidence_count == 0
    return result, no_policy_or_license_rows


def _run_stage_guard_mismatch(
    engine: Engine,
    *,
    dimension: _StageGuardDimension,
) -> tuple[dict[str, bool], bool]:
    workspace_id, _, job_id = _seed_qa_pending_job(engine)
    before = _counts(engine, workspace_id, job_id)
    receipt_before = _receipt_count(engine, workspace_id, job_id)
    with engine.connect() as connection:
        staged = dict(
            connection.execute(
                text(
                    "SELECT * FROM public.bot_decision_job_source_bar "
                    "WHERE workspace_id=:workspace_id AND job_id=:job_id"
                ),
                {"workspace_id": workspace_id, "job_id": job_id},
            )
            .mappings()
            .one()
        )

    marker: str | None = None
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET LOCAL session_replication_role = replica"))
                connection.execute(
                    text("SELECT set_config('app.workspace_id', :value, true)"),
                    {"value": str(staged["workspace_id"])},
                )
                connection.execute(
                    text("SELECT set_config('app.bot_id', :value, true)"),
                    {"value": str(staged["bot_id"])},
                )
                if dimension == "revision":
                    connection.execute(
                        text(
                            "UPDATE public.market_bar SET revision=revision + 1 "
                            "WHERE workspace_id=:workspace_id AND id=:bar_id"
                        ),
                        {"workspace_id": workspace_id, "bar_id": staged["market_bar_id"]},
                    )
                elif dimension == "checksum":
                    connection.execute(
                        text(
                            "UPDATE public.market_bar SET checksum=:checksum "
                            "WHERE workspace_id=:workspace_id AND id=:bar_id"
                        ),
                        {
                            "workspace_id": workspace_id,
                            "bar_id": staged["market_bar_id"],
                            "checksum": "b" * 64,
                        },
                    )
                elif dimension == "source":
                    connection.execute(
                        text(
                            "UPDATE public.market_bar SET source='UNEXPECTED_SOURCE' "
                            "WHERE workspace_id=:workspace_id AND id=:bar_id"
                        ),
                        {"workspace_id": workspace_id, "bar_id": staged["market_bar_id"]},
                    )
                elif dimension == "missing_bar":
                    connection.execute(
                        text(
                            "DELETE FROM public.market_bar "
                            "WHERE workspace_id=:workspace_id AND id=:bar_id"
                        ),
                        {"workspace_id": workspace_id, "bar_id": staged["market_bar_id"]},
                    )
                else:
                    staged["source_event_id"] = "market-bar:" + "0" * 64

                connection.execute(
                    text(
                        "DELETE FROM public.bot_decision_job_source_bar "
                        "WHERE workspace_id=:workspace_id AND job_id=:job_id"
                    ),
                    {"workspace_id": workspace_id, "job_id": job_id},
                )
                connection.execute(text("SET LOCAL session_replication_role = origin"))
                staged["id"] = UUID(int=0)
                connection.execute(
                    text(
                        "INSERT INTO public.bot_decision_job_source_bar ("
                        "id,workspace_id,bot_id,job_id,evidence_id,evidence_hash,"
                        "decision_snapshot_id,decision_time,timeframe,feature_snapshot_id,"
                        "market_bar_id,source_event_id,source_event_digest,ordinal,"
                        "timeframe_bar_count,snapshot_timeframe_count) VALUES ("
                        ":id,:workspace_id,:bot_id,:job_id,:evidence_id,:evidence_hash,"
                        ":decision_snapshot_id,:decision_time,:timeframe,:feature_snapshot_id,"
                        ":market_bar_id,:source_event_id,:source_event_digest,:ordinal,"
                        ":timeframe_bar_count,:snapshot_timeframe_count)"
                    ),
                    staged,
                )
                marker = "RETURNED"
            except DBAPIError as error:
                diagnostic = getattr(error.orig, "diag", None)
                marker = getattr(diagnostic, "message_primary", None)
            finally:
                transaction.rollback()

    except Exception as error:  # pragma: no cover - surfaced on the controlling thread
        raise AssertionError(
            f"Stage trigger probe lỗi ngoài SQL cho {dimension}: {error!r}"
        ) from error

    after = _counts(engine, workspace_id, job_id)
    receipt_after = _receipt_count(engine, workspace_id, job_id)
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
        evidence_count = int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.source_license_evidence "
                    "WHERE workspace_id=:workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            or 0
        )

    return (
        {
            f"{dimension}_guard_denial_exact": marker == _LINEAGE_DENIAL,
            f"{dimension}_no_rights_or_decision_writes": (
                before == after and receipt_before == receipt_after
            ),
        },
        policy_count == evidence_count == 0,
    )


def main() -> None:
    engine = _engine()
    try:
        dimensions: tuple[_MismatchDimension, ...] = (
            "timeframe",
            "symbol",
            "provider",
        )
        outcome: dict[str, bool] = {}
        no_rights_results: list[bool] = []
        for dimension in dimensions:
            workspace_id, bot_id, job_id = _seed_qa_pending_job(engine)
            result, no_rights = _run_lineage_mismatch(
                engine, workspace_id, bot_id, job_id, dimension=dimension
            )
            outcome.update(result)
            no_rights_results.append(no_rights)
        stage_guard_dimensions: tuple[_StageGuardDimension, ...] = (
            "revision",
            "checksum",
            "source_event",
            "source",
            "missing_bar",
        )
        stage_guard_no_rights: list[bool] = []
        for dimension in stage_guard_dimensions:
            result, no_rights = _run_stage_guard_mismatch(engine, dimension=dimension)
            outcome.update(result)
            stage_guard_no_rights.append(no_rights)
        outcome["guard_cases_no_rights_or_decision_writes"] = all(
            all(
                outcome[f"{dimension}_{suffix}"]
                for suffix in ("guard_denial_exact", "no_rights_or_decision_writes")
            )
            for dimension in stage_guard_dimensions
        )
        outcome["no_policy_or_license_rows_seeded"] = all(
            [*no_rights_results, *stage_guard_no_rights]
        )
        if not all(outcome.values()):
            raise AssertionError(f"Scorer source lineage mismatch probe không đạt: {outcome}")
        print(json.dumps(outcome, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
