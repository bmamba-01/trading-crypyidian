from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from trading_bot.cli import learning_shadow
from trading_bot.config.settings import AppSettings

WORKSPACE_ID = UUID("77777777-7777-4777-8777-777777777777")
OBSERVER_SECRET = Path("/run/secrets/database_learning_shadow_observer_password")


def _settings() -> AppSettings:
    return AppSettings(
        process_role="learning-shadow-observer",
        trading_mode="PAPER",
        database_user="crypidian_learning_shadow_observer",
        database_password_file=OBSERVER_SECRET,
        learning_shadow_observer_workspace_id=WORKSPACE_ID,
    )


def test_cli_parser_exposes_once_and_supervised_commands_without_workspace_override() -> None:
    once = learning_shadow._parser().parse_args(["once"])
    run = learning_shadow._parser().parse_args(["run"])

    assert once.command == "once"
    assert run.command == "run"
    with pytest.raises(SystemExit):
        learning_shadow._parser().parse_args(["once", "--workspace-id", str(WORKSPACE_ID)])


def test_pool_uses_fixed_observer_secret_and_rejects_wrong_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    engine = object()

    def database_url_from_secret(**kwargs: object) -> str:
        calls["url"] = kwargs
        return "postgresql+psycopg://observer@postgres/crypidian"

    monkeypatch.setattr(learning_shadow, "database_url_from_secret", database_url_from_secret)
    monkeypatch.setattr(learning_shadow, "create_database_engine", lambda url: engine)
    monkeypatch.setattr(
        learning_shadow, "create_session_factory", lambda value: ("sessions", value)
    )

    result_engine, sessions = learning_shadow.create_observer_pool(_settings())

    assert result_engine is engine
    assert sessions == ("sessions", engine)
    assert calls["url"] == {
        "username": "crypidian_learning_shadow_observer",
        "password_file": OBSERVER_SECRET,
        "host": "postgres",
        "database": "crypidian",
    }

    with pytest.raises(PermissionError, match="process_role=learning-shadow-observer"):
        learning_shadow.create_observer_pool(
            SimpleNamespace(process_role="admin-web", trading_mode="PAPER")  # type: ignore[arg-type]
        )
