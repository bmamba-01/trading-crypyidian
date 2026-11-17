"""AI-5 (CRY-236): nhãn dải R (owner chọn "Small R band", 17/11/2026) và risk_amount (1R)."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_learning_split_candidate import _candidate

from trading_bot.adapters.database.repositories.backtest_outcomes import (
    _trade_from_payload,
    _trade_payload,
)
from trading_bot.application.learning_split_candidate import (
    LearningSplitCandidateConfig,
    build_learning_split_candidate,
)
from trading_bot.domain.learning.evaluation import EvaluationTrade
from trading_bot.domain.learning.trade_labels import (
    NET_PNL_SIGN_V1,
    NET_R_BAND_V1,
    trade_label_value,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _trade(gross: str, *, risk: str | None = "10") -> EvaluationTrade:
    return EvaluationTrade(
        trade_id="t",
        opened_at=NOW,
        closed_at=NOW + timedelta(hours=1),
        gross_pnl=Decimal(gross),
        fees=Decimal("0"),
        funding=Decimal("0"),
        slippage=Decimal("0"),
        exposure=Decimal("100"),
        risk_amount=None if risk is None else Decimal(risk),
    )


@pytest.mark.parametrize(
    ("gross", "expected"),
    [("1", 0), ("-1", 0), ("0", 0), ("1.01", 1), ("-1.01", -1), ("25", 1), ("-10", -1)],
)
def test_r_band_label_uses_tenth_of_one_r(gross: str, expected: int) -> None:
    assert trade_label_value(_trade(gross), NET_R_BAND_V1) == expected


def test_sign_label_unchanged_and_r_band_requires_risk() -> None:
    assert trade_label_value(_trade("1"), NET_PNL_SIGN_V1) == 1
    with pytest.raises(ValueError, match="risk_amount"):
        trade_label_value(_trade("1", risk=None), NET_R_BAND_V1)
    with pytest.raises(ValueError, match="không được hỗ trợ"):
        trade_label_value(_trade("1"), "other")


@pytest.mark.parametrize("risk", ["0", "-1", "NaN"])
def test_risk_amount_must_be_positive_finite(risk: str) -> None:
    with pytest.raises(ValueError, match="risk_amount"):
        _trade("1", risk=risk)


def test_outcome_payload_round_trips_risk_and_keeps_legacy_shape() -> None:
    with_risk = _trade("5")
    payload = _trade_payload(with_risk)
    assert payload["risk_amount"] == "10"
    assert _trade_from_payload(payload) == with_risk
    legacy = _trade("5", risk=None)
    assert "risk_amount" not in _trade_payload(legacy)
    assert _trade_from_payload(_trade_payload(legacy)) == legacy


def test_split_candidate_r_band_version_changes_labels_but_default_config_hash_is_stable() -> None:
    candidate, outcomes, proof = _candidate()
    # Cấu hình mặc định không thêm khóa label_version → canonical cũ giữ nguyên.
    assert "label_version" not in candidate.canonical_configuration
    risky = tuple(
        replace(item, trade=replace(item.trade, risk_amount=Decimal("25"))) for item in outcomes
    )
    config = LearningSplitCandidateConfig(
        holdout_start=candidate.config.holdout_start,
        evaluation_cutoff=candidate.config.evaluation_cutoff,
        folds=candidate.config.folds,
        label_version=NET_R_BAND_V1,
    )
    r_band = build_learning_split_candidate(
        workspace_id=candidate.workspace_id,
        trial_record_id=candidate.trial_record_id,
        proof_record_id=candidate.proof_record_id,
        proof=proof,
        outcomes=risky,
        config=config,
        as_of=candidate.manifest.as_of,
    )
    assert r_band.manifest.label_version == NET_R_BAND_V1
    assert '"label_version":"net-r-band-v1"' in r_band.canonical_configuration
    # net PnL fixture = +1,8 hoặc -2,2; 1R = 25 → |R| ≤ 0,088 ≤ 0,1 → lớp 0.
    assert {item.label_value for item in r_band.manifest.samples} == {0}
    assert r_band.candidate_id != candidate.candidate_id


def test_unknown_label_version_is_rejected() -> None:
    candidate, _, _ = _candidate()
    with pytest.raises(ValueError, match="label_version"):
        LearningSplitCandidateConfig(
            holdout_start=candidate.config.holdout_start,
            evaluation_cutoff=candidate.config.evaluation_cutoff,
            folds=candidate.config.folds,
            label_version="net-r-band-v9",
        )


def test_replay_risk_amount_scales_planned_loss_by_filled_fraction() -> None:
    from trading_bot.application.runtime_replay_evaluator import _trade_risk_amount

    plan = SimpleNamespace(quantity=Decimal("2"), estimated_loss=Decimal("30"))
    assert _trade_risk_amount(plan, SimpleNamespace(filled_quantity=Decimal("1"))) == Decimal("15")
    assert _trade_risk_amount(plan, SimpleNamespace(filled_quantity=Decimal("2"))) == Decimal("30")
    assert _trade_risk_amount(plan, SimpleNamespace(filled_quantity=Decimal("0"))) is None


def test_outcome_risk_migration_pins_hashes_both_directions() -> None:
    path = Path(__file__).parents[2] / "migrations/versions/c9e1a3b5d7f9_ai5_outcome_risk_amount.py"
    spec = importlib.util.spec_from_file_location("ai5_outcome_risk", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "a5c7e9b1d3f5"
    assert len(module.PREIMAGE) == 64
    assert len(module.POSTIMAGE) == 64
    statements: list[str] = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    module.upgrade()
    assert module.PREIMAGE in statements[0]
    assert module.POSTIMAGE in statements[0]
    assert "AI5_OUTCOME_RISK_AMOUNT_INVALID" in statements[0]
    statements.clear()
    module.downgrade()
    assert module.POSTIMAGE in statements[0]
    assert module.PREIMAGE in statements[0]


def test_training_base_plan_uses_learning_vector_schema_hash_of_fixture() -> None:
    """Executor TRAINING_BASE phải ghim đúng schema hash vector học (fixture WP-06 ghim)."""
    from uuid import UUID

    from tests.fixtures.runtime_replay import build_h03_source_pinned_dataset_replay

    from trading_bot.application.backtest_replay_executor import (
        learning_feature_schema_hash_for_bundle,
    )
    from trading_bot.application.strategy_catalog import strategy_registry

    workspace = UUID("a1a50000-0000-4000-8000-000000000005")
    bundle, comparison = build_h03_source_pinned_dataset_replay(workspace)
    release = strategy_registry().get_release("h03-v1")
    expected = comparison.runs[0].sensitivity_run.manifest.feature_schema_hash
    assert (
        learning_feature_schema_hash_for_bundle(bundle, "h03-v1", release.content_hash) == expected
    )
