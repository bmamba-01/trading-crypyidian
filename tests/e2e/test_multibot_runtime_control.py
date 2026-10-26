"""MB05.9: pause bot, kill workspace, resume và bất biến ruling 27 trên PostgreSQL thật.

Chứng minh:
- `operator` pause A qua service chạy dưới role `crypidian_admin_web` thật; runtime giữ lease áp
  dụng trong một transaction có fence: order chưa khớp hủy, reservation giải phóng, stop của vị
  thế mở vẫn fill; B không bị ảnh hưởng; operator pause B -> BOT_NOT_FOUND (SEC-19);
- `risk_admin` kill workspace -> A và B chặn entry, exits vẫn chạy (SECT-25); release/resume
  thiếu MFA gần đây hoặc operator release -> từ chối (SECT-26); dừng admin-web, runtime vẫn đọc
  kill từ DB và giữ exits (SECT-27); `ensure_flat_for_disable` -> BOT_NOT_FLAT (ruling 29);
- race entry/fill (runtime role) với pause/kill (admin role) có barrier, đổi thứ tự xuất phát:
  không entry/fill nào commit sau commit pause/kill, không deadlock, không reservation RESERVED
  sót sau apply (ruling 27).
"""

from __future__ import annotations

from tests.e2e.multibot_runtime_harness import ComposeHarness


def test_pause_kill_resume_permissions_exits_and_ruling_27_race() -> None:
    harness = ComposeHarness("control")
    try:
        harness.start("admin-web")
        harness.state = harness.probe("seed", "migrator", variants={"main": "1", "race": "1"})
        harness.probe("genesis")

        setup = harness.probe("runtime_control_setup")
        assert setup["a_fill"]["status"] == "FILLED", setup
        assert "intent_id" in setup["a2"], setup
        assert "intent_id" in setup["b1"], setup
        harness.state |= setup

        paused = harness.probe("admin_pause", "admin-web")
        assert paused["operator_pause_a"] == [1, True], paused
        assert paused["operator_pause_b"] == {"error": "BOT_NOT_FOUND"}
        assert paused["operator_kill"] == {"error": "CONTROL_PERMISSION_DENIED"}
        assert paused["analyst_pause_a"] == {"error": "BOT_NOT_FOUND"}
        assert paused["strategy_admin_kill"] == {"error": "CONTROL_PERMISSION_DENIED"}
        assert paused["guard_a"] == ["BOT_NOT_FLAT", 1, 1, 1], paused

        after_pause = harness.probe("runtime_after_pause")
        assert after_pause["a2_status"] == "CANCELLED", after_pause
        assert after_pause["view"]["summary"]["paused"] == 1
        assert after_pause["view"]["status"] == "degraded"
        assert after_pause["a_entry"] == {"error": "BOT_PAUSED"}
        assert after_pause["a_stop"] == {"exit_reason": "STOP_LOSS", "closed": True}
        assert after_pause["b_fill"]["status"] == "FILLED"
        assert "intent_id" in after_pause["b_entry"], after_pause
        harness.state |= {"b_fill": after_pause["b_fill"]}
        db = harness.inspect()
        assert db["reservations"]["a"] == [
            ["TRANSFERRED", "OPEN_RISK_RECORDED"],
            ["RELEASED", "CANCELLED_NO_FILL"],
        ], db["reservations"]
        assert db["states"]["a"][2] == 1  # applied_bot_control_revision
        assert db["intents"]["a"][0][1] == {"workspace": 0, "bot": 0}
        audits = [audit[0] for audit in db["audits"]]
        assert "bot.runtime.paused" in audits
        assert "bot.runtime.pause_applied" in audits
        assert audits.count("bot.runtime.control_denied") >= 4

        killed = harness.probe("admin_kill", "admin-web")
        assert killed["risk_admin_kill"] == [1, True], killed
        assert killed["risk_admin_kill_again"] == [1, False]
        assert killed["release_stale_mfa"] == {"error": "RECENT_MFA_REQUIRED"}
        assert killed["operator_release"] == {"error": "CONTROL_PERMISSION_DENIED"}
        assert killed["operator_resume_stale_mfa"] == {"error": "RECENT_MFA_REQUIRED"}

        harness.compose("stop", "admin-web")
        running = harness.compose("ps", "--status", "running", "--services").stdout
        assert "admin-web" not in running.split()
        after_kill = harness.probe("runtime_after_kill")
        assert after_kill["view"]["workspace_control"] == "killed", after_kill
        assert after_kill["a_entry"] == {"error": "WORKSPACE_KILLED"}
        assert after_kill["b_entry"] == {"error": "WORKSPACE_KILLED"}
        assert after_kill["b_stop"] == {"exit_reason": "STOP_LOSS", "closed": True}
        db = harness.inspect()
        assert db["reservations"]["b"][-1] == ["RELEASED", "CANCELLED_NO_FILL"], db
        assert db["states"]["b"][3] == 1  # applied_workspace_control_revision

        race = harness.probe("race", "migrator")
        rounds = race["rounds"]
        assert len(rounds) == 8
        for item in rounds:
            runtime, admin = item["runtime"], item["admin"]
            assert admin[1] is True, item
            old = admin[0] - 1
            if item["mode"] == "entry":
                if "intent_id" in runtime:
                    # Entry đọc control cũ dưới L1 => commit trước pause; apply hủy order sau đó.
                    assert runtime["revisions"][item["kind"]] == old, item
                    assert item["runtime_before_admin"] is True, item
                    assert item["applied_cancelled"] == 1, item
                else:
                    expected = "BOT_PAUSED" if item["kind"] == "bot" else "WORKSPACE_KILLED"
                    assert runtime == {"error": expected}, item
            else:
                assert runtime["status"] in {"FILLED", "CANCELLED"}, item
                if runtime["status"] == "FILLED":
                    assert runtime["fills"] == 1
                    assert item["runtime_before_admin"] is True, item
        race_db = harness.inspect("race")
        statuses = {row[0] for row in race_db["reservations"]["a"]}
        assert statuses <= {"RELEASED", "TRANSFERRED"}, race_db["reservations"]
        assert {row[0] for row in race_db["intents"]["a"]} <= {"CANCELLED", "FILLED"}
        print({"race_outcomes": [(item["mode"], item["kind"], item["runtime"]) for item in rounds]})
    finally:
        harness.down()
