"""MB07a.4 (ruling 40): timeout phiên PostgreSQL cho admin-web vì đường disable/control giữ L1."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from trading_bot import app as app_module
from trading_bot.adapters.database.engine import admin_web_session_timeouts
from trading_bot.config.settings import AppSettings


def test_admin_web_timeouts_bound_lock_wait_and_idle_transactions() -> None:
    timeouts = admin_web_session_timeouts()

    assert timeouts.lock_timeout_ms == 5_000
    assert timeouts.idle_in_transaction_timeout_ms == 15_000
    assert timeouts.statement_timeout_ms == 30_000
    # PostgreSQL: lock_timeout >= statement_timeout là vô nghĩa.
    assert timeouts.lock_timeout_ms < timeouts.statement_timeout_ms
    options = timeouts.as_options()
    assert "-c lock_timeout=5000" in options
    assert "-c idle_in_transaction_session_timeout=15000" in options


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_engine(url: Any, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "engine"

    monkeypatch.setattr(app_module, "create_database_engine", fake_engine)
    return calls


def test_admin_web_engine_applies_session_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = tmp_path / "password"
    password.write_text("pw", encoding="utf-8")
    calls = _capture(monkeypatch)

    app_module.build_database_engine(
        AppSettings(database_user="crypidian_admin_web", database_password_file=password)
    )

    assert calls == [{"session_timeouts": admin_web_session_timeouts()}]


def test_runtime_engine_keeps_lease_derived_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = tmp_path / "password"
    password.write_text("pw", encoding="utf-8")
    calls = _capture(monkeypatch)

    app_module.build_database_engine(
        AppSettings(
            process_role="trading-runtime",
            runtime_workspace_id=uuid4(),
            database_user="crypidian_trading_runtime",
            database_password_file=password,
        )
    )

    assert calls[0]["session_timeouts"] != admin_web_session_timeouts()
