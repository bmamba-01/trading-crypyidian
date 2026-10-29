"""MB05b: persisted multi-bot candidates đi hết qua runtime PAPER trên PostgreSQL thật."""

from __future__ import annotations

import pytest
from tests.e2e.multibot_runtime_harness import ComposeHarness


def test_runtime_fans_out_persisted_candidates_to_bot_specific_paper_intents() -> None:
    harness = ComposeHarness("fanout-runtime")
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        configs = harness.probe("candidate_bot_configs", "migrator")
        assert set(configs["config_ids"]) == {"a", "b"}, configs

        result = harness.probe("runtime_fanout_to_paper_intents")
        expected = result["expected_bot_ids"]
        decisions = {row[0]: row for row in result["decisions"]}
        intents = {row[0]: row for row in result["intents"]}

        assert set(decisions) == set(expected), result
        assert set(intents) == set(expected), result
        assert {row[0]: row[1] for row in result["eligible_contexts"]} == expected, result
        assert [row[1] for row in result["eligibility_exclusions"]] == ["BOT_DISABLED"], result
        assert result["worker_wired"] is True, result
        assert all(row[1] == "CANDIDATE" for row in decisions.values()), result
        assert all(row[2] for row in decisions.values()), result
        assert result["recovered_decision_id"] in {row[3] for row in decisions.values()}, result
        assert result["recovered_decision_id"] in {row[2] for row in intents.values()}, result
        for bot_id, release_name in expected.items():
            decision = decisions[bot_id]
            intent = intents[bot_id]
            assert intent[1] == release_name, result
            assert intent[2] == decision[3], result
            assert intent[3] == decision[2], result
            assert len(intent[4]) == 64, result
    finally:
        harness.down()


def test_runtime_does_not_submit_abstain_or_future_input_veto_decisions() -> None:
    """ABSTAIN/VETO persisted decision được giữ lại nhưng không gọi PAPER runtime."""
    harness = ComposeHarness("fanout-noncandidate")
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        harness.probe("candidate_bot_configs", "migrator")

        result = harness.probe("runtime_non_candidate_decisions", force_future_input=True)
        by_evidence: dict[str, list[tuple[str, str]]] = {}
        for evidence_id, status, reason in result["decisions"]:
            by_evidence.setdefault(evidence_id, []).append((status, reason))
        assert by_evidence[result["abstain_evidence_id"]] == [
            ("ABSTAIN", "SYMBOL_OUT_OF_BOT_UNIVERSE"),
            ("ABSTAIN", "SYMBOL_OUT_OF_BOT_UNIVERSE"),
        ], result
        assert by_evidence[result["veto_evidence_id"]] == [
            ("VETO", "FUTURE_INPUT_REJECTED"),
            ("VETO", "FUTURE_INPUT_REJECTED"),
        ], result
        assert result["submitted_count"] == 0, result
    finally:
        harness.down()


def test_runtime_pause_blocks_persisted_candidate_before_reservation() -> None:
    """Pause được đọc lại sau L1, trước khi candidate có thể tạo intent/reservation."""
    harness = ComposeHarness("fanout-pause")
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        harness.probe("candidate_bot_configs", "migrator")

        prepared = harness.probe("runtime_account_hard_cap_race", pause_candidate=True)
        harness.probe("admin_pause", "admin-web")
        result = harness.probe(
            "runtime_paused_candidate_check", candidate_id=prepared["candidate_id"]
        )
        assert result["pause_reason"] == "BOT_PAUSED", result
    finally:
        harness.down()


def test_runtime_fencing_takeover_blocks_persisted_candidate() -> None:
    """Token fencing cũ không được dùng sau khi lease được takeover trong PostgreSQL."""
    harness = ComposeHarness("fanout-fence")
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        harness.probe("candidate_bot_configs", "migrator")

        result = harness.probe("runtime_fenced_candidate_check", "migrator")
        assert result["reason"] == "STALE_FENCING_TOKEN", result
        assert result["new_token"] >= 2, result
    finally:
        harness.down()


@pytest.mark.parametrize(
    ("overlay", "expected_positions", "notional_cap", "margin_cap", "expected_reason"),
    [
        ({"max_positions": 1}, 1, 10000, 10000, "MAX_POSITIONS_REACHED"),
        ({"max_notional": "3000"}, 2, 3000, 10000, None),
        ({"max_symbol_notional": "3000"}, 2, 3000, 10000, None),
        ({"max_concentration_fraction": "0.25"}, 2, 2500, 10000, None),
        ({}, 2, 10000, 650, None),
    ],
    ids=["positions", "notional", "symbol", "concentration", "initial-margin"],
)
def test_runtime_serializes_same_account_candidates_under_hard_caps(
    overlay: dict[str, object],
    expected_positions: int,
    notional_cap: int,
    margin_cap: int,
    expected_reason: str | None,
) -> None:
    """Hai candidate đồng thời không vượt cap account đã siết ở bot overlay."""
    harness = ComposeHarness("fanout-hard-cap")
    try:
        harness.start()
        harness.state = harness.probe("seed", "migrator")
        harness.probe("genesis")
        configs = harness.probe(
            "candidate_bot_configs",
            "migrator",
            hard_cap_overlay=overlay,
        )
        assert set(configs["config_ids"]) == {"a", "b"}, configs

        result = harness.probe(
            "runtime_account_hard_cap_race",
            **({"candidate_available_margin": str(margin_cap)} if margin_cap < 10000 else {}),
        )
        assert len(result["commands"]) == 2, result
        assert len(set(result["commands"])) == 2, result
        assert result["intent_count"] == result["reserved_count"], result
        assert 1 <= result["reserved_count"] <= expected_positions, result
        assert float(result["reserved_notional"]) <= notional_cap, result
        assert float(result["reserved_margin"]) <= margin_cap, result
        assert result["cross_scope_reason"] == "BOT_DECISION_NOT_FOUND", result
        if expected_reason is not None:
            reasons = [item["reason"] for item in result["outcomes"]]
            assert reasons.count("APPROVED") == 1, result
            assert reasons.count(expected_reason) == 1, result
    finally:
        harness.down()
