from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_bot.config.settings import AppSettings


def test_settings_default_to_paper_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_BOT_TRADING_MODE", raising=False)

    settings = AppSettings()

    assert settings.trading_mode == "PAPER"


def test_settings_reject_unknown_trading_mode() -> None:
    with pytest.raises(ValidationError):
        AppSettings(trading_mode="REAL")


def test_settings_reject_live_mode_until_readiness_gate_exists() -> None:
    with pytest.raises(ValidationError):
        AppSettings(trading_mode="LIVE")


def test_settings_load_mode_from_local_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRADING_BOT_TRADING_MODE", raising=False)
    (tmp_path / ".env").write_text("TRADING_BOT_TRADING_MODE=DEMO\n", encoding="utf-8")

    assert AppSettings().trading_mode == "DEMO"
