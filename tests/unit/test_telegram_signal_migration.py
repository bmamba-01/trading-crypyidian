"""Kiểm hợp đồng migration/quyền của Telegram signal worker mà không cần PostgreSQL."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "a1c3e5f7b9d2_wp07_telegram_signal_delivery.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp07_signal_migration", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    module = _load()
    recorded: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: recorded.append(str(statement)))
    monkeypatch.setattr(module.op, "f", lambda name: name)
    monkeypatch.setattr(module.op, "create_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.op, "create_index", lambda *_args, **_kwargs: None)
    module.upgrade()
    return [re.sub(r"\s+", " ", statement).strip() for statement in recorded]


def test_revision_follows_latest_wp05_wp06_rights_worker_migration() -> None:
    module = _load()

    assert module.revision == "a1c3e5f7b9d2"
    assert module.down_revision == "e4b7d9f1a3c5"


def test_signal_worker_is_read_only_except_delivery_state_and_has_no_order_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _upgrade_statements(monkeypatch)

    assert any("CREATE ROLE crypidian_signal_worker NOLOGIN" in item for item in statements)
    assert (
        "GRANT SELECT (id, workspace_id, bot_id, status, reason_code, decision_hash, payload, "
        "created_at) "
        "ON TABLE public.bot_decision TO crypidian_signal_worker"
    ) in statements
    assert (
        "GRANT SELECT (id, workspace_id, membership_id, verified_at, revoked_at) "
        "ON TABLE public.telegram_binding TO crypidian_signal_worker"
    ) in statements
    assert (
        "GRANT SELECT (id, workspace_id, status) ON TABLE public.user_membership "
        "TO crypidian_signal_worker"
    ) in statements
    assert (
        "GRANT UPDATE (status, attempts, available_at, lease_owner, lease_expires_at, sent_at, "
        "last_error_code) "
        "ON TABLE public.telegram_signal_delivery TO crypidian_signal_worker"
    ) in statements
    assert any("status = 'CANDIDATE'" in item for item in statements)
    assert any(
        "telegram_signal_binding_worker_select" in item and "revoked_at IS NULL" in item
        for item in statements
    )
    assert any(
        "membership_worker_select" in item and "status = 'ACTIVE'" in item for item in statements
    )
    assert any("WP07_SIGNAL_WORKER_HAS_ORDER_PRIVILEGE" in item for item in statements)
    assert not any(
        item.startswith("GRANT")
        and "crypidian_signal_worker" in item
        and any(table in item for table in ("order_intent", "fill", "trade", "risk_reservation"))
        for item in statements
    )
