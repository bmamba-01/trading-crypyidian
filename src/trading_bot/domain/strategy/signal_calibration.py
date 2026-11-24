"""Hiệu chuẩn xác suất P(chạm TP1 trước SL) cho tín hiệu giao dịch (SIG-6, CRY-418).

Quy ước ``sig6-calibration-v1``:
- **Nhãn**: ``1`` khi tín hiệu chạm ``TP1`` trước ``SL`` trong cửa sổ horizon (``FirstTouch.TP1``),
  ``0`` khi chạm ``SL`` trước hoặc hết horizon chưa chạm (``FirstTouch.SL`` / ``FirstTouch.NONE``).
- **Mô hình baseline**: hồi quy logistic thuần Python (chuẩn hóa z-score, phạt L2, tối ưu bằng
  gradient descent kèm backtracking line search).
- **Hiệu chuẩn**: hỗ trợ Isotonic Regression (thuật toán Pool Adjacent Violators — PAV) và
  Temperature Scaling (tối ưu golden-section trên ``log(T)``) theo từng fold walk-forward.
- **Đánh giá ngoài mẫu (walk-forward)**: tính ``Brier score``, ``log-loss``, ``ECE`` và biểu đồ
  độ tin cậy 10 bin (kèm số mẫu ``n`` từng bin).
- **Hiển thị an toàn**: chỉ hiển thị con số ``%`` kèm khoảng tin cậy Wilson 95% và ``n`` khi báo
  cáo đạt đủ ngưỡng mẫu và chất lượng hiệu chuẩn; ngược lại giữ nguyên ``"Chưa hiệu chuẩn"``
  (không có bất kỳ con số ``%`` nào). Không bật ``SHADOW``/``PAPER_GATE``.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from trading_bot.domain.strategy.signal_outcome import FirstTouch, SignalOutcome

SIGNAL_CALIBRATION_VERSION = "sig6-calibration-v1"
RELIABILITY_BIN_COUNT = 10
_PROB_EPS = 1e-6
_LOG_LOSS_EPS = 1e-15


class CalibrationMethod(StrEnum):
    """Phương pháp hiệu chuẩn xác suất hậu xử lý."""

    ISOTONIC = "isotonic"
    TEMPERATURE = "temperature"


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    """Ngưỡng tối thiểu để một báo cáo hiệu chuẩn được phép hiển thị ``%`` trên thẻ tín hiệu."""

    min_samples: int = 50
    min_class_support: int = 10
    max_brier_score: float = 0.25
    max_ece: float = 0.10
    confidence_level_z: float = 1.96
    l2_regularization: float = 1e-2
    max_iterations: int = 400
    tolerance: float = 1e-7

    def __post_init__(self) -> None:
        if self.min_samples < 6:
            raise ValueError("min_samples phải >= 6.")
        if self.min_class_support < 1:
            raise ValueError("min_class_support phải >= 1.")
        if not (0.0 < self.max_brier_score <= 1.0):
            raise ValueError("max_brier_score phải nằm trong (0, 1].")
        if not (0.0 < self.max_ece <= 1.0):
            raise ValueError("max_ece phải nằm trong (0, 1].")
        if self.confidence_level_z <= 0.0:
            raise ValueError("confidence_level_z phải dương.")


@dataclass(frozen=True, slots=True)
class SignalCalibrationSample:
    """Một mẫu huấn luyện/đánh giá hiệu chuẩn gắn nhãn P(chạm TP1 trước SL)."""

    sample_id: str
    setup_key: str
    signal_at: datetime
    features: tuple[float, ...]
    tp1_before_sl: bool

    def __post_init__(self) -> None:
        if self.signal_at.tzinfo is None or self.signal_at.utcoffset() is None:
            raise ValueError("signal_at phải có múi giờ.")
        if not self.features:
            raise ValueError("features không được rỗng.")
        normalized: list[float] = []
        for value in self.features:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Mỗi phần tử features phải là số hữu hạn.")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Mỗi phần tử features phải là số hữu hạn.")
            normalized.append(number)
        object.__setattr__(self, "features", tuple(normalized))

    @property
    def label(self) -> int:
        return 1 if self.tp1_before_sl else 0


def label_tp1_before_sl(outcome: SignalOutcome) -> int:
    """Trả về ``1`` nếu tín hiệu chạm TP1 trước SL trong horizon, ngược lại ``0``."""
    return 1 if outcome.first_touch == FirstTouch.TP1 else 0


def build_calibration_sample(
    *,
    sample_id: str,
    setup_key: str,
    signal_at: datetime,
    features: Sequence[float],
    outcome: SignalOutcome,
) -> SignalCalibrationSample:
    """Tạo mẫu hiệu chuẩn từ một ``SignalOutcome`` đã chấm."""
    return SignalCalibrationSample(
        sample_id=sample_id,
        setup_key=setup_key,
        signal_at=signal_at,
        features=tuple(float(v) for v in features),
        tp1_before_sl=label_tp1_before_sl(outcome) == 1,
    )


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_pos = math.exp(z)
    return exp_pos / (1.0 + exp_pos)


def _clamp_prob(prob: float, eps: float = _PROB_EPS) -> float:
    return max(eps, min(1.0 - eps, prob))


def _prob_to_logit(prob: float) -> float:
    p = _clamp_prob(prob)
    return math.log(p / (1.0 - p))


@dataclass(frozen=True, slots=True)
class LogisticBaselineModel:
    """Mô hình hồi quy logistic nhị phân (đã chuẩn hóa đặc trưng theo z-score)."""

    weights: tuple[float, ...]
    bias: float
    feature_means: tuple[float, ...]
    feature_stds: tuple[float, ...]

    def predict_logit(self, features: Sequence[float]) -> float:
        if len(features) != len(self.weights):
            raise ValueError("Số lượng đặc trưng không khớp với mô hình logistic.")
        total = self.bias
        for value, weight, mean, std in zip(
            features, self.weights, self.feature_means, self.feature_stds, strict=True
        ):
            total += weight * ((float(value) - mean) / std)
        return total

    def predict_proba(self, features: Sequence[float]) -> float:
        return _clamp_prob(_sigmoid(self.predict_logit(features)))


def fit_logistic_baseline(
    samples: Sequence[SignalCalibrationSample],
    *,
    l2_regularization: float = 1e-2,
    max_iterations: int = 400,
    tolerance: float = 1e-7,
) -> LogisticBaselineModel:
    """Huấn luyện mô hình hồi quy logistic nhị phân với phạt L2 và backtracking line search."""
    if not samples:
        raise ValueError("Không thể huấn luyện logistic trên tập rỗng.")
    n_samples = len(samples)
    n_features = len(samples[0].features)
    if any(len(s.features) != n_features for s in samples):
        raise ValueError("Tất cả mẫu phải có cùng số chiều đặc trưng.")

    # Chuẩn hóa z-score trên tập train.
    means: list[float] = []
    stds: list[float] = []
    for j in range(n_features):
        col = [s.features[j] for s in samples]
        mean = math.fsum(col) / n_samples
        var = math.fsum((v - mean) ** 2 for v in col) / n_samples
        std = math.sqrt(var)
        means.append(mean)
        stds.append(std if std > 1e-12 else 1.0)

    x_norm = [
        tuple((s.features[j] - means[j]) / stds[j] for j in range(n_features)) for s in samples
    ]
    labels = [s.label for s in samples]

    weights = [0.0] * n_features
    bias = 0.0

    def _loss_and_grad(w: list[float], b: float) -> tuple[float, list[float], float]:
        nll = 0.0
        grad_w = [l2_regularization * wj for wj in w]
        grad_b = 0.0
        for row, y in zip(x_norm, labels, strict=True):
            logit = b + math.fsum(wj * xj for wj, xj in zip(w, row, strict=True))
            p = _clamp_prob(_sigmoid(logit), _LOG_LOSS_EPS)
            nll -= y * math.log(p) + (1 - y) * math.log(1.0 - p)
            diff = p - y
            grad_b += diff
            for j, xj in enumerate(row):
                grad_w[j] += diff * xj
        reg = 0.5 * l2_regularization * math.fsum(wj * wj for wj in w)
        inv_n = 1.0 / n_samples
        return nll * inv_n + reg, [g * inv_n for g in grad_w], grad_b * inv_n

    # Khởi tạo grad_w không nhân inv_n cho phần reg ở trên; chỉnh lại chính xác:
    def _objective(w: list[float], b: float) -> tuple[float, list[float], float]:
        nll = 0.0
        grad_w = [0.0] * n_features
        grad_b = 0.0
        for row, y in zip(x_norm, labels, strict=True):
            logit = b + math.fsum(wj * xj for wj, xj in zip(w, row, strict=True))
            p = _clamp_prob(_sigmoid(logit), _LOG_LOSS_EPS)
            nll -= y * math.log(p) + (1 - y) * math.log(1.0 - p)
            diff = p - y
            grad_b += diff
            for j, xj in enumerate(row):
                grad_w[j] += diff * xj
        inv_n = 1.0 / n_samples
        loss = nll * inv_n + 0.5 * l2_regularization * math.fsum(wj * wj for wj in w)
        full_grad_w = [grad_w[j] * inv_n + l2_regularization * w[j] for j in range(n_features)]
        return loss, full_grad_w, grad_b * inv_n

    del _loss_and_grad
    loss, grad_w, grad_b = _objective(weights, bias)

    for _ in range(max_iterations):
        grad_norm_sq = math.fsum(g * g for g in grad_w) + grad_b * grad_b
        max_grad = max([abs(grad_b), *(abs(g) for g in grad_w)])
        if max_grad < tolerance or grad_norm_sq < tolerance * tolerance:
            break

        step = 1.0
        accepted = False
        for _ in range(25):
            cand_w = [w - step * g for w, g in zip(weights, grad_w, strict=True)]
            cand_b = bias - step * grad_b
            cand_loss, cand_gw, cand_gb = _objective(cand_w, cand_b)
            if cand_loss <= loss - 1e-4 * step * grad_norm_sq:
                weights = cand_w
                bias = cand_b
                improved = abs(loss - cand_loss) < tolerance
                loss, grad_w, grad_b = cand_loss, cand_gw, cand_gb
                accepted = True
                if improved:
                    return LogisticBaselineModel(
                        weights=tuple(weights),
                        bias=bias,
                        feature_means=tuple(means),
                        feature_stds=tuple(stds),
                    )
                break
            step *= 0.5
        if not accepted:
            break

    return LogisticBaselineModel(
        weights=tuple(weights),
        bias=bias,
        feature_means=tuple(means),
        feature_stds=tuple(stds),
    )


@dataclass(frozen=True, slots=True)
class TemperatureCalibrator:
    """Bộ hiệu chuẩn bằng nhiệt độ cho mô hình nhị phân: ``p = sigmoid(logit / T)``."""

    temperature: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.temperature) or self.temperature <= 0.0:
            raise ValueError("temperature phải dương và hữu hạn.")

    def calibrate_logit(self, logit: float) -> float:
        return _clamp_prob(_sigmoid(logit / self.temperature))

    def calibrate_prob(self, raw_prob: float) -> float:
        return self.calibrate_logit(_prob_to_logit(raw_prob))


def fit_temperature_calibrator(
    logits: Sequence[float],
    labels: Sequence[int],
    *,
    min_temperature: float = 0.05,
    max_temperature: float = 20.0,
    iterations: int = 60,
) -> TemperatureCalibrator:
    """Tìm nhiệt độ ``T`` tối thiểu hóa NLL trên tập validation bằng golden-section search."""
    if not logits or len(logits) != len(labels):
        raise ValueError("logits và labels phải không rỗng và có cùng độ dài.")
    if min_temperature <= 0.0 or min_temperature >= max_temperature:
        raise ValueError("Khoảng nhiệt độ không hợp lệ.")

    def _nll(log_t: float) -> float:
        temp = math.exp(log_t)
        total = 0.0
        for logit, y in zip(logits, labels, strict=True):
            p = _clamp_prob(_sigmoid(logit / temp), _LOG_LOSS_EPS)
            total -= y * math.log(p) + (1 - y) * math.log(1.0 - p)
        return total / len(logits)

    phi = (math.sqrt(5.0) - 1.0) / 2.0
    lo = math.log(min_temperature)
    hi = math.log(max_temperature)
    c = hi - phi * (hi - lo)
    d = lo + phi * (hi - lo)
    fc = _nll(c)
    fd = _nll(d)

    for _ in range(iterations):
        if fc < fd:
            hi = d
            d = c
            fd = fc
            c = hi - phi * (hi - lo)
            fc = _nll(c)
        else:
            lo = c
            c = d
            fc = fd
            d = lo + phi * (hi - lo)
            fd = _nll(d)

    best_log_t = 0.5 * (lo + hi)
    return TemperatureCalibrator(temperature=math.exp(best_log_t))


@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    """Bộ hiệu chuẩn đơn điệu không giảm bằng thuật toán Pool Adjacent Violators (PAV)."""

    anchors: tuple[float, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.anchors or len(self.anchors) != len(self.values):
            raise ValueError("anchors và values phải không rỗng và có cùng độ dài.")

    def calibrate_prob(self, raw_prob: float) -> float:
        p = _clamp_prob(float(raw_prob))
        if p <= self.anchors[0]:
            return _clamp_prob(self.values[0])
        if p >= self.anchors[-1]:
            return _clamp_prob(self.values[-1])
        idx = bisect_right(self.anchors, p)
        left_x = self.anchors[idx - 1]
        right_x = self.anchors[idx]
        left_y = self.values[idx - 1]
        right_y = self.values[idx]
        if right_x <= left_x:
            return _clamp_prob(left_y)
        ratio = (p - left_x) / (right_x - left_x)
        return _clamp_prob(left_y + ratio * (right_y - left_y))


def fit_isotonic_calibrator(
    raw_probs: Sequence[float],
    labels: Sequence[int],
) -> IsotonicCalibrator:
    """Fit hàm hiệu chuẩn isotonic đơn điệu không giảm bằng thuật toán PAV."""
    if not raw_probs or len(raw_probs) != len(labels):
        raise ValueError("raw_probs và labels phải không rỗng và có cùng độ dài.")

    # Gom các điểm trùng raw_prob trước khi chạy PAV.
    paired = sorted(zip(raw_probs, labels, strict=True), key=lambda item: item[0])
    # Mỗi block: [anchor_sum, count, y_sum]
    blocks: list[list[float]] = []
    for prob, y in paired:
        if blocks and abs(prob - (blocks[-1][0] / blocks[-1][1])) <= 1e-12:
            blocks[-1][0] += prob
            blocks[-1][1] += 1.0
            blocks[-1][2] += float(y)
        else:
            blocks.append([float(prob), 1.0, float(y)])
        while len(blocks) >= 2:
            prev_mean = blocks[-2][2] / blocks[-2][1]
            curr_mean = blocks[-1][2] / blocks[-1][1]
            if prev_mean <= curr_mean:
                break
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
            blocks[-1][2] += last[2]

    anchors = tuple(block[0] / block[1] for block in blocks)
    values = tuple(_clamp_prob(block[2] / block[1]) for block in blocks)
    return IsotonicCalibrator(anchors=anchors, values=values)


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """Một bin trong biểu đồ độ tin cậy (reliability diagram) 10 bin."""

    bin_index: int
    lower_bound: float
    upper_bound: float
    sample_count: int
    mean_predicted: float | None
    empirical_rate: float | None


def compute_brier_score(probs: Sequence[float], labels: Sequence[int]) -> float:
    """Tính Brier score nhị phân: trung bình ``(p - y)^2``."""
    if not probs or len(probs) != len(labels):
        raise ValueError("probs và labels phải không rỗng và có cùng độ dài.")
    return math.fsum((p - y) ** 2 for p, y in zip(probs, labels, strict=True)) / len(probs)


def compute_log_loss(probs: Sequence[float], labels: Sequence[int]) -> float:
    """Tính log-loss (binary cross-entropy) ngoài mẫu."""
    if not probs or len(probs) != len(labels):
        raise ValueError("probs và labels phải không rỗng và có cùng độ dài.")
    total = 0.0
    for prob, y in zip(probs, labels, strict=True):
        p = _clamp_prob(prob, _LOG_LOSS_EPS)
        total -= y * math.log(p) + (1 - y) * math.log(1.0 - p)
    return total / len(probs)


def compute_reliability_bins(
    probs: Sequence[float],
    labels: Sequence[int],
    *,
    bin_count: int = RELIABILITY_BIN_COUNT,
) -> tuple[tuple[ReliabilityBin, ...], float]:
    """Chia ``bin_count`` (mặc định 10) bin đều nhau trên [0, 1] và tính ECE."""
    if not probs or len(probs) != len(labels):
        raise ValueError("probs và labels phải không rỗng và có cùng độ dài.")
    if bin_count < 2:
        raise ValueError("bin_count phải >= 2.")

    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bin_count)]
    for prob, y in zip(probs, labels, strict=True):
        p = max(0.0, min(1.0, float(prob)))
        idx = min(int(p * bin_count), bin_count - 1)
        buckets[idx].append((p, int(y)))

    total = len(probs)
    ece = 0.0
    bins: list[ReliabilityBin] = []
    for idx, items in enumerate(buckets):
        lower = round(idx / bin_count, 4)
        upper = round((idx + 1) / bin_count, 4)
        if not items:
            bins.append(
                ReliabilityBin(
                    bin_index=idx,
                    lower_bound=lower,
                    upper_bound=upper,
                    sample_count=0,
                    mean_predicted=None,
                    empirical_rate=None,
                )
            )
            continue
        mean_pred = math.fsum(p for p, _ in items) / len(items)
        emp_rate = math.fsum(y for _, y in items) / len(items)
        ece += (len(items) / total) * abs(mean_pred - emp_rate)
        bins.append(
            ReliabilityBin(
                bin_index=idx,
                lower_bound=lower,
                upper_bound=upper,
                sample_count=len(items),
                mean_predicted=mean_pred,
                empirical_rate=emp_rate,
            )
        )
    return tuple(bins), ece


@dataclass(frozen=True, slots=True)
class SignalCalibrationReport:
    """Báo cáo đánh giá walk-forward của mô hình hiệu chuẩn cho một setup."""

    setup_key: str
    method: CalibrationMethod
    sample_count: int
    positive_count: int
    negative_count: int
    brier_score: float | None
    prior_brier_score: float | None
    log_loss: float | None
    expected_calibration_error: float | None
    reliability_bins: tuple[ReliabilityBin, ...]
    is_qualified: bool
    disqualification_reasons: tuple[str, ...]
    version: str = SIGNAL_CALIBRATION_VERSION


@dataclass(frozen=True, slots=True)
class SignalCalibratorArtifact:
    """Mô hình logistic + bộ hiệu chuẩn đã fit để suy luận cho tín hiệu mới."""

    setup_key: str
    method: CalibrationMethod
    baseline_model: LogisticBaselineModel
    calibrator: IsotonicCalibrator | TemperatureCalibrator
    report: SignalCalibrationReport

    def predict_probability(self, features: Sequence[float]) -> float:
        logit = self.baseline_model.predict_logit(features)
        if isinstance(self.calibrator, TemperatureCalibrator):
            return self.calibrator.calibrate_logit(logit)
        return self.calibrator.calibrate_prob(_sigmoid(logit))


def _fit_calibrator(
    method: CalibrationMethod,
    logits: Sequence[float],
    labels: Sequence[int],
) -> IsotonicCalibrator | TemperatureCalibrator:
    if method == CalibrationMethod.TEMPERATURE:
        return fit_temperature_calibrator(logits, labels)
    return fit_isotonic_calibrator([_sigmoid(z) for z in logits], labels)


def _apply_calibrator(
    calibrator: IsotonicCalibrator | TemperatureCalibrator,
    logit: float,
) -> float:
    if isinstance(calibrator, TemperatureCalibrator):
        return calibrator.calibrate_logit(logit)
    return calibrator.calibrate_prob(_sigmoid(logit))


def evaluate_walk_forward_calibration(
    samples: Sequence[SignalCalibrationSample],
    *,
    setup_key: str,
    method: CalibrationMethod = CalibrationMethod.ISOTONIC,
    policy: CalibrationPolicy | None = None,
    n_folds: int = 3,
) -> tuple[SignalCalibrationReport, SignalCalibratorArtifact | None]:
    """Đánh giá walk-forward không nhìn trước và trả về báo cáo + artifact suy luận."""
    resolved_policy = policy if policy is not None else CalibrationPolicy()
    ordered = sorted(samples, key=lambda item: (item.signal_at, item.sample_id))
    total_all = len(ordered)
    if total_all < 6 or n_folds < 1:
        return (
            SignalCalibrationReport(
                setup_key=setup_key,
                method=method,
                sample_count=0,
                positive_count=0,
                negative_count=0,
                brier_score=None,
                prior_brier_score=None,
                log_loss=None,
                expected_calibration_error=None,
                reliability_bins=(),
                is_qualified=False,
                disqualification_reasons=("insufficient_samples",),
            ),
            None,
        )

    # Chia thành (n_folds + 2) khối thời gian bằng nhau:
    # khối đầu làm train khởi tạo, mỗi fold dùng [0..k] train, [k] val, [k+1] test.
    total_chunks = n_folds + 2
    chunk_size = total_all // total_chunks
    if chunk_size < 2:
        return (
            SignalCalibrationReport(
                setup_key=setup_key,
                method=method,
                sample_count=0,
                positive_count=0,
                negative_count=0,
                brier_score=None,
                prior_brier_score=None,
                log_loss=None,
                expected_calibration_error=None,
                reliability_bins=(),
                is_qualified=False,
                disqualification_reasons=("insufficient_samples",),
            ),
            None,
        )

    oos_probs: list[float] = []
    oos_labels: list[int] = []

    for fold in range(n_folds):
        train_end = chunk_size * (fold + 1)
        val_end = chunk_size * (fold + 2)
        test_end = total_all if fold == n_folds - 1 else chunk_size * (fold + 3)

        train_slice = ordered[:train_end]
        val_slice = ordered[train_end:val_end]
        test_slice = ordered[val_end:test_end]
        if not train_slice or not val_slice or not test_slice:
            continue

        model = fit_logistic_baseline(
            train_slice,
            l2_regularization=resolved_policy.l2_regularization,
            max_iterations=resolved_policy.max_iterations,
            tolerance=resolved_policy.tolerance,
        )
        val_logits = [model.predict_logit(s.features) for s in val_slice]
        val_labels = [s.label for s in val_slice]
        calibrator = _fit_calibrator(method, val_logits, val_labels)

        for sample in test_slice:
            oos_probs.append(_apply_calibrator(calibrator, model.predict_logit(sample.features)))
            oos_labels.append(sample.label)

    eval_count = len(oos_labels)
    pos_count = sum(oos_labels)
    neg_count = eval_count - pos_count
    if eval_count == 0:
        return (
            SignalCalibrationReport(
                setup_key=setup_key,
                method=method,
                sample_count=0,
                positive_count=0,
                negative_count=0,
                brier_score=None,
                prior_brier_score=None,
                log_loss=None,
                expected_calibration_error=None,
                reliability_bins=(),
                is_qualified=False,
                disqualification_reasons=("insufficient_samples",),
            ),
            None,
        )

    brier = compute_brier_score(oos_probs, oos_labels)
    prior_rate = pos_count / eval_count
    prior_brier = compute_brier_score([prior_rate] * eval_count, oos_labels)
    log_loss = compute_log_loss(oos_probs, oos_labels)
    bins, ece = compute_reliability_bins(oos_probs, oos_labels, bin_count=RELIABILITY_BIN_COUNT)

    reasons: list[str] = []
    if eval_count < resolved_policy.min_samples:
        reasons.append("insufficient_samples")
    if (
        pos_count < resolved_policy.min_class_support
        or neg_count < resolved_policy.min_class_support
    ):
        reasons.append("insufficient_class_support")
    if brier > resolved_policy.max_brier_score:
        reasons.append("brier_above_threshold")
    if ece > resolved_policy.max_ece:
        reasons.append("ece_above_threshold")

    report = SignalCalibrationReport(
        setup_key=setup_key,
        method=method,
        sample_count=eval_count,
        positive_count=pos_count,
        negative_count=neg_count,
        brier_score=brier,
        prior_brier_score=prior_brier,
        log_loss=log_loss,
        expected_calibration_error=ece,
        reliability_bins=bins,
        is_qualified=not reasons,
        disqualification_reasons=tuple(reasons),
    )

    # Fit artifact cuối cùng trên 70% đầu (train) + 30% cuối (calibration) theo thứ tự thời gian.
    split_idx = max(2, int(total_all * 0.7))
    if split_idx >= total_all - 1:
        split_idx = total_all - 1
    final_train = ordered[:split_idx]
    final_val = ordered[split_idx:]
    final_model = fit_logistic_baseline(
        final_train,
        l2_regularization=resolved_policy.l2_regularization,
        max_iterations=resolved_policy.max_iterations,
        tolerance=resolved_policy.tolerance,
    )
    final_cal = _fit_calibrator(
        method,
        [final_model.predict_logit(s.features) for s in final_val],
        [s.label for s in final_val],
    )
    artifact = SignalCalibratorArtifact(
        setup_key=setup_key,
        method=method,
        baseline_model=final_model,
        calibrator=final_cal,
        report=report,
    )
    return report, artifact


def wilson_confidence_interval(
    probability: float,
    sample_count: int,
    *,
    z: float = 1.96,
) -> tuple[float, float]:
    """Khoảng tin cậy Wilson cho xác suất hiệu chuẩn ở mức tin cậy ``z``."""
    if sample_count < 1:
        raise ValueError("sample_count phải >= 1.")
    p = max(0.0, min(1.0, float(probability)))
    z2 = z * z
    denom = 1.0 + z2 / sample_count
    center = (p + z2 / (2.0 * sample_count)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * sample_count)) / sample_count)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def format_calibrated_confidence(
    probability: float | None,
    report: SignalCalibrationReport | None,
    *,
    z: float = 1.96,
) -> dict[str, Any]:
    """Định dạng độ tin cậy cho thẻ tín hiệu: chỉ hiện ``%`` khi báo cáo đạt ngưỡng hiệu chuẩn."""
    if (
        report is None
        or not report.is_qualified
        or probability is None
        or not math.isfinite(probability)
    ):
        return {
            "tier": "uncalibrated",
            "label": "Chưa hiệu chuẩn",
            "calibrated": False,
        }

    p = max(0.0, min(1.0, float(probability)))
    ci_low, ci_high = wilson_confidence_interval(p, report.sample_count, z=z)
    pct = round(p * 100.0, 1)
    ci_low_pct = round(ci_low * 100.0, 1)
    ci_high_pct = round(ci_high * 100.0, 1)

    if p >= 0.65:
        tier = "high"
    elif p >= 0.45:
        tier = "medium"
    else:
        tier = "low"

    label = (
        f"{pct:.1f}% (KTC 95%: {ci_low_pct:.1f}%–{ci_high_pct:.1f}%, "
        f"n={report.sample_count})"
    )
    return {
        "tier": tier,
        "label": label,
        "calibrated": True,
        "probability": p,
        "probability_pct": pct,
        "ci_low_pct": ci_low_pct,
        "ci_high_pct": ci_high_pct,
        "sample_count": report.sample_count,
        "brier_score": round(report.brier_score, 4) if report.brier_score is not None else None,
        "method": report.method.value,
    }


__all__ = [
    "RELIABILITY_BIN_COUNT",
    "SIGNAL_CALIBRATION_VERSION",
    "CalibrationMethod",
    "CalibrationPolicy",
    "IsotonicCalibrator",
    "LogisticBaselineModel",
    "ReliabilityBin",
    "SignalCalibrationReport",
    "SignalCalibrationSample",
    "SignalCalibratorArtifact",
    "TemperatureCalibrator",
    "build_calibration_sample",
    "compute_brier_score",
    "compute_log_loss",
    "compute_reliability_bins",
    "evaluate_walk_forward_calibration",
    "fit_isotonic_calibrator",
    "fit_logistic_baseline",
    "fit_temperature_calibrator",
    "format_calibrated_confidence",
    "label_tp1_before_sl",
    "wilson_confidence_interval",
]
