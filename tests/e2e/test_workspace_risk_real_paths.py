"""MB-04 fix (review C1, C2, C3, I5): ngân sách rủi ro qua đường thật trên Compose PostgreSQL.

Không dùng loader trần giả hay lineage trade viết tay:
- C1: overlay bot publish qua `BotConfigAdminService` (role `crypidian_admin_web`), resolver thật
  trong coordinator của runtime PAPER thật phải áp `bot_max_open_risk_fraction`.
- C2: vị thế mở do ledger thật tạo (entry → fill → trade OPEN, reservation TRANSFERRED) phải
  chiếm cap account theo `reserved_risk` của reservation liên kết.
- C3: `evaluated_at` cũ hơn lần siết root đã commit vẫn phải thấy cap mới; trade của bot khác mở
  sau `as_of` vẫn là exposure hiện hành.
- I5: barrier publish→reserve và reserve→publish giữa `SettingsAdminService.publish_draft` thật
  và `evaluate_and_submit` thật; cap mới không bao giờ bị reservation mới vượt, breach được ghi.
"""

from __future__ import annotations

import json
from decimal import Decimal

from tests.e2e.multibot_runtime_harness import ComposeHarness


def test_workspace_risk_caps_hold_through_real_runtime_admin_and_ledger_paths() -> None:
    harness = ComposeHarness("mb04fix")
    try:
        harness.start()
        harness.state = harness.probe(
            "seed",
            "migrator",
            variants={"c1": "1", "c2": "1", "c3a": "1", "c3b": "0.0075", "race5": "1"},
            account_fractions={"c2": "0.015"},
        )
        # Checkpoint genesis cho mọi account (như runtime registry yêu cầu trước hydrate).
        harness.probe("genesis")
        admin = harness.probe("mb04_admin_setup", "admin-web")
        print("admin", json.dumps(admin))
        assert admin["c1_overlay"][0] == "PUBLISHED", admin
        assert admin["c3a_publish"]["status"] == "PUBLISHED", admin
        harness.state["c3a_effective_at"] = admin["c3a_publish"]["effective_at"]

        paths = harness.probe("mb04_runtime_paths")
        print("paths", json.dumps(paths), harness.last_stderr)
        race = harness.probe("mb04_publish_race", "migrator")
        print("race", json.dumps(race), harness.last_stderr)
        inspected = {name: harness.inspect(name) for name in ("c1", "c2", "c3a", "c3b", "race5")}
        print("inspect", json.dumps(inspected))

        # C1: overlay 0.005 × 10.000 = 50 USDT < rủi ro yêu cầu (~100) dù account cap = 1.0.
        assert paths["c1_a"] == {"blocked": "BOT_CAP_EXCEEDED"}, paths
        assert [row[:2] for row in inspected["c1"]["budget_decisions"]] == [
            [False, "BOT_CAP_EXCEEDED"]
        ]
        assert "a" not in inspected["c1"]["reservations"]

        # C2: trade OPEN từ ledger thật giữ cap account 0.015 × 10.000 = 150 USDT.
        assert "intent_id" in paths["c2_a1"], paths
        assert paths["c2_fill"]["status"] == "FILLED", paths
        assert paths["c2_a2"] == {"blocked": "ACCOUNT_CAP_EXCEEDED"}, paths
        assert inspected["c2"]["trades"]["a"] == ["OPEN"]
        assert inspected["c2"]["reservations"]["a"] == [["TRANSFERRED", "OPEN_RISK_RECORDED"]]
        assert Decimal(inspected["c2"]["open_trade_risk"]) > 0

        # C3a: root siết sau evaluated_at nhưng trước khi reserve chạy -> cap mới (20 USDT).
        assert paths["c3a_a"] == {"blocked": "WORKSPACE_CAP_EXCEEDED"}, paths
        assert "a" not in inspected["c3a"]["reservations"]

        # C3b: trade B mở sau as_of của A vẫn tính vào workspace (cap 0.0075 × 20.000 = 150).
        assert paths["c3b_b_fill"]["status"] == "FILLED", paths
        assert paths["c3b_a"] == {"blocked": "WORKSPACE_CAP_EXCEEDED"}, paths
        assert inspected["c3b"]["trades"]["b"] == ["OPEN"]
        assert "a" not in inspected["c3b"]["reservations"]

        # I5 vòng 1 (publish→reserve): entry chờ khóa rồi thấy cap mới, không reserve nào vượt.
        assert race["r1_runtime_blocked"] is True, race
        assert race["r1_publish"]["status"] == "PUBLISHED", race
        assert race["r1_publish"]["capacity"]["status"] == "WITHIN_CAP", race
        assert race["r1_entry"] == {"blocked": "WORKSPACE_CAP_EXCEEDED"}, race
        assert Decimal(race["r1_held_reserved"]) == 0
        # I5 vòng 2 (reserve→publish): publish chờ khóa, đọc exposure mới và ghi breach.
        assert race["r2_publish_blocked"] is True, race
        assert "intent_id" in race["r2_entry"], race
        assert race["r2_publish"]["status"] == "PUBLISHED", race
        capacity = race["r2_publish"]["capacity"]
        assert capacity["status"] == "CAPACITY_BREACH_AFTER_TIGHTENING", race
        assert "WORKSPACE" in capacity["breached_scopes"], race
        assert Decimal(capacity["workspace_exposure_usdt"]) > Decimal(
            capacity["workspace_cap_usdt"]
        )
        assert race["r2_b_entry"] == {"blocked": "CAPACITY_BREACH_AFTER_TIGHTENING"}, race
        race_db = inspected["race5"]
        breach_action = "risk.capacity_breach_after_tightening"
        breach_audits = [audit for audit in race_db["audits"] if audit[0] == breach_action]
        assert len(breach_audits) == 1, race_db["audits"]
        assert "WORKSPACE" in (breach_audits[0][2] or "")
        # Không auto-flatten: reservation của A (duyệt trước khi siết) vẫn giữ; B không có.
        assert race_db["reservations"]["a"] == [["RESERVED", None]]
        assert "b" not in race_db["reservations"]
        exposure = Decimal(capacity["workspace_exposure_usdt"])
        assert Decimal(race_db["held_reserved_risk"]) == exposure
        assert [row[:2] for row in race_db["budget_decisions"]] == [
            [False, "WORKSPACE_CAP_EXCEEDED"],
            [True, "APPROVED"],
            [False, "CAPACITY_BREACH_AFTER_TIGHTENING"],
        ]
    finally:
        harness.down()
