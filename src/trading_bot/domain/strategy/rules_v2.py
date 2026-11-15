"""Release v2 của H03/H06: điều kiện thoát đã khai báo trong recipe được thực thi thật.

Ở v1, `exit_channel_period` (H03) và `exit_feature_name` (H06) chỉ nằm trong recipe mà không
có nơi nào thực thi. v2 giữ nguyên rule vào lệnh, chỉ đổi recipe ID để position monitor và bên
cấp bằng chứng thoát lệnh biết phải áp dụng:

- H03 v2: thoát theo kênh Donchian ngược chiều 20 nến chiến lược (CHANNEL_EXIT).
- H06 v2: thoát khi Z-score 20 về gần trung bình (FEATURE_EXIT).
- Cả hai: `max_hold_bars` đếm theo nến của khung chiến lược, không theo nến execution.

Module tách riêng vì hash release băm toàn bộ nguồn module chứa rule: sửa `rules.py` sẽ đổi
hash của mọi release v1 đang được ghim trong DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.rules import (
    H03DonchianBreakoutRule,
    H06RangeMeanReversionRule,
)

# SHA-256 của `rules.py` tại thời điểm phát hành v2. Rule vào lệnh của v2 kế thừa từ file đó,
# nên hằng này buộc hash release v2 đổi theo mỗi lần `rules.py` đổi (unit test đối chiếu).
BASE_RULES_SOURCE_SHA256 = "9c2f3d708f48afd657c05234142dbbb71ed64f436771e81b9d0fc5ca9c47369d"


@dataclass(frozen=True, slots=True)
class ExitEvidenceSpec:
    """Bằng chứng theo nến mà một recipe cần để thực thi điều kiện thoát đã khai báo."""

    timeframe: str
    channel_period: int | None = None
    feature_name: str | None = None


# Recipe không nằm trong bảng này (toàn bộ v1) giữ nguyên hành vi cũ: không thoát theo kênh,
# không thoát theo feature và đếm thời gian giữ theo nến execution.
EXIT_EVIDENCE_BY_RECIPE = MappingProxyType(
    {
        "h03-exit-v2": ExitEvidenceSpec(timeframe="1h", channel_period=20),
        "h06-exit-v2": ExitEvidenceSpec(timeframe="15m", feature_name="zscore_20"),
    }
)


def exit_evidence_spec(recipe_id: str) -> ExitEvidenceSpec | None:
    """Trả yêu cầu bằng chứng thoát của recipe; None nghĩa là recipe không dùng."""
    return EXIT_EVIDENCE_BY_RECIPE.get(recipe_id)


class H03DonchianBreakoutRuleV2(H03DonchianBreakoutRule):
    """H03 v2: vào lệnh như v1, thoát thêm theo kênh Donchian ngược chiều 20 nến 1h."""

    release_id = "h03-v2"
    __slots__ = ()
    recipe = ExitRecipe(
        "h03-exit-v2", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120, exit_channel_period=20
    )


class H06RangeMeanReversionRuleV2(H06RangeMeanReversionRule):
    """H06 v2: vào lệnh như v1, thoát thêm khi Z-score 20 hồi về gần trung bình."""

    release_id = "h06-v2"
    recipe_id = "h06-exit-v2"
    __slots__ = ()
    # Cùng tham số với v1; hash release vẫn khác vì băm release_id và nguồn module này
    # (gồm BASE_RULES_SOURCE_SHA256 và bảng EXIT_EVIDENCE_BY_RECIPE).
    recipe_manifest = (
        ("stop_atr_multiple", 1.5),
        ("take_profit_atr_multiple", 2.0),
        ("max_hold_bars", 16),
        ("exit_feature_name", "zscore_20"),
        ("long_exit", (">=", -0.2)),
        ("short_exit", ("<=", 0.2)),
        ("allow_scale_in", False),
    )
