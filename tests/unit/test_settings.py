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


def test_ingestion_worker_requires_deployment_workspace_uuid() -> None:
    with pytest.raises(ValidationError, match="ingestion_workspace_id"):
        AppSettings(process_role="ingestion-worker")


def test_ingestion_worker_reads_workspace_identity_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = UUID("22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("TRADING_BOT_INGESTION_WORKSPACE_ID", str(workspace_id))

    settings = AppSettings(process_role="ingestion-worker")

    assert settings.ingestion_workspace_id == workspace_id
    assert settings.ingestion_worker_tick_seconds == 5.0


def test_other_process_roles_reject_ingestion_workspace_identity() -> None:
    with pytest.raises(ValidationError, match="ingestion-worker"):
        AppSettings(
            process_role="admin-web",
            ingestion_workspace_id=UUID("22222222-2222-2222-2222-222222222222"),
        )


@pytest.mark.parametrize("tick_seconds", [0, -0.1, 300.1])
def test_ingestion_worker_rejects_out_of_bounds_tick_interval(tick_seconds: float) -> None:
    with pytest.raises(ValidationError, match="ingestion_worker_tick_seconds"):
        AppSettings(
            process_role="ingestion-worker",
            ingestion_workspace_id=UUID("22222222-2222-2222-2222-222222222222"),
            ingestion_worker_tick_seconds=tick_seconds,
        )


def test_market_signal_worker_requires_its_own_deployment_workspace_uuid() -> None:
    with pytest.raises(ValidationError, match="market_signal_workspace_id"):
        AppSettings(process_role="market-signal-worker")


def test_market_signal_worker_rejects_all_zero_placeholder_workspace() -> None:
    with pytest.raises(ValidationError, match="placeholder toàn-0"):
        AppSettings(
            process_role="market-signal-worker",
            market_signal_workspace_id=UUID(int=0),
        )


def test_market_signal_worker_reads_workspace_identity_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = UUID("33333333-3333-4333-8333-333333333333")
    monkeypatch.setenv("TRADING_BOT_MARKET_SIGNAL_WORKSPACE_ID", str(workspace_id))

    settings = AppSettings(process_role="market-signal-worker")

    assert settings.market_signal_workspace_id == workspace_id
    assert settings.telegram_bot_token_file is None


def test_market_signal_workspace_identity_is_rejected_by_other_roles() -> None:
    with pytest.raises(ValidationError, match="market-signal-worker"):
        AppSettings(
            process_role="signal-worker",
            signal_workspace_id=UUID("22222222-2222-4222-8222-222222222222"),
            telegram_bot_token_file=Path("/run/secrets/telegram_bot_token"),
            market_signal_workspace_id=UUID("33333333-3333-4333-8333-333333333333"),
        )


def test_signal_candidate_age_covers_closed_one_hour_candles() -> None:
    settings = AppSettings(
        process_role="signal-worker",
        signal_workspace_id=UUID("22222222-2222-4222-8222-222222222222"),
        telegram_bot_token_file=Path("/run/secrets/telegram_bot_token"),
    )

    assert settings.signal_worker_max_candidate_age_seconds >= 3600


def test_settings_load_mode_from_local_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRADING_BOT_TRADING_MODE", raising=False)
    (tmp_path / ".env").write_text("TRADING_BOT_TRADING_MODE=DEMO\n", encoding="utf-8")

    assert AppSettings().trading_mode == "DEMO"


def test_backtest_worker_uses_dedicated_replay_writer_credentials() -> None:
    workspace_id = UUID("44444444-4444-4444-8444-444444444444")
    settings = AppSettings(
        process_role="backtest-worker",
        backtest_worker_workspace_id=workspace_id,
        database_user="crypidian_backtest_worker",
        database_password_file=Path("/run/secrets/database_backtest_worker_password"),
        backtest_replay_database_user="crypidian_replay_writer",
        backtest_replay_database_password_file=Path("/run/secrets/database_replay_writer_password"),
    )

    assert settings.backtest_replay_database_user == "crypidian_replay_writer"
    assert settings.backtest_replay_database_password_file == Path(
        "/run/secrets/database_replay_writer_password"
    )


def test_backtest_worker_rejects_trainer_as_replay_writer() -> None:
    with pytest.raises(ValidationError, match="crypidian_replay_writer"):
        AppSettings(
            process_role="backtest-worker",
            backtest_worker_workspace_id=UUID("44444444-4444-4444-8444-444444444444"),
            database_user="crypidian_backtest_worker",
            database_password_file=Path("/run/secrets/database_backtest_worker_password"),
            backtest_replay_database_user="crypidian_trainer_worker",
            backtest_replay_database_password_file=Path(
                "/run/secrets/database_trainer_worker_password"
            ),
        )


def test_trainer_worker_requires_its_dedicated_paper_database_role() -> None:
    workspace_id = UUID("55555555-5555-4555-8555-555555555555")
    settings = AppSettings(
        process_role="trainer-worker",
        trading_mode="PAPER",
        database_user="crypidian_trainer_worker",
        database_password_file=Path("/run/secrets/database_trainer_worker_password"),
        learning_training_workspace_id=workspace_id,
    )

    assert settings.database_user == "crypidian_trainer_worker"
    assert settings.learning_training_workspace_id == workspace_id


def test_trainer_worker_rejects_admin_database_role_and_demo_mode() -> None:
    with pytest.raises(ValidationError, match="login database trainer riêng"):
        AppSettings(
            process_role="trainer-worker",
            database_user="crypidian_admin_web",
            database_password_file=Path("/run/secrets/admin_password"),
            learning_training_workspace_id=UUID("55555555-5555-4555-8555-555555555555"),
        )
    with pytest.raises(ValidationError, match="PAPER"):
        AppSettings(
            process_role="trainer-worker",
            trading_mode="DEMO",
            database_user="crypidian_trainer_worker",
            database_password_file=Path("/run/secrets/database_trainer_worker_password"),
            learning_training_workspace_id=UUID("55555555-5555-4555-8555-555555555555"),
        )


def test_trainer_worker_requires_a_workspace_and_rejects_it_for_other_roles() -> None:
    with pytest.raises(ValidationError, match="learning_training_workspace_id"):
        AppSettings(
            process_role="trainer-worker",
            database_user="crypidian_trainer_worker",
            database_password_file=Path("/run/secrets/database_trainer_worker_password"),
        )

    with pytest.raises(ValidationError, match="chỉ dành cho process_role trainer-worker"):
        AppSettings(
            process_role="admin-web",
            learning_training_workspace_id=UUID("55555555-5555-4555-8555-555555555555"),
        )


def test_learning_shadow_observer_requires_its_dedicated_paper_role() -> None:
    workspace_id = UUID("66666666-6666-4666-8666-666666666666")
    settings = AppSettings(
        process_role="learning-shadow-observer",
        trading_mode="PAPER",
        database_user="crypidian_learning_shadow_observer",
        database_password_file=Path("/run/secrets/database_learning_shadow_observer_password"),
        learning_shadow_observer_workspace_id=workspace_id,
    )

    assert settings.learning_shadow_observer_workspace_id == workspace_id
    assert settings.learning_shadow_observer_poll_seconds == 5.0


def test_learning_shadow_observer_rejects_missing_workspace_or_wrong_database_identity() -> None:
    with pytest.raises(ValidationError, match="learning_shadow_observer_workspace_id"):
        AppSettings(
            process_role="learning-shadow-observer",
            database_user="crypidian_learning_shadow_observer",
            database_password_file=Path("/run/secrets/database_learning_shadow_observer_password"),
        )

    with pytest.raises(ValidationError, match="login database observer riêng"):
        AppSettings(
            process_role="learning-shadow-observer",
            database_user="crypidian_admin_web",
            database_password_file=Path("/run/secrets/database_learning_shadow_observer_password"),
            learning_shadow_observer_workspace_id=UUID("66666666-6666-4666-8666-666666666666"),
        )

    with pytest.raises(ValidationError, match="PAPER"):
        AppSettings(
            process_role="learning-shadow-observer",
            trading_mode="DEMO",
            database_user="crypidian_learning_shadow_observer",
            database_password_file=Path("/run/secrets/database_learning_shadow_observer_password"),
            learning_shadow_observer_workspace_id=UUID("66666666-6666-4666-8666-666666666666"),
        )


def test_learning_shadow_observer_workspace_is_confined_to_its_process_role() -> None:
    with pytest.raises(ValidationError, match="chỉ dành cho process_role learning-shadow-observer"):
        AppSettings(
            process_role="admin-web",
            learning_shadow_observer_workspace_id=UUID("66666666-6666-4666-8666-666666666666"),
        )
