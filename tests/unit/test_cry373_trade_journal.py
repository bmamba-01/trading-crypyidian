"""Unit test cho WP-07D journal, checklist, tag setup, mistake attribution và API (CRY-373)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_bot.admin.trade_journal_router import router as trade_journal_router
from trading_bot.application.trade_journal import TradeJournalError, TradeJournalService
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.domain.portfolio.trade_journal import (
    JournalPrivacyMode,
    JournalValidationError,
    compute_mistake_attribution,
    compute_setup_tag_breakdown,
    is_entry_visible_to_actor,
    normalize_checklist_items,
    normalize_tags,
    sanitize_note,
    validate_attachment_metadata,
    validate_retention_days,
)


def _principal(role: Role, *, ws_id: UUID | None = None, user_id: UUID | None = None) -> Principal:
    return Principal(
        user_id=user_id or uuid4(),
        membership_id=uuid4(),
        workspace_id=ws_id or uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC),
    )


def test_domain_privacy_retention_and_attachment_guards() -> None:
    """Kiểm tra chặn secret, kiểm tra EXIF/URL ngoài ở ảnh đính kèm, retention và privacy."""
    assert sanitize_note("  Vào lệnh đúng kế hoạch  ") == "Vào lệnh đúng kế hoạch"
    with pytest.raises(JournalValidationError) as exc_secret:
        sanitize_note("api_key=abc1234567890")
    assert exc_secret.value.code == "JOURNAL_SECRET_DETECTED"

    assert normalize_tags([" Breakout_20 ", "breakout_20", "Trend"], kind="setup") == (
        "Breakout_20",
        "Trend",
    )
    items = normalize_checklist_items(
        [{"label_vi": "Đúng xu hướng", "checked": True}, {"label_vi": "Đủ R:R", "checked": False}]
    )
    assert len(items) == 2
    assert items[0].checked is True
    assert validate_retention_days(None) == 365
    with pytest.raises(JournalValidationError):
        validate_retention_days(3)

    valid_att = validate_attachment_metadata(
        file_name="../../chart.png",
        content_type="image/png",
        byte_size=1024,
        sha256="a" * 64,
        storage_ref="journal/ws1/entry1/chart.png",
        exif_stripped=True,
    )
    assert valid_att["file_name"] == "chart.png"

    with pytest.raises(JournalValidationError) as exc_exif:
        validate_attachment_metadata(
            file_name="chart.png",
            content_type="image/png",
            byte_size=1024,
            sha256="a" * 64,
            storage_ref="journal/ws1/chart.png",
            exif_stripped=False,
        )
    assert exc_exif.value.code == "JOURNAL_ATTACHMENT_EXIF_REQUIRED"

    with pytest.raises(JournalValidationError) as exc_url:
        validate_attachment_metadata(
            file_name="chart.png",
            content_type="image/png",
            byte_size=1024,
            sha256="a" * 64,
            storage_ref="https://evil.example/img.png",
            exif_stripped=True,
        )
    assert exc_url.value.code == "JOURNAL_ATTACHMENT_REF_INVALID"

    author_id = uuid4()
    other_id = uuid4()
    private_entry = {
        "privacy_mode": JournalPrivacyMode.AUTHOR_ONLY.value,
        "author_user_id": str(author_id),
    }
    assert is_entry_visible_to_actor(
        private_entry, actor_user_id=author_id, is_workspace_owner=False
    )
    assert not is_entry_visible_to_actor(
        private_entry, actor_user_id=other_id, is_workspace_owner=False
    )
    assert is_entry_visible_to_actor(
        private_entry, actor_user_id=other_id, is_workspace_owner=True
    )


def test_mistake_attribution_and_setup_breakdown_keep_quote_assets_separate() -> None:
    """Chi phí lỗi theo tag và hiệu suất tag setup phải tính đúng và tách riêng quote_asset."""
    t1, t2, t3 = str(uuid4()), str(uuid4()), str(uuid4())
    trades = [
        {"trade_id": t1, "net_pnl": "-60.00", "fees": "4.00", "quote_asset": "USDT"},
        {"trade_id": t2, "net_pnl": "-40.00", "fees": "2.00", "quote_asset": "USDT"},
        {"trade_id": t3, "net_pnl": "150.00", "fees": "5.00", "quote_asset": "USDC"},
    ]
    entries = {
        t1: {
            "setup_tags": ["breakout"],
            "mistake_tags": ["fomo_entry"],
            "checklist_items": [{"checked": True}, {"checked": False}],
        },
        t2: {
            "setup_tags": ["breakout"],
            "mistake_tags": ["moved_sl"],
            "checklist_items": [{"checked": True}, {"checked": True}],
        },
        t3: {
            "setup_tags": ["breakout"],
            "mistake_tags": [],
            "checklist_items": [{"checked": True}],
        },
    }
    mistakes = compute_mistake_attribution(trades, entries)
    assert len(mistakes) == 2
    assert mistakes[0]["tag"] == "fomo_entry"
    assert mistakes[0]["loss_cost"] == "60.00"
    assert mistakes[0]["share_of_total_loss_pct"] == "60.00"
    assert mistakes[1]["tag"] == "moved_sl"
    assert mistakes[1]["loss_cost"] == "40.00"
    assert mistakes[1]["share_of_total_loss_pct"] == "40.00"

    setups = compute_setup_tag_breakdown(trades, entries)
    assert len(setups) == 2
    by_quote = {row["quote_asset"]: row for row in setups}
    assert by_quote["USDT"]["trade_count"] == 2
    assert by_quote["USDT"]["net_pnl"] == "-100.00"
    assert by_quote["USDT"]["checklist_completion_pct"] == "75.00"
    assert by_quote["USDC"]["trade_count"] == 1
    assert by_quote["USDC"]["net_pnl"] == "150.00"
    assert by_quote["USDC"]["win_rate"] == "1.0000"


@pytest.mark.asyncio
async def test_journal_service_rbac_audit_retention_and_http_api() -> None:
    """Kiểm tra phân quyền TRADE_READ vs TRADE_REVIEW, audit log, retention và HTTP API."""
    ws_id = uuid4()
    bot_id = uuid4()
    trade_id = uuid4()
    reviewer = _principal(Role.ANALYST_REVIEWER, ws_id=ws_id)
    operator = _principal(Role.OPERATOR, ws_id=ws_id)
    owner = _principal(Role.OWNER, ws_id=ws_id)

    store: dict[str, dict[str, Any]] = {}
    audits: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []

    class _FakeRepo:
        async def get_entry(self, *, workspace_id: UUID, trade_id: UUID) -> dict[str, Any] | None:
            row = store.get(f"{workspace_id}:{trade_id}")
            if row and row.get("deleted_at") is None:
                return dict(row)
            return None

        async def list_entries(self, *, workspace_id: UUID, **_kwargs: Any) -> list[dict[str, Any]]:
            return [
                dict(row)
                for row in store.values()
                if row["workspace_id"] == str(workspace_id) and row.get("deleted_at") is None
            ]

        async def list_attachments(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return list(attachments)

        async def list_audit_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return list(reversed(audits))

        async def upsert_entry(self, **kwargs: Any) -> tuple[dict[str, Any], str]:
            key = f"{kwargs['workspace_id']}:{kwargs['trade_id']}"
            action = "UPDATED" if key in store else "CREATED"
            entry_id = store[key]["id"] if key in store else str(uuid4())
            expires_at = (kwargs["now"] + timedelta(days=kwargs["retention_days"])).isoformat()
            row = {
                "id": entry_id,
                "workspace_id": str(kwargs["workspace_id"]),
                "bot_id": str(kwargs["bot_id"]),
                "trade_id": str(kwargs["trade_id"]),
                "author_user_id": str(kwargs["actor_user_id"]),
                "note_vi": kwargs["note_vi"],
                "checklist_items": kwargs["checklist_items"],
                "setup_tags": kwargs["setup_tags"],
                "mistake_tags": kwargs["mistake_tags"],
                "privacy_mode": kwargs["privacy_mode"],
                "retention_days": kwargs["retention_days"],
                "expires_at": expires_at,
                "created_at": kwargs["now"].isoformat(),
                "updated_at": kwargs["now"].isoformat(),
                "deleted_at": None,
            }
            store[key] = row
            audits.append({"action": action, "created_at": kwargs["now"].isoformat()})
            return dict(row), action

        async def add_attachment(self, **kwargs: Any) -> dict[str, Any]:
            att = {"id": str(uuid4()), **kwargs["attachment_meta"]}
            attachments.append(att)
            audits.append({"action": "ATTACHMENT_ADDED", "created_at": kwargs["now"].isoformat()})
            return att

        async def soft_delete_entry(
            self, *, workspace_id: UUID, trade_id: UUID, now: datetime, **_kwargs: Any
        ) -> bool:
            key = f"{workspace_id}:{trade_id}"
            if key not in store:
                return False
            store[key]["deleted_at"] = now.isoformat()
            audits.append({"action": "SOFT_DELETED", "created_at": now.isoformat()})
            return True

        async def purge_expired(self, **_kwargs: Any) -> int:
            return 1

    class _FakeGrants:
        async def require(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _FakeUow:
        session = object()

        async def __aenter__(self) -> _FakeUow:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    service = TradeJournalService(
        sessions=object(),  # type: ignore[arg-type]
        unit_of_work=lambda *_args, **_kwargs: _FakeUow(),
        repository=lambda _s: _FakeRepo(),
        grants=lambda _s: _FakeGrants(),
    )

    with pytest.raises(TradeJournalError) as exc_op:
        await service.save_trade_journal(operator, bot_id=bot_id, trade_id=trade_id, note_vi="test")
    assert exc_op.value.status_code == 403

    saved = await service.save_trade_journal(
        reviewer,
        bot_id=bot_id,
        trade_id=trade_id,
        note_vi="Vào hơi sớm",
        setup_tags=["breakout_20"],
        mistake_tags=["fomo_entry"],
        privacy_mode="author_only",
        retention_days=90,
    )
    assert saved["last_audit_action"] == "CREATED"
    assert await service.get_trade_journal(operator, trade_id=trade_id, bot_id=bot_id) is None
    assert await service.get_trade_journal(owner, trade_id=trade_id, bot_id=bot_id) is not None

    class _FakeAuthService:
        async def verify_csrf(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    from trading_bot.config.settings import AppSettings

    app = FastAPI()
    app.state.settings = AppSettings()
    app.state.auth_service = _FakeAuthService()
    app.state.trade_journal_service = service
    app.include_router(trade_journal_router)
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=reviewer,
        email="reviewer@crypidian.io",
    )
    client = TestClient(app)
    resp = client.put(
        f"/api/v1/trade-journal/{trade_id}",
        headers={"X-CSRF-Token": "test-csrf"},
        json={
            "bot_id": str(bot_id),
            "note_vi": "Cập nhật qua API",
            "setup_tags": ["breakout_20"],
            "mistake_tags": ["fomo_entry"],
            "privacy_mode": "workspace_team",
            "retention_days": 180,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["entry"]["last_audit_action"] == "UPDATED"

    att_resp = client.post(
        f"/api/v1/trade-journal/{trade_id}/attachments",
        headers={"X-CSRF-Token": "test-csrf"},
        json={
            "bot_id": str(bot_id),
            "file_name": "entry.png",
            "content_type": "image/png",
            "byte_size": 2048,
            "sha256": "b" * 64,
            "storage_ref": "journal/entry.png",
            "exif_stripped": True,
        },
    )
    assert att_resp.status_code == 201
    assert audits[-1]["action"] == "ATTACHMENT_ADDED"
