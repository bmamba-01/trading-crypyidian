"""Bộ phân tích tính trực giao và phát hiện ảo giác đồng thuận (Anti-Consensus Illusion).

Module này đo đạc ma trận tương quan Pearson giữa các chỉ báo đầu ra và phát hiện
các chỉ báo có độ tương quan cao (|r| >= threshold), ngăn chặn việc cộng dồn
các tín hiệu phụ thuộc cùng một dữ liệu giá (Consensus Illusion / Multicollinearity).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def calculate_correlation_matrix(
    series_dict: dict[str, list[float]],
) -> dict[tuple[str, str], float]:
    """Tính ma trận hệ số tương quan Pearson giữa tất cả các cặp chuỗi đầu ra.

    Args:
        series_dict: Từ điển chứa các chuỗi số thực float theo tên chỉ báo.

    Returns:
        Từ điển ánh xạ (node_a, node_b) sang hệ số tương quan Pearson r trong [-1.0, 1.0].

    Raises:
        ValueError: Khi các chuỗi trong từ điển không có cùng độ dài.
    """
    if not series_dict:
        return {}

    keys = list(series_dict.keys())
    first_length = len(series_dict[keys[0]])

    for k in keys[1:]:
        if len(series_dict[k]) != first_length:
            raise ValueError(
                f"Tất cả các chuỗi dữ liệu phải có cùng độ dài để tính ma trận tương quan. "
                f"Chuỗi '{keys[0]}' có {first_length} phần tử, nhưng '{k}' "
                f"có {len(series_dict[k])}."
            )

    matrix: dict[tuple[str, str], float] = {}

    # Nếu độ dài chuỗi < 2 thì không đủ mẫu để tính phương sai
    if first_length < 2:
        for k1 in keys:
            for k2 in keys:
                matrix[(k1, k2)] = 1.0 if k1 == k2 else 0.0
        return matrix

    # Tiền tính trung bình và tổng bình phương sai lệch cho từng chuỗi
    stats: dict[str, tuple[float, float, list[float]]] = {}
    for k in keys:
        series = series_dict[k]
        mean_val = sum(series) / first_length
        diffs = [x - mean_val for x in series]
        sum_sq = sum(d * d for d in diffs)
        stats[k] = (mean_val, sum_sq, diffs)

    # Tính toán hệ số Pearson cho từng cặp
    for i, k1 in enumerate(keys):
        _, sum_sq1, diffs1 = stats[k1]
        matrix[(k1, k1)] = 1.0

        for j in range(i + 1, len(keys)):
            k2 = keys[j]
            _, sum_sq2, diffs2 = stats[k2]

            # Xử lý trường hợp chuỗi hằng số (phương sai xấp xỉ 0)
            if sum_sq1 < 1e-12 or sum_sq2 < 1e-12:
                r = 0.0
            else:
                cov = sum(d1 * d2 for d1, d2 in zip(diffs1, diffs2, strict=True))
                denom = math.sqrt(sum_sq1 * sum_sq2)
                r = cov / denom if denom > 0.0 else 0.0
                # Kẹp giá trị trong khoảng [-1.0, 1.0] để khử sai số dấu chấm động
                r = max(-1.0, min(1.0, r))

            matrix[(k1, k2)] = r
            matrix[(k2, k1)] = r

    return matrix


@dataclass(frozen=True)
class OrthogonalityReport:
    """Báo cáo đánh giá tính trực giao và đa dạng hóa tín hiệu."""

    is_orthogonal: bool
    high_correlation_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    correlation_matrix: dict[tuple[str, str], float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    max_correlation: float = 0.0


class AntiConsensusAnalyzer:
    """Bộ phân tích phát hiện ảo giác đồng thuận và vi phạm tính trực giao của tín hiệu."""

    def analyze_signals(
        self,
        indicator_outputs: dict[str, list[float]],
        threshold: float = 0.85,
    ) -> list[str]:
        """Phát hiện các cặp chỉ báo có tương quan cao (|r| >= threshold) và xuất cảnh báo đỏ.

        Args:
            indicator_outputs: Từ điển tên node chỉ báo và chuỗi giá trị đầu ra tương ứng.
            threshold: Ngưỡng tương quan tối đa cho phép (mặc định 0.85).

        Returns:
            Danh sách các chuỗi thông báo cảnh báo ảo giác đồng thuận.
        """
        report = self.check_orthogonality(indicator_outputs, threshold=threshold)
        return report.warnings

    def check_orthogonality(
        self,
        indicator_outputs: dict[str, list[float]],
        threshold: float = 0.85,
    ) -> OrthogonalityReport:
        """Phân tích tính trực giao toàn diện giữa các chuỗi tín hiệu chỉ báo.

        Args:
            indicator_outputs: Từ điển chuỗi giá trị float của các chỉ báo.
            threshold: Ngưỡng phát hiện tương quan cao.

        Returns:
            Đối tượng OrthogonalityReport chứa thông tin chi tiết và danh sách cảnh báo.
        """
        if not indicator_outputs:
            return OrthogonalityReport(is_orthogonal=True)

        matrix = calculate_correlation_matrix(indicator_outputs)
        keys = sorted(indicator_outputs.keys())
        high_corr_pairs: list[tuple[str, str, float]] = []
        warnings: list[str] = []
        max_abs_r = 0.0

        for i, k1 in enumerate(keys):
            for j in range(i + 1, len(keys)):
                k2 = keys[j]
                r = matrix.get((k1, k2), 0.0)
                abs_r = abs(r)
                if abs_r > max_abs_r:
                    max_abs_r = abs_r

                if abs_r >= threshold:
                    high_corr_pairs.append((k1, k2, r))
                    warnings.append(
                        f"CẢNH BÁO ẢO GIÁC ĐỒNG THUẬN: Chỉ báo '{k1}' và '{k2}' "
                        f"có hệ số tương quan r = {r:.2f} (>= {threshold:.2f}). "
                        f"Cả hai có thể đang đo cùng một biến thể giá đóng cửa."
                    )

        is_orthogonal = len(high_corr_pairs) == 0
        return OrthogonalityReport(
            is_orthogonal=is_orthogonal,
            high_correlation_pairs=high_corr_pairs,
            correlation_matrix=matrix,
            warnings=warnings,
            max_correlation=max_abs_r,
        )
