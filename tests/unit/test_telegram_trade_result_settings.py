"""TG-3: cấu hình gửi kết quả PAPER/tổng hợp có giá trị mặc định an toàn và được kiểm biên."""

import pytest
from pydantic import ValidationError

from trading_bot.config.settings import AppSettings


def test_defaults_follow_owner_decision_with_four_hour_summary() -> None:
    settings = AppSettings(app_env="test")

    assert settings.telegram_trade_results_enabled is True
    assert settings.telegram_trade_result_max_age_minutes == 60
    assert settings.telegram_report_interval_minutes == 240


@pytest.mark.parametrize("minutes", [0, 15, 240, 10_080])
def test_report_interval_accepts_zero_to_disable_or_at_least_fifteen(minutes: int) -> None:
    assert AppSettings(app_env="test", telegram_report_interval_minutes=minutes)


@pytest.mark.parametrize("minutes", [-1, 1, 14, 10_081])
def test_report_interval_rejects_spammy_or_out_of_range_values(minutes: int) -> None:
    with pytest.raises(ValidationError):
        AppSettings(app_env="test", telegram_report_interval_minutes=minutes)


@pytest.mark.parametrize("minutes", [0, 1441])
def test_event_age_window_is_bounded(minutes: int) -> None:
    with pytest.raises(ValidationError):
        AppSettings(app_env="test", telegram_trade_result_max_age_minutes=minutes)
