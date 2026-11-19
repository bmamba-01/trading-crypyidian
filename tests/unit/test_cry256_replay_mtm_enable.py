"""Kiểm thử CRY-256: bật Mark-to-Market (MTM) trong replay job/backtest và chống trộn chế độ."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tests.unit.test_runtime_replay_evaluator import _first_intent_id, _setup

from trading_bot.adapters.database.repositories.backtests import (
    BacktestRepository,
    BacktestRepositoryConflict,
    _report_payload,
)
from trading_bot.application.backtest import (
    EQUITY_VALUATION_MARK_TO_MARKET,
    EQUITY_VALUATION_REALIZED_ONLY,
    BacktestRunner,
    EvaluationReport,
    ReplayRejected,
)
from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission
from trading_bot.application.backtest_sensitivity import (
    SensitivityComparison,
    SensitivityRun,
    SensitivityScenario,
)
from trading_bot.application.runtime_replay_evaluator import (
    ReplayEvaluationContextError,
    RuntimePaperReplayComparison,
    RuntimePaperReplayRun,
    RuntimeReplayEvaluator,
    _ReplayReservation,
)
from trading_bot.domain.execution.paper import PaperBar


def test_manifest_valuation_mode_changes_content_hash_and_preserves_legacy_hash() -> None:
    """Chế độ realized_only giữ nguyên hash cũ; đổi sang mark_to_market phải đổi hash."""
    _, _, _, _, base_manifest = _setup()
    realized_manifest = replace(
        base_manifest,
        equity_valuation_mode=EQUITY_VALUATION_REALIZED_ONLY,
    )
    mtm_manifest = replace(
        base_manifest,
        equity_valuation_mode=EQUITY_VALUATION_MARK_TO_MARKET,
    )

    assert base_manifest.equity_valuation_mode == EQUITY_VALUATION_REALIZED_ONLY
    assert base_manifest.mark_to_market is False
    assert realized_manifest.content_hash == base_manifest.content_hash
    assert mtm_manifest.mark_to_market is True
    assert mtm_manifest.content_hash != base_manifest.content_hash

    with pytest.raises(ValueError, match="equity_valuation_mode"):
        replace(base_manifest, equity_valuation_mode="invalid_mode")  # type: ignore[arg-type]


def test_job_submission_roundtrips_mark_to_market_flag() -> None:
    """BacktestReplayJobSubmission serialize/deserialize cờ mark_to_market chính xác."""
    sub_default = BacktestReplayJobSubmission(
        bundle_hash="a" * 64,
        strategy_release_id="h01-v1",
        execution_interval="15m",
        starting_equity=Decimal("10000"),
        per_trade_risk_fraction=Decimal("0.01"),
        fee_rate=Decimal("0.0004"),
        slippage_rate=Decimal("0.0005"),
        seed=42,
    )
    assert sub_default.mark_to_market is False
    assert "mark_to_market" not in sub_default.to_payload()
    assert BacktestReplayJobSubmission.from_payload(sub_default.to_payload()) == sub_default

    sub_mtm = replace(sub_default, mark_to_market=True)
    payload_mtm = sub_mtm.to_payload()
    assert payload_mtm["mark_to_market"] is True
    assert BacktestReplayJobSubmission.from_payload(payload_mtm) == sub_mtm

    bad_payload = dict(payload_mtm)
    bad_payload["mark_to_market"] = "true"
    with pytest.raises(ValueError, match="mark_to_market"):
        BacktestReplayJobSubmission.from_payload(bad_payload)


def test_multi_position_cross_bar_missing_or_invalid_mark_fails_closed() -> None:
    """Nhiều vị thế mở chéo nến mà thiếu giá mark của 1 mã phải fail-closed."""
    evaluator, context, first, gateway, _ = _setup()
    assert evaluator(first.snapshot) is None
    btc_intent_id = _first_intent_id(evaluator)
    opened = first.snapshot.decision_time + timedelta(seconds=10)
    decision_at = opened + timedelta(seconds=1)
    gateway.apply_market_event(
        PaperBar(
            bar_key="btc-fill",
            symbol="BTCUSDT",
            opened_at=opened,
            closed_at=decision_at,
            available_at=decision_at,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )
    )
    btc_pos = gateway.position(btc_intent_id)
    assert btc_pos is not None
    assert not btc_pos.is_closed

    # Giả lập thêm vị thế mở thứ hai ở mã ETHUSDT trong cùng tài khoản replay
    eth_intent_id = "eth-cross-bar-intent"
    btc_res = evaluator._reservations[btc_intent_id]
    eth_plan = replace(btc_res.plan, symbol="ETHUSDT")
    eth_decision = replace(btc_res.decision)
    evaluator._reservations[eth_intent_id] = _ReplayReservation(
        plan=eth_plan,
        decision=eth_decision,
    )
    gateway._orders[eth_intent_id] = replace(
        gateway.order(btc_intent_id),
        intent_id=eth_intent_id,
        symbol="ETHUSDT",
    )
    gateway._positions[eth_intent_id] = replace(
        btc_pos,
        position_id="pos-eth-1",
        intent_id=eth_intent_id,
        symbol="ETHUSDT",
        entry_execution_price=Decimal("2000"),
        stop_price=Decimal("1900"),
        target_price=Decimal("2200"),
    )

    state = evaluator._account_state
    assert state is not None

    # Đủ cả BTCUSDT và ETHUSDT -> tính tổng UPNL cả 2 vị thế
    account_ok, risk_ok, exposures_ok = state.project(
        context,
        gateway,
        evaluator._reservations,
        candidate_symbol="BTCUSDT",
        mark_prices={"BTCUSDT": Decimal("105"), "ETHUSDT": Decimal("2050")},
    )
    assert len(exposures_ok) == 2
    assert account_ok.equity > context.account.equity
    assert risk_ok.equity == account_ok.equity

    # Thiếu mark của ETHUSDT -> fail-closed
    with pytest.raises(ReplayEvaluationContextError, match="MTM_MARK_PRICE_MISSING.*ETHUSDT"):
        state.project(
            context,
            gateway,
            evaluator._reservations,
            candidate_symbol="BTCUSDT",
            mark_prices={"BTCUSDT": Decimal("105")},
        )

    # Mark <= 0 -> fail-closed
    with pytest.raises(ReplayEvaluationContextError, match="MTM_MARK_PRICE_MISSING.*ETHUSDT"):
        state.project(
            context,
            gateway,
            evaluator._reservations,
            candidate_symbol="BTCUSDT",
            mark_prices={"BTCUSDT": Decimal("105"), "ETHUSDT": Decimal("0")},
        )


def test_runner_and_comparisons_reject_mixing_valuation_modes() -> None:
    """BacktestRunner, SensitivityComparison, RuntimePaperReplayComparison chặn trộn mode."""
    evaluator, _, first, gateway, base_manifest = _setup()
    mtm_manifest = replace(
        base_manifest,
        equity_valuation_mode=EQUITY_VALUATION_MARK_TO_MARKET,
    )
    runner = BacktestRunner()

    # Evaluator realized_only nhưng manifest mark_to_market -> VALUATION_MODE_MISMATCH
    mismatch_res = runner.run(
        (first,),
        manifest=mtm_manifest,
        evaluator=evaluator,
    )
    assert isinstance(mismatch_res, ReplayRejected)
    assert mismatch_res.reason_code == "VALUATION_MODE_MISMATCH"
    assert mismatch_res.equity_valuation_mode == EQUITY_VALUATION_MARK_TO_MARKET
    assert mismatch_res.mark_to_market is True

    # Cùng cấu hình MTM chạy 2 lần trên cùng observation cho cùng report_hash & mode
    mtm_eval_1 = RuntimeReplayEvaluator(
        cycle=evaluator._cycle,
        strategies=evaluator._strategies,
        gateway=gateway,
        manifest=mtm_manifest,
        context_provider=evaluator.context_provider,
        mark_to_market=True,
    )
    _, _, first_2, gateway_2, _ = _setup()
    mtm_eval_2 = RuntimeReplayEvaluator(
        cycle=replace(evaluator._cycle, paper_gateway=gateway_2)
        if hasattr(evaluator._cycle, "__dataclass_fields__")
        else evaluator._cycle,
        strategies=evaluator._strategies,
        gateway=evaluator._gateway,
        manifest=mtm_manifest,
        context_provider=evaluator.context_provider,
        mark_to_market=True,
    )
    report_mtm = runner.run((first,), manifest=mtm_manifest, evaluator=mtm_eval_1)
    assert isinstance(report_mtm, EvaluationReport)
    assert report_mtm.equity_valuation_mode == EQUITY_VALUATION_MARK_TO_MARKET
    assert report_mtm.mark_to_market is True
    payload = _report_payload(report_mtm)
    assert payload["equity_valuation_mode"] == "mark_to_market"
    assert payload["mark_to_market"] is True

    # Chạy realized_only
    _, _, first_r, _, _ = _setup()
    report_realized = runner.run((first_r,), manifest=base_manifest, evaluator=lambda _: None)
    assert isinstance(report_realized, EvaluationReport)
    assert report_realized.equity_valuation_mode == EQUITY_VALUATION_REALIZED_ONLY

    # SensitivityComparison chặn trộn 2 mode
    run_base = SensitivityRun(
        scenario=SensitivityScenario("base", "BASE"),
        manifest=base_manifest,
        result=report_realized,
    )
    run_mtm = SensitivityRun(
        scenario=SensitivityScenario("cost-1", "COST", cost_overrides=(("fee_rate", "0.001"),)),
        manifest=mtm_manifest,
        result=report_mtm,
    )
    with pytest.raises(ValueError, match="Không được trộn hai chế độ định giá vốn"):
        SensitivityComparison(runs=(run_base, run_mtm), is_complete=True)

    with pytest.raises(ValueError, match="Không được trộn hai chế độ định giá vốn"):
        RuntimePaperReplayComparison(
            runs=(
                RuntimePaperReplayRun(run_base, (), 0),
                RuntimePaperReplayRun(run_mtm, (), 0),
            )
        )
    _ = mtm_eval_2


@pytest.mark.asyncio
async def test_repository_rejects_valuation_mode_mismatch() -> None:
    """BacktestRepository.append_result từ chối khi mode của result khác mode của manifest."""
    _, _, first, _, base_manifest = _setup()
    runner = BacktestRunner()
    report = runner.run((first,), manifest=base_manifest, evaluator=lambda _: None)
    assert isinstance(report, EvaluationReport)
    tampered_report = replace(
        report,
        equity_valuation_mode=EQUITY_VALUATION_MARK_TO_MARKET,
    )
    repo = BacktestRepository(session=MagicMock())
    with pytest.raises(BacktestRepositoryConflict, match="Chế độ định giá vốn"):
        await repo.append_result(
            workspace_id=first.snapshot.workspace_id,
            manifest=base_manifest,
            result=tampered_report,
        )
