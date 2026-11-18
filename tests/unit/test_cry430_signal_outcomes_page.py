"""CRY-430: tổng hợp kết quả tín hiệu và trang Admin "Kết quả tín hiệu" (chỉ đọc)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from test_admin_web_views import WORKSPACE, _client

from trading_bot.application.signal_outcome_review import (
    SignalOutcomeReviewError,
    SignalOutcomeReviewService,
)
from trading_bot.auth.policy import Principal
from trading_bot.domain.strategy.signal_outcome_summary import (
    OutcomeRow,
    bucket_key,
    bucket_labels,
    group_summaries,
    repeated_wrong,
    summarize,
)

CREATED = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BOT = UUID("11111111-1111-4111-8111-111111111111")


def _scored(r: str, label: int, touch: str = "NONE", seconds: int | None = None) -> OutcomeRow:
    value = Decimal(r)
    return OutcomeRow(
        status="SCORED",
        label=label,
        outcome_r=value,
        mfe_r=max(value, Decimal(0)) + Decimal("0.5"),
        mae_r=min(value, Decimal(0)) - Decimal("0.25"),
        first_touch=touch,
        same_bar_conflict=touch == "SL" and r == "-1",
        seconds_to_first_touch=seconds,
    )


ROWS = [
    _scored("1", 1, "TP1", 900),
    _scored("1", 1, "TP1", 2700),
    _scored("-1", -1, "SL", 600),
    _scored("0.05", 0),
    OutcomeRow(status="DATA_GAP"),
    OutcomeRow(status="INVALID_SIGNAL"),
]


def test_summarize_counts_rates_and_distribution() -> None:
    summary = summarize(ROWS)
    assert summary["signals"] == 6
    assert summary["scored"] == 4
    assert (summary["correct"], summary["wrong"], summary["neutral"]) == (2, 1, 1)
    assert summary["hit_rate_percent"] == "50.0"
    assert summary["wrong_rate_percent"] == "25.0"
    assert summary["data_gap"] == 1 and summary["invalid"] == 1  # noqa: PT018
    assert (summary["tp1_first"], summary["sl_first"], summary["no_touch"]) == (2, 1, 1)
    assert summary["same_bar_conflicts"] == 1
    assert summary["outcome_r_mean"] == "0.2625"
    assert summary["outcome_r_median"] == "0.5250"
    assert summary["median_seconds_to_tp1"] == 1800
    assert summary["r_distribution"] == {
        "le_m1": 1, "m1_m05": 0, "m05_m01": 0, "flat": 1, "p01_p05": 0, "p05_p1": 0, "ge_p1": 2,
    }  # fmt: skip


def test_empty_summary_has_null_rates() -> None:
    summary = summarize([])
    assert summary["hit_rate_percent"] is None
    assert summary["outcome_r_mean"] is None


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("-1.5", "le_m1"),
        ("-1", "le_m1"),
        ("-0.9999", "m1_m05"),
        ("-0.1001", "m05_m01"),
        ("-0.1", "flat"),
        ("0.1", "flat"),
        ("0.1001", "p01_p05"),
        ("0.5", "p05_p1"),
        ("1", "ge_p1"),
    ],
)
def test_bucket_boundaries_match_label_band(value: str, key: str) -> None:
    assert bucket_key(Decimal(value)) == key


def test_groups_and_repeated_wrong() -> None:
    keyed = [("A", ROWS[0]), ("B", ROWS[2]), ("B", ROWS[2]), ("B", ROWS[3])]
    groups = group_summaries(keyed)
    assert [item["key"] for item in groups] == ["B", "A"]
    wrong = repeated_wrong(keyed, min_wrong=2)
    assert [item["key"] for item in wrong] == ["B"]
    assert wrong[0]["wrong"] == 2
    assert repeated_wrong(keyed, min_wrong=3) == []


def test_service_requires_workspace_read() -> None:
    @asynccontextmanager
    async def never(*_args: Any, **_kwargs: Any):  # pragma: no cover - không được gọi
        raise AssertionError("Không được mở transaction khi thiếu quyền.")
        yield

    service = SignalOutcomeReviewService(None, unit_of_work=never)  # type: ignore[arg-type]
    nobody = Principal(uuid4(), uuid4(), WORKSPACE, frozenset(), CREATED)
    with pytest.raises(SignalOutcomeReviewError) as error:
        asyncio.run(service.overview(nobody))
    assert error.value.status_code == 403


class _Stub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def overview(self, principal: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        summary = summarize(ROWS)
        return {
            "filters": {"bot_id": str(BOT), "release": "h03-v3-15m", "symbol": "", "timeframe": "",
                        "horizon": 4, "period": "30"},
            "options": {"bots": [{"id": str(BOT), "name": "T09"}], "releases": ["h03-v3-15m"],
                        "symbols": ["ZECUSDT"], "timeframes": ["15m"], "horizons": [1, 4, 24],
                        "periods": ["7", "30", "90"]},
            "method_version": "sig-outcome-v2",
            "truncated": False,
            "summary": summary,
            "bucket_labels": bucket_labels(),
            "by_setup": group_summaries([("h03-v3-15m · 15m", row) for row in ROWS]),
            "by_delivery": group_summaries([("Đã gửi", ROWS[0]), ("Bị lọc/giữ lại", ROWS[2])]),
            "by_symbol": group_summaries([("ZECUSDT", row) for row in ROWS]),
            "repeated_wrong": repeated_wrong([("h03-v3-15m · ZECUSDT · long", ROWS[2])] * 2,
                                             min_wrong=2),
            "recent": [{
                "signal_at": CREATED, "bot_name": "T09", "release": "h03-v3-15m",
                "symbol": "ZECUSDT", "timeframe": "15m", "direction": "Mua", "status": "Đã chấm",
                "touch": "SL trước", "conflict": True, "outcome_r": "-1.00R", "mfe_r": "+0.50R",
                "mae_r": "-1.25R", "label": "Sai", "label_value": -1, "delivery": "Đã gửi",
            }],
        }  # fmt: skip


def test_page_renders_filters_kpis_tables_and_nav() -> None:
    client = _client()
    stub = _Stub()
    client.app.state.signal_outcome_review_service = stub
    response = client.get(
        f"/admin/signal-outcomes?bot={BOT}&release=h03-v3-15m&horizon=4&period=30"
    )
    assert response.status_code == 200
    html = response.text
    assert 'href="/admin/signal-outcomes" title="Kết quả tín hiệu"' in html
    assert '<link rel="stylesheet" href="/static/pages/signal-outcomes.css">' in html
    assert f'<option value="{BOT}" selected>T09</option>' in html
    assert '<option value="4" selected>4× khung</option>' in html
    assert ">50.0%<" in html
    assert "h03-v3-15m · ZECUSDT · long" in html
    assert "cùng nến" in html
    assert 'class="badge badge-danger">Sai' in html
    assert 'data-dialog-open="signal-recent-dialog"' in html
    assert '<dialog id="signal-recent-dialog"' in html
    assert "<meter" in html and "style=" not in html.split("<main")[-1]  # noqa: PT018
    assert stub.calls == [
        {
            "bot_id": BOT, "release": "h03-v3-15m", "symbol": None, "timeframe": None,
            "horizon": 4, "period": "30",
        }
    ]  # fmt: skip


def test_page_ignores_bad_bot_id_and_needs_service() -> None:
    client = _client()
    stub = _Stub()
    client.app.state.signal_outcome_review_service = stub
    assert client.get("/admin/signal-outcomes?bot=not-a-uuid").status_code == 200
    assert stub.calls[0]["bot_id"] is None
    client.app.state.signal_outcome_review_service = None
    assert client.get("/admin/signal-outcomes").status_code == 503
