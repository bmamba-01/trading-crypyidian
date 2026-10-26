"""MB05.9: restart giữ state cô lập theo bot, lỗi sibling bị cô lập, ngân sách MB-04 tại runtime.

Chứng minh (Compose, PostgreSQL thật, role `crypidian_trading_runtime`):
- restart dựng lại gateway theo checkpoint với lease mới (token +1), pin settings/strategy/bot
  config giữ nguyên; bot DISABLED không hydrate/không lease;
- checkpoint hỏng của B chỉ làm B `RECOVERY_REQUIRED` (state + audit), A vẫn monitor/entry,
  readiness 200 `degraded` không lộ ID;
- `WorkspaceRiskCoordinator` (MB-04) nằm trên đường entry runtime: cap workspace giữ ở runtime;
- checkpoint NULL token chỉ hợp lệ cho genesis (M3, migration `e1f3a5c7b9d2`).
"""

from __future__ import annotations

import json
import re

from tests.e2e.multibot_runtime_harness import ComposeHarness

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_restart_isolation_sibling_failure_and_runtime_workspace_caps() -> None:
    harness = ComposeHarness("restart")
    try:
        harness.start()
        harness.compose("run", "--rm", "--no-deps", "migrator", "alembic", "check")
        harness.state = harness.probe("seed", "migrator", variants={"main": "1", "caps": "0.0075"})
        genesis = harness.probe("genesis")
        assert genesis["main_a_null_revision_2"] == {"error": "MB05_FENCING_TOKEN_REQUIRED"}
        assert genesis["main_b_null_revision_2"] == {"error": "MB05_FENCING_TOKEN_REQUIRED"}
        # Gap (a): trigger dùng chung trên bảng không có cột `revision` trả đúng mã fence (không
        # phải SQLSTATE 42703 "record new has no field revision").
        for key in ("a", "b"):
            for table in ("bot_runtime_state", "account_activity_incident"):
                assert genesis[f"main_{key}_{table}_null"] == {
                    "error": "MB05_FENCING_TOKEN_REQUIRED"
                }, genesis

        setup = harness.probe("runtime_restart_setup")
        assert setup["start"]["summary"]["total"] == 2, setup  # C DISABLED không có trong registry
        assert setup["start"]["status"] == "ready"
        assert setup["a_fill"]["status"] == "FILLED"
        assert setup["a1"]["revisions"] == {"workspace": 0, "bot": 0}
        harness.state |= {"a1": setup["a1"], "b1": setup["b1"], "a_fill": setup["a_fill"]}
        before = harness.inspect()
        assert set(before["leases"]) == {"a", "b"}  # bot C không có lease
        tokens_before = {key: value[1] for key, value in before["leases"].items()}
        intents_before = before["intents"]

        restarted = harness.probe("runtime_restart_check")
        tokens_after = {key: value["token"] for key, value in restarted["view"]["entries"].items()}
        assert tokens_after == {key: value + 1 for key, value in tokens_before.items()}, (
            tokens_before,
            restarted,
        )
        assert restarted["a_position_open"] is True
        assert restarted["a_position_stop"] == setup["a1"]["stop"]
        assert restarted["b_order_status"] == "ACCEPTED"
        assert restarted["a_bot_config_version_id"] is None  # root-fallback được ghim lại
        assert harness.inspect()["intents"] == intents_before

        corrupt = harness.probe("runtime_corrupt_b")
        assert corrupt["released"] is True
        after = harness.probe("runtime_after_corruption")
        entries = after["view"]["entries"]
        assert entries["a"]["readiness"] == "READY"
        assert entries["b"]["readiness"] == "RECOVERY_REQUIRED"
        assert entries["b"]["reason"] == "PAPER_RECOVERY_CONFLICT"
        assert after["health_status"] == 200
        assert after["health"]["status"] == "degraded"
        assert after["health"]["paper_accounts"]["ready"] == 1
        assert after["health"]["paper_accounts"]["recovery_required"] == 1
        assert not UUID_PATTERN.search(json.dumps(after["health"]))
        assert after["a_stop"] == {"exit_reason": "STOP_LOSS", "closed": True}
        if "intent_id" not in after["a2"]:
            print(json.dumps(harness.probe("diagnose_fills", "migrator"), indent=1))
        assert "intent_id" in after["a2"], (after["a2"], harness.last_stderr)
        assert after["b_entry"] == {"error": "PAPER_ACCOUNT_NOT_READY"}
        final = harness.inspect()
        assert final["states"]["b"][0:2] == ["RECOVERY_REQUIRED", "PAPER_RECOVERY_CONFLICT"]
        assert ["bot.runtime.recovery_required", "b", "PAPER_RECOVERY_CONFLICT"] in final["audits"]
        assert any(audit[0] == "bot.runtime.lease_acquired" for audit in final["audits"])
        # Lineage của A và B không đổi qua restart/hỏng sibling (chỉ thêm intent mới của A).
        assert final["intents"]["b"] == intents_before["b"]
        assert final["intents"]["a"][: len(intents_before["a"])] == intents_before["a"]
        assert final["trades"]["a"] == ["CLOSED"]

        caps = harness.probe("runtime_caps")
        assert "intent_id" in caps["a1"], caps
        assert caps["b1"] == {"blocked": "WORKSPACE_CAP_EXCEEDED"}, caps
        caps_db = harness.inspect("caps")
        assert [row[:2] for row in caps_db["budget_decisions"]] == [
            [True, "APPROVED"],
            [False, "WORKSPACE_CAP_EXCEEDED"],
        ]
        assert caps_db["budget_decisions"][0][2] == {"workspace": 0, "bot": 0}
        assert "b" not in caps_db["intents"]
        assert caps_db["reservations"]["a"] == [["RESERVED", None]]
    finally:
        harness.down()
