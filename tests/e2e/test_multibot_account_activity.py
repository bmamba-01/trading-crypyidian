"""MB05.9: guard hoạt động account ngoại lai trên PostgreSQL thật (adapter kịch bản trong probe).

Chứng minh: order/vị thế lạ trên account A -> incident OPEN, entry A bị chặn, audit có, không có
hàng ledger cho state lạ; vị thế đã biết của A vẫn exit; B tiếp tục; resolution của owner (lý do,
MFA) + inspect sạch -> operator resume A với MFA -> entry lại được; venue adapter bị khóa.
"""

from __future__ import annotations

from tests.e2e.multibot_runtime_harness import ComposeHarness


def test_foreign_activity_blocks_entry_without_adoption_until_resolution_and_resume() -> None:
    harness = ComposeHarness("activity")
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        setup = harness.probe("runtime_activity_setup")
        assert setup["a_fill"]["status"] == "FILLED", setup
        harness.state |= setup
        checkpoints_before = harness.inspect()["checkpoints"]

        foreign = harness.probe("runtime_activity_foreign")
        assert foreign["view"]["summary"]["activity_blocked"] == 1, foreign
        assert foreign["a_entry"] == {"error": "EXTERNAL_ACTIVITY_UNRECONCILED"}
        assert foreign["a_stop"] == {"exit_reason": "STOP_LOSS", "closed": True}
        assert "intent_id" in foreign["b_entry"], foreign
        assert foreign["venue"] == {"error": "VENUE_ACTIVITY_ADAPTER_DISABLED"}
        db = harness.inspect()
        assert db["incidents"]["a"] == ["OPEN"]
        assert "b" not in db["incidents"]
        assert db["states"]["a"][1] == "EXTERNAL_ACTIVITY_UNRECONCILED"
        assert db["foreign_orders_in_ledger"] == 0
        assert db["trades"]["a"] == ["CLOSED"]  # chỉ trade đã biết, không adopt vị thế lạ
        assert ["bot.activity.external_detected"] == [
            audit[0] for audit in db["audits"] if audit[0].startswith("bot.activity")
        ]
        # Chỉ thêm checkpoint của exit đã biết (A) và entry của B.
        assert db["checkpoints"]["a"] == checkpoints_before["a"] + 1

        resolved = harness.probe("admin_activity_resolve", "admin-web")
        assert resolved["operator_pause_a"] == [1, True], resolved
        assert resolved["operator_resolve"] == {"error": "CONTROL_PERMISSION_DENIED"}
        assert resolved["owner_resolve"] == "RESOLVED"
        assert resolved["operator_resume"] == [2, True]

        clean = harness.probe("runtime_activity_clean")
        assert clean["view"]["summary"]["activity_blocked"] == 0, clean
        assert "intent_id" in clean["a_entry"], (clean, harness.last_stderr)
        db = harness.inspect()
        assert db["incidents"]["a"] == ["RESOLVED"]
        assert db["states"]["a"][1] is None
        assert ["bot.activity.resolved", None, "Đã hủy lệnh tay và đóng vị thế trên sàn"] in [
            [audit[0], None, audit[2]] for audit in db["audits"]
        ]
    finally:
        harness.down()
