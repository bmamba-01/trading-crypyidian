"""Unit tests cho CRY-365 (scheduler) và CRY-366 (macro calendar + raw reference trong replay)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_bot.application.replay_macro_raw_evidence import (
    RawPayloadRetentionPolicy,
    ReplayMacroEvidencePoint,
    ReplayMacroStatus,
    attach_macro_and_raw_evidence_to_bundle,
    build_replay_raw_reference,
    evaluate_replay_macro_as_of,
)
from trading_bot.application.workbench_job_scheduler import (
    JobSchedulePolicy,
    ScheduledJobKind,
    SchedulerError,
    ScheduleStatus,
    WorkbenchJobScheduler,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def test_cry365_scheduler_rbac_limits_rights_check_and_cancel() -> None:
    """CRY-365: kiểm RBAC lập lịch, giới hạn tài nguyên, kiểm quyền mỗi tick và hủy có audit."""
    scheduler = WorkbenchJobScheduler(
        policy=JobSchedulePolicy(
            max_concurrent_runs=1, min_interval_seconds=300, max_runs_per_day=2
        )
    )
    ws = uuid4()

    # Vai trò không được phép -> từ chối
    with pytest.raises(SchedulerError) as exc_perm:
        scheduler.create_schedule(
            workspace_id=ws,
            kind=ScheduledJobKind.REPLAY,
            target_ref="bundle-hash-1",
            interval_seconds=600,
            actor_id="reader-1",
            actor_role="report_reader",
            start_at=NOW,
        )
    assert exc_perm.value.reason_code == "SCHEDULER_PERMISSION_DENIED"

    # Tạo lịch hợp lệ bởi owner
    sched = scheduler.create_schedule(
        workspace_id=ws,
        kind=ScheduledJobKind.REPLAY,
        target_ref="bundle-hash-1",
        interval_seconds=600,
        actor_id="owner-1",
        actor_role="owner",
        start_at=NOW,
    )
    dispatched_slots: list[str] = []

    # 1. Đang có active_runs=1 (chạm giới hạn concurrent) -> bỏ qua
    r_limit = scheduler.tick(
        now=NOW,
        active_runs=1,
        rights_checker=lambda _: True,
        dispatcher=lambda _, slot: dispatched_slots.append(slot),
    )
    assert r_limit.skipped_limit_ids == (sched.schedule_id,)
    assert dispatched_slots == []

    # 2. Thiếu quyền hiện hành -> bỏ qua
    r_rights = scheduler.tick(
        now=NOW,
        active_runs=0,
        rights_checker=lambda _: False,
        dispatcher=lambda _, slot: dispatched_slots.append(slot),
    )
    assert r_rights.skipped_rights_ids == (sched.schedule_id,)

    # 3. Đủ quyền & còn tài nguyên -> dispatch thành công
    r_ok = scheduler.tick(
        now=NOW,
        active_runs=0,
        rights_checker=lambda _: True,
        dispatcher=lambda _, slot: dispatched_slots.append(slot),
    )
    assert r_ok.dispatched_schedule_ids == (sched.schedule_id,)
    assert len(dispatched_slots) == 1

    # 4. Hủy lịch -> audit đầy đủ và không còn chạy
    cancelled = scheduler.cancel_schedule(
        sched.schedule_id,
        actor_id="owner-1",
        actor_role="owner",
        reason="Ngừng chạy định kỳ",
        now=NOW + timedelta(minutes=1),
    )
    assert cancelled.status is ScheduleStatus.CANCELLED
    events = [a.event for a in scheduler.audit_log]
    assert "schedule.created" in events
    assert "schedule.skipped_resource_limit" in events
    assert "schedule.skipped_rights_denied" in events
    assert "schedule.dispatched" in events
    assert "schedule.cancelled" in events


def test_cry366_macro_as_of_anti_lookahead_blackout_clear_and_raw_reference() -> None:
    """CRY-366: chống lookahead as-of, phân biệt UNKNOWN/BLACKOUT/CLEAR và gắn raw reference."""
    raw_id = uuid4()
    raw_ref = build_replay_raw_reference(
        raw_reference_id=raw_id,
        source="FED_FOMC_CALENDAR",
        object_key="raw/macro/fomc-2026.html",
        raw_bytes=b"<html>FOMC 2026-11-25 14:00 ET</html>",
        content_type="text/html",
        captured_at=NOW - timedelta(days=5),
        retention=RawPayloadRetentionPolicy(retention_days=90),
    )
    assert len(raw_ref.checksum) == 64
    assert raw_ref.is_expired(NOW) is False
    assert raw_ref.is_expired(NOW + timedelta(days=91)) is True

    fomc_release = NOW + timedelta(minutes=20)
    events = [
        ReplayMacroEvidencePoint(
            source_id="FED_FOMC_CALENDAR",
            event_name="FOMC Statement",
            release_at=fomc_release,
            available_at=NOW - timedelta(hours=2),
            is_high_impact=True,
            raw_reference_id=raw_id,
            raw_checksum=raw_ref.checksum,
        ),
        # Sự kiện chỉ available ở tương lai -> KHÔNG được nhìn trước!
        ReplayMacroEvidencePoint(
            source_id="BEA_RELEASE_SCHEDULE",
            event_name="GDP Release",
            release_at=NOW + timedelta(days=2),
            available_at=NOW + timedelta(hours=1),
            is_high_impact=True,
        ),
    ]

    # Thiếu BEA tại NOW vì available_at của BEA ở tương lai -> UNKNOWN (anti-lookahead)
    st_unknown = evaluate_replay_macro_as_of(
        events,
        decision_time=NOW,
        required_sources=frozenset({"FED_FOMC_CALENDAR", "BEA_RELEASE_SCHEDULE"}),
    )
    assert st_unknown.status is ReplayMacroStatus.UNKNOWN

    # Với nguồn FED đã available và nằm trong cửa sổ 30p trước FOMC -> BLACKOUT
    st_blackout = evaluate_replay_macro_as_of(
        events,
        decision_time=NOW,
        required_sources=frozenset({"FED_FOMC_CALENDAR"}),
    )
    assert st_blackout.status is ReplayMacroStatus.BLACKOUT
    assert st_blackout.matched_event_name == "FOMC Statement"
    assert st_blackout.raw_reference_id == raw_id

    # Sau khi qua cửa sổ blackout (+61p sau release) -> CLEAR (H06 backtest được)
    st_clear = evaluate_replay_macro_as_of(
        events,
        decision_time=fomc_release + timedelta(minutes=61),
        required_sources=frozenset({"FED_FOMC_CALENDAR"}),
    )
    assert st_clear.status is ReplayMacroStatus.CLEAR

    env = attach_macro_and_raw_evidence_to_bundle(
        bundle_hash="c" * 64,
        macro_points=events,
        raw_references=[raw_ref],
    )
    assert len(env.envelope_hash) == 64
    assert env.macro_event_count == 2
    assert env.raw_reference_ids == (raw_id,)
