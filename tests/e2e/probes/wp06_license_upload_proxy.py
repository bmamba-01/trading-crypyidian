"""Chuẩn bị và đối chiếu fixture cho E2E upload qua Caddy và PostgreSQL thật."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_bot.adapters.database.models import (
    RoleBinding,
    Session,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.adapters.database.source_policy_admin import DatabaseSourcePolicyAdminRepository
from trading_bot.app import create_app
from trading_bot.application.source_policy_admin import SourcePolicyAdminService
from trading_bot.auth.service import AuthService, hash_token
from trading_bot.config.settings import AppSettings

PROBE_PAYLOAD: dict[str, object] = globals().get("PROBE_PAYLOAD", {})


def engine(role: str | None = None):
    user = role or "crypidian_admin"
    password_file = (
        f"/run/secrets/database_{role.removeprefix('crypidian_')}_password"
        if role
        else "/run/secrets/postgres_password"
    )
    return create_async_engine(
        URL.create(
            "postgresql+psycopg",
            username=user,
            password=Path(password_file).read_text(encoding="utf-8").strip(),
            host="postgres",
            database="crypidian",
        ),
        connect_args={"options": "-c timezone=UTC"},
    )


async def seed_upload() -> dict[str, object]:
    workspace_id = uuid4()
    user_id = uuid4()
    membership_id = uuid4()
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    base_engine = engine()
    admin_engine = engine("crypidian_admin_web")
    base = async_sessionmaker(base_engine, expire_on_commit=False)
    admin = async_sessionmaker(admin_engine, expire_on_commit=False)
    try:
        async with base() as session, session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    name="Kiểm thử proxy upload",
                    slug=f"upload-proxy-{workspace_id.hex}",
                )
            )
            session.add(
                UserAccount(
                    id=user_id,
                    email_normalized=f"owner-{workspace_id.hex}@example.test",
                    password_hash="disabled",
                )
            )
            await session.flush()
            session.add(
                UserMembership(
                    id=membership_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    status="ACTIVE",
                )
            )
            await session.flush()
            session.add(
                RoleBinding(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    role="owner",
                )
            )
            session.add(
                Session(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    token_hash=hash_token(session_token),
                    csrf_token_hash=hash_token(csrf_token),
                    expires_at=now + timedelta(hours=1),
                    mfa_verified_at=now,
                )
            )

        app = create_app(AppSettings(app_env="test"))
        app.state.auth_service = AuthService(admin, None)
        app.state.source_policy_admin_service = SourcePolicyAdminService(
            DatabaseSourcePolicyAdminRepository(admin)
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            headers = {
                "Cookie": f"crypidian_session={workspace_id}:{session_token}",
                "X-CSRF-Token": csrf_token,
            }

            async def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                response = await client.post(
                    path,
                    json=payload,
                    headers=headers | {"Idempotency-Key": str(uuid4())},
                )
                if response.status_code != 201:
                    raise AssertionError(
                        f"Tạo fixture thất bại ở {path}: HTTP {response.status_code}"
                    )
                return response.json()

            source_id = f"upload-proxy-{workspace_id.hex[:12]}"
            await post(
                "/api/v1/research-sources",
                {
                    "source_id": source_id,
                    "publisher": "Nhà xuất bản thử nghiệm",
                    "endpoint_url": "https://example.invalid/terms",
                    "access_method": "GET",
                    "auth_scope": "public",
                    "reason": "Fixture kiểm tra giới hạn upload qua reverse proxy",
                    "budget": {
                        "window_seconds": 3600,
                        "max_requests": 10,
                        "max_bytes": 10 * 1024 * 1024,
                        "max_cost": "1",
                        "cost_unit": "USD",
                    },
                },
            )
            license_scope = {
                "source_id": source_id,
                "provider": "OFFLINE_PUBLISHER",
                "product": "TERMS_DOCUMENT",
                "venue": "CRYPIDIAN",
                "contract": "NONE",
                "symbols": ["BTCUSDT"],
                "intervals": ["1h"],
                "data_start": "2026-01-01T00:00:00Z",
                "data_end": "2027-01-01T00:00:00Z",
                "allowed_purposes": ["metadata"],
                "allowed_outputs": ["aggregate_report"],
                "derived_use": ["aggregate_reporting"],
                "max_retention_days": None,
                "attribution": "Chỉ là dữ liệu giả dùng cho kiểm thử",
            }
            pending = await post(
                f"/api/v1/research-sources/{source_id}/revisions",
                {
                    "state": "rights_pending",
                    "reason": "Chờ duyệt fixture kiểm thử",
                    "evidence_ref": "fixture-only-unverified",
                    "license_scope": license_scope,
                    "attribution": license_scope["attribution"],
                    "provider": license_scope["provider"],
                    "product": license_scope["product"],
                    "venue": license_scope["venue"],
                    "contract": license_scope["contract"],
                },
            )
        return {
            "workspace_id": str(workspace_id),
            "source_id": source_id,
            "policy_id": str(pending["policy_id"]),
            "cookie": f"{workspace_id}:{session_token}",
            "csrf": csrf_token,
        }
    finally:
        await admin_engine.dispose()
        await base_engine.dispose()


async def verify_evidence(state: dict[str, object]) -> dict[str, object]:
    workspace_id = UUID(str(state["workspace_id"]))
    admin_engine = engine("crypidian_admin_web")
    admin = async_sessionmaker(admin_engine, expire_on_commit=False)
    try:
        async with admin() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace, true)"),
                {"workspace": str(workspace_id)},
            )
            rows = (
                await session.execute(
                    text(
                        "SELECT digest, byte_count FROM source_license_evidence "
                        "WHERE workspace_id=:workspace AND source_id=:source "
                        "AND policy_id=:policy ORDER BY created_at"
                    ),
                    {
                        "workspace": workspace_id,
                        "source": state["source_id"],
                        "policy": state["policy_id"],
                    },
                )
            ).all()
        return {
            "evidence_count": len(rows),
            "digest": rows[0].digest if rows else None,
            "byte_count": rows[0].byte_count if rows else None,
        }
    finally:
        await admin_engine.dispose()


async def main() -> None:
    payload = PROBE_PAYLOAD
    if payload.get("mode") == "seed":
        result = await seed_upload()
    elif payload.get("mode") == "verify":
        result = await verify_evidence(payload["state"])
    else:
        raise ValueError("Chế độ probe không được hỗ trợ")
    print(json.dumps(result))


asyncio.run(main())
