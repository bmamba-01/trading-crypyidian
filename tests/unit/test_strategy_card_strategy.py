"""Unit tests cho adapter đưa Strategy Card L0–L6 vào hợp đồng `Strategy` của runtime."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.application.strategy_catalog import PRODUCT_CARD_IDS, strategy_registry
from trading_bot.application.strategy_pipeline.card_strategy import (
    StrategyCardStrategy,
    strategy_card_release_id,
)
from trading_bot.application.strategy_pipeline.demo_cards import build_default_demo_cards
from trading_bot.application.strategy_pipeline.pipeline_engine import (
    StrategyCandidate as PipelineCandidate,
)
from trading_bot.application.strategy_pipeline.planner_bridge import (
    StrategyPipelinePlannerBridge,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.strategy.contracts import (
    DecisionSnapshot,
    FeatureObservation,
    MarketSeries,
    NoTrade,
    StrategyCandidate,
    StrategySelection,
)
from trading_bot.domain.strategy.registry import StrategyRegistry

DECIDED_AT = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)


def _snapshot(
    opened: str, high: str, low: str, close: str, *, atr_14: float | None = 1.3
) -> DecisionSnapshot:
    bar = OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=DECIDED_AT - timedelta(hours=1),
        close_time=DECIDED_AT,
        event_at=DECIDED_AT,
        received_at=DECIDED_AT,
        available_at=DECIDED_AT,
        open=Decimal(opened),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("150"),
        is_closed=True,
    )
    return DecisionSnapshot(
        symbol="BTCUSDT",
        decision_time=DECIDED_AT,
        snapshot_id="snapshot-1",
        series=(MarketSeries(timeframe="1h", bars=(bar,)),),
        features=(
            (
                FeatureObservation(
                    feature_id="1h:atr_14:snapshot-1",
                    name="atr_14",
                    timeframe="1h",
                    value=atr_14,
                    previous_value=None,
                    available_at=DECIDED_AT,
                ),
            )
            if atr_14 is not None
            else ()
        ),
        macro_evidence_ids=("macro-1",),
    )


def _strategy() -> StrategyCardStrategy:
    return StrategyCardStrategy(build_default_demo_cards()[0])


def _selection(strategy: StrategyCardStrategy) -> StrategySelection:
    return StrategySelection(
        settings_version_id="settings-7",
        active_release_ids=(strategy.release_id,),
        published=True,
    )


def test_release_identity_is_derived_from_card_and_changes_with_content() -> None:
    card = build_default_demo_cards()[0]
    strategy = StrategyCardStrategy(card)

    assert strategy.strategy_id == card.card_id
    assert strategy.release_id == strategy_card_release_id(card)
    assert strategy.release_id == "sc-binance-usdm-vol-breakout-001-v1.0.0"
    assert len(strategy.content_hash) == 64
    assert StrategyCardStrategy(card).content_hash == strategy.content_hash

    changed_nodes = [
        replace(node, parameters={"risk_reward": 3.0}) if node.node_id == "l6_zones" else node
        for node in card.nodes
    ]
    changed = card.model_copy(update={"nodes": changed_nodes})
    assert StrategyCardStrategy(changed).content_hash != strategy.content_hash


def test_hammer_bar_yields_long_candidate_with_pipeline_zones() -> None:
    strategy = _strategy()
    result = strategy.evaluate(_snapshot("100.0", "100.3", "99.0", "100.2"), _selection(strategy))

    assert isinstance(result, StrategyCandidate)
    assert result.strategy_id == strategy.strategy_id
    assert result.strategy_release_id == strategy.release_id
    assert result.release_content_hash == strategy.content_hash
    assert result.settings_version_id == "settings-7"
    assert result.snapshot_id == "snapshot-1"
    assert result.direction == "long"
    assert result.reference_price == Decimal("100.2")
    # ATR lấy từ feature đã quản trị `atr_14` (1,3); L6 đặt stop 2 ATR, mục tiêu theo R:R 2,0.
    assert result.evidence_feature_ids == ("1h:atr_14:snapshot-1", "l5_trigger")
    assert result.atr_value == pytest.approx(1.3)
    assert result.recipe.stop_atr_multiple == pytest.approx(2.0)
    assert result.recipe.take_profit_atr_multiple == pytest.approx(4.0)
    assert result.signal_at == DECIDED_AT
    assert result.eligible_after == DECIDED_AT + timedelta(seconds=1)
    assert result.expires_at == DECIDED_AT + timedelta(minutes=15)
    assert result.macro_evidence_ids == ("macro-1",)
    assert result.reason_code == "QUANTSMART_L6_ZONE_CONFIRMED"
    # Diễn giải mang đúng số liệu pipeline đã tính, không phải câu mẫu cố định.
    assert "L4 volatility_regime_gate ĐẠT (ATR 1,3)" in result.detail_vi
    assert "L5 breakout_candle_trigger điểm 0,7692" in result.detail_vi
    assert "L6 vào 100,2, stop 97,6, mục tiêu 105,4, R:R 2" in result.detail_vi


def test_bar_without_trigger_returns_no_trade_with_reason() -> None:
    strategy = _strategy()
    result = strategy.evaluate(_snapshot("100.0", "102.0", "100.0", "101.0"), _selection(strategy))

    assert isinstance(result, NoTrade)
    assert result.strategy_id == strategy.strategy_id
    assert result.code == "STRATEGY_CARD_NO_TRIGGER"
    assert result.gate_details


def test_invalid_bar_returns_data_hygiene_no_trade() -> None:
    strategy = _strategy()
    snapshot = _snapshot("100.0", "100.3", "99.0", "100.2")
    empty = DecisionSnapshot(
        symbol=snapshot.symbol,
        decision_time=snapshot.decision_time,
        snapshot_id=snapshot.snapshot_id,
        series=(),
        features=(),
    )
    result = strategy.evaluate(empty, _selection(strategy))

    assert isinstance(result, NoTrade)
    assert result.code == "STRATEGY_CARD_DATA_HYGIENE_FAILED"


def test_single_bar_atr_is_rejected_on_the_decision_path() -> None:
    """Không có feature ATR lẫn đủ lịch sử nến thì thẻ không được sinh candidate."""
    strategy = _strategy()
    result = strategy.evaluate(
        _snapshot("100.0", "100.3", "99.0", "100.2", atr_14=None), _selection(strategy)
    )

    assert isinstance(result, NoTrade)
    assert result.code == "STRATEGY_CARD_INDICATOR_WARMUP"
    assert result.missing_features == ("l2_atr",)


def test_bare_registry_has_only_built_in_rules() -> None:
    releases = {release.release_id for release in StrategyRegistry().list_releases()}
    assert _strategy().release_id not in releases


def test_product_catalog_registers_listed_cards_with_stable_hashes() -> None:
    registry = strategy_registry()
    strategy = _strategy()

    assert PRODUCT_CARD_IDS == ("SC-BINANCE-USDM-VOL-BREAKOUT-001",)
    release = registry.get_release(strategy.release_id)
    assert release.strategy_id == strategy.strategy_id
    assert release.status == "EXPERIMENTAL_UNVALIDATED"
    assert release.content_hash == strategy.content_hash
    assert strategy_registry().get_release(strategy.release_id).content_hash == release.content_hash
    # Rule dựng sẵn giữ nguyên ID và hash khi có thêm release của thẻ.
    for built_in in StrategyRegistry().list_releases():
        assert registry.get_release(built_in.release_id).content_hash == built_in.content_hash
    selection = StrategySelection(
        settings_version_id="settings-7", active_release_ids=(strategy.release_id,), published=True
    )
    resolved = registry.resolve(strategy.release_id, selection)
    assert (
        resolved.evaluate(
            _snapshot("100.0", "100.3", "99.0", "100.2"), selection
        ).strategy_release_id
        == strategy.release_id
    )


def test_registry_rejects_duplicate_release_ids() -> None:
    strategy = _strategy()
    rule = (strategy, "Thẻ", "Tóm tắt", "recipe-v1")

    with pytest.raises(ValueError, match="trùng"):
        StrategyRegistry(extra_rules=(rule, rule))


def test_bridge_requires_pipeline_atr_instead_of_assuming_one() -> None:
    strategy = _strategy()
    snapshot = _snapshot("100.0", "100.3", "99.0", "100.2")
    candidate = PipelineCandidate(
        candidate_id="c" * 64,
        strategy_card_id=strategy.card.card_id,
        content_hash="h" * 64,
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=Decimal("100.2"),
        stop_loss=Decimal("97.6"),
        take_profit=Decimal("105.4"),
        risk_reward_ratio=Decimal("2.0"),
        confidence=Decimal("0.8"),
        signal_at=DECIDED_AT,
        metadata={},
    )

    with pytest.raises(ValueError, match="ATR"):
        StrategyPipelinePlannerBridge.to_contract_candidate(candidate, strategy.card, snapshot)
