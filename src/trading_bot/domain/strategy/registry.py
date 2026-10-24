"""Registry chỉ đọc cho các giả thuyết built-in có phiên bản rõ ràng."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trading_bot.domain.strategy.contracts import Strategy, StrategySelection
from trading_bot.domain.strategy.rules import (
    H01EmaTrendRule,
    H03DonchianBreakoutRule,
    H06RangeMeanReversionRule,
    release_content_hash,
)


class UnknownStrategyReleaseError(LookupError):
    """Không tìm thấy mã phát hành trong registry hiện hành."""


class UnpublishedStrategySettingsError(ValueError):
    """Cấu hình chưa công bố nên không được phép kích hoạt strategy."""


class StrategyNotSelectedError(ValueError):
    """Strategy có tồn tại nhưng không được cấu hình chọn."""


@dataclass(frozen=True, slots=True)
class StrategyRelease:
    """Thông tin bất biến giúp tái lập rule đã dùng trong một quyết định."""

    release_id: str
    strategy_id: str
    name_vi: str
    rule_summary_vi: str
    status: str
    rule_version: str
    recipe_version: str
    glossary_url: str
    content_hash: str


_BUILT_INS: tuple[tuple[Strategy, str, str, str], ...] = (
    (
        H01EmaTrendRule(),
        "EMA theo xu hướng",
        (
            "EMA20/80 giao cắt 1h; giá đóng 4h xác nhận phía EMA200; ADX14 >= 20; "
            "stop 2 ATR; trailing 3 ATR; thoát giao cắt ngược; tối đa 120 nến."
        ),
        "h01-exit-v1",
    ),
    (
        H03DonchianBreakoutRule(),
        "Phá kênh Donchian",
        (
            "Giá đóng 1h phá đỉnh/đáy 55 nến trước; quote volume lớn hơn median 20 nến trước; "
            "stop 2 ATR; trailing 3 ATR; kênh thoát 20; tối đa 120 nến."
        ),
        "h03-exit-v1",
    ),
    (
        H06RangeMeanReversionRule(),
        "Hồi quy về trung bình",
        (
            "ADX14 < 18 và ER40 < 0.25; Z20/RSI14 đồng thuận cực trị; lọc blackout và phá biên "
            "120 nến; stop 1.5 ATR; mục tiêu tối đa 2 ATR; thoát tại Z20 ±0.2 hoặc sau 16 nến; "
            "cấm scale-in."
        ),
        "h06-exit-v1",
    ),
)


class StrategyRegistry:
    """Tra cứu các rule thuần; không lưu trạng thái hoặc công bố cấu hình."""

    def __init__(self) -> None:
        rules: dict[str, Strategy] = {}
        releases: dict[str, StrategyRelease] = {}
        for rule, name_vi, formula, recipe_version in _BUILT_INS:
            digest = release_content_hash(rule)
            rules[rule.release_id] = rule
            releases[rule.release_id] = StrategyRelease(
                release_id=rule.release_id,
                strategy_id=rule.strategy_id,
                name_vi=name_vi,
                rule_summary_vi=formula,
                status="EXPERIMENTAL_UNVALIDATED",
                rule_version="1.0.0",
                recipe_version=recipe_version,
                glossary_url="docs/research/glossary-vi.md",
                content_hash=digest,
            )
        self._rules: Mapping[str, Strategy] = MappingProxyType(rules)
        self._releases: Mapping[str, StrategyRelease] = MappingProxyType(releases)

    def list_releases(self) -> tuple[StrategyRelease, ...]:
        """Liệt kê metadata theo release ID để kết quả luôn ổn định."""
        return tuple(self._releases[key] for key in sorted(self._releases))

    def get_release(self, release_id: str) -> StrategyRelease:
        """Đọc metadata của một bản phát hành."""
        try:
            return self._releases[release_id]
        except KeyError as error:
            raise UnknownStrategyReleaseError(release_id) from error

    def resolve(self, release_id: str, settings: StrategySelection) -> Strategy:
        """Trả rule chỉ khi release nằm trong cấu hình đã công bố."""
        if not settings.published:
            raise UnpublishedStrategySettingsError(settings.settings_version_id)
        if release_id not in settings.active_release_ids:
            raise StrategyNotSelectedError(release_id)
        try:
            return self._rules[release_id]
        except KeyError as error:
            raise UnknownStrategyReleaseError(release_id) from error
