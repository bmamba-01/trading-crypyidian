from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.test_workspace_settings import valid_payload

from trading_bot.adapters.database.repositories.workspace_settings import settings_hash
from trading_bot.app import create_app
from trading_bot.application.outbox import AfterCommitCallbackError
from trading_bot.application.paper_decision_cycle import DecisionCycleOutcome
from trading_bot.application.paper_execution_profile import (
    default_paper_execution_profile_catalog,
    resolve_paper_execution_profile,
)
from trading_bot.application.paper_runtime_application import (
    PaperDecisionCommand,
    PaperDecisionContext,
    PaperMonitorCommand,
    PaperMonitorEvidence,
    PaperRuntimeApplicationError,
    PaperRuntimeApplicationService,
)
from trading_bot.config.settings import AppSettings
from trading_bot.config.workspace_settings import PublishedWorkspaceSettings, WorkspaceSettings
from trading_bot.domain.execution.paper import PaperBar
from trading_bot.domain.strategy.registry import StrategyRegistry


def test_application_rejects_account_outside_bootstrapped_map_before_loading_context() -> None:
    workspace_id = uuid4()
    enabled_account_id = uuid4()
    unknown_account_id = uuid4()
    provider_factory_calls = 0
    provider_load_calls = 0

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return SimpleNamespace(
                workspace_id=workspace_id,
                ready=True,
                database_ready=True,
                error_code=None,
                account_states={
                    enabled_account_id: SimpleNamespace(status="READY"),
                },
                active_accounts={
                    enabled_account_id: SimpleNamespace(account_id=enabled_account_id),
                },
            )

    class _Provider:
        async def load_context(self, **_: object) -> None:
            nonlocal provider_load_calls
            provider_load_calls += 1

    def make_context_provider(**_: object) -> _Provider:
        nonlocal provider_factory_calls
        provider_factory_calls += 1
        return _Provider()

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
        paper_decision_context_provider_factory=make_context_provider,
    )

    with TestClient(app):
        service = app.state.paper_application
        with pytest.raises(PaperRuntimeApplicationError, match="PAPER_ACCOUNT_NOT_READY"):
            asyncio.run(
                service.evaluate_and_submit(
                    PaperDecisionCommand(
                        account_id=unknown_account_id,
                        feature_snapshot_id="fixture-feature",
                        evaluated_at=datetime.now(UTC),
                    )
                )
            )

    assert provider_factory_calls == 1
    assert provider_load_calls == 0


def test_lifespan_readiness_turns_not_ready_when_paper_account_is_invalidated() -> None:
    workspace_id = uuid4()
    account_id = uuid4()

    class _Bootstrap:
        async def bootstrap(self) -> SimpleNamespace:
            return SimpleNamespace(
                workspace_id=workspace_id,
                ready=True,
                database_ready=True,
                error_code=None,
                account_states={account_id: SimpleNamespace(status="READY")},
                active_accounts={account_id: SimpleNamespace(account_id=account_id)},
            )

    app = create_app(
        AppSettings(process_role="trading-runtime", runtime_workspace_id=workspace_id),
        session_factory=object(),  # type: ignore[arg-type]
        paper_runtime_bootstrap_factory=lambda **_: _Bootstrap(),
    )

    with TestClient(app) as client:
        app.state.paper_application._invalidate_account(account_id, "PAPER_PROFILE_DRIFT")
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["paper_runtime_error_code"] == "PAPER_PROFILE_DRIFT"
    assert response.json()["paper_accounts"]["recovery_required"] == 1
    assert str(account_id) not in response.text


class _FakeUnitOfWork:
    instances: list[_FakeUnitOfWork] = []
    scalar_results: list[object | None] = []
    strategy_releases: list[object] = []

    def __init__(self, _sessions: object, *, workspace_id, bot_id=None) -> None:
        self.workspace_id = workspace_id
        self.session = _FakeSession()
        self.committed = False
        self.rolled_back = False
        self._callbacks = []
        self.instances.append(self)

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type, *_: object) -> None:
        if exc_type is None:
            self.committed = True
            try:
                for callback in self._callbacks:
                    callback()
            except Exception:
                raise AfterCommitCallbackError("post-commit callback failed") from None
        else:
            self.rolled_back = True

    def after_commit(self, callback) -> None:
        self._callbacks.append(callback)


class _FakeSession:
    async def scalar(self, _statement):
        if _FakeUnitOfWork.scalar_results:
            return _FakeUnitOfWork.scalar_results.pop(0)
        return None

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: list(_FakeUnitOfWork.strategy_releases))


def _approved_context(workspace_id, account_id, published, evaluated_at):
    snapshot_id = "feature-123"
    release_hash = (
        StrategyRegistry().get_release(published.settings.strategy.default_release).content_hash
    )
    account = SimpleNamespace(
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        settings_version_id=str(published.settings_version_id),
        captured_at=evaluated_at - timedelta(seconds=1),
        equity=Decimal("10000"),
        available_margin=Decimal("10000"),
        is_known=True,
    )
    planner = SimpleNamespace(
        workspace_id=str(workspace_id),
        settings_version_id=str(published.settings_version_id),
        settings_hash=published.settings_hash,
        published=True,
        active_release_ids=(published.settings.strategy.default_release,),
        release_content_hashes=((published.settings.strategy.default_release, release_hash),),
    )
    risk_state = SimpleNamespace(
        workspace_id=str(workspace_id),
        account_id=str(account_id),
        settings_version_id=str(published.settings_version_id),
        risk_profile_version_id=str(published.settings.risk.profile_version_id),
        evaluated_at=evaluated_at,
        account_captured_at=account.captured_at,
        market_data_as_of=evaluated_at - timedelta(seconds=2),
        equity=account.equity,
        available_margin=account.available_margin,
        open_position_count=0,
        is_known=True,
        open_exposures=(),
    )
    return PaperDecisionContext(
        snapshot=SimpleNamespace(
            snapshot_id=snapshot_id,
            decision_time=evaluated_at - timedelta(seconds=1),
            symbol="BTCUSDT",
            series=(),
            features=(),
        ),  # type: ignore[arg-type]
        planner_settings=planner,  # type: ignore[arg-type]
        account=account,  # type: ignore[arg-type]
        risk_state=risk_state,  # type: ignore[arg-type]
        account_quote_asset=published.settings.paper_execution.quote_asset,
        open_exposures=(),
        evidence={"fixture": "unit"},
    )


async def _no_bot_config(**_: object) -> None:
    """Bot chưa có version publish: runtime giữ đường root như MB-01."""
    return None


def _application_with_fakes(
    *,
    fail_submission: bool,
    lock_events: list[str] | None = None,
    **service_overrides: object,
):
    workspace_id = uuid4()
    account_id = uuid4()
    evaluated_at = datetime.now(UTC)
    published = PublishedWorkspaceSettings(
        workspace_id=workspace_id,
        settings_version_id=uuid4(),
        settings_hash="a" * 64,
        settings=WorkspaceSettings.model_validate(valid_payload()),
        effective_at=evaluated_at - timedelta(minutes=1),
    )
    profile = resolve_paper_execution_profile(published, default_paper_execution_profile_catalog())
    context = _approved_context(workspace_id, account_id, published, evaluated_at)
    callback_state = SimpleNamespace(published=False, fail_publish=False, invalidations=[])
    counters = SimpleNamespace(context_loads=0, cycles=0, reserves=0, submissions=0, fences=[])
    events = lock_events if lock_events is not None else []

    class _AllowFence:
        """Fence giả mặc định: bot chưa có lease (chế độ chuyển tiếp) luôn hợp lệ."""

        def __init__(self, _session: object) -> None:
            pass

        async def require(self, **kwargs: object) -> None:
            counters.fences.append(kwargs)

    class _ContextProvider:
        async def load_context(self, **_: object) -> PaperDecisionContext:
            counters.context_loads += 1
            return context

    class _SettingsRepository:
        async def lock_workspace(self, _workspace_id: object) -> None:
            events.append("L1")

        async def load_effective_config(self, **_: object) -> PublishedWorkspaceSettings:
            return published

    class _Cycle:
        async def prepare_published_with_risk_profile(self, **kwargs: object) -> SimpleNamespace:
            counters.cycles += 1
            counters.cycle_kwargs = kwargs
            candidate_id = "candidate-123"
            plan_hash = "b" * 64
            return SimpleNamespace(
                outcome=DecisionCycleOutcome.RISK_APPROVED,
                plan=SimpleNamespace(
                    workspace_id=str(workspace_id),
                    account_id=str(account_id),
                    settings_version_id=str(published.settings_version_id),
                    settings_hash=published.settings_hash,
                    risk_profile_version_id=str(published.settings.risk.profile_version_id),
                    snapshot_id="feature-123",
                    symbol="BTCUSDT",
                    direction="long",
                    strategy_release_id=published.settings.strategy.default_release,
                    release_content_hash=StrategyRegistry()
                    .get_release(published.settings.strategy.default_release)
                    .content_hash,
                    candidate_id=candidate_id,
                    cost_model_version=profile.profile_hash,
                    plan_hash=plan_hash,
                ),
                risk_decision=SimpleNamespace(
                    approved=True,
                    plan_hash=plan_hash,
                    requested_risk=Decimal("100"),
                    requested_notional=Decimal("1000"),
                    requested_initial_margin=Decimal("200"),
                    candidate_expires_at=evaluated_at + timedelta(minutes=5),
                    workspace_id=str(workspace_id),
                    account_id=str(account_id),
                    settings_version_id=str(published.settings_version_id),
                    risk_profile_version_id=str(published.settings.risk.profile_version_id),
                    snapshot_id="feature-123",
                    release_content_hash=StrategyRegistry()
                    .get_release(published.settings.strategy.default_release)
                    .content_hash,
                ),
                strategy_result=SimpleNamespace(
                    candidate_id=candidate_id,
                    direction="long",
                ),
            )

    evaluation_id = uuid4()
    reservation_id = uuid4()
    idempotency_keys: list[str] = []

    class _RiskRepository:
        async def record_evaluation(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(risk_decision_id=evaluation_id)

        async def reserve(self, _decision, *, idempotency_key, **_: object) -> SimpleNamespace:
            events.append("L2-L4:reserve")
            counters.reserves += 1
            idempotency_keys.append(idempotency_key)
            return SimpleNamespace(reservation_id=reservation_id)

    class _Coordinator:
        async def submit_approved(self, uow, **kwargs: object) -> SimpleNamespace:
            events.append("L4:submit")
            counters.submissions += 1
            counters.submit_kwargs = kwargs
            callback_state.submitted_evidence = kwargs["evidence"]

            def publish() -> None:
                if callback_state.fail_publish:
                    raise RuntimeError("synthetic post-commit publish failure")
                callback_state.published = True

            uow.after_commit(publish)
            if fail_submission:
                raise RuntimeError("synthetic ledger write failure")
            return SimpleNamespace(duplicate=False, ledger_intent_id=uuid4())

    class _Gateway:
        def order(self, intent_id: str) -> SimpleNamespace:
            return SimpleNamespace(intent_id=intent_id)

    class _WorkspaceRiskCoordinator:
        async def reserve(self, request, **kwargs: object) -> SimpleNamespace:
            events.append("L2-L4:workspace-reserve")
            counters.reserves += 1
            idempotency_keys.append(request.idempotency_key)
            counters.workspace_request = request
            counters.workspace_caps = kwargs
            return SimpleNamespace(
                approved=True,
                decision_id=uuid4(),
                reservation_id=reservation_id,
                decision=SimpleNamespace(approved=True),
            )

    runtime_account = SimpleNamespace(
        account_id=account_id,
        bot_id=uuid4(),
        gateway=_Gateway(),
        coordinator=_Coordinator(),
        execution_profile=profile,
    )
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = []
    _FakeUnitOfWork.strategy_releases = [
        SimpleNamespace(
            id=uuid4(),
            workspace_id=workspace_id,
            name=published.settings.strategy.default_release,
            status="PUBLISHED",
            content_hash=StrategyRegistry()
            .get_release(published.settings.strategy.default_release)
            .content_hash,
        )
    ]
    service = PaperRuntimeApplicationService(
        workspace_id=workspace_id,
        sessions=object(),  # type: ignore[arg-type]
        active_accounts={account_id: runtime_account},
        context_provider=_ContextProvider(),  # type: ignore[arg-type]
        unit_of_work_factory=_FakeUnitOfWork,
        settings_repository_factory=lambda _session: _SettingsRepository(),
        risk_repository_factory=lambda _session: _RiskRepository(),
        workspace_risk_coordinator_factory=lambda _session: _WorkspaceRiskCoordinator(),
        decision_cycle_factory=lambda _gateway: _Cycle(),
        account_invalidation_callback=lambda account, reason: callback_state.invalidations.append(
            (account, reason)
        ),
        **{"bot_config_loader": _no_bot_config, "fence_guard_factory": _AllowFence}
        | service_overrides,  # type: ignore[arg-type]
    )
    command = PaperDecisionCommand(
        account_id=account_id,
        feature_snapshot_id="feature-123",
        evaluated_at=evaluated_at,
    )
    return (
        service,
        command,
        callback_state,
        idempotency_keys,
        counters,
        published,
        profile,
        context,
    )


def test_submission_failure_rolls_back_uow_and_skips_after_commit_publish() -> None:
    service, command, callback_state, _, _, _, _, _ = _application_with_fakes(fail_submission=True)
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = []

    with pytest.raises(RuntimeError, match="synthetic ledger write failure"):
        asyncio.run(service.evaluate_and_submit(command))

    assert len(_FakeUnitOfWork.instances) == 1
    assert _FakeUnitOfWork.instances[0].rolled_back is True
    assert _FakeUnitOfWork.instances[0].committed is False
    assert callback_state.published is False


def test_workspace_budget_denial_skips_paper_submission() -> None:
    service, command, _, _, counters, *_ = _application_with_fakes(fail_submission=False)

    class _DeniedWorkspaceCoordinator:
        async def reserve(self, request, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                approved=False,
                decision_id=uuid4(),
                reservation_id=None,
                decision=SimpleNamespace(approved=False),
            )

    service._workspace_risk_coordinator_factory = lambda _session: _DeniedWorkspaceCoordinator()
    result = asyncio.run(service.evaluate_and_submit(command))

    assert result.decision is not None
    assert result.decision.outcome is DecisionCycleOutcome.RISK_REJECTED
    assert result.decision.workspace_risk_decision is not None
    assert result.submission is None
    assert counters.submissions == 0


def test_retry_reuses_stable_idempotency_key_and_commits_each_uow() -> None:
    service, command, callback_state, idempotency_keys, _, _, _, _ = _application_with_fakes(
        fail_submission=False
    )
    _FakeUnitOfWork.instances = []

    first = asyncio.run(service.evaluate_and_submit(command))
    second = asyncio.run(service.evaluate_and_submit(command))

    assert first.idempotency_key == second.idempotency_key
    assert len(set(idempotency_keys)) == 1
    assert len(_FakeUnitOfWork.instances) == 2
    assert all(item.committed for item in _FakeUnitOfWork.instances)
    assert callback_state.published is True


def test_submission_persists_selected_strategy_release_hash_in_evidence() -> None:
    service, command, callback_state, _, _, published, _, _ = _application_with_fakes(
        fail_submission=False
    )

    asyncio.run(service.evaluate_and_submit(command))

    expected_hash = (
        StrategyRegistry().get_release(published.settings.strategy.default_release).content_hash
    )
    assert callback_state.submitted_evidence["strategy_release_hash"] == expected_hash


def test_duplicate_decision_returns_hydrated_order_without_repeating_risk_or_submit() -> None:
    service, command, _, _, counters, published, profile, _ = _application_with_fakes(
        fail_submission=False
    )
    existing = SimpleNamespace(
        id=uuid4(),
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
        settings_version_id=published.settings_version_id,
        feature_snapshot_id=command.feature_snapshot_id,
        evidence={
            "settings_hash": published.settings_hash,
            "paper_execution_profile_hash": profile.profile_hash,
            "evaluated_at": command.evaluated_at.isoformat(),
            "strategy_release_hash": StrategyRegistry()
            .get_release(published.settings.strategy.default_release)
            .content_hash,
        },
    )
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = [existing]

    result = asyncio.run(service.evaluate_and_submit(command))

    assert result.duplicate is True
    assert result.decision is None
    assert result.submission is not None
    assert result.submission.duplicate
    assert result.submission.order.intent_id == f"paper:{result.idempotency_key}"
    assert counters.context_loads == 0
    assert counters.cycles == 0
    assert counters.reserves == 0
    assert counters.submissions == 0
    assert _FakeUnitOfWork.instances[0].committed


def test_retry_with_same_idempotency_key_but_different_evaluation_time_conflicts() -> None:
    service, command, _, _, _, published, profile, _ = _application_with_fakes(
        fail_submission=False
    )
    existing = SimpleNamespace(
        id=uuid4(),
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
        settings_version_id=published.settings_version_id,
        feature_snapshot_id=command.feature_snapshot_id,
        evidence={
            "settings_hash": published.settings_hash,
            "paper_execution_profile_hash": profile.profile_hash,
            "strategy_release_hash": StrategyRegistry()
            .get_release(published.settings.strategy.default_release)
            .content_hash,
            "evaluated_at": command.evaluated_at.isoformat(),
        },
    )
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = [existing]

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_IDEMPOTENCY_CONFLICT"):
        asyncio.run(
            service.evaluate_and_submit(
                replace(command, evaluated_at=command.evaluated_at + timedelta(seconds=1))
            )
        )


def test_decision_context_rejects_risk_snapshot_that_differs_from_account_snapshot() -> None:
    service, command, _, _, _, published, profile, context = _application_with_fakes(
        fail_submission=False
    )
    mismatched_risk_state = SimpleNamespace(**vars(context.risk_state))
    mismatched_risk_state.equity += Decimal("1")
    mismatched_context = replace(context, risk_state=mismatched_risk_state)
    runtime_account = service._active_accounts[command.account_id]

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_DECISION_CONTEXT_MISMATCH"):
        service._validate_context(
            mismatched_context,
            command=command,
            account=runtime_account,
            published=published,
            profile=profile,
        )


def test_application_rejects_strategy_release_without_persisted_workspace_record() -> None:
    service, command, _, _, _, _, _, _ = _application_with_fakes(fail_submission=False)
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = [None, None]
    _FakeUnitOfWork.strategy_releases = []

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_STRATEGY_RELEASE_NOT_PUBLISHED"):
        asyncio.run(service.evaluate_and_submit(command))


def test_application_rejects_release_record_with_wrong_content_hash() -> None:
    service, command, _, _, _, published, _, _ = _application_with_fakes(fail_submission=False)
    release = _FakeUnitOfWork.strategy_releases[0]
    _FakeUnitOfWork.strategy_releases = [
        SimpleNamespace(**(vars(release) | {"content_hash": "0" * 64}))
    ]
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = [None]

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_STRATEGY_RELEASE_NOT_PUBLISHED"):
        asyncio.run(service.evaluate_and_submit(command))


def test_monitor_evidence_must_match_persisted_scope_and_be_available_before_bar() -> None:
    service, decision_command, _, _, _, published, _, _ = _application_with_fakes(
        fail_submission=False
    )
    account = service._active_accounts[decision_command.account_id]
    opened_at = datetime.now(UTC)
    command = PaperMonitorCommand(
        account_id=decision_command.account_id,
        ledger_intent_id=uuid4(),
        trade_id=uuid4(),
        paper_position_id="paper:existing",
        bar=PaperBar(
            "monitor-1",
            "BTCUSDT",
            opened_at,
            opened_at + timedelta(minutes=1),
            opened_at + timedelta(minutes=1),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
        ),
        feature_snapshot_id="persisted-feature-1",
    )
    expected_hash = (
        StrategyRegistry().get_release(published.settings.strategy.default_release).content_hash
    )
    evidence = PaperMonitorEvidence(
        workspace_id=published.workspace_id,
        account_id=account.account_id,
        feature_snapshot_id=command.feature_snapshot_id,
        symbol=command.bar.symbol,
        available_at=opened_at - timedelta(seconds=1),
        strategy_release_name=published.settings.strategy.default_release,
        strategy_release_hash=expected_hash,
        opposing_signal=True,
        atr=Decimal("2"),
        strategy_settings_version_id=str(published.settings_version_id),
    )

    pinned_release = StrategyRegistry().get_release("h01-v1")
    assert service._validate_monitor_evidence(
        evidence,
        command=command,
        account=account,
        published=published,
        pinned_strategy_release_name="h01-v1",
        pinned_strategy_release_hash=pinned_release.content_hash,
        pinned_strategy_settings_version_id=str(published.settings_version_id),
        pinned_strategy_parameters=(),
    )
    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_MONITOR_EVIDENCE_MISMATCH"):
        service._validate_monitor_evidence(
            replace(evidence, account_id=uuid4()),
            command=command,
            account=account,
            published=published,
            pinned_strategy_release_name="h01-v1",
            pinned_strategy_release_hash=pinned_release.content_hash,
            pinned_strategy_settings_version_id=str(published.settings_version_id),
            pinned_strategy_parameters=(),
        )
    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_MONITOR_EVIDENCE_MISMATCH"):
        service._validate_monitor_evidence(
            replace(evidence, available_at=command.bar.opened_at + timedelta(seconds=1)),
            command=command,
            account=account,
            published=published,
            pinned_strategy_release_name="h01-v1",
            pinned_strategy_release_hash=pinned_release.content_hash,
            pinned_strategy_settings_version_id=str(published.settings_version_id),
            pinned_strategy_parameters=(),
        )


def test_monitor_suppresses_opposing_signal_when_current_strategy_differs_from_position_pin() -> (
    None
):
    service, _, _, _, _, published, _, _ = _application_with_fakes(fail_submission=False)
    account = next(iter(service._active_accounts.values()))
    command = PaperMonitorCommand(
        account_id=account.account_id,
        ledger_intent_id=uuid4(),
        trade_id=uuid4(),
        paper_position_id="paper:pinned-position",
        bar=PaperBar(
            "pinned-monitor-bar",
            "BTCUSDT",
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(minutes=1),
            datetime.now(UTC) + timedelta(minutes=1),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
        ),
        feature_snapshot_id="monitor-feature",
    )
    pinned = StrategyRegistry().get_release("h01-v1")
    current = StrategyRegistry().get_release("h03-v1")
    evidence = PaperMonitorEvidence(
        workspace_id=published.workspace_id,
        account_id=account.account_id,
        feature_snapshot_id=command.feature_snapshot_id,
        symbol="BTCUSDT",
        available_at=command.bar.opened_at - timedelta(seconds=1),
        strategy_release_name=current.release_id,
        strategy_release_hash=current.content_hash,
        opposing_signal=True,
        atr=Decimal("2"),
        strategy_settings_version_id="old-settings-version",
    )

    assert not service._validate_monitor_evidence(
        evidence,
        command=command,
        account=account,
        published=published,
        pinned_strategy_release_name=pinned.release_id,
        pinned_strategy_release_hash=pinned.content_hash,
        pinned_strategy_settings_version_id="old-settings-version",
        pinned_strategy_parameters=(("donchian_lookback", 56),),
    )


def test_open_trade_release_pin_is_resolved_from_immutable_ledger_lineage() -> None:
    service, _, _, _, _, published, _, _ = _application_with_fakes(fail_submission=False)
    release = StrategyRegistry().get_release("h01-v1")
    release_id = uuid4()
    intent = SimpleNamespace(
        strategy_release_id=release_id,
        strategy_release_hash=release.content_hash,
        settings_version_id=published.settings_version_id,
        evidence={},
    )
    settings_json = published.settings.model_dump(mode="json")
    settings_json["strategy"]["parameters"] = {"donchian_lookback": 56}
    trade = SimpleNamespace(
        strategy_release_id=release_id,
        lineage={
            "strategy_release_id": str(release_id),
            "strategy_release_hash": release.content_hash,
            "settings_version_id": str(published.settings_version_id),
        },
    )

    class _Session:
        async def scalar(self, statement: object) -> SimpleNamespace:
            if "strategy_release" in str(statement):
                return SimpleNamespace(
                    id=release_id,
                    workspace_id=published.workspace_id,
                    name="h01-v1",
                    content_hash=release.content_hash,
                )
            return SimpleNamespace(
                id=published.settings_version_id,
                workspace_id=published.workspace_id,
                status="PUBLISHED",
                settings_json=settings_json,
                settings_hash=settings_hash(settings_json),
            )

    pinned = asyncio.run(
        service._load_position_release_pin(
            SimpleNamespace(session=_Session()), intent=intent, trade=trade
        )
    )

    assert pinned.release_name == "h01-v1"
    assert pinned.release_hash == release.content_hash
    assert pinned.settings_version_id == str(published.settings_version_id)
    assert pinned.parameters == (("donchian_lookback", 56),)


def test_open_trade_settings_hash_mismatch_disables_only_strategy_signal_pin() -> None:
    service, _, _, _, _, published, _, _ = _application_with_fakes(fail_submission=False)
    release = StrategyRegistry().get_release("h01-v1")
    release_id = uuid4()
    settings_json = published.settings.model_dump(mode="json")
    intent = SimpleNamespace(
        strategy_release_id=release_id,
        strategy_release_hash=release.content_hash,
        settings_version_id=published.settings_version_id,
    )
    trade = SimpleNamespace(
        strategy_release_id=release_id,
        lineage={
            "settings_version_id": str(published.settings_version_id),
            "strategy_release_id": str(release_id),
            "strategy_release_hash": release.content_hash,
        },
    )

    class _Session:
        async def scalar(self, statement: object) -> SimpleNamespace:
            if "strategy_release" in str(statement):
                return SimpleNamespace(
                    id=release_id,
                    workspace_id=published.workspace_id,
                    name="h01-v1",
                    content_hash=release.content_hash,
                )
            return SimpleNamespace(
                id=published.settings_version_id,
                workspace_id=published.workspace_id,
                status="PUBLISHED",
                settings_json=settings_json,
                settings_hash="0" * 64,
            )

    pinned = asyncio.run(
        service._load_position_release_pin(
            SimpleNamespace(session=_Session()), intent=intent, trade=trade
        )
    )

    assert pinned.release_name is None
    assert pinned.release_hash is None
    assert pinned.settings_version_id is None
    assert pinned.parameters == ()


def test_settings_lineage_drift_keeps_execution_account_ready() -> None:
    service, command, _, _, _, published, _, _ = _application_with_fakes(fail_submission=False)
    account = service._active_accounts[command.account_id]
    changed_payload = published.settings.model_dump(mode="python")
    changed_payload["strategy"]["default_release"] = "h02-v1"
    drifted_settings = replace(
        published,
        settings_version_id=uuid4(),
        settings_hash="b" * 64,
        settings=WorkspaceSettings.model_validate(changed_payload),
    )

    class _SettingsRepository:
        async def lock_workspace(self, _workspace_id: object) -> None:
            return None

        async def load_effective_config(self, **_: object) -> PublishedWorkspaceSettings:
            return drifted_settings

    invalidations: list[tuple[object, str]] = []
    drifted_service = PaperRuntimeApplicationService(
        workspace_id=published.workspace_id,
        sessions=object(),  # type: ignore[arg-type]
        active_accounts={command.account_id: account},
        settings_repository_factory=lambda _session: _SettingsRepository(),
        account_invalidation_callback=lambda account_id, code: invalidations.append(
            (account_id, code)
        ),
    )

    _, resolved = asyncio.run(
        drifted_service._load_published_profile(
            SimpleNamespace(workspace_id=published.workspace_id, session=object()),
            account=account,
            as_of=command.evaluated_at,
        )
    )

    assert resolved.profile_hash == account.execution_profile.profile_hash
    assert invalidations == []
    assert drifted_service.is_ready


def test_new_decision_uses_current_lineage_while_existing_exposure_is_open() -> None:
    service, command, _, _, _, previous, _, _ = _application_with_fakes(fail_submission=False)
    payload = previous.settings.model_dump(mode="python")
    payload["strategy"]["default_release"] = "h03-v1"
    current = replace(
        previous,
        settings_version_id=uuid4(),
        settings_hash="c" * 64,
        settings=WorkspaceSettings.model_validate(payload),
    )
    base_context = _approved_context(
        previous.workspace_id, command.account_id, current, command.evaluated_at
    )
    exposure = SimpleNamespace(
        workspace_id=str(previous.workspace_id),
        account_id=str(command.account_id),
        symbol="ETHUSDT",
        notional=Decimal("500"),
        risk_fraction=Decimal("0.01"),
    )
    context = replace(
        base_context,
        open_exposures=(exposure,),
        risk_state=SimpleNamespace(
            **{
                **vars(base_context.risk_state),
                "open_position_count": 1,
                "open_exposures": (exposure,),
            }
        ),
    )

    class _SettingsRepository:
        async def lock_workspace(self, _workspace_id: object) -> None:
            return None

        async def load_effective_config(self, **_: object) -> PublishedWorkspaceSettings:
            return current

    class _ContextProvider:
        async def load_context(self, **_: object) -> PaperDecisionContext:
            return context

    class _Cycle:
        async def prepare_published_with_risk_profile(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(outcome=DecisionCycleOutcome.NO_TRADE)

    release = StrategyRegistry().get_release("h03-v1")
    _FakeUnitOfWork.strategy_releases = [
        SimpleNamespace(
            id=uuid4(),
            workspace_id=previous.workspace_id,
            name="h03-v1",
            status="PUBLISHED",
            content_hash=release.content_hash,
        )
    ]
    _FakeUnitOfWork.scalar_results = [None]
    service._context_provider = _ContextProvider()
    service._settings_repository_factory = lambda _session: _SettingsRepository()
    service._decision_cycle_factory = lambda _gateway: _Cycle()

    result = asyncio.run(service.evaluate_and_submit(command))

    assert result.decision is not None
    assert result.decision.outcome is DecisionCycleOutcome.NO_TRADE
    assert service.is_ready


def test_after_commit_publish_failure_invalidates_account_and_blocks_retry() -> None:
    service, command, callback_state, _, _, _, _, _ = _application_with_fakes(fail_submission=False)
    callback_state.fail_publish = True
    _FakeUnitOfWork.instances = []
    _FakeUnitOfWork.scalar_results = []

    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_PUBLISH_FAILED"):
        asyncio.run(service.evaluate_and_submit(command))

    assert _FakeUnitOfWork.instances[0].committed
    assert not _FakeUnitOfWork.instances[0].rolled_back
    assert callback_state.invalidations == [(command.account_id, "PAPER_PUBLISH_FAILED")]
    assert not service.is_available_for(command.account_id)
    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_ACCOUNT_NOT_READY"):
        asyncio.run(service.evaluate_and_submit(command))
    assert len(_FakeUnitOfWork.instances) == 1


# --- MB05.4: fencing tại ranh giới thực thi account -----------------------------------------


class _StaleFenceFor:
    """Fence giả: bot trong `stale_bots` bị từ chối với reason code cho trước."""

    def __init__(self, stale_bots: set[object], reason: str = "STALE_FENCING_TOKEN") -> None:
        self.stale_bots = stale_bots
        self.reason = reason
        self.calls: list[dict[str, object]] = []

    def __call__(self, _session: object) -> _StaleFenceFor:
        return self

    async def require(self, **kwargs: object) -> None:
        from trading_bot.domain.execution.runtime_control import RuntimeControlError

        self.calls.append(kwargs)
        if kwargs["bot_id"] in self.stale_bots:
            raise RuntimeControlError(self.reason)


def _two_bot_service(reason: str = "STALE_FENCING_TOKEN"):
    lease_lost: list[tuple[object, str]] = []
    service, command, callback_state, _, counters, _, _, _ = _application_with_fakes(
        fail_submission=False,
        lease_lost_callback=lambda account, code: lease_lost.append((account, code)),
    )
    first = service._active_accounts[command.account_id]
    sibling_id = uuid4()
    sibling = SimpleNamespace(**{**vars(first), "account_id": sibling_id, "bot_id": uuid4()})
    service._active_accounts[sibling_id] = sibling
    guard = _StaleFenceFor({first.bot_id}, reason)
    service._fence_guard_factory = guard
    return service, command, sibling_id, lease_lost, counters, callback_state, guard


@pytest.mark.parametrize(
    "reason", ["STALE_FENCING_TOKEN", "LEASE_EXPIRED", "LEASE_NOT_HELD", "FENCING_TOKEN_REQUIRED"]
)
def test_stale_fence_rejects_decision_before_any_write_and_demotes_only_that_bot(
    reason: str,
) -> None:
    service, command, sibling_id, lease_lost, counters, _, _ = _two_bot_service(reason=reason)
    _FakeUnitOfWork.instances = []

    with pytest.raises(PaperRuntimeApplicationError, match=reason):
        asyncio.run(service.evaluate_and_submit(command))

    assert counters.context_loads == 0
    assert counters.reserves == 0
    assert counters.submissions == 0
    assert _FakeUnitOfWork.instances[0].rolled_back is True
    assert _FakeUnitOfWork.instances[0].committed is False
    assert lease_lost == [(command.account_id, reason)]
    assert not service.is_available_for(command.account_id)
    assert service.is_available_for(sibling_id)
    # Mất lease là STANDBY, không phải lỗi recovery: readiness process không bị hạ cờ.
    assert service.is_ready
    with pytest.raises(PaperRuntimeApplicationError, match="PAPER_ACCOUNT_NOT_READY"):
        asyncio.run(service.evaluate_and_submit(command))


def test_sibling_bot_commands_continue_after_other_bot_loses_lease() -> None:
    service, command, sibling_id, _, counters, _, guard = _two_bot_service()
    with pytest.raises(PaperRuntimeApplicationError, match="STALE_FENCING_TOKEN"):
        asyncio.run(service.evaluate_and_submit(command))

    sibling_command = replace(command, account_id=sibling_id)
    with pytest.raises(PaperRuntimeApplicationError) as error:
        asyncio.run(service.evaluate_and_submit(sibling_command))

    # Sibling qua được fence (lỗi sau đó chỉ do fixture context gắn account đầu tiên).
    assert error.value.reason_code == "PAPER_DECISION_CONTEXT_MISMATCH"
    assert guard.calls[-1]["account_id"] == sibling_id
    assert counters.context_loads == 1


def _bar_for(opened_at: datetime) -> PaperBar:
    return PaperBar(
        "fence-bar",
        "BTCUSDT",
        opened_at,
        opened_at + timedelta(minutes=1),
        opened_at + timedelta(minutes=1),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
    )


def test_stale_fence_rejects_entry_event_and_monitor_before_coordinator() -> None:
    from trading_bot.application.paper_runtime_application import PaperEntryEventCommand

    for command_kind in ("entry", "monitor"):
        service, command, _, lease_lost, _, _, guard = _two_bot_service()
        account = service._active_accounts[command.account_id]
        account.coordinator.apply_entry_market_event = None  # gọi tới sẽ TypeError
        account.coordinator.monitor_market_event = None
        _FakeUnitOfWork.instances = []
        _FakeUnitOfWork.scalar_results = []
        bar = _bar_for(datetime.now(UTC))
        if command_kind == "entry":
            call = service.apply_entry_event(
                PaperEntryEventCommand(command.account_id, uuid4(), "paper:x", bar)
            )
        else:
            call = service.monitor_position(
                PaperMonitorCommand(command.account_id, uuid4(), uuid4(), "paper:x", bar, "f")
            )
        with pytest.raises(PaperRuntimeApplicationError, match="STALE_FENCING_TOKEN"):
            asyncio.run(call)
        assert _FakeUnitOfWork.instances[0].rolled_back is True
        assert lease_lost == [(command.account_id, "STALE_FENCING_TOKEN")]
        assert guard.calls[0]["bot_id"] == account.bot_id


def test_runtime_account_fence_is_presented_and_token_reaches_checkpoint_writer() -> None:
    from trading_bot.application.paper_runtime_bootstrap import PaperRuntimeAccount
    from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeFence

    service, command, _, _, counters, _, _, _ = _application_with_fakes(fail_submission=False)
    old = service._active_accounts[command.account_id]
    service._active_accounts[command.account_id] = PaperRuntimeAccount(
        account_id=old.account_id,
        bot_id=old.bot_id,
        gateway=old.gateway,
        coordinator=old.coordinator,
        execution_profile=old.execution_profile,
        owner_id="runtime-a",
        fencing_token=FencingToken(5),
    )

    asyncio.run(service.evaluate_and_submit(command))

    assert counters.fences[-1]["fence"] == RuntimeFence("runtime-a", FencingToken(5))
    assert counters.fences[-1]["bot_id"] == old.bot_id
    assert counters.submit_kwargs["fencing_token"] == FencingToken(5)


def test_pre_registry_account_presents_no_fence_and_no_token() -> None:
    service, command, _, _, counters, _, _, _ = _application_with_fakes(fail_submission=False)

    asyncio.run(service.evaluate_and_submit(command))

    assert counters.fences[-1]["fence"] is None
    assert counters.submit_kwargs["fencing_token"] is None


def test_runtime_account_requires_owner_and_token_together() -> None:
    from trading_bot.application.paper_runtime_bootstrap import PaperRuntimeAccount
    from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeControlError

    for owner, token in (("runtime-a", None), (None, FencingToken(1))):
        with pytest.raises(RuntimeControlError, match="RUNTIME_FENCE_INCOMPLETE"):
            PaperRuntimeAccount(
                account_id=uuid4(),
                bot_id=uuid4(),
                gateway=object(),  # type: ignore[arg-type]
                coordinator=object(),  # type: ignore[arg-type]
                execution_profile=object(),  # type: ignore[arg-type]
                owner_id=owner,
                fencing_token=token,
            )


def test_db_trigger_stale_token_is_mapped_and_demotes_account() -> None:
    from sqlalchemy.exc import IntegrityError

    lease_lost: list[tuple[object, str]] = []
    service, command, _, _, _, _, _, _ = _application_with_fakes(
        fail_submission=False,
        lease_lost_callback=lambda account, code: lease_lost.append((account, code)),
    )
    account = service._active_accounts[command.account_id]

    async def _rejected(*_: object, **__: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("MB05_STALE_FENCING_TOKEN"))

    account.coordinator.submit_approved = _rejected
    _FakeUnitOfWork.instances = []

    with pytest.raises(PaperRuntimeApplicationError, match="STALE_FENCING_TOKEN"):
        asyncio.run(service.evaluate_and_submit(command))

    assert _FakeUnitOfWork.instances[0].rolled_back is True
    assert lease_lost == [(command.account_id, "STALE_FENCING_TOKEN")]
    assert not service.is_available_for(command.account_id)
