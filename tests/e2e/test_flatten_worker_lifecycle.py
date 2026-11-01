"""MB-07b: kiểm preview cũ, idempotency và phục hồi ledger sau restart runtime."""

from __future__ import annotations

from tests.e2e.multibot_runtime_harness import ROOT, ComposeHarness


class FlattenLifecycleHarness(ComposeHarness):
    def __init__(self) -> None:
        super().__init__("mb07b-flatten-lifecycle")
        base = (ROOT / "tests/e2e/probes/multibot_runtime.py").read_text("utf-8")
        marker = 'if __name__ == "__main__":\n    _main()'
        assert marker in base, "Probe MB-05 đổi entrypoint; cập nhật harness flatten lifecycle."
        bot_control = (ROOT / "tests/e2e/probes/bot_control_api.py").read_text("utf-8")
        lifecycle = (ROOT / "tests/e2e/probes/flatten_worker_lifecycle.py").read_text("utf-8")
        dispatch = "_main()"
        bot_control_stripped = bot_control.rstrip()
        assert bot_control_stripped.endswith(dispatch), (
            "Probe MB-07a đổi entrypoint; cập nhật harness."
        )
        bot_control_without_dispatch = bot_control_stripped[: -len(dispatch)]
        self.probe_code = (
            base.replace(marker, "pass")
            + "\n\n"
            + bot_control_without_dispatch
            + "\n\n"
            + lifecycle
            + "\n\n_main()\n"
        )


def test_flatten_stale_replay_and_runtime_restart_recovery() -> None:
    harness = FlattenLifecycleHarness()
    try:
        harness.start("admin-web")
        harness.state = harness.probe(
            "seed",
            "migrator",
            variants={"main": "1", "other": "1"},
            fresh_mark_only=True,
        )
        harness.probe("genesis")
        harness.state |= harness.probe("mb07b_flatten_single_position_setup")
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        harness.probe("mb07b_seed_fresh_mark", "migrator")
        workspace_id = harness.state["workspaces"]["main"]["workspace"]
        harness.environment["TRADING_BOT_RUNTIME_WORKSPACE_ID"] = workspace_id

        # Khởi tạo worker rồi dừng process trước khi admin tạo operation.
        harness.compose("up", "--detach", "--wait", "--wait-timeout", "180", "trading-runtime")
        harness.compose("stop", "trading-runtime")

        stale = harness.probe("mb07b_flatten_stale_preview", "admin-web")
        harness.state["mb07b_lifecycle_stale_preview"] = stale["preview"]
        harness.probe("mb07b_seed_fresh_mark", "migrator")
        staged = harness.probe("mb07b_flatten_stale_and_queue", "admin-web")
        assert staged["stale_code"] == "FLATTEN_PREVIEW_STALE", staged
        assert staged["preview"]["position_count"] == 1, staged
        assert staged["preview"]["open_order_count"] == 0, staged
        assert staged["operation"]["status"] == "QUEUED", staged
        assert staged["replay"]["id"] == staged["operation"]["id"], staged
        assert staged["replay"]["created"] is False, staged
        assert staged["key_reuse_code"] == "FLATTEN_IDEMPOTENCY_KEY_REUSED", staged
        harness.state["mb07b_lifecycle_operation_id"] = staged["operation"]["id"]

        harness.compose("start", "trading-runtime")
        completed = harness.probe("mb07b_flatten_lifecycle_wait", "admin-web")
        assert completed["status"] == "COMPLETED", completed
        assert completed["items"]
        assert all(item["status"] == "COMPLETED" for item in completed["items"]), completed

        inspected = harness.probe("mb07b_flatten_lifecycle_inspect", "migrator")
        assert inspected["status"] == "COMPLETED", inspected
        assert inspected["exit_fills"] == 1, inspected
        assert inspected["items"]
        assert all(item[0] == "COMPLETED" and item[1] >= 1 for item in inspected["items"]), (
            inspected
        )
    finally:
        harness.down()


def test_flatten_duplicate_workers_are_fenced_and_restart_safe() -> None:
    harness = FlattenLifecycleHarness()
    try:
        harness.start("admin-web")
        harness.state = harness.probe(
            "seed",
            "migrator",
            variants={"main": "1", "other": "1"},
            fresh_mark_only=True,
        )
        harness.probe("genesis")
        harness.state |= harness.probe("runtime_control_setup")
        harness.state |= harness.probe("mb07a_auth_seed", "migrator")
        harness.probe("mb07b_seed_fresh_mark", "migrator")

        staged = harness.probe("mb07b_flatten_duplicate_stage", "admin-web")
        harness.state["mb07b_duplicate_operation_id"] = staged["operation"]["id"]

        result = harness.probe("mb07b_flatten_duplicate_claim", "trading-runtime")

        assert staged["operation"]["status"] == "QUEUED", staged
        assert sum(result["worker_results"]) == len(result["first"]["items"]), result
        assert result["first"]["status"] == "COMPLETED", result
        assert result["first"]["items"]
        assert all(
            item_status == "COMPLETED" and attempt_count == 1
            for item_status, attempt_count in result["first"]["items"]
        ), result
        assert result["first"]["exits"] == 1, result
        assert result["after_restart_result"] == 0, result
        assert result["after_restart"]["status"] == "COMPLETED", result
        assert result["after_restart"]["exits"] == 1, result
    finally:
        harness.down()
