"""Probe policy admin với login PostgreSQL thật, session xác thực và API offline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    RoleBinding,
    Session,
    SourcePolicyRevisionRecord,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
from trading_bot.adapters.database.source_policy_admin import DatabaseSourcePolicyAdminRepository
from trading_bot.app import create_app
from trading_bot.application.source_policy_admin import SourcePolicyAdminService
from trading_bot.auth.service import AuthService, hash_token
from trading_bot.config.settings import AppSettings
from trading_bot.domain.research.source_policy import SourceUsePurpose

STATE = json.loads(os.environ["WP05_ADMIN_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
OTHER = UUID(STATE["other_workspace_id"])
SOURCE = "policy-admin-fixture"
ADMIN = "crypidian_admin_web"


def engine(role=None):
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


async def scope(session, workspace=WORKSPACE):
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace, true)"),
        {"workspace": str(workspace)},
    )


async def seed(sessions):
    actors = {}
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        for workspace in (WORKSPACE, OTHER):
            session.add(Workspace(id=workspace, name="Policy fixture", slug=f"a-{workspace.hex}"))
        await session.flush()
        for name, role, workspace in (
            ("owner", "owner", WORKSPACE),
            ("stale", "owner", WORKSPACE),
            ("reviewer", "analyst_reviewer", WORKSPACE),
            ("foreign", "owner", OTHER),
        ):
            user, member, token, csrf = (
                uuid4(),
                uuid4(),
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
            )
            session.add(
                UserAccount(
                    id=user, email_normalized=f"{name}@example.test", password_hash="disabled"
                )
            )
            await session.flush()
            session.add(
                UserMembership(id=member, workspace_id=workspace, user_id=user, status="ACTIVE")
            )
            await session.flush()
            session.add(RoleBinding(workspace_id=workspace, membership_id=member, role=role))
            session.add(
                Session(
                    workspace_id=workspace,
                    membership_id=member,
                    token_hash=hash_token(token),
                    csrf_token_hash=hash_token(csrf),
                    expires_at=now + timedelta(hours=1),
                    mfa_verified_at=now - timedelta(hours=1) if name == "stale" else now,
                )
            )
            actors[name] = {
                "user": user,
                "member": member,
                "cookie": f"{workspace}:{token}",
                "csrf": csrf,
            }
    return actors


async def counts(sessions):
    async with sessions() as session, session.begin():
        await scope(session)
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "source_policy_revision",
                    "source_policy_observation",
                    "source_quota_event",
                )
            ]
        )


async def denied(sessions, statement, *, workspace=WORKSPACE):
    try:
        async with sessions() as session, session.begin():
            await scope(session, workspace)
            await session.execute(text(statement))
    except DBAPIError as error:
        if error.orig.sqlstate not in {"42501", "23514"}:
            raise AssertionError(str(error)) from error
    else:
        raise AssertionError(f"SQL không bị chặn: {statement}")


async def run():
    engines = [engine(), engine(ADMIN), engine("crypidian_ingestion_worker")]
    base, admin, ingestion = [async_sessionmaker(e, expire_on_commit=False) for e in engines]
    try:
        actors = await seed(base)
        async with admin() as session:
            assert (await session.execute(text("SELECT session_user, current_user"))).one() == (
                ADMIN,
                ADMIN,
            )
            flags = (
                await session.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one()
            assert flags == (False, False)
        repository = DatabaseSourcePolicyAdminRepository(admin)
        service = SourcePolicyAdminService(repository)
        app = create_app(AppSettings(app_env="test"))
        # Xác thực cookie/CSRF/RBAC đọc session thật.
        # Probe không kiểm login mật khẩu/TOTP.
        app.state.auth_service = AuthService(admin, None)
        app.state.source_policy_admin_service = service
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://fixture"
        ) as client:

            async def request(
                method,
                path,
                payload=None,
                *,
                actor="owner",
                key=None,
                csrf=True,
                raw=None,
                extra_headers=None,
            ):
                identity = actors[actor]
                headers = {"Cookie": f"crypidian_session={identity['cookie']}"}
                if csrf:
                    headers["X-CSRF-Token"] = identity["csrf"]
                if method == "POST":
                    headers["Idempotency-Key"] = str(key or uuid4())
                headers.update(extra_headers or {})
                kwargs = {"content": raw} if raw is not None else {"json": payload}
                return await client.request(
                    method, f"/api/v1/research-sources{path}", headers=headers, **kwargs
                )

            payload = {
                "source_id": SOURCE,
                "publisher": "Nguồn fixture",
                "endpoint_url": "https://example.invalid/calendar",
                "access_method": "GET",
                "auth_scope": "public",
                "reason": "Candidate offline",
                "budget": {
                    "window_seconds": 3600,
                    "max_requests": 10,
                    "max_bytes": 1000,
                    "max_cost": "1",
                    "cost_unit": "USD",
                },
            }
            anonymous = await client.get("/api/v1/research-sources")
            assert anonymous.status_code == 401
            assert (await request("POST", "", payload, csrf=False)).status_code == 403
            assert (await request("POST", "", payload, actor="reviewer")).status_code == 403
            assert (await request("POST", "", payload, actor="stale")).status_code == 403
            created = await request("POST", "", payload)
            assert created.status_code == 201, created.text
            license_scope = {
                "source_id": SOURCE,
                "provider": "PROVIDER",
                "product": "CALENDAR",
                "venue": "PUBLIC",
                "contract": "RESEARCH",
                "symbols": ["BTCUSDT"],
                "intervals": ["1h"],
                "data_start": "2026-01-01T00:00:00Z",
                "data_end": "2027-01-01T00:00:00Z",
                "allowed_purposes": ["metadata"],
                "allowed_outputs": ["aggregate_report"],
                "derived_use": ["aggregate_reporting"],
                "max_retention_days": None,
                "attribution": "Nguồn fixture",
            }
            pending_payload = {
                "state": "rights_pending",
                "reason": "Đề nghị xem xét",
                "evidence_ref": "fixture-only-unverified",
                "license_scope": license_scope,
                "attribution": license_scope["attribution"],
                **{
                    name: license_scope[name]
                    for name in ("provider", "product", "venue", "contract")
                },
            }
            key = uuid4()
            # Fault injection đúng ranh giới sau commit append, trước transaction observer.
            with patch.object(
                repository._observer,
                "_finish_policy",
                side_effect=LookupError("fixture observation outage"),
            ):
                failed = await request("POST", f"/{SOURCE}/revisions", pending_payload, key=key)
            assert failed.status_code == 503, failed.text
            assert failed.headers["cache-control"] == "no-store"
            assert await counts(admin) == (2, 1, 0)
            async with ingestion() as session, session.begin():
                try:
                    await SourceRegistryRepository(session).reserve_quota(
                        workspace_id=WORKSPACE,
                        source_id=SOURCE,
                        idempotency_key="deny-gap",
                        purpose=SourceUsePurpose.METADATA,
                        reserved_bytes=1,
                        reserved_cost="0",
                    )
                except PermissionError:
                    pass
                else:
                    raise AssertionError("Receipt thiếu vẫn được dùng nguồn")
            recovered = await request("POST", f"/{SOURCE}/revisions", pending_payload, key=key)
            assert recovered.status_code == 201, recovered.text
            assert await counts(admin) == (2, 2, 0)
            pending_id = recovered.json()["policy_id"]
            changed = await request(
                "POST", f"/{SOURCE}/revisions", pending_payload | {"reason": "Khác"}, key=key
            )
            assert changed.status_code == 409
            upload_path = f"/{SOURCE}/revisions/{pending_id}/license-evidence"

            async def evidence_count():
                async with admin() as session, session.begin():
                    await scope(session)
                    return await session.scalar(
                        text("SELECT count(*) FROM source_license_evidence")
                    )

            async def chunks(*values):
                for value in values:
                    yield value

            upload_key = uuid4()
            upload_headers = {"Content-Type": "text/plain"}
            for extra, body, code in (
                ({"Content-Type": "image/png"}, b"x", 415),
                ({}, b"", 400),
                ({"Content-Length": "bad"}, b"x", 400),
                ({"Content-Length": "-1"}, b"x", 400),
                ({"Content-Length": "2"}, b"x", 400),
                ({"Content-Length": "10485761"}, b"x", 413),
                ({"Content-Length": "1"}, chunks(b"a" * 10485760, b"b"), 413),
            ):
                rejected = await request(
                    "POST", upload_path, raw=body, extra_headers=upload_headers | extra
                )
                assert rejected.status_code == code, rejected.text
                assert rejected.headers["cache-control"] == "no-store"
                assert await evidence_count() == 0
            for actor, csrf, path, code in (
                ("owner", False, upload_path, 403),
                ("stale", True, upload_path, 403),
                ("reviewer", True, upload_path, 403),
                ("foreign", True, upload_path, 404),
                ("owner", True, f"/{SOURCE}/revisions/wrong/license-evidence", 409),
                ("owner", True, "/missing/revisions/wrong/license-evidence", 404),
            ):
                rejected = await request(
                    "POST", path, raw=b"x", actor=actor, csrf=csrf, extra_headers=upload_headers
                )
                assert rejected.status_code == code, rejected.text
                assert rejected.headers["cache-control"] == "no-store"
                assert await evidence_count() == 0
            original_append = SourceRegistryRepository.append_license_evidence

            async def fail_after_flush(repository, **kwargs):
                await original_append(repository, **kwargs)
                raise RuntimeError("fixture rollback after flush")

            with patch.object(
                SourceRegistryRepository, "append_license_evidence", fail_after_flush
            ):
                failed_upload = await request(
                    "POST",
                    upload_path,
                    raw=b"Fixture license",
                    extra_headers=upload_headers,
                    key=upload_key,
                )
            assert failed_upload.status_code == 500, failed_upload.text
            assert failed_upload.headers["cache-control"] == "no-store"
            assert await evidence_count() == 0
            body = b"Fixture license"

            async def initial_upload():
                return await request(
                    "POST",
                    upload_path + "?evidence_ref=attacker&scope=attacker",
                    raw=chunks(body[:3], body[3:]),
                    key=upload_key,
                    extra_headers=upload_headers
                    | {
                        "X-Evidence-Ref": "attacker",
                        "X-Scope": "attacker",
                        "Content-Disposition": 'attachment; filename="secret-name.txt"',
                    },
                )

            uploaded, concurrent = await asyncio.gather(
                initial_upload(),
                request(
                    "POST",
                    upload_path,
                    raw=body,
                    key=upload_key,
                    extra_headers=upload_headers,
                ),
            )
            assert uploaded.status_code == concurrent.status_code == 201, uploaded.text
            assert uploaded.json() == concurrent.json()
            evidence = uploaded.json()
            assert evidence["digest"] == hashlib.sha256(body).hexdigest()
            assert evidence["byte_count"] == len(body)
            assert evidence["evidence_ref"] == pending_payload["evidence_ref"]
            assert evidence["license_scope"]["provider"] == "PROVIDER"
            assert "secret-name" not in uploaded.text
            assert "attacker" not in uploaded.text
            assert uploaded.headers["cache-control"] == "no-store"
            for retry_key in (upload_key, uuid4()):
                retry = await request(
                    "POST", upload_path, raw=body, key=retry_key, extra_headers=upload_headers
                )
                assert retry.status_code == 201, retry.text
                assert retry.json() == evidence, (retry.json(), evidence)
            for retry_key in (upload_key, uuid4()):
                conflict = await request(
                    "POST", upload_path, raw=b"changed", key=retry_key, extra_headers=upload_headers
                )
                assert conflict.status_code == 409, conflict.text
            assert await evidence_count() == 1
            assert (await request("GET", f"/{SOURCE}")).json()["current"][
                "state"
            ] == "rights_pending"
            approval = {
                "allowed_purposes": ["metadata"],
                "valid_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "reason": "Duyệt fixture",
            }
            path = f"/{SOURCE}/revisions/{pending_id}/approve"
            assert (await request("POST", path, approval)).status_code == 403
            assert (await request("POST", path, approval, actor="foreign")).status_code == 404
            assert await counts(admin) == (2, 2, 0)
            approved = await request("POST", path, approval, actor="reviewer")
            assert approved.status_code == 201, approved.text
            assert (
                approved.json()["actor_id"]
                == f"{actors['owner']['user']}:{actors['owner']['member']}"
            )
            assert approved.json()["approved_by"] == str(actors["reviewer"]["user"])
            assert approved.json()["license_evidence_id"] == evidence["evidence_id"]
            assert approved.json()["license_digest"] == evidence["digest"]
            assert (
                await request(
                    "POST", upload_path, raw=body, key=upload_key, extra_headers=upload_headers
                )
            ).status_code == 409
            assert await evidence_count() == 1
            assert await counts(admin) == (3, 3, 0)
            # Usage fixture chỉ ghi bằng login ingestion, admin chỉ đọc snapshot theo DB clock.
            async with ingestion() as session, session.begin():
                await SourceRegistryRepository(session).reserve_quota(
                    workspace_id=WORKSPACE,
                    source_id=SOURCE,
                    idempotency_key="fixture-usage",
                    purpose=SourceUsePurpose.METADATA,
                    reserved_bytes=17,
                    reserved_cost="0.25",
                )
            before = await counts(admin)
            async with admin() as session:
                clock_before = await session.scalar(text("SELECT clock_timestamp()"))
            for _ in range(2):
                detail = await request("GET", f"/{SOURCE}")
                assert detail.status_code == 200, detail.text
                assert detail.headers["cache-control"] == "no-store"
                history = detail.json()["revisions"]
                assert history[-1]["actor_id"] == approved.json()["actor_id"]
                assert history[-1]["approved_by"] == str(actors["reviewer"]["user"])
                quota = detail.json()["quota"]
                async with admin() as session:
                    clock_after = await session.scalar(text("SELECT clock_timestamp()"))
                assert clock_before <= datetime.fromisoformat(quota["as_of"]) <= clock_after
                assert quota["policy_sequence"] == 3
                assert quota["requests_used"] == 1
                assert quota["bytes_used"] == 17
                assert Decimal(quota["cost_used"]) == Decimal("0.25")
            assert await counts(admin) == before == (3, 3, 1)
            assert (await request("GET", f"/{SOURCE}", actor="foreign")).status_code == 404
            assert (await request("GET", "", actor="foreign")).json() == []
        for table in ("source_policy_revision", "source_policy_observation", "source_quota_event"):
            for action in (f"UPDATE {table} SET source_id = source_id", f"DELETE FROM {table}"):
                await denied(admin, action)
            async with admin() as session, session.begin():
                await scope(session, OTHER)
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                assert (
                    await session.scalar(
                        text(
                            "SELECT relrowsecurity AND relforcerowsecurity "
                            f"FROM pg_class WHERE oid='{table}'::regclass"
                        )
                    )
                    is True
                )
            async with admin() as session, session.begin():
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        await denied(admin, "INSERT INTO source_quota_event SELECT * FROM source_quota_event")
        columns = [column.name for column in SourcePolicyRevisionRecord.__table__.columns]
        values = [
            "gen_random_uuid()"
            if name == "id"
            else f"'{OTHER}'::uuid"
            if name == "workspace_id"
            else name
            for name in columns
        ]
        await denied(
            admin,
            f"INSERT INTO source_policy_revision ({','.join(columns)}) "
            f"SELECT {','.join(values)} FROM source_policy_revision WHERE sequence=1",
        )
        async with admin() as session, session.begin():
            await scope(session, OTHER)
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM source_policy_revision WHERE workspace_id=:workspace"
                    ),
                    {"workspace": WORKSPACE},
                )
                == 0
            )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            later = await request("POST", f"/{SOURCE}/revisions", pending_payload)
            assert later.status_code == 201, later.text
            later_path = f"/{SOURCE}/revisions/{later.json()['policy_id']}/approve"
            reuse = approval | {"license_evidence_id": evidence["evidence_id"]}
            reuse_key = uuid4()
            reused = await request("POST", later_path, reuse, actor="reviewer", key=reuse_key)
            assert reused.status_code == 201, reused.text
            retried = await request("POST", later_path, reuse, actor="reviewer", key=reuse_key)
            assert retried.json() == reused.json()
            assert reused.json()["license_evidence_id"] == evidence["evidence_id"]
            assert reused.json()["license_digest"] == evidence["digest"]
            assert reused.json()["license_scope_hash"] == evidence["scope_hash"]
            assert await evidence_count() == 1
            for changes in (
                {"evidence_ref": "archive:changed"},
                {"license_scope": license_scope | {"symbols": ["ETHUSDT"]}},
            ):
                changed = await request("POST", f"/{SOURCE}/revisions", pending_payload | changes)
                assert changed.status_code == 201, changed.text
                changed_path = f"/{SOURCE}/revisions/{changed.json()['policy_id']}/approve"
                denied_reuse = await request("POST", changed_path, reuse, actor="reviewer")
                assert denied_reuse.status_code == 409, denied_reuse.text
                blocked = await request(
                    "POST",
                    f"/{SOURCE}/revisions",
                    {
                        "state": "blocked",
                        "reason": "Sửa lại phạm vi",
                    },
                )
                assert blocked.status_code == 201, blocked.text
            second_source = SOURCE + "-second"
            second_created = await request("POST", "", payload | {"source_id": second_source})
            assert second_created.status_code == 201, second_created.text
            second_pending = await request(
                "POST",
                f"/{second_source}/revisions",
                pending_payload
                | {
                    "license_scope": license_scope | {"source_id": second_source},
                },
            )
            assert second_pending.status_code == 201, second_pending.text
            cross_source = await request(
                "POST",
                f"/{second_source}/revisions/{second_pending.json()['policy_id']}/approve",
                reuse,
                actor="reviewer",
            )
            assert cross_source.status_code == 404, cross_source.text
            foreign_created = await request("POST", "", payload, actor="foreign")
            assert foreign_created.status_code == 201, foreign_created.text
            foreign_pending = await request(
                "POST", f"/{SOURCE}/revisions", pending_payload, actor="foreign"
            )
            assert foreign_pending.status_code == 201, foreign_pending.text
            foreign_uploaded = await request(
                "POST",
                f"/{SOURCE}/revisions/{foreign_pending.json()['policy_id']}/license-evidence",
                actor="foreign",
                raw=body,
                extra_headers=upload_headers,
            )
            assert foreign_uploaded.status_code == 201, foreign_uploaded.text
            final_pending = await request("POST", f"/{SOURCE}/revisions", pending_payload)
            assert final_pending.status_code == 201, final_pending.text
            final_path = f"/{SOURCE}/revisions/{final_pending.json()['policy_id']}/approve"
            cross_workspace = await request(
                "POST",
                final_path,
                reuse
                | {
                    "license_evidence_id": foreign_uploaded.json()["evidence_id"],
                },
                actor="reviewer",
            )
            assert cross_workspace.status_code == 404, cross_workspace.text
            excessive = await request(
                "POST",
                final_path,
                reuse
                | {
                    "allowed_purposes": ["training"],
                },
                actor="reviewer",
            )
            assert excessive.status_code == 409, excessive.text
            assert (await request("POST", final_path, reuse)).status_code == 403
            final_approved = await request("POST", final_path, reuse, actor="reviewer")
            assert final_approved.status_code == 201, final_approved.text
            assert final_approved.json()["license_evidence_id"] == evidence["evidence_id"]
            assert await evidence_count() == 1
            assert await counts(admin) == (13, 13, 1)
        return {
            "prior_evidence_reused_exact_id": True,
            "prior_evidence_scope_workspace_denials": True,
            "stream_evidence_boundary_idempotency": True,
            "evidence_transaction_rollback": True,
            "real_admin_login": True,
            "authenticated_owner_reviewer": True,
            "policy_rows": 13,
            "receipt_rows": 13,
            "quota_rows": 1,
            "self_approval_denied": True,
            "foreign_workspace_denied": True,
            "provenance_preserved": True,
            "observer_retry_idempotent": True,
            "quota_read_only": True,
            "append_only_rls": True,
        }
    finally:
        for item in engines:
            await item.dispose()


print(json.dumps(asyncio.run(run())))
