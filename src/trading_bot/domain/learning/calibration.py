"""Hợp đồng thuần toán cho lát fit nhiệt độ trên validation của scorer WP06."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

CLASS_LABELS = (-1, 0, 1)

_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")


class TemperatureCalibrationFailureCode(StrEnum):
    """Mã lỗi ổn định cho các trường hợp fit phải từ chối."""

    BOUNDARY_OPTIMUM = "boundary_optimum"
    INSUFFICIENT_SUPPORT = "insufficient_support"
    NON_CONVERGENCE = "non_convergence"
    NON_IDENTIFIABLE = "non_identifiable"
    NUMERICAL_FAILURE = "numerical_failure"
    VALIDATION_NLL_NOT_IMPROVED = "validation_nll_not_improved"


class TemperatureCalibrationError(ValueError):
    """Lỗi fail-closed khi dữ liệu hoặc nghiệm không đủ điều kiện."""

    def __init__(self, code: TemperatureCalibrationFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ValidationCalibrationExample:
    """Logit OVR và nhãn của một hàng đã được gateway xác nhận thuộc VALIDATION.

    Giá trị ``fold_role`` giúp type-checker và runtime từ chối nhầm nhãn TEST rõ ràng;
    nó không chứng minh quyền hay vai trò split. Caller phải chỉ tạo mẫu này từ các hàng
    VALIDATION đã được gateway kiểm tra quyền, manifest, fold và membership.
    """

    logits: tuple[float, float, float]
    label: int
    fold_role: Literal["validation"]

    def __post_init__(self) -> None:
        if type(self.fold_role) is not str or self.fold_role != "validation":
            raise ValueError("fold_role phải là VALIDATION.")
        if type(self.logits) is not tuple or len(self.logits) != len(CLASS_LABELS):
            raise ValueError("logits phải là tuple ba logit theo thứ tự lớp (-1, 0, 1).")
        normalized: list[float] = []
        for value in self.logits:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Mỗi logit phải là số hữu hạn.")
            try:
                number = float(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError("Mỗi logit phải là số hữu hạn.") from exc
            if not math.isfinite(number):
                raise ValueError("Mỗi logit phải là số hữu hạn.")
            normalized.append(number)
        object.__setattr__(self, "logits", tuple(normalized))
        if type(self.label) is not int or self.label not in CLASS_LABELS:
            raise ValueError("label phải là một trong các nhãn (-1, 0, 1).")


@dataclass(frozen=True, slots=True)
class TemperatureCalibrationPolicy:
    """Chính sách version hóa giới hạn phép fit nhiệt độ validation."""

    policy_version: str
    minimum_temperature: float
    maximum_temperature: float
    minimum_samples: int
    minimum_class_support: int
    relative_tolerance: float
    maximum_iterations: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_version, str)
            or _VERSION.fullmatch(self.policy_version) is None
        ):
            raise ValueError("policy_version không hợp lệ.")
        for name in ("minimum_temperature", "maximum_temperature", "relative_tolerance"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} phải là số hữu hạn.")
            try:
                number = float(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError(f"{name} phải là số hữu hạn.") from exc
            if not math.isfinite(number):
                raise ValueError(f"{name} phải là số hữu hạn.")
            object.__setattr__(self, name, number)
        if self.minimum_temperature <= 0.0:
            raise ValueError("minimum_temperature phải dương.")
        if self.minimum_temperature >= self.maximum_temperature:
            raise ValueError("minimum_temperature phải nhỏ hơn maximum_temperature.")
        if self.relative_tolerance <= 0.0 or self.relative_tolerance >= 1.0:
            raise ValueError("relative_tolerance phải nằm trong (0, 1).")
        if type(self.minimum_samples) is not int or self.minimum_samples < len(CLASS_LABELS):
            raise ValueError("minimum_samples phải đủ ít nhất một mẫu cho mỗi lớp.")
        if type(self.minimum_class_support) is not int or self.minimum_class_support < 1:
            raise ValueError("minimum_class_support phải là số nguyên dương.")
        if self.minimum_samples < len(CLASS_LABELS) * self.minimum_class_support:
            raise ValueError("minimum_samples không đủ cho minimum_class_support.")
        if type(self.maximum_iterations) is not int or self.maximum_iterations < 1:
            raise ValueError("maximum_iterations phải là số nguyên dương.")


@dataclass(frozen=True, slots=True)
class ValidationTemperatureFit:
    """Kết quả fit trên validation; không phải artifact scorer đã được phê duyệt."""

    policy_version: str
    temperature: float
    sample_count: int
    class_support: tuple[tuple[int, int], ...]
    validation_nll_before: float
    validation_nll_after: float
    iterations: int
    converged: Literal[True] = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_version, str)
            or _VERSION.fullmatch(self.policy_version) is None
        ):
            raise ValueError("policy_version không hợp lệ.")
        for name in ("temperature", "validation_nll_before", "validation_nll_after"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} phải là số hữu hạn.")
        if self.temperature <= 0.0:
            raise ValueError("temperature phải dương.")
        if type(self.sample_count) is not int or self.sample_count < 1:
            raise ValueError("sample_count phải là số nguyên dương.")
        if type(self.class_support) is not tuple or len(self.class_support) != len(CLASS_LABELS):
            raise ValueError("class_support phải theo đúng thứ tự lớp (-1, 0, 1).")
        if any(type(item) is not tuple or len(item) != 2 for item in self.class_support):
            raise ValueError("Mỗi phần tử class_support phải có nhãn và số đếm.")
        if any(type(label) is not int for label, _ in self.class_support):
            raise ValueError("class_support phải theo đúng thứ tự lớp (-1, 0, 1).")
        if tuple(label for label, _ in self.class_support) != CLASS_LABELS:
            raise ValueError("class_support phải theo đúng thứ tự lớp (-1, 0, 1).")
        if any(type(count) is not int or count < 1 for _, count in self.class_support):
            raise ValueError("class_support phải có số đếm dương cho mọi lớp.")
        if sum(count for _, count in self.class_support) != self.sample_count:
            raise ValueError("class_support không khớp sample_count.")
        if type(self.iterations) is not int or self.iterations < 1:
            raise ValueError("iterations phải là số nguyên dương.")
        if self.converged is not True:
            raise ValueError("Kết quả chỉ được tạo khi optimizer hội tụ.")


def fit_validation_temperature(
    examples: tuple[ValidationCalibrationExample, ...],
    policy: TemperatureCalibrationPolicy,
) -> ValidationTemperatureFit:
    """Fit T dương bằng NLL softmax đa lớp trên các mẫu VALIDATION được cấp.

    Hàm chỉ nhận một tuple bất biến có kiểu hàng validation. Caller vẫn phải lấy các
    hàng đó qua gateway đã xác minh role, rights, fold và membership; type này không
    thay thế receipt hoặc phân quyền. Hàm không phát artifact, score hay quyền activate.
    """

    if type(policy) is not TemperatureCalibrationPolicy:
        raise ValueError("policy phải là TemperatureCalibrationPolicy.")
    if type(examples) is not tuple:
        raise ValueError("examples phải là tuple bất biến.")
    if not examples or any(
        type(example) is not ValidationCalibrationExample for example in examples
    ):
        raise ValueError("examples phải chỉ chứa ValidationCalibrationExample.")

    support = tuple(
        (label, sum(example.label == label for example in examples)) for label in CLASS_LABELS
    )
    if len(examples) < policy.minimum_samples or any(
        count < policy.minimum_class_support for _, count in support
    ):
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.INSUFFICIENT_SUPPORT,
            "Validation chưa đủ tổng số mẫu hoặc support cho từng lớp.",
        )
    if all(example.logits[0] == example.logits[1] == example.logits[2] for example in examples):
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.NON_IDENTIFIABLE,
            "NLL validation không phụ thuộc vào nhiệt độ vì các logit mỗi hàng bằng nhau.",
        )

    # NLL theo beta=1/T là hàm lồi; đạo hàm đơn điệu nên có thể tìm nghiệm
    # nội bằng chia đôi xác định, không cần dependency optimizer bên ngoài.
    beta_low = 1.0 / policy.maximum_temperature
    beta_high = 1.0 / policy.minimum_temperature
    _, gradient_low = _mean_nll_and_beta_gradient(examples, beta_low)
    _, gradient_high = _mean_nll_and_beta_gradient(examples, beta_high)
    if gradient_low == 0.0 and gradient_high == 0.0:
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.NON_IDENTIFIABLE,
            "NLL validation không xác định được một nhiệt độ duy nhất.",
        )
    if gradient_low >= 0.0 or gradient_high <= 0.0:
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.BOUNDARY_OPTIMUM,
            "Nghiệm NLL validation nằm trên hoặc ngoài biên policy.",
        )

    converged = False
    iterations = 0
    for iteration in range(1, policy.maximum_iterations + 1):
        iterations = iteration
        beta_mid = beta_low + (beta_high - beta_low) / 2.0
        _, gradient_mid = _mean_nll_and_beta_gradient(examples, beta_mid)
        if gradient_mid < 0.0:
            beta_low = beta_mid
        else:
            beta_high = beta_mid
        if (beta_high - beta_low) <= policy.relative_tolerance * max(beta_low, beta_high):
            converged = True
            break
    if not converged:
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.NON_CONVERGENCE,
            "Optimizer chia đôi không hội tụ trong giới hạn policy.",
        )

    beta_fit = beta_low + (beta_high - beta_low) / 2.0
    temperature = 1.0 / beta_fit
    if not policy.minimum_temperature < temperature < policy.maximum_temperature:
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.BOUNDARY_OPTIMUM,
            "Nhiệt độ fit chạm biên policy sau làm tròn số.",
        )
    nll_after, _ = _mean_nll_and_beta_gradient(examples, beta_fit)
    nll_before, _ = _mean_nll_and_beta_gradient(examples, 1.0)
    return ValidationTemperatureFit(
        policy_version=policy.policy_version,
        temperature=temperature,
        sample_count=len(examples),
        class_support=support,
        validation_nll_before=nll_before,
        validation_nll_after=nll_after,
        iterations=iterations,
    )


def _mean_nll_and_beta_gradient(
    examples: tuple[ValidationCalibrationExample, ...], beta: float
) -> tuple[float, float]:
    losses: list[float] = []
    gradients: list[float] = []
    try:
        for example in examples:
            scaled = tuple(beta * logit for logit in example.logits)
            if not all(math.isfinite(value) for value in scaled):
                raise TemperatureCalibrationError(
                    TemperatureCalibrationFailureCode.NUMERICAL_FAILURE,
                    "Logit sau khi scale vượt miền số hữu hạn.",
                )
            maximum = max(scaled)
            log_partition = maximum + math.log(
                math.fsum(math.exp(value - maximum) for value in scaled)
            )
            label_index = CLASS_LABELS.index(example.label)
            losses.append(log_partition - scaled[label_index])
            probabilities = tuple(math.exp(value - log_partition) for value in scaled)
            expected_logit = math.fsum(
                probability * logit
                for probability, logit in zip(probabilities, example.logits, strict=True)
            )
            gradients.append(expected_logit - example.logits[label_index])
        mean_loss = math.fsum(losses) / len(examples)
        mean_gradient = math.fsum(gradients) / len(examples)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, TemperatureCalibrationError):
            raise
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.NUMERICAL_FAILURE,
            "Không thể tính NLL validation một cách hữu hạn.",
        ) from exc
    if not math.isfinite(mean_loss) or not math.isfinite(mean_gradient):
        raise TemperatureCalibrationError(
            TemperatureCalibrationFailureCode.NUMERICAL_FAILURE,
            "NLL hoặc đạo hàm validation không hữu hạn.",
        )
    return mean_loss, mean_gradient
