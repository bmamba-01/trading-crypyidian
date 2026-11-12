"""Probe HTTP thật cho incident hold; synthetic metadata không phải rights evidence."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    AuditEvent,
    RoleBinding,
    Session,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.app import create_app
from trading_bot.application.learning_incident_entry_hold import (
    LearningIncidentEntryHoldService,
)
from trading_bot.auth.service import AuthService, hash_token
from trading_bot.config.settings import AppSettings

STATE = json.loads(sys.stdin.read())
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


def _load_schema_probe():
    path = Path("/e2e-tests/e2e/probes/wp06_learning_incident_entry_hold_schema.py")
    spec = importlib.util.spec_from_file_location("wp06_hold_schema_seed", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed(sessions):
    now = datetime.now(UTC)
    actors: dict[str, dict[str, str]] = {}
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Workspace(
                    id=WORKSPACE,
                    name="WP06 incident admin",
                    slug=f"wp06-incident-admin-{WORKSPACE.hex[:12]}",
                    mode="PAPER",
                    status="ACTIVE",
                ),
                Workspace(
                    id=OTHER,
                    name="WP06 incident admin foreign",
                    slug=f"wp06-incident-foreign-{OTHER.hex[:12]}",
                    mode="PAPER",
                    status="ACTIVE",
                ),
            ]
        )
        await session.flush()
        entries = (
            ("owner", "owner", WORKSPACE, now, "TOTP"),
            ("risk", "risk_admin", WORKSPACE, now, "TOTP"),
            ("analyst", "analyst_reviewer", WORKSPACE, now, "TOTP"),
            ("stale", "risk_admin", WORKSPACE, now - timedelta(hours=1), "TOTP"),
            ("foreign", "risk_admin", OTHER, now, "TOTP"),
        )
        for name, role, workspace, mfa_at, mfa_method in entries:
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
                    mfa_method=mfa_method,
                )
            )
            actors[name] = {
                "workspace_id": str(workspace),
                "membership_id": str(membership_id),
                "cookie": f"{workspace}:{token}",
                "csrf": csrf,
            }
    return actors


async def _run() -> dict[str, bool]:
    engines = [_engine(), _engine("crypidian_admin_web")]
    base, admin = [async_sessionmaker(item, expire_on_commit=False) for item in engines]
    schema_probe = _load_schema_probe()
    fixture_engine = schema_probe._engine(
        os.environ["TRADING_BOT_DATABASE_USER"],
        os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"],
    )
    try:
        actors = await _seed(base)
        now = datetime.now(UTC) - timedelta(seconds=1)
        with fixture_engine.begin() as connection:
            hold = schema_probe._seed_metadata(
                connection,
                WORKSPACE,
                UUID(actors["risk"]["membership_id"]),
                now,
                seed_hold=True,
            )
            stale_target = schema_probe._seed_metadata(
                connection,
                WORKSPACE,
                UUID(actors["risk"]["membership_id"]),
                now,
                seed_hold=False,
            )

        app = create_app(AppSettings(app_env="test", process_role="admin-web"))
        app.state.auth_service = AuthService(admin, None)
        app.state.learning_incident_entry_hold_service = LearningIncidentEntryHoldService(admin)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://wp06-hold-admin") as client:

            async def request(
                method: str,
                path: str,
                *,
                actor: str | None = "owner",
                csrf: bool = True,
                body: dict[str, object] | None = None,
                key: UUID | None = None,
            ):
                headers: dict[str, str] = {}
                if actor is not None:
                    identity = actors[actor]
                    headers["Cookie"] = f"crypidian_session={identity['cookie']}"
                    if csrf:
                        headers["X-CSRF-Token"] = identity["csrf"]
                if key is not None:
                    headers["Idempotency-Key"] = str(key)
                if method == "POST":
                    return await client.post(path, json=body or {}, headers=headers)
                return await client.get(path, headers=headers)

            base_path = "/api/v1/learning/incident-entry-holds"
            anonymous = await request("GET", base_path, actor=None)
            assert anonymous.status_code == 401, anonymous.text
            denied = await request("GET", base_path, actor="analyst")
            assert denied.status_code == 403, denied.text
            listed = await request("GET", base_path)
            assert listed.status_code == 200, listed.text
            assert listed.headers.get("cache-control") == "no-store"
            assert len(listed.json()["events"]) == 1
            encoded = json.dumps(listed.json(), ensure_ascii=False)
            for forbidden in ("sample_id", "feature_values", "label_value", "source_payload"):
                assert forbidden not in encoded
            foreign = await request("GET", base_path, actor="foreign")
            assert foreign.status_code == 200, foreign.text
            assert foreign.json() == {"events": []}

            release_path = f"{base_path}/{hold.proposal_id}/release"
            release_body = {"expected_revision": 1, "reason": "Đã rà soát và xử lý incident."}
            csrf_denied = await request(
                "POST", release_path, actor="risk", csrf=False, body=release_body, key=uuid4()
            )
            assert csrf_denied.status_code == 403, csrf_denied.text

            hold_path = f"{base_path}/{stale_target.proposal_id}/hold"
            hold_body = {
                "assessment_id": str(stale_target.assessment_id),
                "assessment_hash": stale_target.assessment_hash,
                "expected_revision": 0,
                "expected_deployment_revision": stale_target.deployment_revision,
                "expected_active_pointer_version": stale_target.active_pointer_version,
                "reason": "Thử target đã bị vô hiệu.",
            }
            stale_mfa = await request("POST", hold_path, actor="stale", body=hold_body, key=uuid4())
            assert stale_mfa.status_code == 403, stale_mfa.text
            role_denied = await request(
                "POST", hold_path, actor="analyst", body=hold_body, key=uuid4()
            )
            assert role_denied.status_code == 403, role_denied.text
            strict_body = await request(
                "POST",
                hold_path,
                actor="risk",
                body=hold_body | {"workspace_id": str(WORKSPACE)},
                key=uuid4(),
            )
            assert strict_body.status_code == 422, strict_body.text
            stale_target_denied = await request(
                "POST", hold_path, actor="risk", body=hold_body, key=uuid4()
            )
            assert stale_target_denied.status_code == 409, stale_target_denied.text
            assert stale_target_denied.headers.get("cache-control") == "no-store"

            release_key = uuid4()
            released = await request(
                "POST", release_path, actor="risk", body=release_body, key=release_key
            )
            assert released.status_code == 200, released.text
            assert released.headers.get("cache-control") == "no-store"
            released_body = released.json()
            assert released_body["action"] == "RELEASE"
            assert released_body["revision"] == 2
            assert released_body["actor_membership_id"] == actors["risk"]["membership_id"]
            repeated = await request(
                "POST", release_path, actor="risk", body=release_body, key=release_key
            )
            assert repeated.status_code == 200, repeated.text
            assert repeated.json() == released_body
            conflict = await request(
                "POST",
                release_path,
                actor="risk",
                body={"expected_revision": 1, "reason": "Một ý định khác."},
                key=release_key,
            )
            assert conflict.status_code == 409, conflict.text

            async with admin() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('app.workspace_id', :workspace, true)"),
                    {"workspace": str(WORKSPACE)},
                )
                events = (
                    await session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.workspace_id == WORKSPACE,
                            AuditEvent.action == "learning.incident_entry_hold.release",
                            AuditEvent.target_id == hold.proposal_id,
                        )
                    )
                ).all()
                assert len(events) == 1
            history = await request("GET", base_path)
            assert history.status_code == 200, history.text
            assert len(history.json()["events"]) == 2
        return {
            "anonymous_denied": True,
            "csrf_denied": True,
            "fresh_mfa_required": True,
            "role_denied": True,
            "workspace_scoped_metadata": True,
            "strict_body": True,
            "stale_target_denied": True,
            "release_persisted_once": True,
            "release_retry_idempotent": True,
            "release_retry_conflict": True,
            "audit_atomic_and_unique": True,
        }
    finally:
        fixture_engine.dispose()
        for engine in engines:
            await engine.dispose()


print(json.dumps(asyncio.run(_run())))
