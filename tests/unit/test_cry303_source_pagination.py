"""CRY-303 — phân trang cursor ổn định cho danh sách nguồn (WP-05A)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_bot.application.source_policy_admin import (
    SourceListPage,
    SourcePolicyAdminService,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry

WORKSPACE = uuid4()
BUDGET = SourceBudget(timedelta(hours=1), 3, 1000, Decimal("1"), "USD")


def principal(workspace=WORKSPACE) -> Principal:
    return Principal(uuid4(), uuid4(), workspace, frozenset({Role.OWNER}), datetime.now(UTC))


def source(source_id: str, workspace: uuid4) -> SourceRegistryEntry:
    return SourceRegistryEntry.discover(
        source_id=source_id,
        publisher="Publisher",
        endpoint_url=f"https://example.org/{source_id}",
        access_method="GET",
        auth_scope="public",
        budget=BUDGET,
        actor_id=str(uuid4()),
        at=datetime.now(UTC),
        reason="Candidate",
    )


class PagedRepository:
    def __init__(self) -> None:
        self.entries: dict[tuple[uuid4, str], SourceRegistryEntry] = {}
        for sid in ("s1", "s2", "s3", "s4", "s5"):
            self.entries[(WORKSPACE, sid)] = source(sid, WORKSPACE)
        self.entries[(uuid4(), "other")] = source("other", uuid4())

    async def list_entries(self, *, workspace_id, after=None, limit=200):
        ids = sorted(
            sid
            for (ws, sid) in self.entries
            if ws == workspace_id and (after is None or sid > after)
        )
        return tuple(self.entries[(workspace_id, sid)] for sid in ids[:limit])


@pytest.mark.asyncio
async def test_page_uses_stable_cursor_and_never_leaks_other_workspace() -> None:
    service = SourcePolicyAdminService(PagedRepository()).for_principal(principal())
    page: SourceListPage = await service.list_sources_page(limit=2)
    assert [e.source_id for e in page.entries] == ["s1", "s2"]
    assert page.next_cursor == "s2"

    page = await service.list_sources_page(after=page.next_cursor, limit=2)
    assert [e.source_id for e in page.entries] == ["s3", "s4"]
    assert page.next_cursor == "s4"

    page = await service.list_sources_page(after=page.next_cursor, limit=2)
    assert [e.source_id for e in page.entries] == ["s5"]
    assert page.next_cursor is None
    assert all(e.source_id != "other" for e in page.entries)


@pytest.mark.asyncio
async def test_page_rejects_invalid_limit() -> None:
    service = SourcePolicyAdminService(PagedRepository()).for_principal(principal())
    with pytest.raises(ValueError, match="limit"):
        await service.list_sources_page(limit=0)
    with pytest.raises(ValueError, match="limit"):
        await service.list_sources_page(limit=201)


@pytest.mark.asyncio
async def test_api_exposes_cursor_header_and_keeps_no_store() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from trading_bot.admin.research_source_router import router
    from trading_bot.auth.router import current_identity, require_csrf
    from trading_bot.auth.service import AuthIdentity

    class Facade:
        async def list_sources_page(self, *, after=None, limit=200):
            assert after == "abc"
            assert limit == 2
            return SourceListPage((source("s1", WORKSPACE),), "next-1")

    class Service:
        def for_principal(self, principal):
            return Facade()

    app = FastAPI()
    app.include_router(router)
    app.state.source_policy_admin_service = Service()
    actor = AuthIdentity(
        principal=principal(),
        email="owner@example.test",
    )
    app.dependency_overrides[current_identity] = lambda: actor
    app.dependency_overrides[require_csrf] = lambda: actor
    with TestClient(app) as client:
        resp = client.get("/api/v1/research-sources?after=abc&limit=2")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Next-Cursor"] == "next-1"


def test_console_renders_pager_link_with_cursor() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from trading_bot.admin.research_source_router import admin_router
    from trading_bot.auth.router import current_identity, require_csrf
    from trading_bot.auth.service import AuthIdentity

    class Facade:
        async def list_sources(self):
            return (source("s1", WORKSPACE),)

        async def list_sources_page(self, *, after=None, limit=200):
            assert after == "abc"
            assert limit == 2
            return SourceListPage((source("s1", WORKSPACE),), "next-1")

        async def get_source(self, source_id):
            return None

    class Service:
        def for_principal(self, principal):
            return Facade()

    app = FastAPI()
    app.include_router(admin_router)
    from trading_bot.config.settings import AppSettings

    app.state.settings = AppSettings(app_env="test")
    app.state.source_policy_admin_service = Service()

    class _AuthService:
        async def issue_csrf_token(self, session):
            return "test-csrf"

    app.state.auth_service = _AuthService()
    from pathlib import Path

    from fastapi.templating import Jinja2Templates

    app.state.templates = Jinja2Templates(
        directory=str(Path(__file__).parents[2] / "src/trading_bot/templates")
    )
    actor = AuthIdentity(principal=principal(), email="owner@example.test")
    app.dependency_overrides[current_identity] = lambda: actor
    app.dependency_overrides[require_csrf] = lambda: actor
    with TestClient(app) as client:
        resp = client.get("/admin/research-sources?after=abc&limit=2")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert "after=next-1" in resp.text
