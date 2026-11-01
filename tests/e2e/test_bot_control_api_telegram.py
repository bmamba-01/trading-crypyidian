"""MB07a.5: Admin API điều khiển bot + webhook Telegram trên Compose thật.

Chứng minh (ruling 30–41):
- Admin API thật (cookie phiên + CSRF, `AuthService`, role `crypidian_admin_web`, timeout admin-web)
  ghi control qua cùng service MB-05: operator pause A → runtime chặn entry mới của A, hủy lệnh
  chưa khớp, stop của vị thế mở vẫn fill, B không bị ảnh hưởng (tái dùng pha probe MB-05);
- ma trận quyền: operator không pause B (404 như bot lạ), analyst/strategy_admin không pause, chỉ
  owner/risk_admin kill; release cần MFA gần đây và quyền `workspace:kill`; thiếu CSRF → 403; member
  bị khóa → 401; disable cần owner và bị `BOT_NOT_FLAT` khi còn vị thế, thành công khi đã phẳng;
- webhook Telegram: sai secret → 401, replay `update_id` → duplicate, ID lạ không fallback, lệnh
  mơ hồ liệt kê ID, user bind ở workspace khác không điều khiển được, group/edited bị bỏ, resume
  qua Telegram bị từ chối vì thiếu MFA, `/flatten` bị từ chối;
- audit đủ cho thao tác/từ chối; không token/secret trong response, reply hay audit.
"""

from __future__ import annotations

from typing import Any

from tests.e2e.multibot_runtime_harness import ROOT, ComposeHarness


class BotControlHarness(ComposeHarness):
    """Probe MB-05 (pha runtime tái dùng) + pha MB-07a nối sau, gửi qua stdin."""

    def __init__(self) -> None:
        super().__init__("mb07a")
        base = (ROOT / "tests/e2e/probes/multibot_runtime.py").read_text("utf-8")
        marker = 'if __name__ == "__main__":\n    _main()'
        assert marker in base, "Probe MB-05 đổi khối __main__; cập nhật harness MB-07a."
        extra = (ROOT / "tests/e2e/probes/bot_control_api.py").read_text("utf-8")
        self.probe_code = base.replace(marker, "pass") + "\n\n" + extra


def _actions(audits: list[list[Any]]) -> list[str]:
    return [row[0] for row in audits]


def test_admin_api_and_telegram_share_runtime_control_plane() -> None:
    harness = BotControlHarness()
    try:
        harness.start("admin-web")
        harness.state = harness.probe("seed", "migrator", variants={"main": "1", "other": "1"})
        harness.probe("genesis")
        setup = harness.probe("runtime_control_setup")
        assert setup["a_fill"]["status"] == "FILLED", setup
        harness.state |= setup
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        bots = harness.state["workspaces"]["main"]["bots"]

        paused = harness.probe("mb07a_admin_pause", "admin-web")
        assert paused["no_csrf"] == 403, paused
        assert paused["suspended"] == 401, paused
        assert paused["operator_pause_a"][0] == 200, paused
        assert paused["operator_pause_a"][1]["entry_state"] == "PAUSED"
        assert paused["operator_pause_a"][1]["revision"] == 1
        assert paused["operator_pause_a_again"] is False
        assert paused["operator_pause_b"] == [404, "BOT_NOT_FOUND"]
        assert paused["analyst_pause_a"] == [404, "BOT_NOT_FOUND"]
        assert paused["strategy_admin_pause_a"] == [404, "BOT_NOT_FOUND"]
        assert paused["unknown_matches_ungranted"] is True
        for who in ("operator", "strategy_admin", "analyst_reviewer"):
            assert paused[f"{who}_kill"] == [403, "CONTROL_PERMISSION_DENIED"], who
        assert paused["operator_status_a"]["entry_state"] == "PAUSED"
        assert paused["operator_status_a"]["entry_allowed"] is False
        assert paused["analyst_status_b"] == 404
        assert paused["analyst_list"] == [bots["a"]]
        assert paused["risk_admin_disable"] == "CONTROL_PERMISSION_DENIED"
        assert paused["owner_disable_not_flat"][0] == 409, paused
        not_flat = paused["owner_disable_not_flat"][1]
        assert not_flat["code"] == "BOT_NOT_FLAT"
        assert not_flat["open_positions"] >= 1
        assert paused["no_secret_leak"] is True

        # Pause ghi qua Admin API được runtime áp dụng: entry A bị chặn, exits vẫn chạy, B chạy.
        after_pause = harness.probe("runtime_after_pause")
        assert after_pause["a2_status"] == "CANCELLED", after_pause
        assert after_pause["a_entry"] == {"error": "BOT_PAUSED"}
        assert after_pause["a_stop"] == {"exit_reason": "STOP_LOSS", "closed": True}
        assert after_pause["b_fill"]["status"] == "FILLED"
        assert "intent_id" in after_pause["b_entry"], after_pause
        harness.state |= {"b_fill": after_pause["b_fill"]}

        killed = harness.probe("mb07a_admin_kill", "admin-web")
        assert killed["risk_admin_kill"][0] == 200, killed
        assert killed["risk_admin_kill"][1]["kill_state"] == "KILLED"
        assert killed["owner_kill_again_changed"] is False
        assert killed["risk_admin_stale_release"] == [403, "RECENT_MFA_REQUIRED"]
        assert killed["operator_release"] == [403, "CONTROL_PERMISSION_DENIED"]
        assert killed["strategy_admin_release"] == [403, "CONTROL_PERMISSION_DENIED"]
        assert killed["no_secret_leak"] is True

        after_kill = harness.probe("runtime_after_kill")
        assert after_kill["view"]["workspace_control"] == "killed", after_kill
        assert after_kill["a_entry"] == {"error": "WORKSPACE_KILLED"}
        assert after_kill["b_entry"] == {"error": "WORKSPACE_KILLED"}
        assert after_kill["b_stop"] == {"exit_reason": "STOP_LOSS", "closed": True}

        tg = harness.probe("mb07a_release_and_telegram", "admin-web")
        assert tg["risk_admin_release"][0] == 200, tg
        assert tg["risk_admin_release"][1]["kill_state"] == "ACTIVE"
        assert tg["tg_wrong_secret"] == [401, "unauthorized"]
        assert tg["tg_list"] == [200, "processed"]
        assert bots["a"] in tg["tg_list_reply"]
        assert bots["b"] not in tg["tg_list_reply"]
        assert tg["tg_operator_no_target"] == [200, "processed"]
        assert bots["a"] in tg["tg_operator_no_target_reply"]
        assert bots["a"] in tg["tg_owner_ambiguous_reply"]
        assert bots["b"] in tg["tg_owner_ambiguous_reply"]
        assert tg["tg_unknown_reply"] == "Không tìm thấy bot."
        assert tg["tg_owner_pause_b"] == [200, "processed"]
        assert tg["tg_owner_pause_b_replay"] == [200, "duplicate"]
        assert "Admin Web" in tg["tg_owner_resume_b_reply"]
        assert "không được phép" in tg["tg_flatten_reply"]
        assert tg["tg_other_workspace"] == [200, "unbound"]
        assert tg["tg_group"] == [200, "rejected"]
        assert tg["tg_edited"] == [200, "ignored"]
        assert tg["status_b_entry_state"] == "PAUSED"
        assert tg["status_a_before_disable"] == "PAUSED"
        assert tg["owner_disable_a"] == [
            200,
            {"bot": {"id": bots["a"], "status": "DISABLED", "changed": True}},
        ], tg
        assert tg["enable_config_draft"][0] == 201
        assert tg["enable_config_validation"]["body"]["valid"] is True
        assert tg["enable_config_publish"]["status"] == 200
        assert tg["owner_disable_a_again"] == {
            "bot": {"id": bots["a"], "status": "DISABLED", "changed": False}
        }
        enable_snapshot = tg["enable_snapshot"]
        assert enable_snapshot["account"] == ["DISABLED", "PAPER", "binance_usdm", "DISABLED"]
        assert enable_snapshot["runtime"][0:2] == ["READY", "VERIFIED"]
        assert enable_snapshot["runtime"][2] == enable_snapshot["bot_revision"]
        assert enable_snapshot["incidents"] == 0
        assert enable_snapshot["published_configs"] >= 1
        assert tg["owner_enable_a"] == [
            200,
            {"bot": {"id": bots["a"], "status": "ENABLED", "changed": True}},
        ], {"result": tg["owner_enable_a"], "snapshot": tg["enable_snapshot"]}
        assert tg["status_a_after_enable"] == "ENABLED"
        assert tg["no_secret_leak"] is True
        assert tg["replies_leak"] is False

        db = harness.probe("mb07a_inspect", "migrator")
        actions = _actions(db["audits"])
        for action in (
            "bot.runtime.paused",
            "bot.runtime.paused.noop",
            "bot.runtime.control_denied",
            "workspace.runtime.killed",
            "workspace.runtime.killed.noop",
            "workspace.runtime.released",
            "bot.lifecycle.disable_denied",
            "bot.disabled",
            "bot.disabled.noop",
            "bot.enabled",
            "telegram.command.accepted",
            "telegram.command.rejected",
            "telegram.update.rejected",
        ):
            assert action in actions, (action, actions)
        reasons = {row[1] for row in db["audits"]}
        assert {"BOT_NOT_FLAT", "RECENT_MFA_REQUIRED", "TELEGRAM_NOT_BOUND"} <= reasons
        assert "pause:BOT_TARGET_AMBIGUOUS" in reasons
        assert "resume:RECENT_MFA_REQUIRED" in reasons
        # Mỗi update đúng secret được claim đúng một lần (kể cả replay, group, edited).
        assert db["receipts"] == 12, db["receipts"]  # update 101–112; sai secret không claim
        assert db["bot_status"]["a"] == ["ENABLED", "ENABLED"]
        assert db["bot_status"]["b"] == ["ENABLED", "ENABLED"]
        b_controls = [row for row in db["controls"] if row[0] == bots["b"]]
        assert [row[1:3] for row in b_controls] == [[1, "PAUSED"]]
        assert db["role_after_reenable"] != "crypidian_admin_web"
        assert db["audit_leak"] is False
        assert db["user_text_in_audit"] is False
    finally:
        harness.down()


def test_flatten_operation_is_processed_by_fenced_paper_runtime() -> None:
    harness = BotControlHarness()
    try:
        harness.start("admin-web")
        harness.state = harness.probe(
            "seed",
            "migrator",
            variants={"main": "1", "other": "1"},
            fresh_mark_only=True,
        )
        harness.probe("genesis")
        setup = harness.probe("runtime_control_setup")
        harness.state |= setup
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        harness.probe("mb07b_seed_fresh_mark", "migrator")
        harness.environment["TRADING_BOT_RUNTIME_WORKSPACE_ID"] = harness.state["workspaces"][
            "main"
        ]["workspace"]
        harness.compose("up", "--detach", "--wait", "--wait-timeout", "180", "trading-runtime")
        result = harness.probe("mb07b_flatten_api", "admin-web")
        harness.state["mb07b_operation_id"] = result["operation"]["id"]
        database = harness.probe("mb07b_flatten_inspect", "migrator")

        assert result["preview"]["position_count"] == 1, result
        assert result["operation"]["status"] == "COMPLETED", {
            "status": result["operation"]["status"],
            "items": [
                (item["target_type"], item["status"], item["error_code"], item["result"])
                for item in result["operation"]["items"]
            ],
        }
        assert result["operation"]["items"]
        assert all(item["status"] == "COMPLETED" for item in result["operation"]["items"]), result
        assert any(
            item["target_type"] == "POSITION" and item["result"]["outcome"] == "CLOSED"
            for item in result["operation"]["items"]
        ), result
        assert database["status"] == "COMPLETED", database
        assert database["exit_fill_count"] == 1, database
    finally:
        harness.down()


def test_telegram_binding_link_revoke_rebind_and_locked_member_are_database_backed() -> None:
    harness = BotControlHarness()
    try:
        harness.start("admin-web")
        harness.state = harness.probe("seed", "migrator", variants={"main": "1", "other": "1"})
        harness.probe("genesis")
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")

        result = harness.probe("mb07b_telegram_binding_lifecycle", "admin-web")

        assert result["rebind_history"] == 3, result
        assert result["active_owner_binding"] == "7000004", result
        assert result["link_audit_count"] >= 6, result
        assert result["wrong_workspace_status"] == "link_invalid", result
        harness.probe("mb07b_telegram_lock_seed", "migrator")
        locked = harness.probe("mb07b_telegram_lock_webhook", "admin-web")
        assert locked == {"webhook_status": "unbound", "active_binding_count": 0}, locked
    finally:
        harness.down()


def test_enable_pause_and_disable_pause_are_serialized_by_workspace_l1() -> None:
    """Các mutation cạnh tranh phải xếp hàng ở L1 và giữ cả hai trạng thái cuối."""
    harness = BotControlHarness()
    try:
        harness.start("admin-web")
        harness.state = harness.probe("seed", "migrator", variants={"main": "1", "other": "1"})
        harness.probe("genesis")
        runtime = harness.probe("mb07b_enable_race_runtime_setup")
        assert runtime["closed"]["closed"] is True, runtime
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")

        prepared = harness.probe("mb07b_enable_race_prepare", "admin-web")
        assert prepared["published"]["status"] == 200, prepared
        assert prepared["disabled"]["status"] == 200, prepared

        result = harness.probe("mb07b_enable_pause_disable_race", "admin-web")
        assert result["enable_pause"]["queued_on_l1"] is True, result
        assert result["enable_pause"]["first"]["status"] == 200, result
        assert result["enable_pause"]["second"]["status"] == 200, result
        assert result["resume"]["status"] == 200, result
        assert result["disable_pause"]["queued_on_l1"] is True, result
        assert result["disable_pause"]["first"]["status"] == 200, result
        assert result["disable_pause"]["second"]["status"] == 200, result
        final_bot = result["final_status"]["body"]["bot"]
        assert final_bot["status"] == "DISABLED", final_bot
        assert final_bot["entry_state"] == "PAUSED", final_bot
    finally:
        harness.down()
