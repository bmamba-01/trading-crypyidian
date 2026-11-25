"""Unit test CRY-428: trang Khẩu vị rủi ro, catalog SL/TP theo release, ngưỡng drawdown."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.testclient import TestClient

from trading_bot.admin.risk_appetite_service import (
    RiskAppetiteFormError,
    overlay_conflicts,
    parse_risk_appetite_form,
)
from trading_bot.admin.settings_service import SettingsAdminError
from trading_bot.application.exit_policy_catalog import (
    build_exit_policy_catalog,
    percent_example,
)
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.auth.service import AuthIdentity, Principal
from trading_bot.config.bot_settings import BotConfigOverlay
from trading_bot.config.workspace_settings import RiskSettings
from trading_bot.domain.risk.drawdown_breaker import (
    PAPER_DRAWDOWN_BREAKER_POLICY,
    ClosedTradePnl,
    drawdown_policy_for,
    evaluate_drawdown_breaker,
)

T0 = datetime(2026, 11, 17, tzinfo=UTC)

VALID_FORM = {
    "per_trade_risk_fraction": "0,005",
    "account_max_open_risk_fraction": "0.03",
    "workspace_max_open_risk_fraction": "0.05",
    "max_concentration_fraction": "0.5",
    "max_positions": "4",
    "max_leverage": "5",
    "max_notional": "20000",
    "max_symbol_notional": "10000",
    "max_aggregate_open_risk": "500",
    "max_daily_loss": "300",
    "max_rolling_loss": "600",
    "max_drawdown_fraction": "0.08",
    "reason": "Siết rủi ro sau tuần lỗ",
}


@pytest.mark.asyncio
async def test_risk_appetite_breaker_reads_ledger_with_report_reader_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trading_bot.admin.risk_appetite_service as risk_module

    class SessionContext:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.nested_calls = 0

        def begin_nested(self) -> SessionContext:
            self.nested_calls += 1
            return SessionContext()

    class EquityRepository:
        def __init__(self, session: object) -> None:
            observed_sessions.append(session)

        async def load_paper_equity_snapshot(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                known=True,
                seed_capital=Decimal("1000"),
                equity_amount=Decimal("987.5"),
            )

    admin_session = Session()
    reader_session = Session()
    observed_sessions: list[object] = []
    observed_ledger_sessions: list[object] = []

    class Provider:
        async def _loss_windows(self, session: object, *_args: object, **_kwargs: object):
            observed_ledger_sessions.append(session)
            return SimpleNamespace(
                realized_loss_today=Decimal("12.5"),
                realized_loss_rolling=Decimal("12.5"),
            )

        async def _drawdown_breaker(self, session: object, *_args: object, **_kwargs: object):
            observed_ledger_sessions.append(session)
            return SimpleNamespace(
                drawdown_fraction=Decimal("0.0125"),
                peak_equity=Decimal("1000"),
                current_equity=Decimal("987.5"),
                tripped=False,
            )

    monkeypatch.setattr(risk_module, "AccountEquityRepository", EquityRepository)
    monkeypatch.setattr(risk_module, "mark_policy_from", lambda _settings: object())

    row: dict[str, Any] = {}
    bot = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    account = SimpleNamespace(id=uuid4())
    settings = SimpleNamespace(
        risk=SimpleNamespace(max_equity_age_seconds=30, max_mark_age_seconds=30)
    )
    await risk_module.RiskAppetiteService._breaker_status(
        reader_session,
        Provider(),
        row,
        bot,
        account,
        settings,
        object(),
        datetime.now(UTC),
    )

    assert admin_session.nested_calls == 0
    assert reader_session.nested_calls == 1
    assert observed_ledger_sessions == [reader_session, reader_session]
    assert observed_sessions == [reader_session]
    assert row["loss_today"] == "12.5"
    assert row["loss_rolling"] == "12.5"
    assert row["drawdown"]["fraction"] == "0.0125"


# --- Ngưỡng drawdown cấu hình ---------------------------------------------------------------


def test_risk_settings_drawdown_absent_keeps_canonical_json_unchanged() -> None:
    profile = uuid4()
    legacy = RiskSettings(profile_version_id=profile)
    assert "max_drawdown_fraction" not in legacy.model_dump(mode="json")
    configured = RiskSettings(profile_version_id=profile, max_drawdown_fraction="0.05")
    dumped = configured.model_dump(mode="json")
    assert dumped["max_drawdown_fraction"] == "0.05"
    assert RiskSettings.model_validate(json.loads(json.dumps(dumped))) == configured


@pytest.mark.parametrize("value", ["0", "1", "1.5", "-0.1", 0.1])
def test_risk_settings_rejects_out_of_range_or_float_drawdown(value: object) -> None:
    with pytest.raises(ValueError, match="max_drawdown_fraction|Decimal"):
        RiskSettings(profile_version_id=uuid4(), max_drawdown_fraction=value)


def test_drawdown_policy_uses_configured_threshold_and_records_it() -> None:
    assert drawdown_policy_for(None) is PAPER_DRAWDOWN_BREAKER_POLICY
    trades = [ClosedTradePnl(T0 + timedelta(hours=1), Decimal("-600"))]
    # Vốn 10.000 → 9.400: drawdown 6%: không ngắt ở mặc định 10%, ngắt ở ngưỡng 5%.
    default = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=trades,
        current_equity=Decimal("9400"),
        baseline_at=None,
    )
    tight = evaluate_drawdown_breaker(
        seed_equity=Decimal("10000"),
        closed_trades=trades,
        current_equity=Decimal("9400"),
        baseline_at=None,
        policy=drawdown_policy_for(Decimal("0.05")),
    )
    assert default.tripped is False
    assert tight.tripped is True
    assert tight.evidence()["drawdown_max_fraction"] == "0.05"
    assert default.evidence()["drawdown_max_fraction"] == "0.10"
    assert tight.policy_version == default.policy_version == "fx2-drawdown-v1"


# --- Catalog SL/TP ---------------------------------------------------------------------------


def _by_release() -> dict[str, Any]:
    return {row.release_id: row for row in build_exit_policy_catalog()}


def test_catalog_covers_every_product_release() -> None:
    rows = _by_release()
    assert set(rows) == {release.release_id for release in strategy_registry().list_releases()}


def test_catalog_reads_recipe_values_from_release_code() -> None:
    rows = _by_release()
    h01 = rows["h01-v1"]
    assert (h01.stop_atr_multiple, h01.trailing_atr_multiple) == (Decimal("2.0"), Decimal("3.0"))
    assert h01.take_profit_atr_multiple is None
    assert h01.max_hold_bars == 120
    assert h01.max_hold_label_vi == "120 nến 1h (≈ 5 ngày)"
    assert any("ngược chiều" in text for text in h01.other_exits_vi)

    h03 = rows["h03-v3-4h"]
    assert h03.timeframe == "4h"
    assert any("kênh 20 nến" in text for text in h03.other_exits_vi)

    h06 = rows["h06-v2"]
    assert (h06.stop_atr_multiple, h06.take_profit_atr_multiple) == (
        Decimal("1.5"),
        Decimal("2.0"),
    )
    assert h06.max_hold_label_vi == "16 nến 15m (≈ 4 giờ)"
    assert h06.take_profit_label_vi == "Tối đa 2 × ATR"


def test_catalog_scalping_ladder_and_structural_stops() -> None:
    rows = _by_release()
    h19 = rows["h19-v1"]
    assert h19.stop_atr_multiple == Decimal("1.0")
    assert [(step.r_multiple, step.fraction) for step in h19.ladder] == [
        (Decimal("1"), Decimal("0.5")),
        (Decimal("2"), Decimal("0.5")),
    ]
    assert h19.breakeven_after_first is True
    assert "hòa vốn" in h19.take_profit_label_vi
    assert h19.max_hold_label_vi == "12 nến 5m (≈ 1 giờ)"
    assert rows["h18-v1"].stop_atr_multiple is None
    assert "cấu trúc" in rows["h18-v1"].stop_label_vi
    assert "swing" in rows["h20-v1"].stop_label_vi
    card = rows["sc-binance-usdm-vol-breakout-001-v1.0.0"]
    assert card.source == "strategy_card"
    assert card.stop_atr_multiple is None


def test_percent_example_converts_atr_and_r_multiples() -> None:
    rows = _by_release()
    atr, price = Decimal("100"), Decimal("50000")  # ATR = 0,2% giá
    h01 = percent_example(rows["h01-v1"], atr=atr, price=price)
    assert (h01.stop_pct, h01.trailing_pct, h01.take_profit_pct) == (
        Decimal("0.40"),
        Decimal("0.60"),
        None,
    )
    h19 = percent_example(rows["h19-v1"], atr=atr, price=price)
    assert h19.stop_pct == Decimal("0.20")
    assert h19.ladder_pcts == (Decimal("0.20"), Decimal("0.40"))
    h18 = percent_example(rows["h18-v1"], atr=atr, price=price)
    assert h18.stop_pct is None
    assert h18.ladder_pcts == ()
    with pytest.raises(ValueError, match="ATR"):
        percent_example(rows["h01-v1"], atr=Decimal("0"), price=price)


# --- Form và kiểm overlay --------------------------------------------------------------------


def test_parse_form_accepts_decimal_comma_and_returns_exact_decimals() -> None:
    update = parse_risk_appetite_form(dict(VALID_FORM))
    assert update.per_trade_risk_fraction == Decimal("0.005")
    assert update.max_positions == 4
    assert update.caps["max_leverage"] == Decimal("5")
    assert update.max_drawdown_fraction == Decimal("0.08")
    assert update.reason == "Siết rủi ro sau tuần lỗ"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("per_trade_risk_fraction", ""),
        ("per_trade_risk_fraction", "1.2"),
        ("max_drawdown_fraction", "1"),
        ("max_positions", "2.5"),
        ("max_positions", "0"),
        ("max_leverage", "abc"),
        ("max_daily_loss", "-5"),
        ("max_concentration_fraction", "2"),
        ("reason", "  "),
    ],
)
def test_parse_form_rejects_invalid_field(field: str, value: str) -> None:
    values = dict(VALID_FORM) | {field: value}
    with pytest.raises(RiskAppetiteFormError) as error:
        parse_risk_appetite_form(values)
    assert error.value.field == field


def test_overlay_conflicts_lists_fields_looser_than_new_workspace() -> None:
    overlay = BotConfigOverlay.model_validate(
        {
            "schema_version": 1,
            "risk": {"max_leverage": "5", "per_trade_risk_fraction": "0.002", "max_positions": 3},
        }
    )
    limits = {
        "max_leverage": Decimal("3"),
        "per_trade_risk_fraction": Decimal("0.005"),
        "max_positions": 2,
    }
    assert overlay_conflicts(overlay, limits) == ["max_leverage", "max_positions"]
    assert overlay_conflicts(BotConfigOverlay.model_validate({"schema_version": 1}), limits) == []


# --- Trang HTML ------------------------------------------------------------------------------


def _identity(*roles: str) -> AuthIdentity:
    return AuthIdentity(
        principal=Principal(
            workspace_id=uuid4(),
            user_id=uuid4(),
            membership_id=uuid4(),
            roles=frozenset(roles),
            mfa_verified_at=datetime.now(UTC),
        ),
        email="owner@crypidian.io",
    )


def _overview() -> dict[str, Any]:
    from trading_bot.admin.risk_appetite_service import RiskAppetiteService

    bot_id = str(uuid4())
    return {
        "configured": True,
        "settings": {
            "id": str(uuid4()),
            "version": 7,
            "revision": 1,
            "effective_at": None,
            "workspace_max_open_risk_fraction": "0.05",
            "max_drawdown_fraction": "0.1",
            "max_drawdown_is_default": True,
            "default_release": "h03-v3-1h",
            "symbols": ["BTCUSDT", "ETHUSDT"],
        },
        "profile": {
            "id": str(uuid4()),
            "name": "paper-default",
            "version": 3,
            "limits": {
                "per_trade_risk_fraction": "0.01",
                "account_max_open_risk_fraction": "0.03",
                "max_positions": "6",
                "max_leverage": "5",
                "max_notional": "50000",
                "max_symbol_notional": "20000",
                "max_aggregate_open_risk": "1000",
                "max_concentration_fraction": "0.5",
                "max_daily_loss": "500",
                "max_rolling_loss": "1000",
            },
        },
        "profile_history": [
            {"name": "paper-default", "version": 3, "created_at": T0.isoformat(), "active": True}
        ],
        "bots": [
            {
                "id": bot_id,
                "name": "T01 BTC",
                "status": "ENABLED",
                "release_id": "h03-v3-1h",
                "limits": {
                    "per_trade_risk_fraction": "0.002",
                    "max_leverage": "2",
                    "max_positions": "1",
                    "max_notional": "50000",
                    "max_daily_loss": "500",
                    "max_rolling_loss": "1000",
                },
                "overridden": ["per_trade_risk_fraction", "max_leverage", "max_positions"],
                "error": None,
                "loss_today": "12.5",
                "loss_rolling": "12.5",
                "drawdown": {"fraction": "0.031", "peak": "1", "current": "1", "tripped": False},
            }
        ],
        "symbol": "BTCUSDT",
        "atr": {"1h": {"atr": "100", "price": "50000"}},
        "exit_policies": RiskAppetiteService._exit_rows({"1h": (Decimal("100"), Decimal("50000"))}),
    }


def _client(
    identity: AuthIdentity, service: AsyncMock, auth: AsyncMock | None = None
) -> TestClient:
    from trading_bot.admin.risk_appetite_page_router import router
    from trading_bot.auth.router import current_identity

    auth_service = auth or AsyncMock()
    auth_service.issue_csrf_token.return_value = "csrf-test"
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory="src/trading_bot/templates")
    app.state.risk_appetite_service = service
    app.state.settings = SimpleNamespace(app_env="test")
    app.state.auth_service = auth_service
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(app, follow_redirects=False)


def test_page_renders_workspace_limits_bots_and_exit_table() -> None:
    service = AsyncMock()
    service.overview.return_value = _overview()
    response = _client(_identity("owner"), service).get("/admin/risk?symbol=BTCUSDT")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    assert 'name="per_trade_risk_fraction"' in html
    assert 'value="0.01"' in html
    assert 'name="max_drawdown_fraction"' in html
    assert "Đang dùng mặc định 10%." in html
    assert 'name="csrf_token" value="csrf-test"' in html
    # Bot: rủi ro/lệnh 0,2% được đánh dấu siết bởi overlay, đòn bẩy 2x.
    assert 'class="num num--strong" data-label="Rủi ro mỗi lệnh"' in html
    assert ">0,2%<" in html
    assert ">2x<" in html
    # Bảng thoát: h01 SL 2 ATR → 0,40% với ATR 0,2% giá.
    assert 'data-release-id="h01-v1"' in html
    assert "SL 0,40%" in html
    assert "trailing 0,60%" in html
    assert "không có ví dụ % cố định" in html  # Strategy Card: ghi chú, không quy đổi
    assert "Lưu và publish" in html
    service.overview.assert_awaited_once()
    assert service.overview.await_args.kwargs["symbol"] == "BTCUSDT"


def test_page_is_read_only_without_risk_write_permission() -> None:
    service = AsyncMock()
    service.overview.return_value = _overview()
    html = _client(_identity("analyst_reviewer"), service).get("/admin/risk").text
    assert "Lưu và publish" not in html
    assert " disabled" in html


def test_post_valid_form_saves_and_redirects() -> None:
    service = AsyncMock()
    service.save.return_value = {"status": "published", "version": 8}
    identity = _identity("owner")
    response = _client(identity, service).post(
        "/admin/risk", data=VALID_FORM | {"csrf_token": "csrf-test"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/risk?saved=8"
    principal, update = service.save.await_args.args
    assert principal is identity.principal
    assert update.max_drawdown_fraction == Decimal("0.08")


def test_post_invalid_form_rerenders_with_field_error_without_saving() -> None:
    service = AsyncMock()
    service.overview.return_value = _overview()
    response = _client(_identity("owner"), service).post(
        "/admin/risk", data=VALID_FORM | {"max_leverage": "x", "csrf_token": "csrf-test"}
    )
    assert response.status_code == 422
    assert "Đòn bẩy tối đa: Phải là số thập phân." in response.text
    assert 'value="x"' in response.text  # giữ giá trị người dùng đã nhập
    service.save.assert_not_awaited()


def test_post_without_recent_mfa_shows_reauth_hint() -> None:
    service = AsyncMock()
    service.overview.return_value = _overview()
    service.save.side_effect = SettingsAdminError(403, "Cần xác thực MFA gần đây.")
    response = _client(_identity("owner"), service).post(
        "/admin/risk", data=VALID_FORM | {"csrf_token": "csrf-test"}
    )
    assert response.status_code == 403
    assert "Cần xác thực MFA gần đây." in response.text
    assert "đăng nhập lại" in response.text


def test_post_rejects_bad_csrf_before_parsing() -> None:
    from trading_bot.auth.service import AuthenticationFailure

    service = AsyncMock()
    auth = AsyncMock()
    auth.verify_csrf.side_effect = AuthenticationFailure(403, "Token CSRF không hợp lệ.")
    response = _client(_identity("owner"), service, auth).post(
        "/admin/risk", data=VALID_FORM | {"csrf_token": "sai"}
    )
    assert response.status_code == 403
    service.save.assert_not_awaited()


def test_saved_query_only_accepts_version_or_unchanged() -> None:
    service = AsyncMock()
    service.overview.return_value = _overview()
    client = _client(_identity("owner"), service)
    assert "Đã publish cấu hình workspace v8." in client.get("/admin/risk?saved=8").text
    assert "<script>" not in client.get("/admin/risk?saved=%3Cscript%3E").text


def test_pct_vi_formats_exact_fractions_and_rounds_measurements() -> None:
    from trading_bot.admin.risk_appetite_page_router import pct_vi

    assert pct_vi("0.005") == "0,5"
    assert pct_vi("0.1") == "10"
    assert pct_vi("0.03645189414722563772340080108", 2) == "3,65"
    assert pct_vi("0", 2) == "0"
    assert pct_vi("abc") == "abc"


def test_bot_table_shows_exit_override_or_release_default() -> None:
    overview = _overview()
    overview["bots"][0]["exit_override"] = {
        "stop_atr_multiple": "1.5",
        "ladder": {
            "steps": [
                {"r_multiple": "1", "fraction": "0.5"},
                {"r_multiple": "3", "fraction": "0.5"},
            ],
            "breakeven_after_first": True,
        },
    }
    service = AsyncMock()
    service.overview.return_value = overview
    html = _client(_identity("owner"), service).get("/admin/risk").text
    assert "Ghi đè" in html
    assert "SL ×ATR 1.5" in html
    assert "1R:50%, 3R:50% +hòa vốn" in html
    overview["bots"][0]["exit_override"] = None
    html = _client(_identity("owner"), service).get("/admin/risk").text
    assert "Theo release" in html
