"""Fixture quyền tổng hợp chỉ tồn tại trong PostgreSQL Compose tạm."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.engine import create_session_factory
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

GATE = "SELECT public.wp06_require_scorer_paper_source_rights(:workspace,:source)"


async def verify_paper_rights(seed_engine, admin_engine, workspace_id):
    """Kiểm purpose, revoke và hai thứ tự khóa; không tạo report đủ điều kiện."""
    sessions = create_session_factory(seed_engine)
    now = datetime.now(UTC)
    both = frozenset({SourceUsePurpose.TRAINING, SourceUsePurpose.PAPER_DEMO})

    async def append(unit, revision):
        repository = SourceRegistryRepository(unit.session)
        await repository.append_revision(workspace_id=workspace_id, revision=revision)

    async def finalize(unit, revision):
        repository = SourceRegistryRepository(unit.session)
        await repository.finalize_policy(
            workspace_id=workspace_id,
            source_id=revision.policy.source_id,
            policy_id=revision.policy.policy_id,
        )

    async def seed(name, purposes, *, expired=False):
        source = f"WP06_PAPER_GATE_TEST_{name}"
        entry = (
            SourceRegistryEntry.discover(
                source_id=source,
                publisher="Fixture tổng hợp PostgreSQL tạm",
                endpoint_url="https://example.test/wp06-paper-rights-fixture",
                access_method="GET",
                auth_scope="fixture-only",
                budget=SourceBudget(
                    window=timedelta(hours=1),
                    max_requests=1,
                    max_bytes=1,
                    max_cost=Decimal("0"),
                    cost_unit="USD",
                ),
                actor_id="fixture-operator",
                at=now - timedelta(minutes=3),
                reason="Fixture kỹ thuật; không cấp quyền dữ liệu sản phẩm.",
            )
            .transition(
                policy_id=f"{source}:pending",
                state=SourcePolicyState.RIGHTS_PENDING,
                actor_id="fixture-operator",
                at=now - timedelta(minutes=2),
                reason="Review fixture kỹ thuật.",
            )
            .transition(
                policy_id=f"{source}:verified",
                state=SourcePolicyState.VERIFIED,
                actor_id="fixture-operator",
                at=now - timedelta(minutes=1),
                reason="Chỉ xác minh fixture trong database tạm.",
                allowed_purposes=purposes,
                evidence_ref="https://example.test/fixture-license",
                reviewed_at=now - timedelta(minutes=1),
                valid_until=(
                    datetime.now(UTC) + timedelta(seconds=2) if expired else now + timedelta(days=1)
                ),
                approved_by="fixture-reviewer",
            )
        )
        for revision in entry.revisions:
            async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                await append(unit, revision)
            async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                await finalize(unit, revision)
        return entry

    async def check(source, *, expected=None):
        try:
            async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                await unit.session.execute(
                    text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader")
                )
                await unit.session.execute(
                    text(GATE), {"workspace": workspace_id, "source": source}
                )
        except DBAPIError as error:
            if expected is None or expected not in str(error.orig):
                raise AssertionError(str(error.orig)) from error
        else:
            assert expected is None, f"Source {source} phải bị từ chối"

    denied = "WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED"
    for name, purposes, expired in (
        ("TRAINING_ONLY", frozenset({SourceUsePurpose.TRAINING}), False),
        ("PAPER_ONLY", frozenset({SourceUsePurpose.PAPER_DEMO}), False),
        ("EXPIRED", both, True),
    ):
        entry = await seed(name, purposes, expired=expired)
        if expired:
            expiry = entry.current.policy.valid_until
            await asyncio.sleep(max((expiry - datetime.now(UTC)).total_seconds(), 0) + 0.05)
        await check(entry.source_id, expected=denied)
    await check("WP06_PAPER_GATE_TEST_MISSING", expected=denied)
    await check("CRYPIDIAN_FIRST_PARTY_QA", expected="WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED")

    entry = await seed("BOTH", both)
    await check(entry.source_id)
    async with seed_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM learning_scoring_deployment_event")
        )
        assert count == 0, "Đủ purpose không được tự kích hoạt scorer"

    # Mất riêng từng purpose sau khi đã đủ cả hai cũng phải chặn.
    for missing in (SourceUsePurpose.TRAINING, SourceUsePurpose.PAPER_DEMO):
        entry = await seed(f"REMOVE_{missing.name}", both)
        pending = entry.transition(
            policy_id=f"{entry.source_id}:recheck",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="fixture-operator",
            at=now,
            reason="Thu hồi một purpose của fixture.",
        )
        changed = pending.transition(
            policy_id=f"{entry.source_id}:limited",
            state=SourcePolicyState.VERIFIED,
            actor_id="fixture-operator",
            at=now,
            reason="Giữ lại đúng một purpose.",
            allowed_purposes=both - {missing},
            evidence_ref="https://example.test/fixture-license",
            reviewed_at=now,
            valid_until=now + timedelta(days=1),
            approved_by="fixture-reviewer",
        )
        for revision in changed.revisions[-2:]:
            async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                await append(unit, revision)
            async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
                await finalize(unit, revision)
        await check(entry.source_id, expected=denied)

    # Writer đi trước: reader phải chờ commit rồi thấy revision revoked.
    entry = await seed("RACE", both)
    revoked = entry.transition(
        policy_id=f"{entry.source_id}:revoked",
        state=SourcePolicyState.REVOKED,
        actor_id="fixture-operator",
        at=now,
        reason="Thu hồi fixture trong race test.",
    ).current
    async with UnitOfWork(sessions, workspace_id=workspace_id) as writer:
        await append(writer, revoked)
        pending = asyncio.create_task(check(entry.source_id, expected=denied))
        done, _ = await asyncio.wait({pending}, timeout=0.2)
        assert not done, "Reader phải đợi source writer"
    await asyncio.wait_for(pending, timeout=10)

    # Reader đi trước: append revision không vượt khóa đến khi reader commit.
    entry = await seed("READER_FIRST", both)
    revoked = entry.transition(
        policy_id=f"{entry.source_id}:revoked",
        state=SourcePolicyState.REVOKED,
        actor_id="fixture-operator",
        at=now,
        reason="Kiểm writer chờ verifier.",
    ).current
    async with UnitOfWork(sessions, workspace_id=workspace_id) as reader:
        await reader.session.execute(
            text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader")
        )
        await reader.session.execute(
            text(GATE), {"workspace": workspace_id, "source": entry.source_id}
        )
        try:
            async with UnitOfWork(sessions, workspace_id=workspace_id) as writer:
                await writer.session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await append(writer, revoked)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise AssertionError(str(error.orig)) from error
        else:
            raise AssertionError("Writer phải đợi khóa source của verifier")

    # Role ứng dụng không gọi được helper nội bộ hoặc cấp quyền bằng source tùy ý.
    async with admin_engine.begin() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT has_function_privilege(current_user,"
                    "'public.wp06_require_scorer_paper_source_rights(uuid,text)','EXECUTE')"
                )
            )
            is False
        )
    return True
