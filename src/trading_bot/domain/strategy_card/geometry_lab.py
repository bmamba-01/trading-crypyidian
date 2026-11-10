"""Phòng thí nghiệm hình học công thức và toán học tọa độ (Coordinate Lab Math Engine).

Module này cung cấp các công cụ khảo sát toán học:
- Quét lưới không gian tham số (Parameter Grid Sweep) và dựng ma trận bề mặt 2D/3D.
- Phát hiện điểm kỳ dị toán học (Formula Singularity): chia cho 0, NaN, vô cực (Infinity).
- Phân tích độ nhạy gradient sai phân hữu hạn để cảnh báo bề mặt răng cưa (Knife-edge Fragility).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from typing import Any


class CoordinateLabEngine:
    """Động cơ toán học khảo sát hình học công thức đa chiều và độ nhạy gradient."""

    def sweep_parameter_grid(
        self,
        formula_fn: Callable[..., float],
        bounds: dict[str, tuple[float, float, int]],
    ) -> dict[str, Any]:
        """Quét lưới không gian tham số, tạo lưới tọa độ và tính toán bề mặt/lát cắt.

        Args:
            formula_fn: Hàm công thức cần khảo sát.
            bounds: Ánh xạ tên tham số sang tuple (min_val, max_val, num_steps).

        Returns:
            Từ điển chứa param_names, grid_axes, evaluated_points, slices, surface_matrix.
        """
        param_names = list(bounds.keys())
        grid_axes: dict[str, list[float]] = {}

        for name in param_names:
            min_val, max_val, steps = bounds[name]
            if steps <= 1:
                grid_axes[name] = [float(min_val)]
            else:
                step_size = (max_val - min_val) / (steps - 1)
                grid_axes[name] = [min_val + i * step_size for i in range(steps)]

        axes_values = [grid_axes[name] for name in param_names]
        evaluated_points: list[dict[str, Any]] = []

        # Tạo tích Đề-các (Cartesian product) của tất cả các trục
        for combo in itertools.product(*axes_values):
            point_kwargs = dict(zip(param_names, combo, strict=True))
            try:
                output = float(formula_fn(**point_kwargs))
            except ZeroDivisionError, ValueError, OverflowError:
                output = float("nan")

            record = dict(point_kwargs)
            record["output"] = output
            evaluated_points.append(record)

        # Tính toán các lát cắt (Slices): cố định các biến khác ở mốc trung tâm
        slices: dict[str, list[dict[str, Any]]] = {}
        center_fixed: dict[str, float] = {
            name: grid_axes[name][len(grid_axes[name]) // 2] for name in param_names
        }

        for name in param_names:
            slice_points: list[dict[str, Any]] = []
            for val in grid_axes[name]:
                kwargs = dict(center_fixed)
                kwargs[name] = val
                try:
                    out = float(formula_fn(**kwargs))
                except ZeroDivisionError, ValueError, OverflowError:
                    out = float("nan")
                slice_record = dict(kwargs)
                slice_record["output"] = out
                slice_points.append(slice_record)
            slices[name] = slice_points

        # Tạo ma trận bề mặt 2D nếu có đúng 2 tham số
        surface_matrix: list[list[float]] | None = None
        if len(param_names) == 2:
            p1_name, p2_name = param_names[0], param_names[1]
            p1_vals = grid_axes[p1_name]
            p2_vals = grid_axes[p2_name]
            surface_matrix = []
            for v1 in p1_vals:
                row: list[float] = []
                for v2 in p2_vals:
                    try:
                        out = float(formula_fn(**{p1_name: v1, p2_name: v2}))
                    except ZeroDivisionError, ValueError, OverflowError:
                        out = float("nan")
                    row.append(out)
                surface_matrix.append(row)

        return {
            "param_names": param_names,
            "grid_axes": grid_axes,
            "evaluated_points": evaluated_points,
            "slices": slices,
            "surface_matrix": surface_matrix,
        }

    def detect_singularities(
        self,
        formula_fn: Callable[..., float],
        test_cases: list[dict[str, float]],
    ) -> list[dict[str, Any]]:
        """Quét tìm các điểm kỳ dị toán học: chia cho 0, NaN, vô cực hoặc lỗi miền giá trị.

        Args:
            formula_fn: Hàm công thức cần thẩm định.
            test_cases: Danh sách các bộ tham số đầu vào cần kiểm tra.

        Returns:
            Danh sách các dict chứa thông tin điểm kỳ dị cùng đề xuất safe default.
        """
        singularities: list[dict[str, Any]] = []

        for case in test_cases:
            try:
                res = formula_fn(**case)
                if math.isnan(res):
                    singularities.append(
                        {
                            "params": case,
                            "singularity_type": "NAN_VALUE",
                            "error_message": "Kết quả tính toán trả về NaN (Not a Number).",
                            "safe_default": 0.0,
                        }
                    )
                elif math.isinf(res):
                    singularities.append(
                        {
                            "params": case,
                            "singularity_type": "INFINITY_VALUE",
                            "error_message": "Kết quả tính toán bùng nổ vô cực (Infinity).",
                            "safe_default": 0.0,
                        }
                    )
            except ZeroDivisionError:
                singularities.append(
                    {
                        "params": case,
                        "singularity_type": "ZERO_DIVISION",
                        "error_message": "Lỗi chia cho 0 khi mẫu số bằng 0 (ví dụ High == Low).",
                        "safe_default": 0.0,
                    }
                )
            except ValueError as err:
                singularities.append(
                    {
                        "params": case,
                        "singularity_type": "MATH_DOMAIN_ERROR",
                        "error_message": f"Lỗi miền giá trị toán học: {err}",
                        "safe_default": 0.0,
                    }
                )
            except OverflowError:
                singularities.append(
                    {
                        "params": case,
                        "singularity_type": "OVERFLOW_ERROR",
                        "error_message": "Lỗi tràn số dấu chấm động (Floating-point overflow).",
                        "safe_default": 0.0,
                    }
                )
            except Exception as err:
                singularities.append(
                    {
                        "params": case,
                        "singularity_type": "UNKNOWN_ERROR",
                        "error_message": f"Lỗi không xác định: {err}",
                        "safe_default": 0.0,
                    }
                )

        return singularities

    def calculate_gradient_sensitivity(
        self,
        formula_fn: Callable[..., float],
        baseline_params: dict[str, float],
        epsilon: float = 1e-4,
    ) -> dict[str, float]:
        """Tính xấp xỉ đạo hàm riêng trung tâm (Central Finite Difference) cho từng tham số.

        dy/dxi ~ (f(xi + eps) - f(xi - eps)) / (2 * eps)

        Args:
            formula_fn: Hàm công thức cần tính đạo hàm.
            baseline_params: Điểm tham chiếu cần tính độ dốc.
            epsilon: Bước nhảy sai phân (mặc định 1e-4).

        Returns:
            Từ điển ánh xạ tên tham số sang đạo hàm riêng tương ứng.
        """
        gradients: dict[str, float] = {}

        for name in baseline_params:
            params_plus = dict(baseline_params)
            params_minus = dict(baseline_params)
            params_plus[name] += epsilon
            params_minus[name] -= epsilon

            f_plus = formula_fn(**params_plus)
            f_minus = formula_fn(**params_minus)

            grad = (f_plus - f_minus) / (2.0 * epsilon)
            gradients[name] = grad

        return gradients

    def evaluate_fragility(
        self,
        formula_fn: Callable[..., float],
        baseline_params: dict[str, float],
        gradient_threshold: float = 1000.0,
        epsilon: float = 1e-4,
    ) -> dict[str, Any]:
        """Đánh giá tính mỏng manh (Knife-Edge Fragility) của bề mặt công thức tại điểm tham chiếu.

        Args:
            formula_fn: Hàm công thức cần thẩm định.
            baseline_params: Điểm tham số cơ sở.
            gradient_threshold: Ngưỡng độ dốc tối đa cho phép trước khi cảnh báo răng cưa.
            epsilon: Bước nhảy sai phân.

        Returns:
            Từ điển chứa is_fragile, gradients, warnings, max_gradient.
        """
        gradients = self.calculate_gradient_sensitivity(formula_fn, baseline_params, epsilon)
        warnings: list[str] = []
        max_grad = 0.0

        for name, grad in gradients.items():
            abs_grad = abs(grad)
            if abs_grad > max_grad:
                max_grad = abs_grad

            if abs_grad >= gradient_threshold:
                warnings.append(
                    f"CẢNH BÁO BỀ MẶT NHẠY CẢM NGUY HIỂM (Knife-Edge Fragility): Tham số '{name}' "
                    f"có độ dốc gradient |df/dx| = {abs_grad:.2f} (>= {gradient_threshold:.2f}). "
                    f"Công thức có nguy cơ quá khớp cực cao (Overfitting)."
                )

        return {
            "is_fragile": len(warnings) > 0,
            "gradients": gradients,
            "warnings": warnings,
            "max_gradient": max_grad,
        }
