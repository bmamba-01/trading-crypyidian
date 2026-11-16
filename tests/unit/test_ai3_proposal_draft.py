"""AI-3: tạo bản nháp config từ đề xuất AI đã backtest (owner vẫn kiểm định + công bố MFA)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from test_admin_web_views import BOT_ID, AuthServiceStub, _client

from trading_bot.admin.bot_service import BotAdminError
from trading_bot.application.proposal_backtest import draft_overlay_from_backtest

BACKTEST_ID = UUID("0b8f5a8e-6a7f-4a52-9a52-6e8d5c1a2b3c")
HEAD = {
    "schema_version": 1,
    "strategy": {
        "release_id": "h03-v3-4h",
        "parameters": {"donchian_lookback": 30, "volume_median_lookback": 30},
    },
    "risk": {"per_trade_risk_fraction": "0.002", "max_positions": 6},
    "universe": {"symbols": ["ATOMUSDT"]},
}
PROPOSAL = {"changes": [{"field": "strategy.parameters.donchian_lookback", "to": "20"}]}
BASELINE_ARM = {
    "release_id": "h03-v3-4h",
    "parameters": {"donchian_lookback": 30, "volume_median_lookback": 30},
    "per_trade_risk_fraction": "0.002",
}


def _result(verdict: str = "BETTER", baseline_arm: dict | None = None) -> dict:
    return {"verdict": verdict, "baseline_arm": baseline_arm or BASELINE_ARM}


def test_draft_overlay_applies_change_on_current_published_config() -> None:
    overlay = draft_overlay_from_backtest(HEAD, PROPOSAL, _result())

    assert overlay["strategy"]["parameters"] == {
        "donchian_lookback": 20,
        "volume_median_lookback": 30,
    }
    # Phần còn lại giữ nguyên; overlay gốc không bị sửa tại chỗ.
    assert overlay["universe"] == HEAD["universe"]
    assert overlay["risk"] == HEAD["risk"]
    assert HEAD["strategy"]["parameters"]["donchian_lookback"] == 30
    assert draft_overlay_from_backtest(HEAD, PROPOSAL, _result("INCONCLUSIVE"))


@pytest.mark.parametrize("verdict", ["WORSE", "UNSUPPORTED", ""])
def test_draft_blocked_for_unfavourable_or_untested_verdict(verdict: str) -> None:
    with pytest.raises(ValueError, match="Kết luận"):
        draft_overlay_from_backtest(HEAD, PROPOSAL, _result(verdict))


def test_draft_blocked_when_config_changed_since_backtest() -> None:
    stale = dict(BASELINE_ARM, parameters={"donchian_lookback": 40, "volume_median_lookback": 30})
    with pytest.raises(ValueError, match="backtest lại"):
        draft_overlay_from_backtest(HEAD, PROPOSAL, _result(baseline_arm=stale))
    with pytest.raises(ValueError, match="không có thay đổi"):
        draft_overlay_from_backtest(HEAD, {"changes": []}, _result())


def test_risk_fraction_change_keeps_decimal_value() -> None:
    overlay = draft_overlay_from_backtest(
        HEAD,
        {"changes": [{"field": "risk.per_trade_risk_fraction", "to": "0.003"}]},
        _result(),
    )
    assert Decimal(str(overlay["risk"]["per_trade_risk_fraction"])) == Decimal("0.003")


def _proposal_row(verdict: str) -> dict[str, object]:
    return {
        "id": str(BACKTEST_ID),
        "created_at": None,
        "verdict": verdict,
        "changes": PROPOSAL["changes"],
        "rationale": "",
        "baseline": {"trades": 43, "net_pnl": "26.17", "max_drawdown": "100.79"},
        "variant": {"trades": 52, "net_pnl": "1.06", "max_drawdown": "121.69"},
        "notes": [],
        "window": None,
        "draftable": verdict in {"BETTER", "INCONCLUSIVE"},
    }


def test_bot_detail_lists_ai_proposals_with_draft_button_only_when_draftable() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()

    async def rows(_principal: object, *, bot_id: UUID) -> list[dict[str, object]]:
        assert bot_id == BOT_ID
        return [_proposal_row("BETTER"), _proposal_row("WORSE")]

    client.app.state.bot_admin_service.list_proposal_backtests = rows
    html = client.get(f"/admin/bots/{BOT_ID}").text

    assert 'id="ai-proposals"' in html
    assert "strategy.parameters.donchian_lookback → 20" in html
    assert "43 → 52" in html
    assert "26.17 → 1.06" in html
    assert html.count(f'action="/admin/bots/{BOT_ID}/ai-proposals/{BACKTEST_ID}/draft"') == 1
    assert "Không áp dụng" in html


def test_bot_detail_survives_proposal_read_failure() -> None:
    client = _client()
    client.app.state.auth_service = AuthServiceStub()

    async def broken(_principal: object, *, bot_id: UUID) -> list[dict[str, object]]:
        raise RuntimeError("bảng chưa có")

    client.app.state.bot_admin_service.list_proposal_backtests = broken
    page = client.get(f"/admin/bots/{BOT_ID}")
    assert page.status_code == 200
    assert "Chưa có đề xuất AI nào được backtest" in page.text


def test_draft_form_requires_csrf_and_redirects_with_known_status() -> None:
    client = _client()
    auth = AuthServiceStub()
    client.app.state.auth_service = auth
    calls: list[tuple[UUID, UUID]] = []

    async def create(_principal: object, *, bot_id: UUID, backtest_id: UUID) -> dict:
        calls.append((bot_id, backtest_id))
        return {}

    client.app.state.bot_admin_service.create_draft_from_proposal = create
    url = f"/admin/bots/{BOT_ID}/ai-proposals/{BACKTEST_ID}/draft"
    ok = client.post(url, data={"csrf_token": "csrf-token-test-123"}, follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["Location"] == f"/admin/bots/{BOT_ID}?ai_proposal=created#ai-proposals"
    assert calls == [(BOT_ID, BACKTEST_ID)]
    assert auth.verified_tokens == ["csrf-token-test-123"]

    for error, code in (
        (BotAdminError(409, "Cấu hình bot đã đổi …; cần backtest lại trước khi áp."), "stale"),
        (BotAdminError(409, "Kết luận WORSE: không tạo bản nháp."), "verdict"),
        (BotAdminError(403, "Cần xác thực MFA gần đây."), "mfa"),
        (BotAdminError(403, "Không đủ quyền cho thao tác bot này."), "permission"),
        (BotAdminError(404, "Không tìm thấy."), "missing"),
    ):

        async def fail(_p: object, *, bot_id: UUID, backtest_id: UUID, _e=error) -> dict:
            raise _e

        client.app.state.bot_admin_service.create_draft_from_proposal = fail
        response = client.post(
            url, data={"csrf_token": "csrf-token-test-123"}, follow_redirects=False
        )
        assert response.headers["Location"].endswith(f"?ai_proposal_error={code}#ai-proposals")

    # Lỗi hiển thị chỉ lấy từ bảng mẫu đã biết; mã lạ không chèn văn bản vào trang.
    html = client.get(f"/admin/bots/{BOT_ID}?ai_proposal_error=<script>").text
    assert "cần backtest lại trước khi áp" not in html
    stale_html = client.get(f"/admin/bots/{BOT_ID}?ai_proposal_error=stale").text
    assert "cần backtest lại trước khi áp" in stale_html
