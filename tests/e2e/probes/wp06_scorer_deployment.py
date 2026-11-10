"""Probe PostgreSQL cho scorer deployment CAS, RLS và fail-closed rights gate."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from trading_bot.adapters.database.engine import create_session_factory
from trading_bot.adapters.database.models.learning_scoring_deployment_entities import (
    LearningScoringDeploymentEventRecord,
)
from trading_bot.application.learning_scoring_deployment import ScorerDeploymentService
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.domain.learning.scoring_deployment import (
    ScorerDeploymentEvent,
    ScorerDeploymentMode,
    ScorerDeploymentTarget,
)


def _engine(username: str, password: str) -> AsyncEngine:
    return create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username=username,
            password=password,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        ),
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


async def _seed_workspace(engine: AsyncEngine, workspace_id, suffix: str) -> tuple[Any, Any]:
    user_id = uuid4()
    membership_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO public.workspace(id,name,slug,mode,status) "
                "VALUES (:id,:name,:slug,'PAPER','ACTIVE')"
            ),
            {"id": workspace_id, "name": f"Scorer test {suffix}", "slug": f"scorer-{suffix}"},
        )
        await connection.execute(
            text(
                "INSERT INTO public.user_account(id,email_normalized,password_hash,status) "
                "VALUES (:id,:email,'test-only-password-hash','ACTIVE')"
            ),
            {"id": user_id, "email": f"scorer-{suffix}@example.test"},
        )
        await connection.execute(
            text(
                "INSERT INTO public.user_membership(id,workspace_id,user_id,status) "
                "VALUES (:id,:workspace_id,:user_id,'ACTIVE')"
            ),
            {"id": membership_id, "workspace_id": workspace_id, "user_id": user_id},
        )
        await connection.execute(
            text(
                "INSERT INTO public.role_binding(id,workspace_id,membership_id,role) "
                "VALUES (:id,:workspace_id,:membership_id,'owner')"
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "membership_id": membership_id},
        )
    return user_id, membership_id


def _principal(workspace_id, membership_id, *, verified_at: datetime | None = None) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=membership_id,
        workspace_id=workspace_id,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=verified_at or datetime.now(UTC) - timedelta(seconds=1),
        mfa_method=MfaMethod.TOTP,
    )


async def _insert_and_expect_error(
    sessions: async_sessionmaker,
    *,
    workspace_id,
    membership_id,
    event: ScorerDeploymentEvent,
    marker: str,
) -> bool:
    try:
        async with UnitOfWork(
            sessions, workspace_id=workspace_id, actor_membership_id=membership_id
        ) as unit:
            unit.session.add(LearningScoringDeploymentEventRecord.from_domain(event))
            await unit.session.flush()
    except DBAPIError as error:
        return marker in str(error.orig)
    return False


async def _probe() -> dict[str, object]:
    admin_password = Path(os.environ["WP06_ADMIN_PASSWORD_FILE"]).read_text().strip()
    admin_engine = _engine("crypidian_admin_web", admin_password)
    admin_sessions = create_session_factory(admin_engine)

    super_password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    seed_engine = _engine(os.environ["TRADING_BOT_DATABASE_USER"], super_password)
    workspace_id, second_workspace_id = uuid4(), uuid4()
    _user_id, membership_id = await _seed_workspace(seed_engine, workspace_id, uuid4().hex[:8])
    _second_user_id, second_membership_id = await _seed_workspace(
        seed_engine, second_workspace_id, uuid4().hex[:8]
    )

    async with admin_engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == "crypidian_admin_web"
        table_state = {}
        for table in (
            "learning_scoring_deployment_event",
            "learning_scoring_deployment_pointer",
        ):
            table_state[table] = await connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = CAST(:name AS regclass)"
                ),
                {"name": f"public.{table}"},
            )
            assert table_state[table].one() == (True, True)
        privileges = {
            role: {
                table: {
                    action: await connection.scalar(
                        text("SELECT has_table_privilege(:role, :table_name, :privilege)"),
                        {
                            "role": role,
                            "table_name": f"public.{table}",
                            "privilege": action,
                        },
                    )
                    for action in ("SELECT", "INSERT", "UPDATE", "DELETE")
                }
                for table in (
                    "learning_scoring_deployment_event",
                    "learning_scoring_deployment_pointer",
                )
            }
            for role in ("crypidian_admin_web", "crypidian_trading_runtime")
        }
        event_policy = (
            await connection.execute(
                text(
                    "SELECT cmd, with_check FROM pg_policies WHERE schemaname='public' "
                    "AND tablename='learning_scoring_deployment_event'"
                )
            )
        ).all()
        trigger_names = (
            await connection.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgrelid='public.learning_scoring_deployment_event'::regclass"
                )
            )
        ).all()
    assert privileges["crypidian_admin_web"]["learning_scoring_deployment_event"] == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["crypidian_admin_web"]["learning_scoring_deployment_pointer"] == {
        "SELECT": True,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["crypidian_trading_runtime"]["learning_scoring_deployment_event"] == {
        "SELECT": True,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["crypidian_trading_runtime"]["learning_scoring_deployment_pointer"] == {
        "SELECT": True,
        "INSERT": False,
        "UPDATE": False,
        "DELETE": False,
    }
    assert any(
        command == "INSERT" and "current_setting" in (check or "")
        for command, check in event_policy
    )
    assert {
        "learning_scoring_deployment_event_guard",
        "learning_scoring_deployment_event_apply",
        "learning_scoring_deployment_event_immutable",
    } <= set(trigger_names)

    actor = _principal(workspace_id, membership_id)
    facade = ScorerDeploymentService(admin_sessions).for_principal(actor)
    idempotency_key = uuid4()
    first = await facade.set_mode(
        mode=ScorerDeploymentMode.DISABLED,
        expected_pointer_version=0,
        expected_deployment_revision=0,
        idempotency_key=idempotency_key,
        reason="Tắt scorer sau khi rà soát trạng thái.",
    )
    retry = (
        await ScorerDeploymentService(admin_sessions)
        .for_principal(
            _principal(
                workspace_id, membership_id, verified_at=datetime.now(UTC) - timedelta(seconds=2)
            )
        )
        .set_mode(
            mode=ScorerDeploymentMode.DISABLED,
            expected_pointer_version=0,
            expected_deployment_revision=0,
            idempotency_key=idempotency_key,
            reason="Tắt scorer sau khi rà soát trạng thái.",
        )
    )
    same_intent_retry = retry.event_id == first.event_id
    try:
        await facade.set_mode(
            mode=ScorerDeploymentMode.DISABLED,
            expected_pointer_version=0,
            expected_deployment_revision=0,
            idempotency_key=idempotency_key,
            reason="Tắt scorer sau khi quyền nguồn bị thu hồi.",
        )
    except Exception as error:
        changed_intent_conflict = "ý định deployment khác" in str(error)
    else:
        changed_intent_conflict = False

    state = await facade.load_admin_scoring_deployment()
    current_state_readable = (
        state.mode is ScorerDeploymentMode.DISABLED
        and state.deployment_revision == 1
        and state.active_pointer_version == 0
        and not state.runtime_usable
    )

    async def turn_off_with_key(key):
        try:
            return await facade.set_mode(
                mode=ScorerDeploymentMode.DISABLED,
                expected_pointer_version=0,
                expected_deployment_revision=1,
                idempotency_key=key,
                reason="Tắt scorer sau khi rà soát trạng thái.",
            )
        except Exception as error:  # kết quả race được phân loại bên dưới
            return error

    race_results = await asyncio.gather(turn_off_with_key(uuid4()), turn_off_with_key(uuid4()))
    race_has_one_winner = (
        sum(isinstance(result, ScorerDeploymentEvent) for result in race_results) == 1
    )
    race_has_one_conflict = sum(isinstance(result, Exception) for result in race_results) == 1
    winner = next(result for result in race_results if isinstance(result, ScorerDeploymentEvent))

    # Mô phỏng pointer legacy active nhưng chưa có quyền hiện hành được xác minh;
    # runtime phải từ chối, còn admin đọc CAS hiện hành để ghi event DISABLED.
    async with seed_engine.begin() as connection:
        await connection.execute(text("SET LOCAL session_replication_role = 'replica'"))
        await connection.execute(
            text(
                "UPDATE public.learning_scoring_deployment_pointer SET mode='SHADOW' "
                "WHERE workspace_id=:workspace_id"
            ),
            {"workspace_id": workspace_id},
        )
    stale_active_state = await facade.load_admin_scoring_deployment()
    stale_active_readable = (
        stale_active_state.mode is ScorerDeploymentMode.SHADOW
        and stale_active_state.deployment_revision == winner.revision
        and stale_active_state.active_pointer_version == 0
        and not stale_active_state.runtime_usable
    )
    runtime_rejects_unverified_active = False
    try:
        await facade.load_active_scoring_deployment()
    except Exception as error:
        runtime_rejects_unverified_active = "rights gateway" in str(error)
    disabled_after_stale_read = await facade.set_mode(
        mode=ScorerDeploymentMode.DISABLED,
        expected_pointer_version=stale_active_state.active_pointer_version,
        expected_deployment_revision=stale_active_state.deployment_revision,
        idempotency_key=uuid4(),
        reason="Tắt scorer vì cần rà soát quyền hiện hành.",
    )

    stale_cas_rejected = False
    try:
        await facade.set_mode(
            mode=ScorerDeploymentMode.DISABLED,
            expected_pointer_version=0,
            expected_deployment_revision=winner.revision,
            idempotency_key=uuid4(),
            reason="Tắt scorer bằng CAS cũ để xác minh lỗi.",
        )
    except Exception as error:
        stale_cas_rejected = "CAS đã cũ" in str(error)

    shadow_target = ScorerDeploymentTarget(
        registry_id=uuid4(),
        registry_revision=1,
        active_pointer_version=0,
        scorer_artifact_id=uuid4(),
        scorer_artifact_hash="a" * 64,
        evaluation_report_id=uuid4(),
        evaluation_report_hash="b" * 64,
        evaluation_status="ACCEPTED_SHADOW_ONLY",
        proof_id=uuid4(),
        proof_hash="c" * 64,
        candidate_completeness_verified=False,
        report_scope_hash="d" * 64,
        proof_scope_hash="d" * 64,
        policy_id="scorer-policy-v1",
        policy_hash="e" * 64,
        current_rights_receipt_hash="f" * 64,
    )
    shadow_event = ScorerDeploymentEvent(
        event_id=uuid4(),
        workspace_id=workspace_id,
        revision=disabled_after_stale_read.revision + 1,
        mode=ScorerDeploymentMode.SHADOW,
        target=shadow_target,
        actor_membership_id=membership_id,
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=1),
        mfa_method="TOTP",
        expected_pointer_version=0,
        expected_deployment_revision=disabled_after_stale_read.revision,
        reason="Bật SHADOW chỉ để test quyền gate.",
        idempotency_key=uuid4(),
        previous_event_id=disabled_after_stale_read.event_id,
        previous_event_hash=disabled_after_stale_read.event_hash,
        created_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    shadow_rejected = await _insert_and_expect_error(
        admin_sessions,
        workspace_id=workspace_id,
        membership_id=membership_id,
        event=shadow_event,
        marker="WP06_SCORING_DEPLOYMENT_RIGHTS_GATE_UNAVAILABLE",
    )

    bad_cas_event = ScorerDeploymentEvent(
        event_id=uuid4(),
        workspace_id=workspace_id,
        revision=disabled_after_stale_read.revision + 2,
        mode=ScorerDeploymentMode.DISABLED,
        target=None,
        actor_membership_id=membership_id,
        mfa_verified_at=datetime.now(UTC) - timedelta(seconds=1),
        mfa_method="TOTP",
        expected_pointer_version=0,
        expected_deployment_revision=disabled_after_stale_read.revision + 1,
        reason="Ghi event có CAS cũ để test rollback.",
        idempotency_key=uuid4(),
        previous_event_id=disabled_after_stale_read.event_id,
        previous_event_hash=disabled_after_stale_read.event_hash,
        created_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    rollback_after_cas_rejection = await _insert_and_expect_error(
        admin_sessions,
        workspace_id=workspace_id,
        membership_id=membership_id,
        event=bad_cas_event,
        marker="WP06_SCORING_DEPLOYMENT_STALE_REVISION",
    )

    async with admin_sessions() as database, database.begin():
        await database.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(workspace_id)},
        )
        event_count = await database.scalar(
            text("SELECT count(*) FROM public.learning_scoring_deployment_event")
        )
    async with admin_sessions() as database, database.begin():
        await database.execute(
            text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(second_workspace_id)},
        )
        cross_workspace_events = await database.scalar(
            text("SELECT count(*) FROM public.learning_scoring_deployment_event")
        )

    await admin_engine.dispose()
    await seed_engine.dispose()
    result: dict[str, object] = {
        "admin_insert_policy": True,
        "cas_race_one_winner": race_has_one_winner,
        "cas_race_one_conflict": race_has_one_conflict,
        "changed_intent_conflict": changed_intent_conflict,
        "cross_workspace_rls": cross_workspace_events == 0,
        "current_state_readable": current_state_readable,
        "event_count": event_count,
        "least_privilege": True,
        "same_intent_retry": same_intent_retry,
        "shadow_rights_gate_rejected": shadow_rejected,
        "stale_active_admin_readable": stale_active_readable,
        "stale_active_disabled": disabled_after_stale_read.mode is ScorerDeploymentMode.DISABLED,
        "runtime_rejects_unverified_active": runtime_rejects_unverified_active,
        "stale_cas_rejected": stale_cas_rejected,
        "stale_cas_rollback": rollback_after_cas_rejection,
        "forced_rls": True,
    }
    assert all(value is True for key, value in result.items() if key not in {"event_count"}), result
    assert event_count == 3, result
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_probe()), sort_keys=True))
