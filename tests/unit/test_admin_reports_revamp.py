"""Unit tests cho revamp báo cáo, theme và API AI Retraining Ingestion."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from trading_bot.application.learning_post_mortem import LearningPostMortemService
from trading_bot.auth.service import AuthIdentity, Principal
from trading_bot.domain.portfolio.reporting import (
    BotLedgerSnapshot,
    DecisionLineage,
    IntentLineage,
    LedgerCost,
    LedgerTrade,
    build_bot_report,
)


@pytest.fixture
def mock_identity() -> AuthIdentity:
    workspace_id = uuid4()
    user_id = uuid4()
    principal = Principal(
        workspace_id=workspace_id,
        user_id=user_id,
        membership_id=uuid4(),
        roles=frozenset({"trader"}),
        mfa_verified_at=datetime.now(UTC),
    )
    return AuthIdentity(
        principal=principal,
        email="trader@crypidian.io",
    )


def test_admin_reports_renders_metric_cards_and_post_mortem(
    mock_identity: AuthIdentity,
) -> None:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from trading_bot.admin.presentation import label_vi, local_time, number_vi, pnl_tone
    from trading_bot.admin.web_router import router

    app = FastAPI()
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters["local_time"] = local_time
    templates.env.filters["number_vi"] = number_vi
    templates.env.filters["label_vi"] = label_vi
    templates.env.filters["pnl_tone"] = pnl_tone
    app.state.templates = templates

    bot_id = uuid4()
    workspace_id = mock_identity.principal.workspace_id
    account_id = uuid4()
    intent_id = uuid4()
    trade_id = uuid4()
    now = datetime.now(UTC)

    trade = LedgerTrade(
        trade_id=trade_id,
        intent_id=intent_id,
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        status="CLOSED",
        symbol="BTCUSDT",
        side="LONG",
        quote_asset="USDT",
        opened_at=now - timedelta(hours=2),
        closed_at=now - timedelta(minutes=10),
        gross_pnl=Decimal("200.0"),
        fee_total=Decimal("4.0"),
        funding_total=Decimal("1.0"),
        slippage_total=Decimal("0.5"),
        total_cost=Decimal("5.5"),
        net_pnl=Decimal("194.5"),
        settings_version_id=uuid4(),
        strategy_release_id=uuid4(),
        strategy_release_hash="hash123",
        feature_snapshot_id="feat123",
        candidate_id="cand123",
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
    )
    costs = (
        LedgerCost(
            trade_id=trade_id,
            workspace_id=workspace_id,
            bot_id=bot_id,
            cost_type="FEE",
            amount=Decimal("4.0"),
            currency="USDT",
        ),
        LedgerCost(
            trade_id=trade_id,
            workspace_id=workspace_id,
            bot_id=bot_id,
            cost_type="FUNDING",
            amount=Decimal("1.0"),
            currency="USDT",
        ),
        LedgerCost(
            trade_id=trade_id,
            workspace_id=workspace_id,
            bot_id=bot_id,
            cost_type="SLIPPAGE",
            amount=Decimal("0.5"),
            currency="USDT",
        ),
    )
    snapshot = BotLedgerSnapshot(
        workspace_id=workspace_id,
        bot_id=bot_id,
        account_id=account_id,
        closed_trades=(trade,),
        open_trades=(),
        costs=costs,
        intents=(
            IntentLineage(
                intent_id=intent_id,
                bot_id=bot_id,
                evidence={},
            ),
        ),
        decisions=(
            DecisionLineage(
                decision_id=uuid4(),
                bot_id=bot_id,
                candidate_id="cand123",
                decision_hash="dec_hash_123",
                bot_config_version_id=uuid4(),
                bot_config_hash="cfg_hash",
                root_settings_version_id=uuid4(),
                model_release_id=None,
                candidate={
                    "strategy_id": "PULLBACK_V1",
                    "target_leverage": 5,
                    "risk_reward_ratio": "2.0",
                },
            ),
        ),
    )

    mock_report = build_bot_report(
        snapshot=snapshot,
        start=now - timedelta(days=1),
        end=now,
    )

    mock_reporting_service = AsyncMock()
    mock_reporting_service.get_bot_report.return_value = mock_report
    app.state.bot_reporting_service = mock_reporting_service

    mock_admin_service = AsyncMock()
    mock_admin_service.list_bots.return_value = []
    app.state.bot_admin_service = mock_admin_service
    app.state.learning_post_mortem_service = LearningPostMortemService()

    app.include_router(router)

    from trading_bot.auth.router import current_identity

    app.dependency_overrides[current_identity] = lambda: mock_identity

    client = TestClient(app)
    start_str = (now - timedelta(days=1)).isoformat().replace("+", "%2B")
    end_str = now.isoformat().replace("+", "%2B")
    response = client.get(
        f"/admin/reports?scope=bot&bot_id={bot_id}&start={start_str}&end={end_str}"
    )
    assert response.status_code == 200
    html = response.text

    # Xác nhận các thành phần của Trading Design System đã render
    assert "Tỷ Lệ Thắng (Win Rate)" in html
    assert "Profit Factor" in html
    assert "Max Drawdown" in html
    assert "tooltip-term" in html
    assert "quick-filter-group" in html
    assert "Danh Sách Lệnh Khớp Trong Kỳ" in html


def test_api_ingest_trade_negative_sample(
    mock_identity: AuthIdentity,
) -> None:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from trading_bot.admin.web_router import router

    app = FastAPI()
    app.state.templates = Jinja2Templates(directory="src/trading_bot/templates")
    service = LearningPostMortemService()
    app.state.learning_post_mortem_service = service
    app.include_router(router)

    from trading_bot.auth.router import current_identity

    app.dependency_overrides[current_identity] = lambda: mock_identity

    trade_id = uuid4()
    bot_id = uuid4()

    client = TestClient(app)
    payload = {
        "bot_id": str(bot_id),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "gross_pnl": "-100.0",
        "fee_total": "2.0",
        "funding_total": "0.5",
        "slippage_total": "25.0",  # Trượt giá nặng
        "net_pnl": "-127.5",
        "exit_reason": "STOP_LOSS",
        "holding_duration_seconds": 600,
        "reason": "Phát hiện trượt giá cao trên sổ lệnh sàn thực tế",
    }
    response = client.post(
        f"/admin/api/trades/{trade_id}/ingest-negative-sample",
        json=payload,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "sample" in data
    assert data["sample"]["trade_id"] == str(trade_id)
    assert data["sample"]["counterfactual_action"] == "ABSTAIN"
    assert len(service.list_ingested_samples()) == 1
