"""MB05.9: một writer/account với hai process runtime thật và `docker pause` (worker bị treo).

Chứng minh: container 2 ở STANDBY khi container 1 giữ lease; pause container 1 lâu hơn TTL ->
container 2 lấy lease (token +1) và hydrate; unpause container 1 -> mọi lệnh của nó bị từ chối
`STALE_FENCING_TOKEN` (renew, fence application, trigger DB khi bỏ qua application), không có
ghi ledger/checkpoint nào, container 1 tự hạ bot về STANDBY (Kleppmann 2016; docker pause).

Fix round 1 (I2): worker treo *giữa transaction* (giữ L1 + `FOR SHARE` hàng lease) dưới engine
production: chờ khóa của bot khác thất bại có giới hạn (`lock_timeout`), PostgreSQL cắt phiên treo
sau `idle_in_transaction_session_timeout`, rồi L1/khóa lease đi tiếp và container 2 hồi `ready`.
"""

from __future__ import annotations

import time

from tests.e2e.multibot_runtime_harness import ComposeHarness

RUNTIME_ENV = {
    "TRADING_BOT_RUNTIME_LEASE_TTL_SECONDS": "6",
    "TRADING_BOT_RUNTIME_SUPERVISOR_TICK_SECONDS": "1",
}


def _counts(status: int, body: dict, **expected: int) -> bool:
    counts = body.get("paper_accounts", {})
    return status == 200 and all(counts.get(key) == value for key, value in expected.items())


def test_paused_worker_loses_lease_and_is_fenced_out() -> None:
    harness = ComposeHarness("lease")
    containers: list[str] = []
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        workspace = harness.state["workspaces"]["main"]["workspace"]

        first = harness.run_runtime(workspace, **RUNTIME_ENV)
        containers.append(first)
        harness.wait_health(first, lambda s, b: _counts(s, b, ready=2) and b["status"] == "ready")
        second = harness.run_runtime(workspace, **RUNTIME_ENV)
        containers.append(second)
        standby = harness.wait_health(second, lambda s, b: _counts(s, b, standby=2))
        assert standby["status"] == "degraded"
        held = harness.inspect()["leases"]
        assert {value[1] for value in held.values()} == {1}

        assert harness.docker("pause", first).returncode == 0
        try:
            harness.wait_health(second, lambda s, b: _counts(s, b, ready=2), timeout=60)
            taken = harness.inspect()
            assert {key: value[1] for key, value in taken["leases"].items()} == {"a": 2, "b": 2}
            assert {value[0] for value in taken["leases"].values()} != {
                value[0] for value in held.values()
            }
            counts_before = (taken["checkpoints"], taken["intents"], taken["trades"])
            time.sleep(2)
        finally:
            assert harness.docker("unpause", first).returncode == 0

        harness.wait_health(first, lambda s, b: _counts(s, b, standby=2, ready=0), timeout=60)
        harness.state["old_leases"] = held
        stale = harness.probe("lease_stale_writes")
        for key in ("a", "b"):
            assert stale[f"{key}_application"] == {"error": "STALE_FENCING_TOKEN"}, stale
            assert stale[f"{key}_raw_insert"] == {"error": "MB05_STALE_FENCING_TOKEN"}, stale
        final = harness.inspect()
        assert (final["checkpoints"], final["intents"], final["trades"]) == counts_before
        lost = [audit for audit in final["audits"] if audit[0] == "bot.runtime.lease_lost"]
        assert {audit[1] for audit in lost} >= {"a", "b"}, final["audits"]
        assert {audit[2] for audit in lost} == {"STALE_FENCING_TOKEN"}
        # Process mới vẫn là writer duy nhất: container 2 READY, container 1 STANDBY.
        assert _counts(*harness.health(second), ready=2)

        frozen = harness.probe("frozen_worker")
        # TTL 6s -> lock 1s, idle-in-transaction 2s, statement 3s (đặt khi mở kết nối).
        assert frozen["session_settings"] == ["1s", "2s", "3s"], frozen
        for key in ("blocked_l1_b", "blocked_lease_a"):
            assert frozen[key][0] == "SQLSTATE_55P03", frozen
            assert 0.8 <= frozen[key][1] < 2.5, frozen
        for key in ("after_l1_b", "after_lease_a"):
            assert frozen[key][0] == "OK", frozen
            assert frozen[key][1] < 1.0, frozen
        assert frozen["frozen"] != "COMMITTED", frozen
        # Runtime thật (container 2, cùng timeout) bị chặn tạm thời rồi tự hồi, giữ nguyên lease.
        harness.wait_health(
            second, lambda s, b: _counts(s, b, ready=2) and b["status"] == "ready", timeout=30
        )
        after = harness.inspect()
        assert {key: value[1] for key, value in after["leases"].items()} == {"a": 2, "b": 2}
    finally:
        for container in containers:
            harness.docker("unpause", container)
            harness.docker("rm", "--force", container)
        harness.down()
