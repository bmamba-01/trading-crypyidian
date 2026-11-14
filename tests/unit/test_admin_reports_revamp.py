"""Unit tests cho revamp báo cáo, theme và API AI Retraining Ingestion."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from starlette.testclient import TestClient

from trading_bot.application.learning_post_mortem import LearningPostMortemService
from trading_bot.auth.policy import Role
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

    # Chọn một giao dịch để mở panel hậu kiểm: chỉ hiện dữ liệu ledger, không tự điền L4–L6.
    report_payload = mock_report.as_dict()
    selected = report_payload["trades"][0]
    mock_reporting_service.get_trade_chart.return_value = {
        "symbol": selected["symbol"],
        "side": selected["side"],
        "timeframe": "1h",
        "trade": selected,
        "bars": [],
        "notice": None,
    }
    mock_reporting_service.get_trade_plan_summaries.return_value = {}
    chart_response = client.get(
        f"/admin/reports?scope=bot&bot_id={bot_id}&start={start_str}&end={end_str}"
        f"&chart_trade_id={selected['trade_id']}"
    )
    assert chart_response.status_code == 200
    chart_html = chart_response.text
    assert "Mổ Xẻ Quyết Định Định Lượng" in chart_html
    assert "Ledger chưa lưu" in chart_html
    assert 'id="post-mortem-unevaluated"' in chart_html
    assert "lỗ vượt kế hoạch" in chart_html
    for fabricated in ("5x (Rủi ro", "78.5/100", "82.0/100", "ĐẠT (TRENDING)", "1 : 2.0"):
        assert fabricated not in chart_html
    assert "Mục tiêu chốt lời (TP)" not in chart_html


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

    from trading_bot.auth.router import current_identity, require_csrf

    app.dependency_overrides[current_identity] = lambda: mock_identity
    # Kiểm CSRF được thay bằng danh tính đã xác thực; hành vi chặn có test riêng.
    reviewer = replace(
        mock_identity,
        principal=replace(mock_identity.principal, roles=frozenset({Role.ANALYST_REVIEWER})),
    )
    app.dependency_overrides[require_csrf] = lambda: reviewer

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


def test_admin_reports_post_mortem_uses_approved_plan(
    mock_identity: AuthIdentity,
) -> None:
    """Khi ledger ghim kế hoạch đã duyệt, panel hiện SL/TP, đòn bẩy và ngân sách rủi ro thật."""
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from trading_bot.admin.presentation import label_vi, local_time, number_vi, pnl_tone
    from trading_bot.admin.web_router import router
    from trading_bot.auth.router import current_identity

    trade_id = str(uuid4())
    bot_id = uuid4()
    now = datetime.now(UTC)
    trade = {
        "trade_id": trade_id,
        "bot_id": str(bot_id),
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quote_asset": "USDT",
        "opened_at": (now - timedelta(hours=2)).isoformat(),
        "closed_at": (now - timedelta(hours=1)).isoformat(),
        "gross_pnl": "-140",
        "fee_total": "8",
        "funding_total": "0",
        "slippage_total": "2",
        "total_cost": "10",
        "net_pnl": "-150",
        "entry_reason": {"strategy_id": "SC-DEMO", "detail_vi": "L4 ĐẠT; L5 điểm 0,77."},
        "exit_reason": "STOP_LOSS",
        "fills": [
            {"role": "ENTRY", "quantity": "31.3", "price": "100.4", "occurred_at": now.isoformat()},
        ],
    }

    class _Report:
        def as_dict(self) -> dict[str, object]:
            return {"scope": "bot", "totals": {}, "trades": [dict(trade)], "open_trades": []}

    service = AsyncMock()
    service.get_bot_report.return_value = _Report()
    service.get_trade_chart.return_value = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "trade": dict(trade),
        "bars": [],
        "notice": None,
    }
    service.get_trade_plan_summaries.return_value = {
        UUID(trade_id): {
            "entry_price": "100.4",
            "stop_price": "97.6",
            "target_price": "105.4",
            "leverage": "5",
            "risk_budget": "100",
            "entry_slippage_cost": "1",
        }
    }
    admin = AsyncMock()
    admin.list_bots.return_value = []

    app = FastAPI()
    templates = Jinja2Templates(directory="src/trading_bot/templates")
    templates.env.filters.update(
        label_vi=label_vi, local_time=local_time, number_vi=number_vi, pnl_tone=pnl_tone
    )
    app.state.templates = templates
    app.state.bot_reporting_service = service
    app.state.bot_admin_service = admin
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: mock_identity

    start = (now - timedelta(days=1)).isoformat().replace("+", "%2B")
    end = now.isoformat().replace("+", "%2B")
    response = TestClient(app).get(
        f"/admin/reports?scope=bot&bot_id={bot_id}&start={start}&end={end}"
        f"&chart_trade_id={trade_id}"
    )
    assert response.status_code == 200
    html = response.text

    assert "5x" in html
    assert "1 : 1.79" in html
    assert "SL: 97,6" in html
    assert "TP: 105,4" in html
    assert "kế hoạch dự trù 2 USDT" in html
    assert "100 USDT" in html
    assert "kết quả -1.50R" in html
    assert "Mục tiêu chốt lời (TP)" in html
    assert "SAI LỆCH" in html
    assert 'id="post-mortem-unevaluated"' not in html
