"""Đua quyền trên bundle replay v5: consumer/writer giữ khóa nguồn, revoke và retention.

Khôi phục coverage mất khi kịch bản source-item WP05 thành fail-closed (CRY-427). Mọi phase
gọi gate replay thật (`DatabaseReplayTrainingRightsGate`) hoặc repository Source Registry thật
dưới login service của container; khóa `research-source:{workspace}:{source}` là khóa chung
mà gate SQL và writer policy cùng lấy.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.replay_training_rights import (
    DatabaseReplayTrainingRightsGate,
    ReplayTrainingOperation,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.first_party_qa_dataset import (
    CRYPIDIAN_FIRST_PARTY_QA,
    CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose

sys.path.insert(0, "/e2e-tests")
from e2e.probes.source_license_fixture import (  # noqa: E402
    append_fixture_license_evidence,
    consume_rights_events,
    fixture_license_scope,
    pending_license_kwargs,
    verified_license_kwargs,
)

WORKSPACE_ID = UUID(os.environ["CRYPIDIAN_E2E_WORKSPACE_ID"])
PHASE = os.environ["WP06_RACE_PHASE"]
SOURCE_ID = CRYPIDIAN_FIRST_PARTY_QA
HOLD_SECONDS = float(os.environ.get("WP06_RACE_HOLD_SECONDS", "3"))


def _engine():
    return create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )


def _operation() -> ReplayTrainingOperation:
    return ReplayTrainingOperation(os.environ["WP06_RACE_OPERATION"])


async def _consumer_hold(sessions) -> dict[str, object]:
    """Consumer authorize thành công rồi giữ khóa nguồn tới hết transaction."""
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unit:
        receipts = await DatabaseReplayTrainingRightsGate(unit.session).authorize(
            workspace_id=WORKSPACE_ID,
            bundle_hash=os.environ["WP06_RACE_BUNDLE_HASH"],
            operation=_operation(),
        )
        print("WP06_REPLAY_CONSUMER_LOCK_HELD", flush=True)
        await asyncio.sleep(HOLD_SECONDS)
    return {"authorized_receipts": len(receipts)}


async def _consumer_denied(sessions) -> dict[str, object]:
    """Consumer gọi gate sau revoke/relicense: phải từ chối bằng token, không trả receipt."""
    started = time.monotonic()
    token = None
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unit:
            await DatabaseReplayTrainingRightsGate(unit.session).authorize(
                workspace_id=WORKSPACE_ID,
                bundle_hash=os.environ["WP06_RACE_BUNDLE_HASH"],
                operation=_operation(),
            )
    except PermissionError as error:
        token = str(error)
    return {"denied_token": token, "elapsed_seconds": round(time.monotonic() - started, 3)}


async def _writer_revoke(sessions) -> dict[str, object]:
    """Writer thu hồi policy hiện hành; tùy chọn giữ khóa trước để consumer phải chờ."""
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unit:
        if os.environ.get("WP06_RACE_WRITER_FIRST") == "1":
            await unit.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"research-source:{WORKSPACE_ID}:{SOURCE_ID}"},
            )
            print("WP06_REPLAY_WRITER_LOCK_HELD", flush=True)
            await asyncio.sleep(HOLD_SECONDS)
        started = time.monotonic()
        repository = SourceRegistryRepository(unit.session)
        entry = await repository.get_entry(workspace_id=WORKSPACE_ID, source_id=SOURCE_ID)
        assert entry is not None
        assert entry.current.state is SourcePolicyState.VERIFIED
        now = await repository.database_clock(workspace_id=WORKSPACE_ID)
        revoked = entry.transition(
            policy_id=f"{SOURCE_ID}:race-revoked:{uuid4().hex[:8]}",
            state=SourcePolicyState.REVOKED,
            actor_id="race-fixture-operator",
            at=now,
            reason="E2E đua quyền: thu hồi policy dưới khóa nguồn.",
        )
        await repository.append_revision(workspace_id=WORKSPACE_ID, revision=revoked.current)
        lock_wait = time.monotonic() - started
    async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unit:
        await SourceRegistryRepository(unit.session).finalize_policy(
            workspace_id=WORKSPACE_ID,
            source_id=SOURCE_ID,
            policy_id=revoked.current.policy.policy_id,
        )
    await consume_rights_events(sessions, workspace_id=WORKSPACE_ID)
    return {"revoked": True, "lock_wait_seconds": round(lock_wait, 3)}


async def _relicense_with_retention(sessions) -> dict[str, object]:
    """Cấp lại VERIFIED có license retention hữu hạn (đủ purpose khác) rồi chạy consumer."""
    scope = fixture_license_scope(
        SOURCE_ID, max_retention_days=int(os.environ["WP06_RACE_RETENTION_DAYS"])
    )
    for state in (SourcePolicyState.RIGHTS_PENDING, SourcePolicyState.VERIFIED):
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unit:
            repository = SourceRegistryRepository(unit.session)
            entry = await repository.get_entry(workspace_id=WORKSPACE_ID, source_id=SOURCE_ID)
            assert entry is not None
            now = await repository.database_clock(workspace_id=WORKSPACE_ID)
            if state is SourcePolicyState.RIGHTS_PENDING:
                candidate = entry.transition(
                    policy_id=f"{SOURCE_ID}:retention-pending",
                    state=state,
                    actor_id="race-fixture-operator",
                    at=now,
                    reason="E2E: license mới có retention hữu hạn.",
                    **pending_license_kwargs(scope, CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF),
                )
            else:
                candidate = entry.transition(
                    policy_id=f"{SOURCE_ID}:retention-verified",
                    state=state,
                    actor_id="race-fixture-operator",
                    approved_by="race-fixture-reviewer",
                    at=now,
                    reviewed_at=now,
                    valid_until=now + timedelta(days=7),
                    allowed_purposes=frozenset(
                        {SourceUsePurpose.BACKTEST_RESEARCH, SourceUsePurpose.TRAINING}
                    ),
                    reason="E2E: license retention hữu hạn chưa có lifecycle xóa.",
                    **verified_license_kwargs(
                        scope, CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF, uuid4()
                    ),
                )
                await append_fixture_license_evidence(
                    unit.session, workspace_id=WORKSPACE_ID, revision=candidate.current
                )
            await repository.append_revision(workspace_id=WORKSPACE_ID, revision=candidate.current)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE_ID) as unit:
            await SourceRegistryRepository(unit.session).finalize_policy(
                workspace_id=WORKSPACE_ID,
                source_id=SOURCE_ID,
                policy_id=candidate.current.policy.policy_id,
            )
    await consume_rights_events(sessions, workspace_id=WORKSPACE_ID)
    return {"relicensed_retention_days": scope.max_retention_days}


async def main() -> dict[str, object]:
    engine = _engine()
    sessions = create_session_factory(engine)
    try:
        if PHASE == "consumer-hold":
            return await _consumer_hold(sessions)
        if PHASE == "consumer-denied":
            return await _consumer_denied(sessions)
        if PHASE == "writer-revoke":
            return await _writer_revoke(sessions)
        if PHASE == "relicense-retention":
            return await _relicense_with_retention(sessions)
        raise ValueError(f"Phase chưa hỗ trợ: {PHASE}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), sort_keys=True), flush=True)
