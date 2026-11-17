"""Unit test CRY-428: ghi đè thông số thoát lệnh theo bot (quyết định owner 17/11/2026)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from tests.unit.test_bot_config_resolver import World, codes
from tests.unit.test_paper_decision_cycle import WORKSPACE_ID, _inputs
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.admin.bot_service import exit_changed
from trading_bot.admin.web_router import _exit_form, _exit_from_form, _exit_review
from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    compute_effective_hash,
)
from trading_bot.application.exit_policy_catalog import validate_exit_override
from trading_bot.application.paper_decision_cycle import (
    DecisionCycleOutcome,
    PaperDecisionCycle,
)
from trading_bot.application.proposal_backtest import BacktestArm, arm_from_overlay
from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, exit_section
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperExecutionGateway, PaperExecutionPolicy
from trading_bot.domain.risk.engine import RiskEngine
from trading_bot.domain.risk.planner import TradePlan, TradePlanner
from trading_bot.domain.risk.take_profit import LadderStep, TakeProfitLadder
from trading_bot.domain.strategy.exit_override import ExitOverride, exit_override_from_payload
from trading_bot.domain.strategy.recipes import ExitRecipe
from trading_bot.domain.strategy.registry import StrategyRegistry

LADDER_1_3 = TakeProfitLadder(
    steps=(LadderStep(Decimal("1"), Decimal("0.5")), LadderStep(Decimal("3"), Decimal("0.5"))),
    breakeven_after_first=False,
)


# --- Domain ----------------------------------------------------------------------------------


def test_exit_override_applies_fields_and_keeps_recipe_id() -> None:
    recipe = ExitRecipe("h03-exit-v1", 2.0, trailing_atr_multiple=3.0, max_hold_bars=120)
    override = ExitOverride(stop_atr_multiple=Decimal("1.5"), max_hold_bars=60)
    applied = override.apply_to_recipe(recipe)
    assert applied.recipe_id == "h03-exit-v1"
    assert (applied.stop_atr_multiple, applied.trailing_atr_multiple) == (1.5, 3.0)
    assert applied.max_hold_bars == 60
    assert override.apply_to_ladder(None) is None  # release không có thang thì không thêm


def test_exit_override_payload_is_canonical_and_round_trips() -> None:
    override = ExitOverride(trailing_atr_multiple=Decimal("2.50"), ladder=LADDER_1_3)
    payload = override.payload()
    assert payload == {
        "trailing_atr_multiple": "2.5",
        "ladder": {
            "steps": [
                {"r_multiple": "1", "fraction": "0.5"},
                {"r_multiple": "3", "fraction": "0.5"},
            ],
            "breakeven_after_first": False,
        },
    }
    again = exit_override_from_payload(json.loads(json.dumps(payload)))
    assert again == override
    assert again is not None
    assert again.content_hash() == override.content_hash()
    assert override.apply_to_ladder(TakeProfitLadder(steps=(LadderStep(Decimal(1), Decimal(1)),)))


@pytest.mark.parametrize(
    "payload",
    [
        {"stop_atr_multiple": "0.1"},
        {"take_profit_atr_multiple": "11"},
        {"max_hold_bars": 0},
        {"max_hold_bars": 5000},
        {"stop_atr_multiple": 1.5},
        {"unknown": "1"},
        {"ladder": {"steps": [{"r_multiple": "1", "fraction": "0.6"}]}},
        {"ladder": {"steps": [{"r_multiple": "20", "fraction": "1"}]}},
    ],
)
def test_exit_override_rejects_out_of_bounds_or_unknown(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"\S"):
        exit_override_from_payload(payload)


def test_empty_override_is_none_or_rejected() -> None:
    assert exit_override_from_payload({}) is None
    with pytest.raises(ValueError, match="ít nhất một trường"):
        ExitOverride()


# --- Chỉ trường release vốn có ------------------------------------------------------------------


def _fields(release: str, override: ExitOverride) -> set[str]:
    return {field for field, _ in validate_exit_override(release, override)}


def test_validator_allows_only_fields_the_release_already_has() -> None:
    full = ExitOverride(
        stop_atr_multiple=Decimal("1"),
        trailing_atr_multiple=Decimal("2"),
        take_profit_atr_multiple=Decimal("2"),
        max_hold_bars=10,
        ladder=LADDER_1_3,
    )
    # H03: SL + trailing + giữ tối đa; không có TP ATR, không có thang.
    assert _fields("h03-v3-1h", full) == {"exit.take_profit_atr_multiple", "exit.ladder"}
    # H06: SL + TP + giữ tối đa; không trailing, không thang.
    assert _fields("h06-v2", full) == {"exit.trailing_atr_multiple", "exit.ladder"}
    # H18: SL theo cấu trúc → không chỉnh SL; có thang.
    assert _fields("h18-v1", full) == {
        "exit.stop_atr_multiple",
        "exit.trailing_atr_multiple",
        "exit.take_profit_atr_multiple",
    }
    # H19: SL 1 ATR + thang.
    assert "exit.stop_atr_multiple" not in _fields("h19-v1", full)
    # Strategy Card: chỉ giữ tối đa.
    assert _fields("sc-binance-usdm-vol-breakout-001-v1.0.0", full) == {
        "exit.stop_atr_multiple",
        "exit.trailing_atr_multiple",
        "exit.take_profit_atr_multiple",
        "exit.ladder",
    }
    assert validate_exit_override("h03-v3-1h", ExitOverride(stop_atr_multiple=Decimal("1.5"))) == []
    assert _fields("khong-ton-tai", full) == {"exit"}


# --- Overlay và resolver ------------------------------------------------------------------


def test_overlay_without_exit_keeps_canonical_json_and_exit_section() -> None:
    plain = BotConfigOverlay.model_validate({"schema_version": 1})
    assert canonical_overlay(plain) == {"schema_version": 1}
    assert exit_section(plain) == {}
    with pytest.raises(ValueError, match="ít nhất một trường"):
        BotConfigOverlay.model_validate({"schema_version": 1, "exit": {}})
    with_exit = BotConfigOverlay.model_validate(
        {"schema_version": 1, "exit": {"stop_atr_multiple": "1.50"}}
    )
    assert exit_section(with_exit) == {"stop_atr_multiple": "1.5"}


def test_resolver_carries_valid_override_into_effective_hash() -> None:
    world = World()  # release gốc h03-v1
    plain = world.resolve(world.version({"schema_version": 1}))
    tuned = world.resolve(
        world.version({"schema_version": 1, "exit": {"stop_atr_multiple": "1.5"}})
    )
    assert plain.exit_override is None
    assert tuned.exit_override == ExitOverride(stop_atr_multiple=Decimal("1.5"))
    assert tuned.verify_hash()
    assert compute_effective_hash(replace(tuned, exit_override=None)) != tuned.effective_hash


def test_resolver_rejects_field_the_release_does_not_have() -> None:
    world = World()
    with pytest.raises(BotConfigResolutionError) as error:
        world.resolve(
            world.version({"schema_version": 1, "exit": {"take_profit_atr_multiple": "2"}})
        )
    assert "exit.take_profit_atr_multiple" in codes(error)


# --- Decision cycle -----------------------------------------------------------------------


def _prepare(exit_override: ExitOverride | None) -> TradePlan:
    snapshot, now, planner_settings, account, state, costs = _inputs()
    version_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    planner_settings = replace(
        planner_settings, settings_version_id=str(version_id), settings_hash="f" * 64
    )
    published = PublishedWorkspaceSettings(
        workspace_id=WORKSPACE_ID,
        settings_version_id=version_id,
        settings_hash="f" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
    )
    gateway = PaperExecutionGateway(
        PaperExecutionPolicy(
            version_id="paper-fill-v1",
            latency=timedelta(0),
            spread_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            fee_rate=Decimal("0.001"),
        )
    )
    cycle = PaperDecisionCycle(StrategyRegistry(), TradePlanner(), RiskEngine(), gateway)
    result = cycle.prepare_published(
        published_settings=published,
        snapshot=snapshot,
        evaluated_at=now + timedelta(seconds=1),
        planner_settings=planner_settings,
        account=replace(account, settings_version_id=str(version_id)),
        risk_state=replace(state, settings_version_id=str(version_id)),
        open_exposures=(),
        cost_model=costs,
        exit_override=exit_override,
    )
    assert result.outcome is DecisionCycleOutcome.RISK_APPROVED, result
    assert isinstance(result.plan, TradePlan)
    return result.plan


def test_cycle_plans_with_bot_exit_override() -> None:
    base = _prepare(None)
    tuned = _prepare(ExitOverride(stop_atr_multiple=Decimal("1"), max_hold_bars=30))
    assert base.exit_recipe.stop_atr_multiple == 2.0
    assert tuned.exit_recipe.stop_atr_multiple == 1.0
    assert tuned.exit_recipe.max_hold_bars == 30
    assert tuned.exit_recipe.recipe_id == base.exit_recipe.recipe_id
    base_distance = abs(base.entry_price - base.stop_price)
    tuned_distance = abs(tuned.entry_price - tuned.stop_price)
    # Stop 1 ATR thay 2 ATR: khoảng stop ~ một nửa; khối lượng không giảm (fixture chạm trần
    # notional nên bằng nhau), lỗ dự kiến tại stop nhỏ hơn.
    assert tuned_distance < base_distance
    assert tuned.quantity >= base.quantity
    assert tuned.estimated_loss < base.estimated_loss


# --- Backtest arm, gate và form ---------------------------------------------------------------


def test_backtest_arm_carries_exit_block() -> None:
    arm = arm_from_overlay(
        {
            "strategy": {"release_id": "h03-v3-1h"},
            "exit": {"stop_atr_multiple": "1.5"},
        }
    )
    assert arm.payload()["exit"] == {"stop_atr_multiple": "1.5"}
    plain = arm_from_overlay({"strategy": {"release_id": "h03-v3-1h"}})
    assert "exit" not in plain.payload()
    assert plain == BacktestArm("h03-v3-1h", (), plain.per_trade_risk_fraction)


def test_submission_validates_exit_override_against_release() -> None:
    from trading_bot.application.backtest_replay_jobs import BacktestReplayJobSubmission

    common = {
        "bundle_hash": "a" * 64,
        "execution_interval": "1h",
        "starting_equity": Decimal("10000"),
        "per_trade_risk_fraction": Decimal("0.005"),
        "fee_rate": Decimal("0.0005"),
        "slippage_rate": Decimal("0.0005"),
        "seed": 1,
    }
    ok = BacktestReplayJobSubmission(
        strategy_release_id="h03-v3-1h",
        exit_override_json=json.dumps({"stop_atr_multiple": "1.5"}),
        **common,
    )
    assert ok.exit_override() == ExitOverride(stop_atr_multiple=Decimal("1.5"))
    with pytest.raises(ValueError, match="take_profit_atr_multiple"):
        BacktestReplayJobSubmission(
            strategy_release_id="h03-v3-1h",
            exit_override_json=json.dumps({"take_profit_atr_multiple": "2"}),
            **common,
        )


def test_exit_changed_compares_with_published_head() -> None:
    from types import SimpleNamespace

    head = SimpleNamespace(overlay_json={"schema_version": 1, "exit": {"max_hold_bars": 60}})
    same = BotConfigOverlay.model_validate({"schema_version": 1, "exit": {"max_hold_bars": 60}})
    other = BotConfigOverlay.model_validate({"schema_version": 1})
    assert exit_changed(same, head) is False  # type: ignore[arg-type]
    assert exit_changed(other, head) is True  # type: ignore[arg-type]
    assert exit_changed(other, None) is False
    assert exit_changed(same, None) is True


def test_exit_form_shows_only_release_fields_with_base_placeholders() -> None:
    h03 = _exit_form({"strategy": {"release_id": "h03-v3-1h"}}, "h01-v1")
    assert [field["name"] for field in h03["fields"]] == [
        "stop_atr_multiple",
        "trailing_atr_multiple",
        "max_hold_bars",
    ]
    assert h03["fields"][0]["base"] == "2"
    assert h03["ladder"] is None
    h18 = _exit_form({"strategy": {"release_id": "h18-v1"}, "exit": {"max_hold_bars": 6}}, "x")
    assert h18["stop_fixed"] is True
    assert [field["name"] for field in h18["fields"]] == ["max_hold_bars"]
    assert h18["fields"][0]["value"] == "6"
    assert h18["ladder"]["base"] == "1:0.5, 2:0.5"
    assert h18["ladder"]["breakeven"] is True


def test_exit_from_form_parses_blank_as_inherit_and_ladder_text() -> None:
    assert _exit_from_form({"exit_stop_atr_multiple": " "}) == {}
    parsed = _exit_from_form(
        {
            "exit_stop_atr_multiple": "1,5",
            "exit_max_hold_bars": "48",
            "exit_ladder": "1:0.5; 3:0.5",
            "exit_ladder_breakeven": "on",
        }
    )
    assert parsed == {
        "stop_atr_multiple": "1.5",
        "max_hold_bars": 48,
        "ladder": {
            "steps": [
                {"r_multiple": "1", "fraction": "0.5"},
                {"r_multiple": "3", "fraction": "0.5"},
            ],
            "breakeven_after_first": True,
        },
    }
    BotConfigOverlay.model_validate({"schema_version": 1, "exit": parsed})
    with pytest.raises(ValueError, match="R:tỷ lệ"):
        _exit_from_form({"exit_ladder": "1 0.5"})
    with pytest.raises(ValueError, match="số nguyên"):
        _exit_from_form({"exit_max_hold_bars": "1.5"})


def test_exit_review_matches_owner_draft_backtest_of_this_draft() -> None:
    draft = {"id": "d1", "overlay": {"schema_version": 1, "exit": {"max_hold_bars": 6}}}
    published = {"overlay": {"schema_version": 1}}
    backtests = [
        {"source": "AI_PROPOSAL", "draft_id": None, "verdict": "BETTER"},
        {"source": "OWNER_DRAFT", "draft_id": "old", "verdict": "BETTER"},
        {"source": "OWNER_DRAFT", "draft_id": "d1", "verdict": "WORSE"},
    ]
    review = _exit_review(draft, published, backtests)
    assert review is not None
    assert review["after"] == {"max_hold_bars": 6}
    assert review["backtest"]["verdict"] == "WORSE"
    assert _exit_review(draft, published, backtests[:2])["backtest"] is None  # type: ignore[index]
    assert _exit_review({"id": "d2", "overlay": {"schema_version": 1}}, published, []) is None
    assert _exit_review(None, published, []) is None
