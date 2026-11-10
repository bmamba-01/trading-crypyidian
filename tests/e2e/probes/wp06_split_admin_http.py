"""Probe HTTP split review với session, quyền và PostgreSQL Compose thật."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    BacktestReportRecord,
    BacktestRunRecord,
    BacktestScenarioRecord,
    BacktestTrialRecord,
    CandidateUniverseProofRecord,
    LearningSplitApprovalRecord,
    LearningSplitCandidateRecord,
    RoleBinding,
    Session,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.repositories.learning_split_candidates import (
    LearningSplitCandidateRepository,
)
from trading_bot.app import create_app
from trading_bot.application.learning_split_admin import LearningSplitAdminService
from trading_bot.auth.service import AuthService, hash_token
from trading_bot.config.settings import AppSettings

STATE = json.loads(os.environ["WP06_SPLIT_HTTP_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
OTHER = UUID(STATE["other_workspace_id"])


def _engine(role: str | None = None):
    user = role or os.environ["TRADING_BOT_DATABASE_USER"]
    password_file = (
        f"/run/secrets/database_{role.removeprefix('crypidian_')}_password"
        if role
        else os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]
    )
    return create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username=user,
            password=Path(password_file).read_text().strip(),
            host="postgres",
            database="crypidian",
        ),
        connect_args={"options": "-c timezone=UTC"},
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _seed(sessions):
    now = datetime.now(UTC)
    actors: dict[str, dict[str, object]] = {}
    candidate_id = uuid4()
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=WORKSPACE,
                    name="WP06 split HTTP",
                    slug=f"wp06-split-http-{WORKSPACE.hex[:12]}",
                    mode="PAPER",
                    status="ACTIVE",
                ),
                Workspace(
                    id=OTHER,
                    name="WP06 split HTTP foreign",
                    slug=f"wp06-split-foreign-{OTHER.hex[:12]}",
                    mode="PAPER",
                    status="ACTIVE",
                ),
            ]
        )
        await session.flush()
        for name, role, workspace, mfa_at in (
            ("reviewer", "analyst_reviewer", WORKSPACE, now),
            ("stale", "analyst_reviewer", WORKSPACE, now - timedelta(hours=1)),
            ("operator", "operator", WORKSPACE, now),
            ("foreign", "analyst_reviewer", OTHER, now),
        ):
            user_id, membership_id = uuid4(), uuid4()
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session.add(
                UserAccount(
                    id=user_id,
                    email_normalized=f"{name}-{user_id.hex}@example.test",
                    password_hash="fixture-session-only",
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                UserMembership(
                    id=membership_id,
                    workspace_id=workspace,
                    user_id=user_id,
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                RoleBinding(
                    workspace_id=workspace,
                    membership_id=membership_id,
                    role=role,
                )
            )
            session.add(
                Session(
                    workspace_id=workspace,
                    membership_id=membership_id,
                    token_hash=hash_token(token),
                    csrf_token_hash=hash_token(csrf),
                    expires_at=now + timedelta(hours=1),
                    mfa_verified_at=mfa_at,
                )
            )
            actors[name] = {
                "workspace_id": str(workspace),
                "user_id": str(user_id),
                "membership_id": str(membership_id),
                "cookie": f"{workspace}:{token}",
                "csrf": csrf,
            }

        run_id, trial_id, report_id, scenario_record_id, proof_id = (uuid4() for _ in range(5))
        run_canonical = json.dumps(
            {"fixture": "wp06-split-admin-http", "run_id": str(run_id)},
            sort_keys=True,
            separators=(",", ":"),
        )
        run_hash = _hash(run_canonical)
        session.add(
            BacktestRunRecord(
                id=run_id,
                workspace_id=WORKSPACE,
                manifest_hash=run_hash,
                manifest=json.loads(run_canonical),
                canonical_manifest=run_canonical,
            )
        )
        trial_canonical = json.dumps(
            {"fixture": "wp06-split-admin-http", "trial_id": str(trial_id)},
            sort_keys=True,
            separators=(",", ":"),
        )
        trial_hash = _hash(trial_canonical)
        session.add(
            BacktestTrialRecord(
                id=trial_id,
                workspace_id=WORKSPACE,
                run_id=run_id,
                trial_id=trial_hash,
                manifest_hash=run_hash,
                parameter_family_hash=_hash("wp06-split-admin-http-parameters"),
                trial_index=0,
                trial_count=1,
                status="COMPLETED",
                observation_count=0,
                completed_observation_count=0,
                trade_count=0,
                error_type=None,
                error_detail=None,
                content_hash=trial_hash,
                canonical_trial=trial_canonical,
            )
        )
        await session.flush()
        report_canonical = json.dumps(
            {"fixture": "wp06-split-admin-http", "report_id": str(report_id)},
            sort_keys=True,
            separators=(",", ":"),
        )
        report_hash = _hash(report_canonical)
        session.add(
            BacktestReportRecord(
                id=report_id,
                workspace_id=WORKSPACE,
                run_id=run_id,
                trial_record_id=trial_id,
                manifest_hash=run_hash,
                report_hash=report_hash,
                observation_count=0,
                usable_observation_count=0,
                trade_count=0,
                coverage=0.0,
                report=json.loads(report_canonical),
                canonical_json=report_canonical,
            )
        )
        scenario_canonical = json.dumps(
            {"fixture": "wp06-split-admin-http", "scenario_id": "BASE"},
            sort_keys=True,
            separators=(",", ":"),
        )
        scenario_hash = _hash(scenario_canonical)
        session.add(
            BacktestScenarioRecord(
                id=scenario_record_id,
                workspace_id=WORKSPACE,
                run_id=run_id,
                trial_record_id=trial_id,
                manifest_hash=run_hash,
                scenario_id="BASE",
                kind="BASE",
                scenario_hash=scenario_hash,
                scenario=json.loads(scenario_canonical),
                canonical_scenario=scenario_canonical,
            )
        )
        await session.flush()
        proof_canonical = json.dumps(
            {
                "fixture": "wp06-split-admin-http",
                "trial_record_id": str(trial_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        proof_hash = _hash(proof_canonical)
        session.add(
            CandidateUniverseProofRecord(
                id=proof_id,
                workspace_id=WORKSPACE,
                run_id=run_id,
                trial_record_id=trial_id,
                report_id=report_id,
                scenario_id=scenario_record_id,
                query_version="base-closed-trade-universe-v1",
                manifest_hash=run_hash,
                proof_hash=proof_hash,
                membership_digest=_hash("[]"),
                candidate_count=0,
                eligible_count=0,
                exclusion_count=0,
                canonical_proof=proof_canonical,
            )
        )
        session.add(
            LearningSplitCandidateRecord(
                id=candidate_id,
                workspace_id=WORKSPACE,
                trial_record_id=trial_id,
                proof_record_id=proof_id,
                proof_hash=proof_hash,
                manifest_hash=_hash("wp06-split-admin-http-manifest"),
                split_policy_version="wp06-walk-forward-v1",
                fold_plan_hash=_hash("wp06-split-admin-http-fold-plan"),
                evaluation_cutoff=now + timedelta(days=1),
                preview={
                    "sample_count": 4,
                    "development_count": 3,
                    "holdout_count": 1,
                    "folds": [{"train": {"count": 1}, "validation": {"count": 1}}],
                },
                canonical_manifest="{}",
                canonical_configuration="{}",
            )
        )
    return actors, candidate_id


async def _run() -> dict[str, bool]:
    engines = [_engine(), _engine("crypidian_admin_web")]
    base, admin = [async_sessionmaker(item, expire_on_commit=False) for item in engines]
    try:
        actors, candidate_id = await _seed(base)
        app = create_app(AppSettings(app_env="test"))
        app.state.auth_service = AuthService(admin, None)
        app.state.learning_split_admin_service = LearningSplitAdminService(admin)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://wp06-split-fixture") as client:

            async def request(
                method: str,
                path: str,
                *,
                actor: str | None = "reviewer",
                csrf: bool = True,
                reason: str = "Duyệt split fixture qua HTTP thật.",
                idempotency_key: UUID | None = None,
            ):
                headers: dict[str, str] = {}
                if actor is not None:
                    identity = actors[actor]
                    headers["Cookie"] = f"crypidian_session={identity['cookie']}"
                    if csrf:
                        headers["X-CSRF-Token"] = str(identity["csrf"])
                if method == "POST":
                    headers["Idempotency-Key"] = str(idempotency_key or uuid4())
                    return await client.post(
                        path,
                        json={"reason": reason},
                        headers=headers,
                    )
                return await client.get(path, headers=headers)

            list_path = "/api/v1/learning-splits/candidates"
            anonymous = await request("GET", list_path, actor=None)
            assert anonymous.status_code == 401, anonymous.text
            role_denied = await request("GET", list_path, actor="operator")
            assert role_denied.status_code == 403, role_denied.text
            listed = await request("GET", list_path)
            assert listed.status_code == 200, listed.text
            assert listed.headers.get("cache-control") == "no-store"
            assert len(listed.json()["candidates"]) == 1
            row = listed.json()["candidates"][0]
            assert row["candidate_id"] == str(candidate_id)
            assert row["preview"]["sample_count"] == 4
            serialized = json.dumps(row, ensure_ascii=False)
            for forbidden in (
                "canonical_manifest",
                "canonical_configuration",
                "label_value",
                "features",
                "sample_id",
                "outcome_id",
            ):
                assert forbidden not in serialized

            candidate_path = f"/api/v1/learning-splits/candidates/{candidate_id}/approve"
            no_csrf = await request("POST", candidate_path, csrf=False)
            assert no_csrf.status_code == 403, no_csrf.text
            stale_mfa = await request("POST", candidate_path, actor="stale")
            assert stale_mfa.status_code == 403, stale_mfa.text
            denied_role = await request("POST", candidate_path, actor="operator")
            assert denied_role.status_code == 403, denied_role.text

            foreign_list = await request("GET", list_path, actor="foreign")
            assert foreign_list.status_code == 200, foreign_list.text
            assert foreign_list.json() == {"candidates": []}
            foreign_approval = await request("POST", candidate_path, actor="foreign")
            assert foreign_approval.status_code == 404, foreign_approval.text

            key = uuid4()
            approved = await request("POST", candidate_path, idempotency_key=key)
            assert approved.status_code == 201, approved.text
            assert approved.headers.get("cache-control") == "no-store"
            approved_body = approved.json()
            duplicate = await request("POST", candidate_path, idempotency_key=key)
            assert duplicate.status_code == 201, duplicate.text
            assert duplicate.json() == approved_body

            changed = await request(
                "POST",
                candidate_path,
                reason="Lý do khác, phải báo xung đột.",
                idempotency_key=key,
            )
            assert changed.status_code == 409, changed.text

            async with admin() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(WORKSPACE)},
                )
                rows = (
                    await session.scalars(
                        select(LearningSplitApprovalRecord).where(
                            LearningSplitApprovalRecord.workspace_id == WORKSPACE
                        )
                    )
                ).all()
                assert len(rows) == 1
                stored = rows[0].to_domain()
                assert stored.idempotency_key == key
                assert stored.actor_membership_id == UUID(actors["reviewer"]["membership_id"])
                assert stored.manifest_hash == row["manifest_hash"]
                assert stored.fold_plan_hash == row["fold_plan_hash"]
                candidate_rows = await LearningSplitCandidateRepository(session).list_for_review(
                    workspace_id=WORKSPACE
                )
                assert candidate_rows[0].approved_at == stored.approved_at
        return {
            "anonymous_denied": True,
            "csrf_denied": True,
            "foreign_workspace_hidden": True,
            "mfa_required": True,
            "metadata_only": True,
            "persisted_once": True,
            "retry_idempotent": True,
            "role_denied": True,
            "stale_key_conflict": True,
            "valid_reviewer_approved": True,
        }
    finally:
        for item in engines:
            await item.dispose()


print(json.dumps(asyncio.run(_run())))
