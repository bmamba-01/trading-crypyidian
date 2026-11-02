from pathlib import Path

import pytest

from trading_bot.adapters.database import engine as engine_module
from trading_bot.adapters.database.engine import database_url_from_secret


def test_database_url_reads_password_from_secret_file(tmp_path: Path) -> None:
    password_file = tmp_path / "database_password"
    password_file.write_text("local/secret:p@ss", encoding="utf-8")

    url = database_url_from_secret(
        username="crypidian_admin_web",
        password_file=password_file,
        host="postgres",
        database="crypidian",
    )

    assert url.password == "local/secret:p@ss"
    assert url.username == "crypidian_admin_web"
    assert url.host == "postgres"
    assert url.database == "crypidian"
    assert "local/secret:p@ss" not in str(url)


def test_database_url_rejects_empty_secret(tmp_path: Path) -> None:
    password_file = tmp_path / "database_password"
    password_file.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="rỗng"):
        database_url_from_secret(
            username="crypidian_admin_web",
            password_file=password_file,
            host="postgres",
            database="crypidian",
        )


@pytest.mark.parametrize("with_timeouts", [False, True])
def test_every_database_engine_pins_utc_and_keeps_timeout_options(monkeypatch, with_timeouts):
    captured = {}

    def create(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(engine_module, "create_async_engine", create)
    timeouts = engine_module.admin_web_session_timeouts() if with_timeouts else None
    engine_module.create_database_engine("postgresql+psycopg://fixture", session_timeouts=timeouts)
    options = captured.get("connect_args", {}).get("options", "")
    assert "-c timezone=UTC" in options
    if timeouts:
        assert timeouts.as_options() in options
