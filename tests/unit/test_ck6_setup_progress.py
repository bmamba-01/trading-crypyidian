"""CK-6: tiến độ setup k/n từ checklist đã lưu, điều kiện còn thiếu và thời điểm đạt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trading_bot.domain.strategy.setup_progress import (
    SETUP_FORMULA_VERSION,
    SETUP_RULES,
    context_conditions,
    setup_progress,
)

NOW = datetime(2026, 11, 15, 15, 7, 30, tzinfo=UTC)


def _item(key: str, passed: bool | None, actual: str = "x") -> dict[str, Any]:
    return {
        "key": key,
        "label_vi": f"Điều kiện {key}",
        "actual": actual,
        "operator": ">=",
        "threshold": "20",
        "passed": passed,
    }


def _decision(
    minutes_ago: int,
    items: list[dict[str, Any]],
    *,
    status: str = "ABSTAIN",
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "evaluated_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "status": status,
        "reason_code": "H01_ADX_BELOW_20",
        "detail_vi": "ADX14 1h phải đạt tối thiểu 20.",
        "checklist": items,
        "candidate": candidate,
    }


def test_rules_mirror_strategy_checklists() -> None:
    assert SETUP_FORMULA_VERSION == "ck6-setup-v1"
    assert SETUP_RULES["H01"].required == ("ema_cross", "htf_trend", "adx")
    assert SETUP_RULES["H03"].required == ("donchian_breakout", "volume_above_median")
    assert SETUP_RULES["H06"].required[0] == "macro_clear"


def test_not_ready_reports_progress_first_missing_and_achieved_time() -> None:
    decisions = [
        _decision(
            0, [_item("ema_cross", True), _item("htf_trend", True), _item("adx", False, "12")]
        ),
        _decision(
            60, [_item("ema_cross", False), _item("htf_trend", True), _item("adx", False, "11")]
        ),
        _decision(120, [_item("ema_cross", False), _item("htf_trend", True), _item("adx", False)]),
    ]
    progress = setup_progress(strategy_release_id="h01-v1", decisions=decisions, bonus=[], now=NOW)
    assert progress["status"] == "not_ready"
    assert progress["status_label"] == "Chưa đủ"
    assert progress["required"] == {"passed": 2, "total": 3}
    assert progress["missing"] == "Còn thiếu: Điều kiện adx — hiện 12, cần >= 20"
    items = {item["key"]: item for item in progress["conditions"]}
    assert items["ema_cross"]["kind"] == "required"
    # Đạt liên tục từ quyết định mới nhất về trước → thời điểm đạt = quyết định cũ nhất của chuỗi.
    assert items["ema_cross"]["achieved_at"] == decisions[0]["evaluated_at"]
    assert items["htf_trend"]["achieved_at"] == decisions[2]["evaluated_at"]
    assert items["adx"]["achieved_at"] is None


def test_unknown_condition_is_reported_before_later_failures() -> None:
    decisions = [
        _decision(
            0,
            [
                _item("ema_cross", None, "thiếu dữ liệu"),
                _item("htf_trend", False),
                _item("adx", True),
            ],
        )
    ]
    progress = setup_progress(strategy_release_id="h01-v1", decisions=decisions, bonus=[], now=NOW)
    assert progress["missing"] == "Còn thiếu: Điều kiện ema_cross — chưa xác định (thiếu dữ liệu)"


def test_candidate_ready_then_expired() -> None:
    items = [_item("ema_cross", True), _item("htf_trend", True), _item("adx", True)]
    live = {"direction": "long", "expires_at": (NOW + timedelta(minutes=10)).isoformat()}
    ready = setup_progress(
        strategy_release_id="h01-v1",
        decisions=[_decision(0, items, status="CANDIDATE", candidate=live)],
        bonus=[_item("killzone", True), _item("funding_clear", False), _item("news", None)],
        now=NOW,
    )
    assert ready["status"] == "ready"
    assert ready["status_label"] == "Đủ điều kiện"
    assert ready["direction"] == "long"
    assert ready["missing"] is None
    assert ready["bonus"] == {"passed": 1, "total": 3, "k": 0}
    assert [item["kind"] for item in ready["conditions"]][-3:] == ["bonus", "bonus", "bonus"]

    stale = {"direction": "long", "expires_at": (NOW - timedelta(minutes=1)).isoformat()}
    expired = setup_progress(
        strategy_release_id="h01-v1",
        decisions=[_decision(0, items, status="CANDIDATE", candidate=stale)],
        bonus=[],
        now=NOW,
    )
    assert expired["status"] == "expired"
    assert expired["status_label"] == "Mất hiệu lực"


def test_no_decision_and_legacy_decision_without_checklist() -> None:
    empty = setup_progress(strategy_release_id="h01-v1", decisions=[], bonus=[], now=NOW)
    assert empty["status"] == "no_data"
    legacy = setup_progress(
        strategy_release_id="h01-v1", decisions=[_decision(0, [])], bonus=[], now=NOW
    )
    assert legacy["status"] == "not_ready"
    assert legacy["required"] == {"passed": 0, "total": 0}
    assert legacy["missing"] == "Quyết định gần nhất chưa có checklist: H01_ADX_BELOW_20"


def test_context_conditions_killzone_funding_and_news() -> None:
    # 15:07:30 UTC = 10:07:30 EST: trong Silver Bullet sáng; funding 16:00 còn 52 phút.
    items = {
        item["key"]: item
        for item in context_conditions(
            NOW, next_funding_time=datetime(2026, 11, 15, 16, tzinfo=UTC)
        )
    }
    assert items["killzone"]["passed"] is True
    assert items["funding_clear"]["passed"] is True
    assert items["news"]["passed"] is None
    near = {
        item["key"]: item
        for item in context_conditions(
            datetime(2026, 11, 15, 15, 55, tzinfo=UTC),
            next_funding_time=datetime(2026, 11, 15, 16, tzinfo=UTC),
        )
    }
    assert near["funding_clear"]["passed"] is False
    assert near["killzone"]["passed"] is True  # 10:55 EST vẫn trong Silver Bullet 10–11
    outside = {
        item["key"]: item
        for item in context_conditions(
            datetime(2026, 11, 15, 22, 0, tzinfo=UTC), next_funding_time=None
        )
    }
    assert outside["killzone"]["passed"] is False  # 17:00 EST: ngoài mọi cửa sổ
    unknown = {item["key"]: item for item in context_conditions(NOW, next_funding_time=None)}
    assert unknown["funding_clear"]["passed"] is None


def test_build_setup_progress_uses_only_hash_verified_decisions() -> None:
    from tests.unit.test_sig3_signal_card import _decision_for
    from tests.unit.test_strategy_hypotheses import _h01_snapshot

    from trading_bot.application.bot_reporting import build_setup_progress

    abstain = _decision_for("h01-v1", _h01_snapshot(adx=10.0))
    candidate = _decision_for("h01-v1")
    tampered = dict(candidate.canonical_payload(), status="CANDIDATE", reason_code="SỬA_TAY")
    rows = [
        {"payload": tampered, "decision_hash": candidate.decision_hash},
        {"payload": abstain.canonical_payload(), "decision_hash": abstain.decision_hash},
    ]
    progress = build_setup_progress(rows, bonus=[], now=abstain.evaluated_at)
    assert progress["unverified_decisions"] == 1
    assert progress["status"] == "not_ready"
    assert progress["missing"].startswith("Còn thiếu: ADX14 1h đủ mạnh — hiện 10")
    # Đọc tiến độ không làm đổi payload/hash đã lưu.
    assert abstain.canonical_payload() == rows[1]["payload"]

    ready = build_setup_progress(
        [{"payload": candidate.canonical_payload(), "decision_hash": candidate.decision_hash}],
        bonus=[],
        now=candidate.evaluated_at,
    )
    assert ready["status"] == "ready"
    assert ready["card"]["direction"] == "long"
