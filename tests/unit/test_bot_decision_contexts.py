"""Adapter EffectiveBotConfig (MB-02) → BotDecisionContext (MB-03) và điều kiện fan-out.

Ruling tích hợp: chỉ bot có cấu hình bot PUBLISHED hiệu lực tại `as_of` mới được fan-out;
bot chưa publish bị loại kèm lý do được ghi lại (vẫn giữ root-fallback của đường PAPER cũ).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from tests.unit.test_bot_dispatcher import (
    ACCOUNT_A,
    ACCOUNT_B,
    BOT_A,
    BOT_B,
    CONFIG_A,
    CONFIG_B,
    DECISION_TIME,
    ROOT_VERSION,
    WORKSPACE,
    _dispatcher,
    _evidence,
)

from trading_bot.application.bot_config_resolver import (
    BotConfigResolutionError,
    EffectiveBotConfig,
    EffectiveStrategy,
    compute_effective_hash,
)
from trading_bot.application.bot_decision_contexts import (
    BOT_CONFIG_NOT_PUBLISHED,
    EffectiveBotDecisionContextProvider,
    FanOutEligibility,
    FanOutExclusion,
    RuntimeBotRef,
    bot_decision_context_from_effective,
)
from trading_bot.application.bot_dispatcher import SignalEvidenceFanOutConsumer
from trading_bot.domain.risk.planner import RiskLimits

BOT_C = UUID("c3c3c3c3-0000-4000-8000-000000000003")
ACCOUNT_C = UUID("c3c3c3c3-1111-4111-8111-111111111111")
BOT_D = UUID("d4d4d4d4-0000-4000-8000-000000000004")
ACCOUNT_D = UUID("d4d4d4d4-1111-4111-8111-111111111111")
ROOT_HASH = "1" * 64
AS_OF = DECISION_TIME


def _limits() -> RiskLimits:
    return RiskLimits(
        per_trade_risk_fraction=Decimal("0.005"),
        max_aggregate_open_risk=Decimal("250"),
        max_notional=Decimal("10000"),
        max_leverage=Decimal("5"),
        max_positions=2,
        max_symbol_notional=Decimal("2000"),
        max_concentration_fraction=Decimal("0.2"),
        max_daily_loss=Decimal("200"),
        max_rolling_loss=Decimal("500"),
        max_account_age=timedelta(seconds=30),
        max_data_age=timedelta(seconds=300),
    )


def _effective(**overrides: Any) -> EffectiveBotConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "bot_id": BOT_A,
        "account_id": ACCOUNT_A,
        "settings_version_id": ROOT_VERSION,
        "settings_hash": ROOT_HASH,
        "bot_config_version_id": CONFIG_A,
        "bot_config_hash": "a" * 64,
        "risk_profile_version_id": UUID("eeeeeeee-0000-4000-8000-000000000000"),
        "effective_at": DECISION_TIME - timedelta(days=1),
        "resolved_at": AS_OF,
        "strategy": EffectiveStrategy(
            release_id="h01-v1",
            release_content_hash="9" * 64,
            workspace_release_id=UUID("ffffffff-0000-4000-8000-000000000000"),
            parameters=(),
            entry_timeframe="1h",
            confirmation_timeframe="4h",
        ),
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "timeframes": ("1h", "4h"),
        "risk_limits": _limits(),
        "effective_hash": "",
    }
    values.update(overrides)
    config = EffectiveBotConfig(**values)
    return replace(config, effective_hash=compute_effective_hash(config))


def _effective_b() -> EffectiveBotConfig:
    return _effective(
        bot_id=BOT_B,
        account_id=ACCOUNT_B,
        bot_config_version_id=CONFIG_B,
        bot_config_hash="b" * 64,
        strategy=EffectiveStrategy(
            release_id="h03-v1",
            release_content_hash="8" * 64,
            workspace_release_id=UUID("ffffffff-1111-4111-8111-111111111111"),
            parameters=(("donchian_lookback", 55),),
            entry_timeframe="1h",
            confirmation_timeframe="4h",
        ),
        symbols=("BTCUSDT",),
    )


class FakeUnitOfWork:
    """UoW giả: ghi lại scope (workspace, account đã bind) của từng transaction."""

    def __init__(self, log: list[tuple[UUID, UUID | None]], workspace_id: UUID) -> None:
        self._log = log
        self.workspace_id = workspace_id
        self.bot_id: UUID | None = None
        self.account_id: UUID | None = None
        self.session = object()

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, *_: object) -> None:
        self._log.append((self.workspace_id, self.account_id))

    async def bind_account(self, account_id: UUID, *, allow_switch: bool = False) -> UUID:
        if self.account_id is not None and not allow_switch:
            raise AssertionError("Một transaction chỉ được bind một bot")
        self.account_id = account_id
        self.bot_id = {ACCOUNT_A: BOT_A, ACCOUNT_B: BOT_B, ACCOUNT_C: BOT_C, ACCOUNT_D: BOT_D}[
            account_id
        ]
        return self.bot_id


class Harness:
    """Dựng provider với bot lister/config loader giả nhưng đi qua đúng luồng UoW."""

    def __init__(
        self,
        bots: tuple[RuntimeBotRef, ...],
        configs: dict[UUID, EffectiveBotConfig | None | BaseException],
    ) -> None:
        self.bots = bots
        self.configs = configs
        self.transactions: list[tuple[UUID, UUID | None]] = []
        self.loader_calls: list[tuple[UUID, UUID | None, datetime]] = []
        self.exclusions: list[FanOutExclusion] = []
        self.recorder = _Recorder()

        def factory(_sessions: object, *, workspace_id: UUID) -> FakeUnitOfWork:
            return FakeUnitOfWork(self.transactions, workspace_id)

        async def list_bots(uow: FakeUnitOfWork) -> tuple[RuntimeBotRef, ...]:
            assert uow.account_id is None, "Liệt kê bot phải ở scope workspace"
            return self.bots

        async def load(
            uow: FakeUnitOfWork, bot: RuntimeBotRef, as_of: datetime
        ) -> EffectiveBotConfig | None:
            assert uow.account_id == bot.account_id, "Nạp config phải trong scope đúng bot"
            self.loader_calls.append((bot.bot_id, uow.account_id, as_of))
            outcome = self.configs[bot.bot_id]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.provider = EffectiveBotDecisionContextProvider(
            object(),  # type: ignore[arg-type]
            unit_of_work_factory=factory,  # type: ignore[arg-type]
            list_bots=list_bots,  # type: ignore[arg-type]
            load_config=load,  # type: ignore[arg-type]
            on_exclusion=self.exclusions.append,
            runtime_gate=_open_gate,  # type: ignore[arg-type]
            exclusion_recorder=self.recorder,
        )


async def _open_gate(uow: object, bot: RuntimeBotRef) -> str | None:
    del uow, bot
    return None


class _Recorder:
    def __init__(self) -> None:
        self.recorded: list[FanOutEligibility] = []

    async def record(self, eligibility: FanOutEligibility) -> None:
        self.recorded.append(eligibility)


def _refs(*pairs: tuple[UUID, UUID]) -> tuple[RuntimeBotRef, ...]:
    return tuple(RuntimeBotRef(bot_id=bot, account_id=account) for bot, account in pairs)


def test_adapter_maps_effective_config_and_pins_root_settings_version() -> None:
    config = _effective_b()

    context = bot_decision_context_from_effective(config)

    assert (context.workspace_id, context.bot_id, context.account_id) == (
        WORKSPACE,
        BOT_B,
        ACCOUNT_B,
    )
    assert context.bot_config_version_id == CONFIG_B
    assert context.bot_config_hash == "b" * 64
    assert context.root_settings_version_id == ROOT_VERSION
    assert context.root_settings_hash == ROOT_HASH
    assert context.strategy_release_id == "h03-v1"
    assert context.strategy_parameters == (("donchian_lookback", 55),)
    assert context.symbols == ("BTCUSDT",)
    assert context.effective_at == config.effective_at
    selection = context.strategy_selection()
    # Ruling MB-03 fix round: selection mang ROOT settings version, không phải bot config.
    assert selection.settings_version_id == str(ROOT_VERSION)
    assert selection == config.strategy_selection()


def test_adapter_is_deterministic_and_ignores_resolution_wall_clock() -> None:
    first = bot_decision_context_from_effective(_effective())
    later = bot_decision_context_from_effective(_effective(resolved_at=AS_OF + timedelta(hours=3)))

    assert first == later
    assert first.context_hash == later.context_hash
    assert first.canonical_payload() == later.canonical_payload()


def test_adapter_rejects_config_whose_hash_does_not_verify() -> None:
    tampered = replace(_effective(), symbols=("ETHUSDT",))

    with pytest.raises(BotConfigResolutionError) as error:
        bot_decision_context_from_effective(tampered)

    assert error.value.code == "BOT_CONFIG_HASH_MISMATCH"


async def test_only_bots_with_published_config_are_fanned_out_others_are_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(
        _refs((BOT_B, ACCOUNT_B), (BOT_C, ACCOUNT_C), (BOT_A, ACCOUNT_A)),
        {BOT_A: _effective(), BOT_B: _effective_b(), BOT_C: None},
    )

    with caplog.at_level(logging.WARNING, logger="trading_bot.application.bot_decision_contexts"):
        eligibility = await harness.provider.eligibility(workspace_id=WORKSPACE, as_of=AS_OF)

    assert [context.bot_id for context in eligibility.contexts] == sorted([BOT_A, BOT_B])
    assert eligibility.exclusions == (
        FanOutExclusion(bot_id=BOT_C, account_id=ACCOUNT_C, reason_code=BOT_CONFIG_NOT_PUBLISHED),
    )
    assert harness.exclusions == list(eligibility.exclusions)
    assert BOT_CONFIG_NOT_PUBLISHED == "BOT_CONFIG_NOT_PUBLISHED"
    assert any(
        BOT_CONFIG_NOT_PUBLISHED in record.getMessage() and str(BOT_C) in record.getMessage()
        for record in caplog.records
    )


async def test_resolution_errors_exclude_only_that_bot_with_its_code() -> None:
    harness = Harness(
        _refs((BOT_A, ACCOUNT_A), (BOT_D, ACCOUNT_D)),
        {BOT_A: _effective(), BOT_D: BotConfigResolutionError("BOT_CONFIG_ROOT_MISMATCH")},
    )

    eligibility = await harness.provider.eligibility(workspace_id=WORKSPACE, as_of=AS_OF)

    assert [context.bot_id for context in eligibility.contexts] == [BOT_A]
    assert eligibility.exclusions == (
        FanOutExclusion(bot_id=BOT_D, account_id=ACCOUNT_D, reason_code="BOT_CONFIG_ROOT_MISMATCH"),
    )


async def test_config_for_another_bot_or_workspace_is_excluded_fail_closed() -> None:
    harness = Harness(
        _refs((BOT_A, ACCOUNT_A), (BOT_B, ACCOUNT_B)),
        {
            BOT_A: _effective(workspace_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")),
            BOT_B: _effective(),  # config của bot A trả cho bot B
        },
    )

    eligibility = await harness.provider.eligibility(workspace_id=WORKSPACE, as_of=AS_OF)

    assert eligibility.contexts == ()
    assert {item.reason_code for item in eligibility.exclusions} == {"BOT_CONFIG_SCOPE_MISMATCH"}


async def test_each_bot_is_resolved_in_its_own_bot_scoped_transaction() -> None:
    harness = Harness(
        _refs((BOT_A, ACCOUNT_A), (BOT_B, ACCOUNT_B)),
        {BOT_A: _effective(), BOT_B: _effective_b()},
    )

    await harness.provider.contexts_for(workspace_id=WORKSPACE, as_of=AS_OF)

    assert harness.transactions == [
        (WORKSPACE, None),
        (WORKSPACE, ACCOUNT_A),
        (WORKSPACE, ACCOUNT_B),
    ]
    assert harness.loader_calls == [(BOT_A, ACCOUNT_A, AS_OF), (BOT_B, ACCOUNT_B, AS_OF)]


async def test_same_as_of_yields_identical_contexts() -> None:
    harness = Harness(
        _refs((BOT_A, ACCOUNT_A), (BOT_B, ACCOUNT_B)),
        {BOT_A: _effective(), BOT_B: _effective_b()},
    )

    first = await harness.provider.contexts_for(workspace_id=WORKSPACE, as_of=AS_OF)
    second = await harness.provider.contexts_for(
        workspace_id=WORKSPACE, as_of=AS_OF.astimezone(UTC)
    )

    assert first == second
    assert [item.context_hash for item in first] == [item.context_hash for item in second]


async def test_naive_as_of_is_rejected_before_any_transaction() -> None:
    harness = Harness(_refs((BOT_A, ACCOUNT_A)), {BOT_A: _effective()})

    naive = AS_OF.replace(tzinfo=None)

    with pytest.raises(ValueError, match="múi giờ"):
        await harness.provider.contexts_for(workspace_id=WORKSPACE, as_of=naive)

    assert harness.transactions == []


async def test_outbox_consumer_fans_out_only_to_bots_with_published_config() -> None:
    dispatcher, store, _ = _dispatcher()
    harness = Harness(
        _refs((BOT_A, ACCOUNT_A), (BOT_B, ACCOUNT_B), (BOT_C, ACCOUNT_C)),
        {
            BOT_A: _effective(symbols=("BTCUSDT",)),
            BOT_B: _effective_b(),
            BOT_C: None,
        },
    )
    consumer = SignalEvidenceFanOutConsumer(
        store, dispatcher, harness.provider, worker_id="worker-1"
    )
    await dispatcher.record_evidence(_evidence(), evaluated_at=AS_OF)

    jobs = await consumer.consume_once(workspace_id=WORKSPACE, now=datetime.now(UTC))

    assert sorted(job.bot_id for job in jobs) == sorted([BOT_A, BOT_B])
    assert {job.bot_config_version_id for job in jobs} == {CONFIG_A, CONFIG_B}
    assert harness.exclusions == [
        FanOutExclusion(bot_id=BOT_C, account_id=ACCOUNT_C, reason_code=BOT_CONFIG_NOT_PUBLISHED)
    ]
    decisions = [
        decision
        for account in (ACCOUNT_A, ACCOUNT_B)
        for decision in await dispatcher.process_pending(workspace_id=WORKSPACE, account_id=account)
    ]
    assert [decision.strategy_release_id for decision in decisions] == ["h01-v1", "h03-v1"]
    assert {decision.root_settings_version_id for decision in decisions} == {ROOT_VERSION}


# --- MB05.8 / ruling 18: lý do loại khỏi fan-out theo trạng thái runtime, lưu bền ----------


def _gated_harness(
    gate_codes: dict[UUID, str | None], bots: tuple[RuntimeBotRef, ...] | None = None
) -> Harness:
    from trading_bot.application.bot_decision_contexts import (
        EffectiveBotDecisionContextProvider,
    )

    harness = Harness(
        bots or _refs((BOT_A, ACCOUNT_A), (BOT_B, ACCOUNT_B)),
        {BOT_A: _effective(), BOT_B: _effective_b(), BOT_C: _effective()},
    )
    gate_calls: list[UUID | None] = []

    async def gate(uow: FakeUnitOfWork, bot: RuntimeBotRef) -> str | None:
        assert uow.account_id == bot.account_id, "Gate runtime phải đọc trong scope đúng bot"
        gate_calls.append(uow.account_id)
        return gate_codes.get(bot.bot_id)

    provider = harness.provider
    harness.provider = EffectiveBotDecisionContextProvider(
        object(),  # type: ignore[arg-type]
        unit_of_work_factory=provider._unit_of_work_factory,
        list_bots=provider._list_bots,
        load_config=provider._load_config,
        on_exclusion=harness.exclusions.append,
        runtime_gate=gate,  # type: ignore[arg-type]
        exclusion_recorder=provider._exclusion_recorder,
    )
    harness.gate_calls = gate_calls  # type: ignore[attr-defined]
    return harness


@pytest.mark.parametrize(
    "code",
    [
        "BOT_PAUSED",
        "WORKSPACE_KILLED",
        "EXTERNAL_ACTIVITY_UNRECONCILED",
        "BOT_RECOVERY_REQUIRED",
        "BOT_STANDBY",
    ],
)
async def test_runtime_blocked_bot_is_excluded_with_code(code: str) -> None:
    harness = _gated_harness({BOT_A: code})

    eligibility = await harness.provider.eligibility(workspace_id=WORKSPACE, as_of=AS_OF)

    assert [context.bot_id for context in eligibility.contexts] == [BOT_B]
    assert [(item.bot_id, item.reason_code) for item in eligibility.exclusions] == [(BOT_A, code)]


async def test_disabled_bot_is_listed_and_excluded_without_resolving_config() -> None:
    disabled = RuntimeBotRef(bot_id=BOT_C, account_id=ACCOUNT_C, enabled=False)
    harness = _gated_harness({}, bots=(*_refs((BOT_A, ACCOUNT_A)), disabled))

    eligibility = await harness.provider.eligibility(workspace_id=WORKSPACE, as_of=AS_OF)

    assert [(item.bot_id, item.reason_code) for item in eligibility.exclusions] == [
        (BOT_C, "BOT_DISABLED")
    ]
    assert all(call[0] != BOT_C for call in harness.loader_calls)
    assert ACCOUNT_C not in harness.gate_calls  # type: ignore[attr-defined]


async def test_runtime_gate_reads_control_incident_and_state_in_bot_scope() -> None:
    from types import SimpleNamespace

    from trading_bot.application.bot_decision_contexts import RuntimeFanOutGate
    from trading_bot.domain.execution.runtime_control import (
        BotControlState,
        BotEntryState,
        RuntimeControlSnapshot,
        WorkspaceControlState,
        WorkspaceKillState,
    )

    def gate(control: RuntimeControlSnapshot, incidents: int, readiness: str | None):
        async def controls(_session: object, **_: object) -> RuntimeControlSnapshot:
            return control

        async def open_incidents(_session: object, **_: object) -> int:
            return incidents

        async def state(_session: object, **_: object) -> object:
            return None if readiness is None else SimpleNamespace(readiness=readiness)

        return RuntimeFanOutGate(
            control_reader=controls, incident_counter=open_incidents, state_loader=state
        )

    uow = FakeUnitOfWork([], WORKSPACE)
    await uow.bind_account(ACCOUNT_A)
    bot = RuntimeBotRef(bot_id=BOT_A, account_id=ACCOUNT_A)
    clear = RuntimeControlSnapshot(None, None)
    killed = RuntimeControlSnapshot(WorkspaceControlState(1, WorkspaceKillState.KILLED), None)
    paused = RuntimeControlSnapshot(None, BotControlState(2, BotEntryState.PAUSED))

    assert await gate(clear, 0, None)(uow, bot) is None  # chưa có state: chưa chạy runtime
    assert await gate(clear, 0, "READY")(uow, bot) is None
    assert await gate(killed, 0, "READY")(uow, bot) == "WORKSPACE_KILLED"
    assert await gate(paused, 0, "READY")(uow, bot) == "BOT_PAUSED"
    assert await gate(clear, 1, "READY")(uow, bot) == "EXTERNAL_ACTIVITY_UNRECONCILED"
    assert await gate(clear, 0, "RECOVERY_REQUIRED")(uow, bot) == "BOT_RECOVERY_REQUIRED"
    assert await gate(clear, 0, "STANDBY")(uow, bot) == "BOT_STANDBY"


async def test_exclusion_recorder_audits_only_on_code_change_and_persists_for_held_bot() -> None:
    from types import SimpleNamespace

    from trading_bot.application.bot_decision_contexts import (
        FanOutEligibility,
        FanOutExclusionRecorder,
    )
    from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeFence

    audits: list[dict[str, Any]] = []
    states: dict[UUID, Any] = {}

    class Uow:
        def __init__(self, _sessions: object, **kwargs: Any) -> None:
            self.workspace_id = kwargs["workspace_id"]
            self.session = object()

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def record_audit(self, **values: Any) -> None:
            audits.append(values)

    class States:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, *, workspace_id: UUID, bot_id: UUID) -> Any:
            return states.get(bot_id)

        async def upsert(self, **values: Any) -> None:
            assert isinstance(values["fence"], RuntimeFence)
            states[values["bot_id"]] = SimpleNamespace(
                fanout_exclusion_code=values["fanout_exclusion_code"],
                readiness=values["readiness"],
                reason_code=values["reason_code"],
            )

    held = SimpleNamespace(
        account_id=ACCOUNT_A, bot_id=BOT_A, fence=RuntimeFence("runtime-1", FencingToken(2))
    )
    recorder = FanOutExclusionRecorder(
        sessions=object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        accounts={ACCOUNT_A: held}.get,
        unit_of_work_factory=Uow,
        state_repository_factory=States,
        clock=lambda: AS_OF,
    )

    def eligibility(*codes: tuple[UUID, UUID, str]) -> FanOutEligibility:
        return FanOutEligibility(
            workspace_id=WORKSPACE,
            as_of=AS_OF,
            contexts=(),
            exclusions=tuple(
                FanOutExclusion(bot_id=bot, account_id=account, reason_code=code)
                for bot, account, code in codes
            ),
        )

    await recorder.record(
        eligibility((BOT_A, ACCOUNT_A, "BOT_PAUSED"), (BOT_C, ACCOUNT_C, "BOT_DISABLED"))
    )
    await recorder.record(
        eligibility((BOT_A, ACCOUNT_A, "BOT_PAUSED"), (BOT_C, ACCOUNT_C, "BOT_DISABLED"))
    )
    await recorder.record(eligibility((BOT_A, ACCOUNT_A, "WORKSPACE_KILLED")))
    # Bot A lại đủ điều kiện (có context) -> xóa mã, không audit thêm.
    await recorder.record(
        replace(eligibility(), contexts=(bot_decision_context_from_effective(_effective()),))
    )

    assert [(audit["target_id"], audit["reason"]) for audit in audits] == [
        (BOT_A, "BOT_PAUSED"),
        (BOT_C, "BOT_DISABLED"),
        (BOT_A, "WORKSPACE_KILLED"),
    ]
    assert all(audit["action"] == "bot.fanout.excluded" for audit in audits)
    # Bot đang giữ lease: mã lưu bền trong bot_runtime_state; hết bị loại thì xóa mã.
    assert states[BOT_A].fanout_exclusion_code is None
    assert BOT_C not in states
