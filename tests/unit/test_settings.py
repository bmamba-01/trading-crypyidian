from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from trading_bot.config.settings import AppSettings
from trading_bot.config.workspace_settings import WorkspaceSettings


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


def test_settings_reject_unknown_process_role() -> None:
    with pytest.raises(ValidationError):
        AppSettings(process_role="unrestricted-worker")


def test_trading_runtime_requires_deployment_workspace_uuid() -> None:
    with pytest.raises(ValidationError, match="runtime_workspace_id"):
        AppSettings(process_role="trading-runtime")


def test_trading_runtime_rejects_non_paper_mode() -> None:
    with pytest.raises(ValidationError, match="PAPER"):
        AppSettings(
            process_role="trading-runtime",
            trading_mode="DEMO",
            runtime_workspace_id=UUID("11111111-1111-1111-1111-111111111111"),
        )


def test_trading_runtime_rejects_invalid_workspace_uuid() -> None:
    with pytest.raises(ValidationError, match="runtime_workspace_id"):
        AppSettings(process_role="trading-runtime", runtime_workspace_id="not-a-uuid")


def test_runtime_workspace_identity_is_not_a_business_setting() -> None:
    workspace_id = UUID("11111111-1111-1111-1111-111111111111")
    settings = AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id)

    assert settings.runtime_workspace_id == workspace_id
    assert "runtime_workspace_id" not in WorkspaceSettings.model_fields


def test_trading_runtime_reads_workspace_identity_from_deployment_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("TRADING_BOT_RUNTIME_WORKSPACE_ID", str(workspace_id))

    settings = AppSettings(process_role="trading-runtime")

    assert settings.runtime_workspace_id == workspace_id


def test_other_process_roles_do_not_require_workspace_identity() -> None:
    assert AppSettings(process_role="admin-web").runtime_workspace_id is None


def test_other_process_roles_reject_runtime_workspace_identity() -> None:
    with pytest.raises(ValidationError, match="trading-runtime"):
        AppSettings(
            process_role="admin-web",
            runtime_workspace_id=UUID("11111111-1111-1111-1111-111111111111"),
        )


def test_settings_load_mode_from_local_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRADING_BOT_TRADING_MODE", raising=False)
    (tmp_path / ".env").write_text("TRADING_BOT_TRADING_MODE=DEMO\n", encoding="utf-8")

    assert AppSettings().trading_mode == "DEMO"
