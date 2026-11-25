"""Unit test TDD cho CRY-214 (SIG-3 phần còn lại):

1. G5 planner_bridge giữ vùng vào, R:R, điểm/metadata có cấu trúc của Strategy Card.
2. Lý do thoát từng fill trong báo cáo domain và xuất CSV.
3. Hiển thị thang chốt lời (TakeProfitLadder) + R:R ròng từng TP trên thẻ Admin và Telegram.
4. Checklist có cấu trúc L0–L6 cho Strategy Card (bổ sung L6 giữ parity 100%).
5. Trường có cấu trúc `invalidation[]`, `expires_at`, `confidence_tier`, `entry_zone_low/high`,
   `targets[]`, `setup_id` trong `StrategyCandidate` v2 — tương thích ngược hoàn toàn với v1.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.adapters.telegram.card_renderer import _ladder_rows, render_signal_card
from trading_bot.admin.web_router import REPORT_CSV_FIELDS, _exit_reason_label_vi, _report_csv_row
from trading_bot.app import create_app
from trading_bot.application.bot_dispatcher import _candidate_payload
from trading_bot.application.strategy_catalog import strategy_registry
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    StrategyCandidate as PipelineCandidate,
)
from trading_bot.application.strategy_pipeline.planner_bridge import (
    StrategyPipelinePlannerBridge,
)
from trading_bot.application.telegram_cards import signal_card_caption, signal_card_view_from
from trading_bot.application.telegram_signals import (
    TelegramSignalCandidate,
    build_signal_message,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.portfolio.reporting import (
    BotLedgerSnapshot,
    LedgerCost,
    LedgerFill,
    LedgerTrade,
    PositionTransition,
    build_bot_report,
)
from trading_bot.domain.strategy.checklists import strategy_checklist
from trading_bot.domain.strategy.contracts import (
    CandidateTarget,
    DecisionSnapshot,
    InvalidationRule,
    MarketSeries,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.signal_card import build_signal_card
from trading_bot.domain.strategy_card.models import (
    CompositionLayer,
    ExecutionProfile,
    GraphNode,
    NodeExecutionRole,
)
from trading_bot.domain.strategy_card.schema import StrategyCard

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
BOT_ID = UUID("77117cc4-6c7f-407e-8f23-948d740a46c2")
ACCOUNT_ID = UUID("66117cc4-6c7f-407e-8f23-948d740a46c2")
USER_ID = UUID("d33fa90f-697d-43bd-8b8f-0a89f3e97f57")
MEMBERSHIP_ID = UUID("a93c4b18-f76c-4cfb-9bad-9e0aee5ccadc")


def _sample_card() -> StrategyCard:
    return StrategyCard(
        card_id="card_pinbar_01",
        name="Pinbar Rejection Strategy",
        version="1.0.0",
        author="Quant Test",
        created_at=NOW,
        description="Chiến lược bắt đáy râu nến",
        layers=[
            CompositionLayer.L1_CANDLE_ANATOMY,
            CompositionLayer.L4_CONTEXT_GATE,
            CompositionLayer.L5_STRATEGY_TRIGGER,
            CompositionLayer.L6_ZONES,
        ],
        nodes=[
            GraphNode(
                node_id="n1",
                layer=CompositionLayer.L1_CANDLE_ANATOMY,
                role=NodeExecutionRole.STATELESS_TRANSFORM,
                operation="wick_rejection",
            ),
            GraphNode(
                node_id="n4",
                layer=CompositionLayer.L4_CONTEXT_GATE,
                role=NodeExecutionRole.GATE_FILTER,
                operation="trend_filter",
            ),
            GraphNode(
                node_id="n5",
                layer=CompositionLayer.L5_STRATEGY_TRIGGER,
                role=NodeExecutionRole.GATE_FILTER,
                operation="threshold",
            ),
            GraphNode(
                node_id="n6",
                layer=CompositionLayer.L6_ZONES,
                role=NodeExecutionRole.TERMINAL_ZONE,
                operation="atr_zones",
                parameters={"sl_multiplier": 2.0, "rr_ratio": 2.5, "max_hold_bars": 16},
            ),
        ],
        edges=[],
        execution_profile=ExecutionProfile(
            assumed_taker_fee=Decimal("0.0005"),
            assumed_slippage_bps=Decimal("1.0"),
        ),
    )


def _sample_snapshot() -> DecisionSnapshot:
    bar = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="15m",
        open_time=NOW - timedelta(minutes=15),
        close_time=NOW,
        event_at=NOW,
        received_at=NOW,
        available_at=NOW,
        open=Decimal("65000.0"),
        high=Decimal("65500.0"),
        low=Decimal("64900.0"),
        close=Decimal("65400.0"),
        volume=Decimal("120.5"),
        is_closed=True,
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=NOW,
        snapshot_id="snap_btc_01",
        series=(MarketSeries(timeframe="15m", bars=(bar,)),),
        features=(),
    )


# ---------------------------------------------------------------------------
# 1 & 5: StrategyCandidate v2 + G5 planner_bridge
# ---------------------------------------------------------------------------


def test_v1_candidate_hash_and_payload_remain_backward_compatible() -> None:
    """Candidate không truyền trường v2 giữ nguyên hash và không phát sinh khóa v2 thừa."""
    cand = StrategyCandidate(
        strategy_id="H01",
        strategy_release_id="h01-v1",
        release_content_hash="a" * 64,
        settings_version_id="settings-1",
        snapshot_id="snap-1",
        symbol="BTCUSDT",
        direction="long",
        reference_price=Decimal("100"),
        atr_value=2.0,
        signal_at=NOW,
        eligible_after=NOW + timedelta(microseconds=1),
        expires_at=NOW + timedelta(minutes=15),
        recipe=ExitRecipe("r1", stop_atr_multiple=1.5, take_profit_atr_multiple=3.0),
        evidence_feature_ids=("f1",),
        evidence_bar_keys=("k1",),
        macro_evidence_ids=(),
        geometry_version=None,
        reason_code="H01_LONG_ENTRY",
        detail_vi="Tín hiệu mẫu.",
    )
    payload = _candidate_payload(cand)
    for v2_key in (
        "entry_zone_low",
        "entry_zone_high",
        "stop_price",
        "targets",
        "setup_id",
        "invalidation",
        "confidence_tier",
        "calibrated_probability",
        "risk_reward_ratio",
        "card_score",
        "card_metadata",
    ):
        assert v2_key not in payload


def test_v2_candidate_validates_structured_fields_and_serializes() -> None:
    """Candidate v2 chấp nhận entry_zone, targets, invalidation[], confidence_tier."""
    cand = StrategyCandidate(
        strategy_id="H18",
        strategy_release_id="h18-v1",
        release_content_hash="b" * 64,
        settings_version_id="settings-1",
        snapshot_id="snap-2",
        symbol="BTCUSDT",
        direction="long",
        reference_price=Decimal("68000"),
        atr_value=200.0,
        signal_at=NOW,
        eligible_after=NOW + timedelta(microseconds=1),
        expires_at=NOW + timedelta(minutes=15),
        recipe=ExitRecipe("r2", stop_atr_multiple=1.5, take_profit_atr_multiple=3.0),
        evidence_feature_ids=("f1",),
        evidence_bar_keys=("k1",),
        macro_evidence_ids=(),
        geometry_version="sig3-v2",
        reason_code="H18_LONG_ENTRY",
        detail_vi="Quét đáy phiên.",
        entry_zone_low=Decimal("67950"),
        entry_zone_high=Decimal("68050"),
        stop_price=Decimal("67650"),
        targets=(
            CandidateTarget(
                index=1,
                price=Decimal("68400"),
                fraction=Decimal("0.5"),
                r_multiple=Decimal("1"),
                label="TP1 (50%)",
                stop_to_entry_after=True,
            ),
            CandidateTarget(
                index=2,
                price=Decimal("68800"),
                fraction=Decimal("0.5"),
                r_multiple=Decimal("2"),
                label="TP2 (50%)",
            ),
        ),
        setup_id="H18:sweep-reclaim",
        invalidation=(
            InvalidationRule(
                kind="STOP_LOSS",
                detail_vi="Giá chạm SL 67.650.",
                price=Decimal("67650"),
            ),
            InvalidationRule(
                kind="EXPIRY",
                detail_vi="Hết hạn sau 15 phút.",
                expires_at=NOW + timedelta(minutes=15),
            ),
        ),
        confidence_tier="uncalibrated",
        calibrated_probability=None,
    )
    payload = _candidate_payload(cand)
    assert payload["entry_zone_low"] == "67950"
    assert payload["entry_zone_high"] == "68050"
    assert payload["stop_price"] == "67650"
    assert payload["setup_id"] == "H18:sweep-reclaim"
    assert payload["confidence_tier"] == "uncalibrated"
    assert len(payload["targets"]) == 2
    assert payload["targets"][0]["stop_to_entry_after"] is True
    assert len(payload["invalidation"]) == 2
    assert payload["invalidation"][0]["kind"] == "STOP_LOSS"

    # Chặn TP sai phía trên lệnh SHORT.
    with pytest.raises(ValueError, match="SHORT"):
        StrategyCandidate(
            strategy_id="H18",
            strategy_release_id="h18-v1",
            release_content_hash="b" * 64,
            settings_version_id="settings-1",
            snapshot_id="snap-2",
            symbol="BTCUSDT",
            direction="short",
            reference_price=Decimal("68000"),
            atr_value=200.0,
            signal_at=NOW,
            eligible_after=NOW + timedelta(microseconds=1),
            expires_at=NOW + timedelta(minutes=15),
            recipe=ExitRecipe("r2", stop_atr_multiple=1.5, take_profit_atr_multiple=3.0),
            evidence_feature_ids=("f1",),
            evidence_bar_keys=("k1",),
            macro_evidence_ids=(),
            geometry_version=None,
            reason_code="H18_SHORT_ENTRY",
            detail_vi="Sai hướng TP.",
            stop_price=Decimal("68400"),
            targets=(CandidateTarget(index=1, price=Decimal("68500"), fraction=Decimal("1")),),
        )


def test_g5_planner_bridge_preserves_structured_zone_rr_score_and_metadata() -> None:
    """G5: planner_bridge giữ vùng vào, SL/TP, R:R, điểm, invalidation và metadata của card."""
    card = _sample_card()
    snap = _sample_snapshot()
    pipeline_cand = PipelineCandidate(
        candidate_id="cand_orig_01",
        strategy_card_id="card_pinbar_01",
        content_hash="a" * 64,
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=Decimal("65400.0"),
        stop_loss=Decimal("64400.0"),
        take_profit=Decimal("67900.0"),
        risk_reward_ratio=Decimal("2.5"),
        confidence=Decimal("0.92"),
        signal_at=NOW,
        evidence_nodes=("n1", "n5"),
        metadata={
            "atr": "500.0",
            "trigger_score": "0.88",
            "entry_zone_low": "65350.0",
            "entry_zone_high": "65450.0",
        },
    )
    cand = StrategyPipelinePlannerBridge.to_contract_candidate(
        pipeline_candidate=pipeline_cand, card=card, snapshot=snap
    )
    assert cand.entry_zone_low == Decimal("65350.0")
    assert cand.entry_zone_high == Decimal("65450.0")
    assert cand.stop_price == Decimal("64400.0")
    assert cand.risk_reward_ratio == Decimal("2.5")
    assert cand.card_score == Decimal("0.88")
    assert cand.confidence_tier == "uncalibrated"
    assert cand.setup_id == "card_pinbar_01:v1.0.0"
    assert len(cand.targets) == 1
    assert cand.targets[0].price == Decimal("67900.0")
    assert cand.targets[0].r_multiple == Decimal("2.5")
    kinds = {rule.kind for rule in cand.invalidation}
    assert {"STOP_LOSS", "EXPIRY", "MAX_HOLD_BARS"} <= kinds
    meta = dict(cand.card_metadata)
    assert meta["card_name"] == "Pinbar Rejection Strategy"
    assert meta["l4_gates"] == "trend_filter"
    assert meta["risk_reward_ratio"] == "2.5"
    assert meta["trigger_score"] == "0.88"


# ---------------------------------------------------------------------------
# 2: Lý do thoát từng fill trong báo cáo và CSV
# ---------------------------------------------------------------------------


def test_report_and_csv_include_per_fill_exit_reasons() -> None:
    """Mỗi exit fill mang đúng exit_reason riêng trong báo cáo và cột exit_fill_reasons ở CSV."""
    trade_id = uuid4()
    intent_id = uuid4()
    opened = NOW - timedelta(hours=2)
    tp1_at = NOW - timedelta(hours=1)
    closed = NOW - timedelta(minutes=10)
    trade = LedgerTrade(
        trade_id=trade_id,
        intent_id=intent_id,
        workspace_id=WORKSPACE,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        status="CLOSED",
        symbol="BTCUSDT",
        side="LONG",
        quote_asset="USDT",
        opened_at=opened,
        closed_at=closed,
        gross_pnl=Decimal("30"),
        fee_total=Decimal("2"),
        funding_total=Decimal("0"),
        slippage_total=Decimal("1"),
        total_cost=Decimal("3"),
        net_pnl=Decimal("27"),
        settings_version_id=uuid4(),
        strategy_release_id=uuid4(),
        strategy_release_hash="c" * 64,
        feature_snapshot_id="snap-1",
        candidate_id="d" * 64,
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
    )
    costs = (
        LedgerCost(trade_id, WORKSPACE, BOT_ID, "FEE", Decimal("2"), "USDT"),
        LedgerCost(trade_id, WORKSPACE, BOT_ID, "SLIPPAGE", Decimal("1"), "USDT"),
    )
    fills = (
        LedgerFill(
            "entry-1",
            intent_id,
            WORKSPACE,
            BOT_ID,
            ACCOUNT_ID,
            "ENTRY",
            Decimal("0.2"),
            Decimal("100"),
            opened,
            Decimal("0.8"),
            Decimal("0.4"),
            "bar-0",
        ),
        LedgerFill(
            "exit-tp1",
            intent_id,
            WORKSPACE,
            BOT_ID,
            ACCOUNT_ID,
            "EXIT",
            Decimal("0.1"),
            Decimal("110"),
            tp1_at,
            Decimal("0.6"),
            Decimal("0.3"),
            "bar-1",
        ),
        LedgerFill(
            "exit-final",
            intent_id,
            WORKSPACE,
            BOT_ID,
            ACCOUNT_ID,
            "EXIT",
            Decimal("0.1"),
            Decimal("120"),
            closed,
            Decimal("0.6"),
            Decimal("0.3"),
            "bar-2",
        ),
    )
    events = (
        PositionTransition(
            "ev-tp1",
            intent_id,
            WORKSPACE,
            BOT_ID,
            ACCOUNT_ID,
            "REDUCED",
            tp1_at,
            {"fill_key": "exit-tp1", "exit_reason": "TAKE_PROFIT_1"},
        ),
        PositionTransition(
            "ev-close",
            intent_id,
            WORKSPACE,
            BOT_ID,
            ACCOUNT_ID,
            "CLOSED",
            closed,
            {"fill_key": "exit-final", "exit_reason": "TAKE_PROFIT"},
        ),
    )
    snap = BotLedgerSnapshot(
        workspace_id=WORKSPACE,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        closed_trades=(trade,),
        open_trades=(),
        costs=costs,
        intents=(),
        decisions=(),
        fills=fills,
        position_events=events,
    )
    report = build_bot_report(snapshot=snap, start=opened - timedelta(hours=1), end=NOW).as_dict()
    row = report["trades"][0]
    assert [f["exit_reason"] for f in row["fills"]] == [None, "TAKE_PROFIT_1", "TAKE_PROFIT"]
    assert "exit_fill_reasons" in REPORT_CSV_FIELDS
    csv_row = _report_csv_row(row)
    assert "TAKE_PROFIT_1" in csv_row["exit_fill_reasons"]
    assert "TAKE_PROFIT@" in csv_row["exit_fill_reasons"]
    assert _exit_reason_label_vi("TAKE_PROFIT_1") == "Chốt lời bậc 1 (TP1)"


# ---------------------------------------------------------------------------
# 3: Thang chốt lời + R:R ròng từng TP trên Signal Card, Admin & Telegram
# ---------------------------------------------------------------------------


def test_signal_card_and_telegram_render_plan_take_profit_ladder_and_net_rr() -> None:
    """Thẻ tín hiệu đọc plan.targets (TakeProfitLadder), tỷ lệ chốt, hòa vốn và R:R ròng."""
    candidate_payload = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "strategy_id": "H18",
        "strategy_release_id": "h18-v1",
        "setup_id": "H18:sweep-reclaim",
        "reason_code": "H18_LONG_ENTRY",
        "feature_snapshot_id": "snap-h18",
        "signal_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "reference_price": "68000",
        "entry_zone_low": "67980",
        "entry_zone_high": "68020",
        "atr_value": "200",
        "confidence_tier": "uncalibrated",
        "invalidation": [
            {"kind": "STOP_LOSS", "detail_vi": "Giá chạm SL 67.700.", "price": "67700"},
        ],
        "recipe": {"stop_atr_multiple": "1.5", "take_profit_atr_multiple": "3.0"},
    }
    plan_payload = {
        "entry_price": "68000",
        "stop_price": "67700",
        "target_price": "68600",
        "account_risk_pct": "0.5",
        "targets": [
            {
                "index": 1,
                "price": "68300",
                "fraction": "0.5",
                "r_multiple": "1",
                "net_reward_to_risk": "0.88",
                "stop_to_entry_after": True,
            },
            {
                "index": 2,
                "price": "68600",
                "fraction": "0.5",
                "r_multiple": "2",
                "net_reward_to_risk": "1.82",
                "stop_to_entry_after": False,
            },
        ],
    }
    card = build_signal_card(candidate_payload, plan=plan_payload)
    assert card["has_take_profit_ladder"] is True
    assert card["setup_id"] == "H18:sweep-reclaim"
    assert card["entry_zone"] == {"low": 67980.0, "high": 68020.0}
    assert len(card["targets"]) == 2
    tp1, tp2 = card["targets"]
    assert tp1["source"] == "plan_ladder"
    assert tp1["close_fraction"] == pytest.approx(0.5)
    assert tp1["stop_to_entry_after"] is True
    assert tp1["net_r_multiple"] == pytest.approx(0.88)
    assert tp2["net_r_multiple"] == pytest.approx(1.82)
    assert card["invalidation_rules"][0]["kind"] == "STOP_LOSS"

    tg_cand = TelegramSignalCandidate(
        decision_id=uuid4(),
        symbol="BTCUSDT",
        direction="LONG",
        timeframe="5m",
        strategy_id="H18",
        strategy_release_id="h18-v1",
        reference_price=Decimal("68000"),
        signal_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        reason_code="H18_LONG_ENTRY",
        detail_vi="Quét thanh khoản đáy phiên.",
        card=card,
    )
    msg = build_signal_message(tg_cand)
    assert "TP1" in msg
    assert "50%" in msg
    assert "dời SL hòa vốn" in msg
    assert "ròng 0,9R" in msg
    assert "ròng 1,8R" in msg

    view = signal_card_view_from(tg_cand)
    assert view.targets[0].close_fraction == Decimal("0.5")
    assert view.targets[0].stop_to_entry_after is True
    caption = signal_card_caption(view)
    assert "ròng" in caption
    rows = _ladder_rows(view)
    target_chips = [r.chip_text for r in rows if r.kind == "target"]
    assert any("ròng" in chip for chip in target_chips)
    png = render_signal_card(view)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# 4: Checklist L0–L6 có cấu trúc cho Strategy Card
# ---------------------------------------------------------------------------


def test_strategy_card_checklist_includes_structured_l6_item_and_preserves_parity() -> None:
    """Checklist Strategy Card gồm đủ tầng L0, warmup L2–L3, L4, L5 và L6 giữ parity 100%."""
    card = build_default_demo_cards()[0]
    release_id = f"{card.card_id.lower()}-v{card.version}"
    bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1h",
            open_time=NOW - timedelta(hours=30 - i),
            close_time=NOW - timedelta(hours=29 - i) - timedelta(microseconds=1),
            event_at=NOW - timedelta(hours=29 - i) - timedelta(microseconds=1),
            received_at=NOW - timedelta(hours=29 - i) - timedelta(microseconds=1),
            available_at=NOW - timedelta(hours=29 - i) - timedelta(microseconds=1),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("100"),
            is_closed=True,
        )
        for i in range(30)
    )
    snap = DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=bars[-1].available_at,
        snapshot_id="snap-card-l6",
        series=(MarketSeries(timeframe="1h", bars=bars),),
        features=(),
        position_is_flat=True,
    )
    settings = StrategySelection(
        settings_version_id="s-1",
        active_release_ids=(release_id,),
        published=True,
    )
    items = strategy_checklist(release_id, snap, settings)
    keys = [item.key for item in items]
    assert "l0_data_hygiene" in keys
    assert "indicator_warmup" in keys
    assert "l6_execution_zones" in keys
    result = strategy_registry().resolve(release_id, settings).evaluate(snap, settings)
    all_passed = all(item.passed is True for item in items)
    assert all_passed == isinstance(result, StrategyCandidate)


def test_export_csv_endpoint_includes_exit_fill_reasons_for_partial_exits() -> None:
    """GET /admin/reports/export.csv xuất rõ lý do từng exit fill (TP1 partial + TP2 close)."""

    class _StubReport:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def as_dict(self) -> dict[str, object]:
            return self._payload

    class _StubReportingService:
        async def get_bot_report(self, query: object, principal: Principal) -> _StubReport:
            return _StubReport(
                {
                    "scope": "bot",
                    "report_version": "bot-report.v2",
                    "report_hash": "e" * 64,
                    "trades": [
                        {
                            "trade_id": "trade-partial-1",
                            "bot_id": str(BOT_ID),
                            "symbol": "BTCUSDT",
                            "side": "LONG",
                            "quote_asset": "USDT",
                            "opened_at": (NOW - timedelta(hours=2)).isoformat(),
                            "closed_at": NOW.isoformat(),
                            "gross_pnl": "30",
                            "fee_total": "2",
                            "funding_total": "0",
                            "slippage_total": "1",
                            "total_cost": "3",
                            "net_pnl": "27",
                            "entry_reason": "H18_LONG_ENTRY",
                            "exit_reason": "TAKE_PROFIT",
                            "fills": [
                                {"role": "ENTRY", "quantity": "0.2", "price": "100"},
                                {
                                    "role": "EXIT",
                                    "quantity": "0.1",
                                    "price": "110",
                                    "position_event_type": "REDUCED",
                                    "exit_reason": "TAKE_PROFIT_1",
                                },
                                {
                                    "role": "EXIT",
                                    "quantity": "0.1",
                                    "price": "120",
                                    "position_event_type": "CLOSED",
                                    "exit_reason": "TAKE_PROFIT",
                                },
                            ],
                        }
                    ],
                }
            )

    app = create_app(AppSettings(app_env="test"))
    principal = Principal(
        user_id=USER_ID,
        workspace_id=WORKSPACE,
        membership_id=MEMBERSHIP_ID,
        roles=frozenset({Role.OWNER}),
        mfa_verified_at=NOW,
    )
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=principal, email="owner@crypidian.io"
    )
    app.state.bot_reporting_service = _StubReportingService()
    client = TestClient(app)

    resp = client.get(
        "/admin/reports/export.csv",
        params={
            "scope": "bot",
            "bot_id": str(BOT_ID),
            "start": (NOW - timedelta(days=1)).isoformat(),
            "end": (NOW + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    assert rows[0]["exit_fill_reasons"] == "TAKE_PROFIT_1@110(0.1); TAKE_PROFIT@120(0.1)"


def test_signal_card_confidence_tier_and_candidate_ladder_without_plan() -> None:
    """Candidate v2 có confidence_tier và nhiều mục tiêu hiển thị đúng mà không bịa phần trăm."""
    card = build_signal_card(
        {
            "symbol": "ETHUSDT",
            "direction": "short",
            "strategy_id": "H20",
            "strategy_release_id": "h20-v1",
            "reason_code": "H20_SHORT_ENTRY",
            "signal_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
            "reference_price": "3000",
            "atr_value": "20",
            "stop_price": "3030",
            "confidence_tier": "high",
            "targets": [
                {
                    "index": 1,
                    "price": "2970",
                    "fraction": "0.5",
                    "r_multiple": "1",
                    "stop_to_entry_after": True,
                },
                {"index": 2, "price": "2940", "fraction": "0.5", "r_multiple": "2"},
            ],
            "recipe": {"stop_atr_multiple": "1.5"},
        },
        fee_rate=Decimal("0.0005"),
    )
    assert card["has_take_profit_ladder"] is True
    assert card["confidence"] == {"tier": "high", "label": "Hạng Cao"}
    assert "%" not in card["confidence"]["label"]
    assert [t["source"] for t in card["targets"]] == ["candidate_ladder", "candidate_ladder"]
    assert all(t["net_r_multiple"] is not None for t in card["targets"])
    assert card["targets"][0]["stop_to_entry_after"] is True
