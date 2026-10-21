from pathlib import Path

import pytest

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
