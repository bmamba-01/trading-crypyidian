from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import trading_bot.app as app_module
from trading_bot.adapters.database.engine import admin_web_session_timeouts
from trading_bot.config.settings import AppSettings

REPORT_READER_USER = "crypidian_admin_report_reader"
REPORT_READER_PASSWORD_FILE = Path("/run/secrets/database_admin_report_reader_password")


def _reader_settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        process_role="admin-web",
        admin_report_reader_database_user=REPORT_READER_USER,
        admin_report_reader_database_password_file=REPORT_READER_PASSWORD_FILE,
    )


def test_build_admin_report_reader_engine_uses_dedicated_validated_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _reader_settings()
    database_url_calls: list[dict[str, object]] = []
    engine_calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        app_module,
        "database_url_from_secret",
        lambda **kwargs: database_url_calls.append(kwargs) or "postgresql+asyncpg://reader",
    )
    monkeypatch.setattr(
        app_module,
        "create_database_engine",
        lambda url, *, session_timeouts=None: (
            engine_calls.append((url, session_timeouts)) or "reader-engine"
        ),
    )

    result = app_module.build_admin_report_reader_database_engine(settings)

    assert result == "reader-engine"
    assert database_url_calls == [
        {
            "username": REPORT_READER_USER,
            "password_file": REPORT_READER_PASSWORD_FILE,
            "host": settings.database_host,
            "database": settings.database_name,
        }
    ]
    assert engine_calls[0][0] == "postgresql+asyncpg://reader"
    assert engine_calls[0][1] == admin_web_session_timeouts()


def test_build_admin_report_reader_engine_is_unavailable_without_admin_reader_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        app_module,
        "database_url_from_secret",
        lambda **kwargs: database_url_calls.append(kwargs) or "unexpected-url",
    )

    assert (
        app_module.build_admin_report_reader_database_engine(
            AppSettings(_env_file=None, process_role="admin-web")
        )
        is None
    )
    assert database_url_calls == []

    wrong_process_settings = AppSettings.model_construct(
        process_role="signal-worker",
        admin_report_reader_database_user=REPORT_READER_USER,
        admin_report_reader_database_password_file=REPORT_READER_PASSWORD_FILE,
    )
    assert app_module.build_admin_report_reader_database_engine(wrong_process_settings) is None
    assert database_url_calls == []


@pytest.mark.asyncio
async def test_admin_web_exposes_separate_report_reader_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_engine = SimpleNamespace(dispose=AsyncMock())
    reader_engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(app_module, "build_database_engine", lambda _settings: admin_engine)
    monkeypatch.setattr(
        app_module,
        "build_admin_report_reader_database_engine",
        lambda _settings: reader_engine,
    )
    monkeypatch.setattr(
        app_module,
        "create_session_factory",
        lambda engine: async_sessionmaker(bind=engine),
    )
    application = app_module.create_app(_reader_settings(), database_probe=lambda: _ready())

    async with application.router.lifespan_context(application):
        admin_factory = application.state.session_factory
        reader_factory = application.state.admin_report_reader_session_factory
        assert admin_factory is not None
        assert reader_factory is not None
        assert admin_factory is not reader_factory
        assert admin_factory.kw["bind"] is admin_engine
        assert reader_factory.kw["bind"] is reader_engine
        assert application.state.bot_admin_service._report_reader_sessions is reader_factory
        assert application.state.risk_appetite_service._report_reader_sessions is reader_factory
        assert application.state.bot_reporting_service._report_reader_sessions is reader_factory
        assert application.state.live_monitor_service._report_reader_sessions is reader_factory
        assert application.state.market_scanner_service._report_reader_sessions is reader_factory
        assert application.state.analyst_review_service._report_reader_sessions is reader_factory
        assert (
            application.state.signal_outcome_review_service._report_reader_sessions
            is reader_factory
        )
        assert application.state.strategy_card_service._bars_provider._sessions is reader_factory
        snapshot_provider = application.state.flatten_all_service._snapshot_provider
        assert snapshot_provider._report_reader_sessions is reader_factory
        assert snapshot_provider._require_report_reader is True

    admin_engine.dispose.assert_awaited_once()
    reader_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_report_reader_credentials_never_fall_back_to_admin_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_engine = SimpleNamespace(dispose=AsyncMock())
    admin_factory = async_sessionmaker(bind=admin_engine)
    reader_builder_calls = 0

    def build_reader(_settings: AppSettings) -> None:
        nonlocal reader_builder_calls
        reader_builder_calls += 1
        return None

    monkeypatch.setattr(app_module, "build_admin_report_reader_database_engine", build_reader)
    application = app_module.create_app(
        AppSettings(_env_file=None, process_role="admin-web"),
        session_factory=admin_factory,
        database_probe=lambda: _ready(),
    )

    async with application.router.lifespan_context(application):
        assert application.state.session_factory is admin_factory
        assert application.state.admin_report_reader_session_factory is None
        assert application.state.admin_report_reader_session_factory is not admin_factory
        assert application.state.bot_admin_service._report_reader_sessions is None
        assert application.state.risk_appetite_service._report_reader_sessions is None
        assert application.state.bot_reporting_service._report_reader_sessions is None
        assert application.state.live_monitor_service._report_reader_sessions is None
        assert application.state.market_scanner_service._report_reader_sessions is None
        assert application.state.analyst_review_service._report_reader_sessions is None
        assert application.state.signal_outcome_review_service._report_reader_sessions is None
        assert application.state.strategy_card_service._bars_provider is None
        snapshot_provider = application.state.flatten_all_service._snapshot_provider
        assert snapshot_provider._report_reader_sessions is None
        assert snapshot_provider._require_report_reader is True
        assert reader_builder_calls == 1


@pytest.mark.asyncio
async def test_startup_factory_failure_disposes_engines_created_so_far(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposal_order: list[str] = []

    async def dispose_admin() -> None:
        disposal_order.append("admin")

    async def dispose_reader() -> None:
        disposal_order.append("report-reader")

    admin_dispose = AsyncMock(side_effect=dispose_admin)
    reader_dispose = AsyncMock(side_effect=dispose_reader)
    admin_engine = SimpleNamespace(dispose=admin_dispose)
    reader_engine = SimpleNamespace(dispose=reader_dispose)
    monkeypatch.setattr(app_module, "build_database_engine", lambda _settings: admin_engine)
    monkeypatch.setattr(
        app_module,
        "build_admin_report_reader_database_engine",
        lambda _settings: reader_engine,
    )
    calls = 0

    def create_factory(engine):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("reader factory initialization failed")
        return async_sessionmaker(bind=engine)

    monkeypatch.setattr(app_module, "create_session_factory", create_factory)
    application = app_module.create_app(_reader_settings(), database_probe=lambda: _ready())

    with pytest.raises(RuntimeError, match="reader factory initialization failed"):
        async with application.router.lifespan_context(application):
            pytest.fail("Startup should fail before lifespan yields.")

    admin_dispose.assert_awaited_once()
    reader_dispose.assert_awaited_once()
    assert disposal_order == ["report-reader", "admin"]


async def _ready() -> bool:
    return True
