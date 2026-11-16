"""CRY-383: trang Admin "Phân tích AI" (danh sách lượt, chi tiết, backtest đề xuất) — chỉ đọc."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from test_admin_web_views import WORKSPACE, _client

from trading_bot.application.analyst_review import (
    AnalystReviewError,
    AnalystReviewService,
    _backtest_view,
    _context_view,
    _output_view,
)
from trading_bot.auth.policy import Principal

RUN_ID = UUID("6f0c8a52-6c1f-4d7a-9a1e-0f2b9d3c4e5f")
CREATED = datetime(2026, 11, 16, 16, 35, tzinfo=UTC)
OUTPUT = {
    "summary": "Nhóm H03 có 3 lệnh đóng.",
    "confidence": "low",
    "findings": [
        {"statement": "T09 có 2 lệnh.", "evidence_refs": ["/bots/6/performance/closed_trades"]}
    ],
    "hypotheses": [{"statement": "Phí ăn edge.", "how_to_test": "Tách phí.", "evidence_refs": []}],
    "proposals": [
        {
            "bot_name": "T06",
            "changes": [{"field": "risk.max_positions", "to": "3"}],
            "rationale": "Bị chặn nhiều.",
            "risk": "Rủi ro tương quan.",
            "evidence_refs": ["/bots/4"],
        }
    ],
}
CONTEXT = {
    "macro_calendar": {
        "state": {"blackout": True},
        "upcoming": [{"vn_date": "17/11", "vn_time": "20:30", "label_vi": "Doanh số bán lẻ"}],
    },
    "derivatives": {
        "symbol_count": 20,
        "symbols": [{"symbol": "BTCUSDT", "funding_rate_pct": "0.0005"}],
    },
    "listings": {
        "watched_at_risk": [
            {"symbol": "ZECUSDT", "status": "TRADING", "delist_at_vn": "20/11 15:00"}
        ]
    },
}


def test_views_tolerate_partial_or_missing_data() -> None:
    view = _output_view(OUTPUT)
    assert view["confidence"] == "Thấp"
    assert view["findings"][0]["refs"] == ["/bots/6/performance/closed_trades"]
    assert view["proposals"][0]["changes"] == ["risk.max_positions → 3"]
    empty = _output_view(None)
    assert empty["findings"] == [] and empty["confidence"] == "—"  # noqa: PT018
    assert _output_view({"findings": ["không phải object"]})["findings"] == []
    context = _context_view(CONTEXT)
    assert context is not None
    assert context["macro_events"] == ["17/11 20:30 · Doanh số bán lẻ"]
    assert context["macro_blackout"] is True
    assert context["listing_risks"] == ["ZECUSDT: TRADING — gỡ 20/11 15:00 (giờ VN)"]
    assert _context_view(None) is None
    backtest = _backtest_view(
        SimpleNamespace(
            id=uuid4(),
            created_at=CREATED,
            proposal_index=0,
            bot_id=None,
            bot_name="T06",
            verdict="BETTER",
            proposal={"changes": [{"field": "a", "to": 1}]},
            result={"baseline": {"trades": 12}, "variant": {"trades": 14}, "notes": ["n"]},
        )  # fmt: skip
    )
    assert backtest["verdict_vi"] == "Tốt hơn"
    assert backtest["changes"] == ["a → 1"]


def test_service_requires_workspace_read() -> None:
    @asynccontextmanager
    async def never(*_args: Any, **_kwargs: Any):  # pragma: no cover - không được gọi
        raise AssertionError("Không được mở transaction khi thiếu quyền.")
        yield

    service = AnalystReviewService(None, unit_of_work=never)  # type: ignore[arg-type]
    nobody = Principal(uuid4(), uuid4(), WORKSPACE, frozenset(), CREATED)
    with pytest.raises(AnalystReviewError) as error:
        asyncio.run(service.list_runs(nobody))
    assert error.value.status_code == 403


class _ReviewStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_runs(self, principal: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "topics": ["family:H01", "family:H03"],
            "runs": [
                {
                    "id": str(RUN_ID), "created_at": CREATED, "topic_key": "family:H03",
                    "engine": "claude-cli", "model": "x", "valid": True, "error": None,
                    "resumed": True, "error_count": 0, "cost_usd": "0.1250",
                    "summary": "Nhóm H03 có 3 lệnh đóng.", "proposal_count": 1, "backtest_count": 1,
                },
                {
                    "id": str(uuid4()), "created_at": CREATED, "topic_key": "family:H01",
                    "engine": "claude-cli", "model": None, "valid": False, "error": None,
                    "resumed": False, "error_count": 2, "cost_usd": None, "summary": "",
                    "proposal_count": 0, "backtest_count": 0,
                },
            ],
        }  # fmt: skip

    async def get_run(self, principal: Any, run_id: UUID) -> dict[str, Any]:
        if run_id != RUN_ID:
            raise AnalystReviewError(404, "Không tìm thấy lượt phân tích.")
        return {
            "id": str(RUN_ID), "created_at": CREATED, "topic_key": "family:H03",
            "engine": "claude-cli", "model": "claude", "cli_version": "1", "session_id": "s",
            "resumed": True, "prompt_version": "ai2-prompt-v5",
            "output_version": "analyst_output.v2",
            "run_report_hash": "a" * 64, "input_sha256": "b" * 64, "valid": False, "error": None,
            "exit_code": 0, "cost_usd": "0.1250", "tokens_in": 10, "tokens_out": 5,
            "validation_errors": ["findings[0]: số '9' không khớp"],
            "output": _output_view(OUTPUT), "has_output": True, "context": _context_view(CONTEXT),
            "backtests": [{
                "id": "x", "created_at": CREATED, "proposal_index": 0,
                "bot_id": "11111111-1111-1111-1111-111111111111", "bot_name": "T06",
                "verdict": "INCONCLUSIVE", "verdict_vi": "Chưa kết luận",
                "changes": ["risk.max_positions → 3"], "baseline": {"trades": 12, "net_pnl": "-5"},
                "variant": {"trades": 14, "net_pnl": "-1"}, "notes": [],
            }],
        }  # fmt: skip


def test_list_page_renders_runs_filters_and_nav() -> None:
    client = _client()
    stub = _ReviewStub()
    client.app.state.analyst_review_service = stub
    page = client.get("/admin/ai-analyses?topic=family:H03&valid=no")
    assert page.status_code == 200
    html = page.text
    assert '<link rel="stylesheet" href="/static/pages/ai.css">' in html
    assert 'href="/admin/ai-analyses" title="Phân tích AI"' in html
    assert f'href="/admin/ai-analyses/{RUN_ID}"' in html
    assert "Bị loại (2 lỗi)" in html
    assert "Hợp lệ" in html
    assert stub.calls == [{"topic": "family:H03", "valid": False}]
    assert '<option value="family:H03" selected>' in html


def test_detail_page_shows_output_errors_backtests_and_context() -> None:
    client = _client()
    client.app.state.analyst_review_service = _ReviewStub()
    html = client.get(f"/admin/ai-analyses/{RUN_ID}").text
    assert "T09 có 2 lệnh." in html
    assert "/bots/6/performance/closed_trades" in html
    assert "số &#39;9&#39; không khớp" in html
    assert "Chưa kết luận" in html
    assert 'href="/admin/bots/11111111-1111-1111-1111-111111111111#ai-proposals"' in html
    assert "17/11 20:30 · Doanh số bán lẻ" in html
    assert "đang trong khoảng cấm" in html
    assert "ZECUSDT: TRADING — gỡ 20/11 15:00 (giờ VN)" in html
    assert client.get(f"/admin/ai-analyses/{uuid4()}").status_code == 404


def test_page_returns_503_without_service() -> None:
    client = _client()
    client.app.state.analyst_review_service = None
    assert client.get("/admin/ai-analyses").status_code == 503


def test_decimal_cost_is_formatted() -> None:
    from trading_bot.application.analyst_review import _money

    assert _money(Decimal("0.12345678")) == "0.1235"
    assert _money(None) is None
