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
        assert tg["owner_disable_a_again"] == {
            "bot": {"id": bots["a"], "status": "DISABLED", "changed": False}
        }
        assert tg["status_a_after_disable"] == "DISABLED"
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
        assert db["bot_status"]["a"] == ["DISABLED", "DISABLED"]
        assert db["bot_status"]["b"] == ["ENABLED", "ENABLED"]
        b_controls = [row for row in db["controls"] if row[0] == bots["b"]]
        assert [row[1:3] for row in b_controls] == [[1, "PAUSED"]]
        assert db["admin_web_reenable"] == "MB07A_ACCOUNT_STATUS_TRANSITION_DENIED", db
        assert db["role_after_reenable"] != "crypidian_admin_web"
        assert db["audit_leak"] is False
        assert db["user_text_in_audit"] is False
    finally:
        harness.down()
