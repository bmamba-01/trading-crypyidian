"""Unit test CRY-51 / CRY-418 (SIG-6): nhãn TP1 trước SL, hiệu chuẩn xác suất và G7."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.strategy.signal_calibration import (
    RELIABILITY_BIN_COUNT,
    CalibrationMethod,
    CalibrationPolicy,
    SignalCalibrationSample,
    build_calibration_sample,
    compute_brier_score,
    compute_log_loss,
    compute_reliability_bins,
    evaluate_walk_forward_calibration,
    fit_isotonic_calibrator,
    fit_logistic_baseline,
    fit_temperature_calibrator,
    format_calibrated_confidence,
    label_tp1_before_sl,
)
from trading_bot.domain.strategy.signal_card import build_signal_card
from trading_bot.domain.strategy.signal_outcome import FirstTouch, SignalOutcome
from trading_bot.domain.strategy_card.orthogonality import AntiConsensusAnalyzer

BASE_TIME = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _make_outcome(first_touch: FirstTouch) -> SignalOutcome:
    return SignalOutcome(
        horizon_bars=4,
        horizon_end_at=BASE_TIME + timedelta(minutes=20),
        first_touch=first_touch,
        same_bar_conflict=False,
        tp1_hit=first_touch == FirstTouch.TP1,
        tp2_hit=False,
        invalidated=first_touch == FirstTouch.SL,
        bars_to_first_touch=None if first_touch == FirstTouch.NONE else 2,
        seconds_to_first_touch=None if first_touch == FirstTouch.NONE else 600,
        mfe_r=Decimal("1.2") if first_touch == FirstTouch.TP1 else Decimal("0.2"),
        mae_r=Decimal("-0.2") if first_touch == FirstTouch.TP1 else Decimal("-1.0"),
        close_r=Decimal("1.0") if first_touch == FirstTouch.TP1 else Decimal("-1.0"),
        outcome_r=Decimal("1.0") if first_touch == FirstTouch.TP1 else Decimal("-1.0"),
        label=1 if first_touch == FirstTouch.TP1 else -1,
        input_hash="a" * 64,
    )


def _synthetic_samples(count: int = 150) -> list[SignalCalibrationSample]:
    """Tạo chuỗi mẫu có tín hiệu phân tách rõ theo thời gian (deterministic)."""
    samples: list[SignalCalibrationSample] = []
    for i in range(count):
        # Tạo 2 đặc trưng: f1 mang tín hiệu mạnh, f2 nhiễu nhẹ.
        phase = (i % 10) / 9.0
        f1 = (phase - 0.5) * 4.0
        f2 = math.sin(i * 0.7) * 0.5
        # Xác suất thật theo hàm sigmoid của f1
        true_p = 1.0 / (1.0 + math.exp(-1.8 * f1))
        # Gán nhãn tất định theo ngưỡng xen kẽ ổn định
        tp1_before_sl = ((i * 37) % 100) / 100.0 < true_p
        samples.append(
            SignalCalibrationSample(
                sample_id=f"sig-{i:04d}",
                setup_key="H18",
                signal_at=BASE_TIME + timedelta(minutes=5 * i),
                features=(f1, f2),
                tp1_before_sl=tp1_before_sl,
            )
        )
    return samples


def _sample_candidate() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "direction": "long",
        "strategy_id": "H18",
        "strategy_release_id": "h18-v1",
        "reason_code": "H18_LONG_ENTRY",
        "feature_snapshot_id": "snap-001",
        "signal_at": BASE_TIME.isoformat(),
        "expires_at": (BASE_TIME + timedelta(minutes=15)).isoformat(),
        "reference_price": "68000",
        "atr_value": "200",
        "recipe": {"stop_atr_multiple": "1.5", "take_profit_atr_multiple": "3.0"},
    }


def test_tp1_before_sl_label_mapping() -> None:
    assert label_tp1_before_sl(_make_outcome(FirstTouch.TP1)) == 1
    assert label_tp1_before_sl(_make_outcome(FirstTouch.SL)) == 0
    assert label_tp1_before_sl(_make_outcome(FirstTouch.NONE)) == 0

    sample = build_calibration_sample(
        sample_id="s1",
        setup_key="H18",
        signal_at=BASE_TIME,
        features=(1.2, -0.4),
        outcome=_make_outcome(FirstTouch.TP1),
    )
    assert sample.tp1_before_sl is True
    assert sample.label == 1


def test_logistic_baseline_separates_signal_and_handles_constant_feature() -> None:
    samples = [
        SignalCalibrationSample(
            sample_id=f"pos-{i}",
            setup_key="H18",
            signal_at=BASE_TIME + timedelta(minutes=i),
            features=(2.0 + 0.1 * i, 5.0),  # Cột 2 là hằng số
            tp1_before_sl=True,
        )
        for i in range(15)
    ] + [
        SignalCalibrationSample(
            sample_id=f"neg-{i}",
            setup_key="H18",
            signal_at=BASE_TIME + timedelta(minutes=20 + i),
            features=(-2.0 - 0.1 * i, 5.0),
            tp1_before_sl=False,
        )
        for i in range(15)
    ]

    model = fit_logistic_baseline(samples)
    assert model.predict_proba((2.5, 5.0)) > 0.85
    assert model.predict_proba((-2.5, 5.0)) < 0.15


def test_isotonic_and_temperature_calibrators() -> None:
    # Isotonic PAV phải đảm bảo đơn điệu không giảm.
    raw_probs = [0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9]
    labels = [0, 1, 0, 0, 1, 0, 1, 1]
    iso = fit_isotonic_calibrator(raw_probs, labels)
    calibrated = [iso.calibrate_prob(p) for p in raw_probs]
    assert all(calibrated[i] <= calibrated[i + 1] + 1e-12 for i in range(len(calibrated) - 1))

    # Temperature scaling phải giảm NLL khi logits bị overconfident.
    logits = [6.0, -6.0, 5.0, -5.0, 4.0, -4.0]
    y_val = [1, 0, 0, 1, 1, 0]  # Có nhiễu nên logit +/-6 quá tự tin
    nll_before = compute_log_loss([1.0 / (1.0 + math.exp(-z)) for z in logits], y_val)
    temp_cal = fit_temperature_calibrator(logits, y_val)
    assert temp_cal.temperature > 1.0
    nll_after = compute_log_loss([temp_cal.calibrate_logit(z) for z in logits], y_val)
    assert nll_after < nll_before


def test_brier_log_loss_and_10_bin_reliability() -> None:
    probs = [0.05, 0.15, 0.25, 0.55, 0.85, 0.95]
    labels = [0, 0, 1, 1, 1, 1]

    brier = compute_brier_score(probs, labels)
    log_loss = compute_log_loss(probs, labels)
    bins, ece = compute_reliability_bins(probs, labels)

    assert 0.0 < brier < 0.25
    assert log_loss > 0.0
    assert len(bins) == RELIABILITY_BIN_COUNT == 10
    assert sum(b.sample_count for b in bins) == len(probs)
    assert bins[0].sample_count == 1
    assert bins[0].empirical_rate == 0.0
    assert bins[3].sample_count == 0
    assert bins[3].mean_predicted is None
    assert 0.0 <= ece <= 1.0


@pytest.mark.parametrize("method", [CalibrationMethod.ISOTONIC, CalibrationMethod.TEMPERATURE])
def test_walk_forward_calibration_qualifies_and_reports_10_bins(
    method: CalibrationMethod,
) -> None:
    samples = _synthetic_samples(180)
    # Đảo thứ tự đầu vào để chắc chắn hàm tự sắp theo thời gian, không nhìn trước.
    reversed_samples = list(reversed(samples))
    policy = CalibrationPolicy(
        min_samples=50,
        min_class_support=10,
        max_brier_score=0.25,
        max_ece=0.15,
    )

    report, artifact = evaluate_walk_forward_calibration(
        reversed_samples,
        setup_key="H18",
        method=method,
        policy=policy,
        n_folds=3,
    )

    assert artifact is not None
    assert report.is_qualified is True
    assert report.disqualification_reasons == ()
    assert report.sample_count >= 50
    assert len(report.reliability_bins) == 10
    assert report.brier_score is not None
    assert report.brier_score <= 0.25
    assert report.log_loss is not None
    assert report.log_loss > 0.0
    assert report.expected_calibration_error is not None

    prob = artifact.predict_probability((1.5, 0.0))
    assert 0.0 < prob < 1.0

    card = build_signal_card(
        _sample_candidate(),
        calibration_report=report,
        calibrated_probability=prob,
    )
    conf = card["confidence"]
    assert conf["calibrated"] is True
    assert "%" in conf["label"]
    assert f"n={report.sample_count}" in conf["label"]


def test_uncalibrated_card_never_displays_percentage() -> None:
    # 1. Không có báo cáo hiệu chuẩn -> "Chưa hiệu chuẩn", không có "%".
    card_no_report = build_signal_card(_sample_candidate())
    assert card_no_report["confidence"]["label"] == "Chưa hiệu chuẩn"
    assert "%" not in card_no_report["confidence"]["label"]

    # 2. Có báo cáo nhưng thiếu mẫu -> không đạt ngưỡng -> vẫn "Chưa hiệu chuẩn", không có "%".
    small_samples = _synthetic_samples(24)
    report, artifact = evaluate_walk_forward_calibration(
        small_samples,
        setup_key="H18",
        method=CalibrationMethod.ISOTONIC,
        policy=CalibrationPolicy(min_samples=50),
    )
    assert report.is_qualified is False
    assert "insufficient_samples" in report.disqualification_reasons
    prob = artifact.predict_probability((1.0, 0.0)) if artifact is not None else 0.7

    card_unqualified = build_signal_card(
        _sample_candidate(),
        calibration_report=report,
        calibrated_probability=prob,
    )
    assert card_unqualified["confidence"] == {
        "tier": "uncalibrated",
        "label": "Chưa hiệu chuẩn",
        "calibrated": False,
    }
    assert "%" not in card_unqualified["confidence"]["label"]

    # 3. Direct formatter cũng giữ nguyên quy tắc khi probability=None.
    formatted = format_calibrated_confidence(None, report)
    assert formatted["label"] == "Chưa hiệu chuẩn"
    assert "%" not in formatted["label"]


def test_anti_consensus_merges_two_highly_correlated_conditions() -> None:
    """Xác nhận G7: hai điều kiện tương quan cao bị phát hiện và gộp thành 1 nguồn độc lập."""
    n = 80
    cond_a = [math.sin(i * 0.25) for i in range(n)]
    # cond_b gần như tuyến tính với cond_a (r > 0.99)
    cond_b = [2.0 * v + 0.01 * math.cos(i) for i, v in enumerate(cond_a)]
    # cond_c độc lập (tần số khác)
    cond_c = [math.cos(i * 1.13) for i in range(n)]

    series = {"cond_a": cond_a, "cond_b": cond_b, "cond_c": cond_c}
    report = AntiConsensusAnalyzer().check_orthogonality(series, threshold=0.85)

    assert report.is_orthogonal is False
    assert len(report.high_correlation_pairs) == 1
    pair = report.high_correlation_pairs[0]
    assert {pair[0], pair[1]} == {"cond_a", "cond_b"}
    assert abs(pair[2]) >= 0.85

    # Đếm thành phần liên thông khi gộp điều kiện tương quan cao: 3 -> 2 nguồn độc lập.
    parent = {key: key for key in series}

    def _find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right, _r in report.high_correlation_pairs:
        rl, rr = _find(left), _find(right)
        if rl != rr:
            parent[rr] = rl

    independent_count = len({_find(key) for key in series})
    assert independent_count == 2
