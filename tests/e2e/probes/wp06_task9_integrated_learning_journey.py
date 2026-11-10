"""Probe Compose cho hành trình learning WP06 Task9 bằng dữ liệu PostgreSQL thật."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

STATE: dict[str, Any] = json.loads(os.environ["WP06_TASK9_STATE"])
WORKSPACE = UUID(STATE["workspace_id"])
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")


def _connect() -> psycopg.Connection[Any]:
    password_path = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"])
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password_path.read_text(encoding="utf-8").strip(),
        autocommit=True,
    )


def _insert(connection: psycopg.Connection[Any], table: str, **values: Any) -> None:
    values.setdefault("id", uuid4())
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values),
    )
    connection.execute(query, tuple(values.values()))


def _seed_paper() -> dict[str, Any]:
    from trading_bot.adapters.database.repositories.workspace_settings import (
        canonical_settings,
        settings_hash,
    )
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash
    from trading_bot.config.workspace_settings import WorkspaceSettings
    from trading_bot.domain.strategy.registry import StrategyRegistry

    membership_id = UUID(STATE["membership_id"])
    user_id = UUID(STATE["account_id"])
    account_id = uuid4()
    risk_profile_id = uuid4()
    root_settings_id = uuid4()
    bot_config_id = uuid4()
    instrument_ids = {symbol: uuid4() for symbol in SYMBOLS}
    now = datetime.now(UTC)
    effective_at = now - timedelta(days=1)
    bot_id: UUID
    with _connect() as connection:
        _insert(
            connection,
            "role_binding",
            workspace_id=WORKSPACE,
            membership_id=membership_id,
            role="owner",
        )
        _insert(
            connection,
            "trading_account",
            id=account_id,
            workspace_id=WORKSPACE,
            name="WP06 Task9 PAPER signal account",
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status="ENABLED",
            max_notional=Decimal("10000"),
        )
        result = connection.execute(
            "SELECT id FROM bot_instance WHERE workspace_id=%s AND trading_account_id=%s",
            (WORKSPACE, account_id),
        ).fetchone()
        assert result is not None
        bot_id = result[0]
        connection.execute(
            "UPDATE bot_instance SET status='ENABLED' WHERE workspace_id=%s AND id=%s",
            (WORKSPACE, bot_id),
        )

        hard_caps = {
            "schema_version": 1,
            "hard_caps": {
                "max_aggregate_open_risk": "250",
                "max_notional": "10000",
                "max_leverage": "5",
                "max_positions": 2,
                "max_symbol_notional": "2000",
                "max_concentration_fraction": "0.2",
                "max_daily_loss": "200",
                "max_rolling_loss": "500",
                "max_account_age_seconds": 86400,
                "max_data_age_seconds": 7200,
            },
        }
        _insert(
            connection,
            "risk_profile_version",
            id=risk_profile_id,
            workspace_id=WORKSPACE,
            name="WP06 Task9 signal risk",
            version=1,
            max_risk_fraction=Decimal("0.01"),
            max_open_risk_fraction=Decimal("1.0"),
            settings=Jsonb(hard_caps),
            is_active=True,
        )
        settings = WorkspaceSettings.model_validate(
            {
                "schema_version": 1,
                "runtime": {"mode": "PAPER", "timezone": "UTC"},
                "market_data": {
                    "venue": "BINANCE_USDM",
                    "symbols": list(SYMBOLS),
                    "timeframes": ["1h", "4h"],
                    "max_age_seconds": 7200,
                },
                "features": {
                    "formula_version": "crypidian-features-v1",
                    "sessions_timezone": "UTC",
                },
                "strategy": {
                    "default_release": "h01-v1",
                    "parameters": {},
                    "entry_timeframe": "1h",
                    "confirmation_timeframe": "4h",
                },
                "risk": {"profile_version_id": str(risk_profile_id)},
                "paper_execution": {
                    "quote_asset": "USDT",
                    "starting_equity": "10000",
                    "fee_schedule_version": "paper-fees-v1",
                    "fill_model": "next_bar_with_adverse_costs",
                },
            }
        )
        settings_payload = canonical_settings(settings)
        root_hash = settings_hash(settings_payload)
        _insert(
            connection,
            "workspace_settings_version",
            id=root_settings_id,
            workspace_id=WORKSPACE,
            version=1,
            schema_version=1,
            settings_json=Jsonb(settings_payload),
            settings_hash=root_hash,
            status="PUBLISHED",
            revision=1,
            created_by=membership_id,
            approved_by=membership_id,
            published_at=effective_at,
            effective_at=effective_at,
        )
        release = StrategyRegistry().get_release("h01-v1")
        _insert(
            connection,
            "strategy_release",
            workspace_id=WORKSPACE,
            name="h01-v1",
            version=1,
            status="PUBLISHED",
            recipe=Jsonb({}),
            content_hash=release.content_hash,
        )
        overlay = canonical_overlay(
            BotConfigOverlay.model_validate(
                {
                    "schema_version": 1,
                    "strategy": {"release_id": "h01-v1"},
                    "universe": {"symbols": list(SYMBOLS)},
                }
            )
        )
        _insert(
            connection,
            "bot_config_version",
            id=bot_config_id,
            workspace_id=WORKSPACE,
            bot_id=bot_id,
            account_id=account_id,
            version=1,
            schema_version=1,
            overlay_json=Jsonb(overlay),
            overlay_hash=overlay_hash(overlay),
            base_settings_version_id=root_settings_id,
            base_settings_hash=root_hash,
            status="DRAFT",
            revision=1,
            created_by=membership_id,
            updated_by=membership_id,
        )
        connection.execute(
            "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
            "published_at=%s, effective_at=%s WHERE workspace_id=%s AND id=%s",
            (membership_id, effective_at, effective_at, WORKSPACE, bot_config_id),
        )
        for symbol, instrument_id in instrument_ids.items():
            _insert(
                connection,
                "market_instrument",
                id=instrument_id,
                workspace_id=WORKSPACE,
                source="BINANCE_USDM",
                idempotency_key=f"wp06-task9-instrument:{symbol}",
                symbol=symbol,
                base_asset=symbol.removesuffix("USDT"),
                quote_asset="USDT",
                tick_size=Decimal("0.1"),
                quantity_step=Decimal("0.001"),
                min_notional=Decimal("5"),
                status="TRADING",
                revision=1,
                checksum="a" * 64,
                supersedes_id=None,
                event_at=effective_at,
                received_at=effective_at,
                available_at=effective_at,
            )
    return {
        "paper_account_id": str(account_id),
        "paper_bot_id": str(bot_id),
        "paper_risk_profile_id": str(risk_profile_id),
        "paper_root_settings_id": str(root_settings_id),
        "paper_bot_config_id": str(bot_config_id),
        "paper_instrument_ids": {symbol: str(value) for symbol, value in instrument_ids.items()},
        "paper_membership_id": str(membership_id),
        "paper_user_id": str(user_id),
    }


def _seed_market_bars() -> dict[str, Any]:
    instrument_ids = {
        symbol: UUID(value) for symbol, value in STATE["paper_instrument_ids"].items()
    }
    now = datetime.now(UTC)
    seeded = 0
    with _connect() as connection:
        for symbol, instrument_id in instrument_ids.items():
            symbol_index = SYMBOLS.index(symbol)
            price_offset = float(symbol_index * 100)
            for timeframe, interval, count, breakout in (
                ("1h", timedelta(hours=1), 206, True),
                ("4h", timedelta(hours=4), 205, False),
            ):
                final_close = now - timedelta(seconds=3)
                final_open = final_close - interval + timedelta(microseconds=1)
                first_open = final_open - (count - 1) * interval
                closes = (
                    [300.0 + price_offset - index * 0.1 for index in range(count - 1)]
                    + [300.0 + price_offset - (count - 2) * 0.1 + 50.0]
                    if breakout
                    else [200.0 + price_offset + index * 0.5 for index in range(count)]
                )
                for index, close in enumerate(closes):
                    opened = first_open + index * interval
                    closed = opened + interval - timedelta(microseconds=1)
                    previous = closes[index - 1] if index > 0 else close
                    open_price = previous if breakout and index > 0 else close
                    high = max(open_price, close) + 1.0
                    low = min(open_price, close) - 1.0
                    assert low > 0
                    _insert(
                        connection,
                        "market_bar",
                        id=uuid4(),
                        workspace_id=WORKSPACE,
                        instrument_id=instrument_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        source="BINANCE_USDM",
                        logical_key=f"WP06-TASK9:{symbol}:{timeframe}:{opened.isoformat()}",
                        idempotency_key=f"wp06-task9:{symbol}:{timeframe}:{index}",
                        revision=1,
                        checksum=f"{index:064x}",
                        supersedes_id=None,
                        raw_reference_id=None,
                        source_cursor=f"wp06-task9:{symbol}:{timeframe}:{index}",
                        open_time=opened,
                        close_time=closed,
                        event_at=closed,
                        received_at=closed + timedelta(milliseconds=500),
                        available_at=closed + timedelta(seconds=1),
                        open=Decimal(str(open_price)),
                        high=Decimal(str(high)),
                        low=Decimal(str(low)),
                        close=Decimal(str(close)),
                        volume=Decimal("100"),
                        quote_volume=Decimal(str(close * 100)),
                        is_closed=True,
                    )
                    seeded += 1
    return {"market_bars_seeded": seeded}


async def _grant_paper_rights() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    source_id = "BINANCE_USDM_REST"
    policy_id = f"{source_id}:task9-paper-demo"
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        repository = SourceRegistryRepository(unit.session)
        history = await repository.get_entry(workspace_id=WORKSPACE, source_id=source_id)
        now = await repository.database_clock(workspace_id=WORKSPACE)
        pending = history.transition(
            policy_id=f"{source_id}:task9-paper-pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            actor_id="task9-fixture-operator",
            at=now,
            reason="Mở xét quyền PAPER_DEMO cho journey tích hợp.",
        )
        await repository.append_revision(workspace_id=WORKSPACE, revision=pending.current)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        await SourceRegistryRepository(unit.session).finalize_policy(
            workspace_id=WORKSPACE,
            source_id=source_id,
            policy_id=pending.current.policy.policy_id,
        )
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        repository = SourceRegistryRepository(unit.session)
        history = await repository.get_entry(workspace_id=WORKSPACE, source_id=source_id)
        now = await repository.database_clock(workspace_id=WORKSPACE)
        verified = history.transition(
            policy_id=policy_id,
            state=SourcePolicyState.VERIFIED,
            actor_id="task9-fixture-operator",
            at=now,
            reason="Cho phép dùng dữ liệu fixture trong PAPER_DEMO.",
            allowed_purposes=frozenset({SourceUsePurpose.PAPER_DEMO}),
            evidence_ref="https://example.invalid/wp06-task9-paper-demo-fixture",
            reviewed_at=now,
            valid_until=now + timedelta(days=1),
            approved_by="task9-fixture-reviewer",
        )
        await repository.append_revision(workspace_id=WORKSPACE, revision=verified.current)
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        await SourceRegistryRepository(unit.session).finalize_policy(
            workspace_id=WORKSPACE,
            source_id=source_id,
            policy_id=verified.current.policy.policy_id,
        )
    await engine.dispose()
    return {"paper_rights_policy_id": policy_id}


async def _approve_split() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.learning_split_admin import LearningSplitAdminService
    from trading_bot.auth.policy import Principal, Role

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    principal = Principal(
        user_id=UUID(STATE["account_id"]),
        membership_id=UUID(STATE["membership_id"]),
        workspace_id=WORKSPACE,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    service = LearningSplitAdminService(sessions).for_principal(principal)
    candidates = await service.list_candidates()
    assert len(candidates) == 1
    candidate = candidates[0]
    staged = STATE["candidate"]
    assert str(candidate.candidate_id) == staged["candidate_id"]
    assert candidate.manifest_hash == staged["manifest_hash"]
    approval = await service.approve(
        candidate_id=candidate.candidate_id,
        reason="Duyệt split sau khi kiểm tra proof và holdout.",
        idempotency_key=uuid4(),
    )
    repeated = await service.approve(
        candidate_id=candidate.candidate_id,
        reason="Duyệt split sau khi kiểm tra proof và holdout.",
        idempotency_key=approval.idempotency_key,
    )
    assert repeated == approval
    await engine.dispose()
    return {"approval_hash": approval.content_hash}


async def _verify_sealed() -> dict[str, Any]:
    from sqlalchemy import select, text

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models.candidate_universe_entities import (
        CandidateUniverseProofRecord,
    )
    from trading_bot.adapters.database.models.learning_entities import (
        LearningSplitApprovalRecord,
    )
    from trading_bot.adapters.database.models.learning_fold_sample_entities import (
        NormalizedFoldAssignmentRecord,
        NormalizedLearningDatasetRecord,
        NormalizedLearningSampleRecord,
    )
    from trading_bot.adapters.database.models.learning_split_candidate_entities import (
        LearningSplitCandidateRecord,
    )
    from trading_bot.application.outbox import UnitOfWork

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    candidate_id = UUID(STATE["candidate"]["candidate_id"])
    trial_id = UUID(STATE["trial_ids"][6])
    async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
        candidate = await unit.session.scalar(
            select(LearningSplitCandidateRecord).where(
                LearningSplitCandidateRecord.workspace_id == WORKSPACE,
                LearningSplitCandidateRecord.id == candidate_id,
            )
        )
        proof = await unit.session.scalar(
            select(CandidateUniverseProofRecord).where(
                CandidateUniverseProofRecord.workspace_id == WORKSPACE,
                CandidateUniverseProofRecord.trial_record_id == trial_id,
            )
        )
        approval = await unit.session.scalar(
            select(LearningSplitApprovalRecord).where(
                LearningSplitApprovalRecord.workspace_id == WORKSPACE,
                LearningSplitApprovalRecord.manifest_hash == STATE["candidate"]["manifest_hash"],
            )
        )
        dataset = await unit.session.scalar(
            select(NormalizedLearningDatasetRecord).where(
                NormalizedLearningDatasetRecord.workspace_id == WORKSPACE,
                NormalizedLearningDatasetRecord.manifest_hash
                == STATE["candidate"]["manifest_hash"],
            )
        )
        assert candidate is not None
        assert proof is not None
        assert approval is not None
        assert dataset is not None
        assert approval.mfa_verified_at is not None
        assert approval.actor_membership_id == UUID(STATE["membership_id"])
        assert dataset.approval_hash == approval.approval_hash
        sample_count = await unit.session.scalar(
            select(text("count(*)")).select_from(NormalizedLearningSampleRecord)
        )
        assignment_count = await unit.session.scalar(
            select(text("count(*)")).select_from(NormalizedFoldAssignmentRecord)
        )
        assert sample_count == 3
        assert assignment_count == 3
        assert (
            proof.candidate_count
            == candidate.preview["development_count"] + candidate.preview["holdout_count"]
        )
    await engine.dispose()
    return {
        "actual_candidate_proof": True,
        "reviewer_mfa_approval": True,
        "normalized_payload_sealed": True,
        "holdout_excluded": dataset.sample_count == 3,
    }


async def _enqueue_training() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.learning_training_admin import LearningTrainingAdminService
    from trading_bot.auth.policy import Principal, Role

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    principal = Principal(
        user_id=UUID(STATE["account_id"]),
        membership_id=UUID(STATE["membership_id"]),
        workspace_id=WORKSPACE,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    service = LearningTrainingAdminService(sessions).for_principal(principal)
    options = await service.list_eligible_folds()
    assert len(options) == 1
    option = options[0]
    assert option.manifest_hash == STATE["candidate"]["manifest_hash"]
    submissions = [
        await service.enqueue(
            normalized_dataset_id=option.normalized_dataset_id,
            fold_id=option.fold_id,
            idempotency_key=uuid4(),
        )
        for _ in range(3)
    ]
    assert all(item.status == "queued" and item.job_id is not None for item in submissions)
    assert len({item.attempt_id for item in submissions}) == 3
    await engine.dispose()
    return {
        "attempt_ids": [str(item.attempt_id) for item in submissions],
        "job_ids": [str(item.job_id) for item in submissions],
        "manifest_hash": option.manifest_hash,
        "fold_id": option.fold_id,
    }


async def _evaluate_fold() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.learning_evaluation import (
        LearningEvaluationReportService,
        LearningEvaluationRequest,
    )
    from trading_bot.domain.learning.evaluation import TemporalWindow, WalkForwardSplit

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    fold_payload = STATE["config"]["folds"][0]
    fold = WalkForwardSplit(
        train=TemporalWindow(*(datetime.fromisoformat(item) for item in fold_payload["train"])),
        validation=TemporalWindow(
            *(datetime.fromisoformat(item) for item in fold_payload["validation"])
        ),
        test=TemporalWindow(*(datetime.fromisoformat(item) for item in fold_payload["test"])),
    )
    request = LearningEvaluationRequest(
        workspace_id=WORKSPACE,
        manifest_hash=STATE["candidate"]["manifest_hash"],
        folds=(fold,),
        evaluation_cutoff=datetime.fromisoformat(STATE["config"]["evaluation_cutoff"]),
        seed=17,
        idempotency_key=uuid4(),
    )
    result = await LearningEvaluationReportService(sessions).evaluate(request)
    await engine.dispose()
    return {
        "report_id": str(result.record_id),
        "report_hash": result.report.report_hash,
        "candidate_completeness_verified": result.report.candidate_completeness_verified,
        "evaluation_sample_count": sum(
            len(getattr(result.report.fold_evaluations[0], role).sample_ids)
            for role in ("train", "validation", "test")
        ),
    }


async def _principal_registry_service(sessions: Any) -> Any:
    from trading_bot.auth.policy import Principal, Role

    principal = Principal(
        user_id=UUID(STATE["account_id"]),
        membership_id=UUID(STATE["membership_id"]),
        workspace_id=WORKSPACE,
        roles=frozenset({Role.ANALYST_REVIEWER}),
        mfa_verified_at=datetime.now(UTC),
    )
    from trading_bot.application.learning_model_registry import LearningModelRegistryService

    return LearningModelRegistryService(sessions).for_principal(principal)


async def _register_and_approve() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    service = await _principal_registry_service(create_session_factory(engine))
    registry_ids: list[str] = []
    artifact_ids = [item["artifact_id"] for item in STATE["artifacts"][:2]]
    for artifact_id in artifact_ids:
        entry = await service.register_artifact(
            artifact_id=UUID(artifact_id),
            idempotency_key=uuid4(),
            reason="Đăng ký artifact PAPER do trainer Compose sinh.",
        )
        entry = await service.attach_evaluation(
            registry_id=entry.id,
            evaluation_report_id=UUID(STATE["report_id"]),
            evaluation_report_hash=STATE["report_hash"],
            expected_revision=entry.revision,
            idempotency_key=uuid4(),
            reason="Gắn báo cáo walk-forward đọc outcome đã persist.",
        )
        entry = await service.approve(
            registry_id=entry.id,
            expected_revision=entry.revision,
            idempotency_key=uuid4(),
            reason="Reviewer MFA duyệt artifact chỉ cho trạng thái PAPER.",
        )
        assert entry.state.value == "evaluated"
        assert entry.revision == 3
        registry_ids.append(str(entry.id))
    active, pointer = await service.activate(
        registry_id=UUID(registry_ids[0]),
        expected_revision=3,
        expected_pointer_version=0,
        expected_current_registry_id=None,
        idempotency_key=uuid4(),
        reason="Kích hoạt incumbent PAPER trong journey E2E.",
    )
    assert active.state.value == "paper_active"
    assert pointer.version == 1
    async with engine.begin() as connection:
        sqlalchemy = __import__("sqlalchemy")
        await connection.execute(
            sqlalchemy.text("SELECT set_config('app.workspace_id', :workspace, true)"),
            {"workspace": str(WORKSPACE)},
        )
        artifact_states = (
            await connection.execute(
                sqlalchemy.text(
                    "SELECT (artifact.artifact ->> 'calibrated')::boolean, "
                    "artifact.artifact ->> 'scorer_state' "
                    "FROM learning_training_artifact AS artifact "
                    "WHERE artifact.workspace_id=:workspace "
                    "ORDER BY artifact.created_at, artifact.id LIMIT 3"
                ),
                {"workspace": WORKSPACE},
            )
        ).all()
    await engine.dispose()
    assert len(artifact_states) == 3
    return {
        "registry_ids": registry_ids,
        "registry_id": registry_ids[1],
        "incumbent_registry_id": registry_ids[0],
        "artifact_ids": artifact_ids,
        "human_approval_events": len(registry_ids),
        "initial_pointer_version": pointer.version,
        "all_artifacts_abstain_uncalibrated": all(
            calibrated is False and scorer_state == "abstain"
            for calibrated, scorer_state in artifact_states
        ),
    }


async def _register_third() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    service = await _principal_registry_service(create_session_factory(engine))
    entry = await service.register_artifact(
        artifact_id=UUID(STATE["artifacts"][2]["artifact_id"]),
        idempotency_key=uuid4(),
        reason="Đăng ký artifact trainer thứ ba cho cohort sau rollback.",
    )
    entry = await service.attach_evaluation(
        registry_id=entry.id,
        evaluation_report_id=UUID(STATE["report_id"]),
        evaluation_report_hash=STATE["report_hash"],
        expected_revision=entry.revision,
        idempotency_key=uuid4(),
        reason="Gắn report diagnostic thật từ fold outcome đã lưu.",
    )
    entry = await service.approve(
        registry_id=entry.id,
        expected_revision=entry.revision,
        idempotency_key=uuid4(),
        reason="Reviewer MFA duyệt challenger sau rollback.",
    )
    await engine.dispose()
    return {"third_registry_id": str(entry.id), "third_revision": entry.revision}


async def _activate_and_rollback() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    service = await _principal_registry_service(create_session_factory(engine))
    incumbent_id = UUID(STATE["incumbent_registry_id"])
    challenger_id = UUID(STATE["registry_id"])
    challenger, pointer_two = await service.activate(
        registry_id=challenger_id,
        expected_revision=3,
        expected_pointer_version=1,
        expected_current_registry_id=incumbent_id,
        idempotency_key=uuid4(),
        reason="Tạm thay challenger đã review để kiểm rollback.",
    )
    incumbent, pointer_three = await service.rollback(
        registry_id=incumbent_id,
        expected_revision=5,
        expected_pointer_version=pointer_two.version,
        expected_current_registry_id=challenger_id,
        idempotency_key=uuid4(),
        reason="Rollback về incumbent trước để ghim revision mới.",
    )
    await engine.dispose()
    assert challenger.state.value == "paper_active"
    return {
        "pointer_version": pointer_three.version,
        "incumbent_revision": incumbent.revision,
        "incumbent_registry_id": str(incumbent.id),
    }


async def _activate_challenger() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    try:
        service = await _principal_registry_service(create_session_factory(engine))
        active, pointer = await service.activate(
            registry_id=UUID(STATE["registry_id"]),
            expected_revision=3,
            expected_pointer_version=int(STATE["initial_pointer_version"]),
            expected_current_registry_id=UUID(STATE["incumbent_registry_id"]),
            idempotency_key=uuid4(),
            reason="Kích hoạt challenger sau khi observer nhả active-pointer lock.",
        )
        assert active.state.value == "paper_active"
        return {
            "active_registry_id": str(pointer.registry_id),
            "pointer_version": pointer.version,
        }
    finally:
        await engine.dispose()


async def _rollback_after_pointer_race() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    try:
        service = await _principal_registry_service(create_session_factory(engine))
        incumbent_id = UUID(STATE["incumbent_registry_id"])
        challenger_id = UUID(STATE["registry_id"])
        incumbent = await service.get_entry(incumbent_id)
        active_pointer = await service.get_active_pointer()
        assert active_pointer.registry_id == challenger_id
        assert active_pointer.version == int(STATE["raced_pointer_version"])
        restored, pointer = await service.rollback(
            registry_id=incumbent_id,
            expected_revision=incumbent.revision,
            expected_pointer_version=active_pointer.version,
            expected_current_registry_id=challenger_id,
            idempotency_key=uuid4(),
            reason="Khôi phục incumbent sau kiểm tra race active pointer.",
        )
        assert restored.state.value == "paper_active"
        return {
            "pointer_version": pointer.version,
            "incumbent_revision": restored.revision,
            "incumbent_registry_id": str(restored.id),
        }
    finally:
        await engine.dispose()


def _execution_snapshot() -> dict[str, Any]:
    with _connect() as connection:
        order_counts = {
            table: int(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE workspace_id=%s").format(
                        sql.Identifier(table)
                    ),
                    (WORKSPACE,),
                ).fetchone()[0]
            )
            for table in (
                "order_intent",
                "exchange_order",
                "fill",
                "trade",
                "risk_reservation",
                "venue_order_intent",
            )
        }
        job_count = int(
            connection.execute(
                "SELECT count(*) FROM learning_training_job WHERE workspace_id=%s", (WORKSPACE,)
            ).fetchone()[0]
        )
        pointer = connection.execute(
            "SELECT registry_id, version FROM learning_model_active_pointer WHERE workspace_id=%s",
            (WORKSPACE,),
        ).fetchone()
        decisions = int(
            connection.execute(
                "SELECT count(*) FROM bot_decision WHERE workspace_id=%s", (WORKSPACE,)
            ).fetchone()[0]
        )
        return {
            "order_counts": order_counts,
            "training_job_count": job_count,
            "pointer_registry_id": str(pointer[0]) if pointer else None,
            "pointer_version": int(pointer[1]) if pointer else 0,
            "paper_decision_count": decisions,
        }


def _verify_scorer_artifacts() -> dict[str, Any]:
    with _connect() as connection:
        connection.execute(
            "SELECT set_config('app.workspace_id', %s, false)",
            (str(WORKSPACE),),
        )
        rows = connection.execute(
            "SELECT scorer.id, scorer.attempt_id, scorer.base_artifact_id, "
            "scorer.base_artifact_hash, scorer.artifact_hash, scorer.canonical_artifact, "
            "scorer.artifact, scorer.state, scorer.reason_code, "
            "scorer.strategy_release_id, scorer.strategy_release_content_hash, "
            "scorer.vector_schema_version, scorer.vector_schema_hash, "
            "scorer.training_membership_digest, scorer.validation_membership_digest, "
            "scorer.training_sample_count, scorer.validation_sample_count, "
            "base.id, base.artifact_hash, base.canonical_artifact, base.artifact "
            "FROM learning_scorer_artifact AS scorer "
            "JOIN learning_training_artifact AS base "
            "ON base.workspace_id=scorer.workspace_id AND base.attempt_id=scorer.attempt_id "
            "WHERE scorer.workspace_id=%s ORDER BY scorer.attempt_id",
            (WORKSPACE,),
        ).fetchall()
        assert len(rows) == len(STATE["attempt_ids"]) == 3

        for row in rows:
            (
                scorer_id,
                attempt_id,
                base_id,
                base_hash,
                scorer_hash,
                scorer_canonical,
                scorer_payload,
                scorer_state,
                scorer_reason,
                release_id,
                release_hash,
                vector_version,
                vector_schema_hash,
                train_digest,
                validation_digest,
                train_count,
                validation_count,
                joined_base_id,
                joined_base_hash,
                base_canonical,
                base_payload,
            ) = row
            parsed_scorer = json.loads(scorer_canonical)
            parsed_base = json.loads(base_canonical)
            assert str(scorer_id)
            assert str(attempt_id) in STATE["attempt_ids"]
            assert base_id == joined_base_id
            assert base_hash == joined_base_hash
            assert hashlib.sha256(scorer_canonical.encode("utf-8")).hexdigest() == scorer_hash
            assert hashlib.sha256(base_canonical.encode("utf-8")).hexdigest() == base_hash
            assert parsed_scorer == scorer_payload
            assert parsed_base == base_payload
            assert parsed_base["calibrated"] is False
            assert parsed_base["scorer_state"] == "abstain"
            assert parsed_scorer["base_artifact_hash"] == base_hash
            assert parsed_scorer["state"] == scorer_state == "abstain"
            assert (
                parsed_scorer["reason_code"] == scorer_reason == ("authoritative_lineage_missing")
            )
            assert release_id is None
            assert release_hash is None
            assert vector_version is None
            assert len(vector_schema_hash) == len(train_digest) == len(validation_digest) == 64
            assert train_count > 0
            assert validation_count > 0
            assert not {"samples", "labels", "features", "test_payload"}.intersection(parsed_scorer)

        privileges = connection.execute(
            "SELECT has_table_privilege(current_user, "
            "'public.learning_scorer_artifact', 'SELECT'), "
            "has_table_privilege(current_user, 'public.learning_scorer_artifact', 'INSERT'), "
            "has_table_privilege(current_user, 'public.learning_scorer_artifact', 'UPDATE'), "
            "has_table_privilege(current_user, 'public.learning_scorer_artifact', 'DELETE'), "
            "has_table_privilege(current_user, 'public.learning_training_artifact', 'UPDATE'), "
            "has_table_privilege(current_user, 'public.learning_training_artifact', 'DELETE'), "
            "has_column_privilege(current_user, 'public.backtest_trade_outcome', 'id', 'SELECT'), "
            "has_column_privilege(current_user, 'public.backtest_decision_opportunity', "
            "'event_id', 'SELECT')"
        ).fetchone()
        assert privileges == (True, True, False, False, False, False, False, False), privileges

        hidden = connection.execute(
            "SELECT count(*) FROM learning_scorer_artifact WHERE workspace_id=%s",
            (uuid4(),),
        ).fetchone()[0]
        assert hidden == 0

        before_count = len(rows)
        tampering_error: psycopg.Error | None = None
        try:
            connection.execute(
                "INSERT INTO public.learning_scorer_artifact "
                "SELECT (jsonb_populate_record(NULL::public.learning_scorer_artifact, "
                "to_jsonb(source) || jsonb_build_object('id', %s, "
                "'training_sample_count', source.training_sample_count + 1))).* "
                "FROM public.learning_scorer_artifact AS source "
                "WHERE source.workspace_id=%s ORDER BY source.attempt_id LIMIT 1",
                (uuid4(), WORKSPACE),
            )
        except psycopg.Error as error:
            tampering_error = error
        assert tampering_error is not None, (
            "DB phải chặn denormalized metadata lệch canonical JSON."
        )
        assert tampering_error.sqlstate == "P0001", (
            tampering_error.sqlstate,
            str(tampering_error),
        )
        assert "WP06_SCORER_ARTIFACT_CANONICAL_INVALID" in str(tampering_error)

        after_count = connection.execute(
            "SELECT count(*) FROM learning_scorer_artifact WHERE workspace_id=%s",
            (WORKSPACE,),
        ).fetchone()[0]
        assert after_count == before_count
        v1_mutation_error: psycopg.Error | None = None
        try:
            connection.execute(
                "UPDATE public.learning_training_artifact SET artifact_hash=artifact_hash "
                "WHERE workspace_id=%s",
                (WORKSPACE,),
            )
        except psycopg.Error as error:
            v1_mutation_error = error
        assert v1_mutation_error is not None
        assert v1_mutation_error.sqlstate == "42501"
        for table in ("backtest_trade_outcome", "backtest_decision_opportunity"):
            try:
                connection.execute(f"SELECT * FROM public.{table} LIMIT 0")
            except psycopg.errors.InsufficientPrivilege:
                continue
            raise AssertionError(f"Trainer không được SELECT trực tiếp public.{table}.")

    return {
        "scorer_artifact_count": len(rows),
        "base_v1_unchanged_diagnostic_only": True,
        "canonical_hash_and_lineage_verified": True,
        "trainer_least_privilege_and_workspace_rls": True,
        "base_v1_update_denied": True,
        "denormalized_tampering_rejected": True,
        "raw_outcome_and_opportunity_reads_denied": True,
    }


async def _verify_scorer_repository_retries() -> dict[str, Any]:
    from dataclasses import replace

    from sqlalchemy import select

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import LearningScorerArtifactRecord
    from trading_bot.adapters.database.repositories.learning_scorer_artifacts import (
        LearningScorerArtifactConflict,
        LearningScorerArtifactRepository,
    )
    from trading_bot.application.learning_scorer_artifact import (
        LearningScorerArtifact,
        VerifiedLearningScorerArtifact,
    )
    from trading_bot.application.outbox import UnitOfWork

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            row = await unit.session.scalar(
                select(LearningScorerArtifactRecord)
                .where(LearningScorerArtifactRecord.workspace_id == WORKSPACE)
                .order_by(LearningScorerArtifactRecord.attempt_id)
                .limit(1)
            )
        assert row is not None
        payload = json.loads(row.canonical_artifact)
        payload["validation_class_support"] = tuple(
            tuple(item) for item in payload["validation_class_support"]
        )
        artifact = LearningScorerArtifact(**payload)
        verified = VerifiedLearningScorerArtifact(
            workspace_id=row.workspace_id,
            attempt_id=row.attempt_id,
            normalized_dataset_id=row.normalized_dataset_id,
            artifact_hash=row.artifact_hash,
            base_artifact_hash=row.base_artifact_hash,
            canonical_artifact=row.canonical_artifact,
            artifact=artifact,
        )
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            duplicate = await LearningScorerArtifactRepository(unit.session).append_success(
                workspace_id=WORKSPACE,
                artifact=verified,
            )
        assert duplicate.is_duplicate is True

        changed_artifact = replace(artifact, reason_code="changed_scorer_content")
        changed_verified = replace(
            verified,
            artifact_hash=changed_artifact.artifact_hash,
            canonical_artifact=changed_artifact.canonical_json,
            artifact=changed_artifact,
        )
        changed_conflict = False
        try:
            async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
                await LearningScorerArtifactRepository(unit.session).append_success(
                    workspace_id=WORKSPACE,
                    artifact=changed_verified,
                )
        except LearningScorerArtifactConflict:
            changed_conflict = True
        assert changed_conflict
        return {"identical_retry_is_duplicate": True, "changed_retry_conflicts": True}
    finally:
        await engine.dispose()


def _verify_paper_decisions() -> dict[str, Any]:
    with _connect() as connection:
        decision_count, snapshot_count, source_count = connection.execute(
            "SELECT count(DISTINCT decision.id), count(DISTINCT evidence.feature_snapshot_id), "
            "count(DISTINCT ref.value) "
            "FROM bot_decision AS decision "
            "JOIN shared_signal_evidence AS evidence "
            "ON evidence.workspace_id=decision.workspace_id "
            "AND evidence.id=decision.evidence_id "
            "AND evidence.evidence_hash=decision.evidence_hash "
            "CROSS JOIN LATERAL jsonb_array_elements_text(evidence.source_refs) AS ref(value) "
            "WHERE decision.workspace_id=%s AND decision.status='CANDIDATE'",
            (WORKSPACE,),
        ).fetchone()
    assert decision_count >= 3
    assert snapshot_count >= 3
    return {
        "decision_count": decision_count,
        "snapshot_count": snapshot_count,
        "market_event_source_count": source_count,
    }


async def _produce_paper_decision() -> dict[str, Any]:
    # Workspace phải khớp identity bắt buộc của market-signal-worker trong Compose.
    os.environ["TRADING_BOT_MARKET_SIGNAL_WORKSPACE_ID"] = str(WORKSPACE)

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.market_signal_composition import build_market_signal_scanner
    from trading_bot.config.settings import AppSettings
    from trading_bot.workers.market_signal_producer import MarketSignalProducerWorker

    settings = AppSettings()
    assert settings.process_role == "market-signal-worker"
    assert settings.market_signal_workspace_id == WORKSPACE
    max_age = timedelta(seconds=settings.signal_worker_max_candidate_age_seconds)
    engine = create_database_engine(
        database_url_from_secret(
            username=settings.database_user,
            password_file=settings.database_password_file,
            host=settings.database_host,
            database=settings.database_name,
        )
    )
    sessions = create_session_factory(engine)
    scanner = build_market_signal_scanner(
        sessions,
        max_observation_age=max_age,
        signal_validity=max_age,
    )
    worker = MarketSignalProducerWorker(
        scanner,
        workspace_id=WORKSPACE,
        tick_seconds=settings.signal_worker_tick_seconds,
    )
    try:
        # WP07 E2E kiểm tra app lifespan; Task9 tập trung vào cùng production worker/composition
        # nhưng gọi một tick trong event loop probe để không lồng TestClient đồng bộ.
        result = await asyncio.wait_for(worker.tick(), timeout=45)
        return {
            "decision_count": result.decision_count,
            "dispatched_count": result.dispatched_count,
            "workspace_id": str(WORKSPACE),
        }
    finally:
        await engine.dispose()


async def _observe() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.learning_shadow import LearningShadowApplication
    from trading_bot.domain.learning.shadow import ShadowAbstainReason

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    batch = await LearningShadowApplication(sessions).observe_pending(workspace_id=WORKSPACE)
    await engine.dispose()
    return {
        "scanned_count": batch.scanned_count,
        "created_count": batch.created_count,
        "all_abstain": all(
            observation.challenger_reason
            in {
                ShadowAbstainReason.FEATURE_SCHEMA_MISMATCH,
                ShadowAbstainReason.UNCALIBRATED_SCORE,
            }
            for observation in batch.observations
        ),
        "rights_verified": all(
            observation.rights_eligible
            and observation.rights_fingerprint is not None
            and observation.rights_policy_id == STATE["paper_rights_policy_id"]
            for observation in batch.observations
        ),
        "observations": [
            {
                "id": str(item.observation_id),
                "challenger_registry_id": str(item.challenger_model.registry_id),
                "challenger_revision": item.challenger_model.revision,
                "active_registry_id": str(item.active_registry_id),
                "active_registry_revision": item.active_registry_revision,
                "active_pointer_version": item.active_pointer_version,
                "reason": item.challenger_reason.value,
                "schema_compatible": item.schema_compatible,
            }
            for item in batch.observations
        ],
    }


async def _observe_holding_pointer_lock() -> dict[str, Any]:
    from dataclasses import replace

    from sqlalchemy import func, select
    from sqlalchemy.exc import DBAPIError

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import LearningShadowObservationRecord
    from trading_bot.adapters.database.repositories.learning_shadow import (
        LearningShadowRepository,
        _record_from_observation,
    )
    from trading_bot.application.learning_shadow import LearningShadowApplication
    from trading_bot.domain.learning.shadow import ShadowAbstainReason

    hold_seconds = float(os.environ["WP06_TASK9_HOLD_POINTER_LOCK_SECONDS"])
    if not 1.0 <= hold_seconds <= 10.0:
        raise ValueError("Test lock-hold phải nằm trong 1–10 giây.")

    rights_receipt_checks: dict[str, bool] = {}

    class HoldingLearningShadowRepository(LearningShadowRepository):
        async def lock_active_pointer(self, *, workspace_id: UUID) -> None:
            await super().lock_active_pointer(workspace_id=workspace_id)
            print("WP06_OBSERVER_ACTIVE_POINTER_LOCK_HELD", flush=True)
            await asyncio.sleep(hold_seconds)

        async def append(self, *, observation):
            result = await super().append(observation=observation)
            if not result.is_duplicate and not rights_receipt_checks:
                before_count = await self._session.scalar(
                    select(func.count())
                    .select_from(LearningShadowObservationRecord)
                    .where(
                        LearningShadowObservationRecord.workspace_id == WORKSPACE,
                        LearningShadowObservationRecord.challenger_registry_id
                        == observation.challenger_model.registry_id,
                    )
                )
                receipt = await self.current_paper_rights(workspace_id=WORKSPACE)
                if receipt is None:
                    raise AssertionError("Rights receipt phải còn hiệu lực trong source lock.")

                candidates = (
                    replace(
                        observation,
                        observation_id=uuid4(),
                        rights_source_id="BINANCE_USDM",
                    ),
                    replace(
                        observation,
                        observation_id=uuid4(),
                        rights_fingerprint=hashlib.sha256(b"forged-rights-fingerprint").hexdigest(),
                    ),
                )
                rejected: list[bool] = []
                for candidate in candidates:
                    try:
                        async with self._session.begin_nested():
                            self._session.add(_record_from_observation(candidate))
                            await self._session.flush()
                    except DBAPIError as error:
                        rejected.append(
                            "WP06_SHADOW_OBSERVATION_SOURCE_RIGHTS_UNVERIFIED" in str(error.orig)
                        )
                    else:
                        rejected.append(False)
                after_count = await self._session.scalar(
                    select(func.count())
                    .select_from(LearningShadowObservationRecord)
                    .where(
                        LearningShadowObservationRecord.workspace_id == WORKSPACE,
                        LearningShadowObservationRecord.challenger_registry_id
                        == observation.challenger_model.registry_id,
                    )
                )
                rights_receipt_checks.update(
                    {
                        "wrong_policy_source_rejected": rejected[0],
                        "wrong_fingerprint_rejected": rejected[1],
                        "observation_count_unchanged": before_count == after_count,
                    }
                )
            return result

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    try:
        batch = await LearningShadowApplication(
            create_session_factory(engine),
            repository_factory=HoldingLearningShadowRepository,
        ).observe_pending(workspace_id=WORKSPACE)
        return {
            "scanned_count": batch.scanned_count,
            "created_count": batch.created_count,
            "all_abstain": all(
                observation.challenger_reason
                in {
                    ShadowAbstainReason.FEATURE_SCHEMA_MISMATCH,
                    ShadowAbstainReason.UNCALIBRATED_SCORE,
                }
                for observation in batch.observations
            ),
            "rights_verified": all(
                observation.rights_eligible
                and observation.rights_fingerprint is not None
                and observation.rights_policy_id == STATE["paper_rights_policy_id"]
                for observation in batch.observations
            ),
            "observations": [
                {
                    "id": str(item.observation_id),
                    "challenger_registry_id": str(item.challenger_model.registry_id),
                    "challenger_revision": item.challenger_model.revision,
                    "active_registry_id": str(item.active_registry_id),
                    "active_registry_revision": item.active_registry_revision,
                    "active_pointer_version": item.active_pointer_version,
                    "reason": item.challenger_reason.value,
                    "schema_compatible": item.schema_compatible,
                }
                for item in batch.observations
            ],
            "rights_receipt_checks": rights_receipt_checks,
        }
    finally:
        await engine.dispose()


async def _assess(registry_key: str) -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.learning_drift import LearningDriftApplication

    registry_id = UUID(STATE[registry_key])
    revision = int(STATE.get("third_revision", 3)) if registry_key == "third_registry_id" else 3
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    service = LearningDriftApplication(create_session_factory(engine))
    first = await service.assess_cohort(
        workspace_id=WORKSPACE,
        challenger_registry_id=registry_id,
        challenger_revision=revision,
    )
    assert first is not None
    repeated = await service.assess_cohort(
        workspace_id=WORKSPACE,
        challenger_registry_id=registry_id,
        challenger_revision=revision,
    )
    await engine.dispose()
    assert repeated is not None
    return {
        "assessment_id": str(first.assessment.assessment_id),
        "assessment_status": first.assessment.data_quality_status.value,
        "performance_status": first.assessment.performance_status.value,
        "reasons": [item.value for item in first.assessment.reason_codes],
        "member_count": first.assessment.member_count,
        "proposal_created": first.proposal is not None,
        "retry_is_duplicate": repeated.is_duplicate,
    }


async def _revoke_paper_demo_rights() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.source_registry import (
        SourceRegistryRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.research.source_policy import SourcePolicyState

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    source_id = "BINANCE_USDM_REST"
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            repository = SourceRegistryRepository(unit.session)
            history = await repository.get_entry(workspace_id=WORKSPACE, source_id=source_id)
            assert history is not None
            now = await repository.database_clock(workspace_id=WORKSPACE)
            revoked = history.transition(
                policy_id=f"{source_id}:task9-paper-revoked",
                state=SourcePolicyState.REVOKED,
                actor_id="task9-fixture-reviewer",
                at=now,
                reason="Thu hồi quyền PAPER_DEMO để kiểm tra assessment fail-closed.",
            )
            await repository.append_revision(workspace_id=WORKSPACE, revision=revoked.current)
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            receipt = await SourceRegistryRepository(unit.session).finalize_policy(
                workspace_id=WORKSPACE,
                source_id=source_id,
                policy_id=revoked.current.policy.policy_id,
            )
        return {"policy_sequence": receipt.sequence, "state": revoked.current.policy.state.value}
    finally:
        await engine.dispose()


async def _assess_after_rights_revocation() -> dict[str, Any]:
    from sqlalchemy import func, select

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import LearningDriftAssessmentRecord
    from trading_bot.adapters.database.repositories.learning_drift import LearningDriftRepository
    from trading_bot.adapters.database.repositories.learning_shadow import LearningShadowRepository
    from trading_bot.application.learning_drift import (
        LearningDriftApplication,
        LearningDriftRightsConflict,
    )
    from trading_bot.application.outbox import UnitOfWork

    registry_id = UUID(STATE["third_registry_id"])
    revision = int(STATE["third_revision"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    try:
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            receipt = await LearningShadowRepository(unit.session).current_paper_rights(
                workspace_id=WORKSPACE,
                source_id="BINANCE_USDM_REST",
            )
            members = await LearningDriftRepository(unit.session).list_cohort(
                workspace_id=WORKSPACE,
                registry_id=registry_id,
                revision=revision,
            )
            before_count = await unit.session.scalar(
                select(func.count())
                .select_from(LearningDriftAssessmentRecord)
                .where(
                    LearningDriftAssessmentRecord.workspace_id == WORKSPACE,
                    LearningDriftAssessmentRecord.cohort_registry_id == registry_id,
                    LearningDriftAssessmentRecord.cohort_revision == revision,
                )
            )
        eligible_member_count = sum(member.observation.rights_eligible for member in members)
        assert receipt is None
        assert eligible_member_count >= 1
        try:
            await LearningDriftApplication(sessions).assess_cohort(
                workspace_id=WORKSPACE,
                challenger_registry_id=registry_id,
                challenger_revision=revision,
            )
        except LearningDriftRightsConflict:
            rejected = True
        else:
            rejected = False
        async with UnitOfWork(sessions, workspace_id=WORKSPACE) as unit:
            after_count = await unit.session.scalar(
                select(func.count())
                .select_from(LearningDriftAssessmentRecord)
                .where(
                    LearningDriftAssessmentRecord.workspace_id == WORKSPACE,
                    LearningDriftAssessmentRecord.cohort_registry_id == registry_id,
                    LearningDriftAssessmentRecord.cohort_revision == revision,
                )
            )
        return {
            "current_rights_receipt_missing": receipt is None,
            "eligible_cohort_rejected": rejected,
            "assessment_count_unchanged": before_count == after_count,
            "eligible_member_count": eligible_member_count,
            "assessment_count_before": int(before_count or 0),
            "assessment_count_after": int(after_count or 0),
        }
    finally:
        await engine.dispose()


async def _role_matrix() -> dict[str, Any]:
    with _connect() as connection:
        role = connection.execute("SELECT current_user").fetchone()[0]
        if role == "crypidian_learning_shadow_observer":
            expected = {
                "normalized_payload_denied": not connection.execute(
                    "SELECT has_column_privilege(current_user, "
                    "'public.learning_normalized_sample','features','SELECT')"
                ).fetchone()[0],
                "holdout_denied": not connection.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.learning_fold_assignment','SELECT')"
                ).fetchone()[0],
                "order_write_denied": not connection.execute(
                    "SELECT has_table_privilege(current_user, 'public.order_intent','INSERT')"
                ).fetchone()[0],
                "risk_write_denied": not connection.execute(
                    "SELECT has_table_privilege(current_user, 'public.risk_reservation','INSERT')"
                ).fetchone()[0],
                "pointer_write_denied": not connection.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.learning_model_active_pointer','UPDATE')"
                ).fetchone()[0],
                "training_job_write_denied": not connection.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.learning_training_job','INSERT')"
                ).fetchone()[0],
            }
        else:
            expected = {
                "observer_write_denied": not connection.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.learning_shadow_observation','INSERT')"
                ).fetchone()[0]
                and not connection.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'public.learning_drift_assessment','INSERT')"
                ).fetchone()[0],
            }
    assert all(expected.values()), (role, expected)
    return {"role": role, **expected}


async def _cross_workspace() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.repositories.learning_shadow import LearningShadowRepository
    from trading_bot.application.outbox import UnitOfWork

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    foreign_workspace = uuid4()
    async with UnitOfWork(
        create_session_factory(engine),
        workspace_id=foreign_workspace,
    ) as unit:
        rows = await LearningShadowRepository(unit.session).list_pending(
            workspace_id=foreign_workspace,
            limit=10,
        )
    await engine.dispose()
    assert rows == ()
    return {"cross_workspace_hidden": True}


async def _restart_check() -> dict[str, Any]:
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.application.learning_drift import LearningDriftApplication
    from trading_bot.application.learning_shadow import LearningShadowApplication

    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host="postgres",
            database="crypidian",
        )
    )
    sessions = create_session_factory(engine)
    batch = await LearningShadowApplication(sessions).observe_pending(workspace_id=WORKSPACE)
    assessment = await LearningDriftApplication(sessions).assess_cohort(
        workspace_id=WORKSPACE,
        challenger_registry_id=UUID(STATE["third_registry_id"]),
        challenger_revision=int(STATE["third_revision"]),
    )
    await engine.dispose()
    assert assessment is not None
    return {
        "ledger_survived_restart": assessment.assessment.member_count >= 1,
        "observation_retry_is_idempotent": batch.created_count == 0,
        "assessment_retry_is_idempotent": assessment.is_duplicate,
    }


async def _main(phase: str) -> dict[str, Any]:
    if phase == "seed-paper":
        return _seed_paper()
    if phase == "seed-paper-market":
        return _seed_market_bars()
    if phase == "grant-paper-demo-rights":
        return await _grant_paper_rights()
    if phase == "approve-split":
        return await _approve_split()
    if phase == "verify-sealed":
        return await _verify_sealed()
    if phase == "enqueue-training":
        return await _enqueue_training()
    if phase == "evaluate-fold":
        return await _evaluate_fold()
    if phase == "register-and-approve":
        return await _register_and_approve()
    if phase == "register-third":
        return await _register_third()
    if phase == "activate-and-rollback":
        return await _activate_and_rollback()
    if phase == "activate-challenger":
        return await _activate_challenger()
    if phase == "rollback-after-pointer-race":
        return await _rollback_after_pointer_race()
    if phase == "execution-snapshot":
        return _execution_snapshot()
    if phase == "verify-scorer-artifacts":
        return _verify_scorer_artifacts()
    if phase == "verify-scorer-repository-retries":
        return await _verify_scorer_repository_retries()
    if phase == "verify-paper-decisions":
        return _verify_paper_decisions()
    if phase == "produce-paper-decision":
        return await _produce_paper_decision()
    if phase == "observe":
        return await _observe()
    if phase == "observe-hold-pointer":
        return await _observe_holding_pointer_lock()
    if phase == "assess-and-retry":
        return await _assess("registry_id")
    if phase == "observe-after-rollback":
        batch = await _observe()
        assert batch["created_count"] >= 1
        assert all(
            item["challenger_registry_id"] == STATE["third_registry_id"]
            and item["active_registry_id"] == STATE["incumbent_registry_id"]
            and item["active_registry_revision"] == STATE["incumbent_revision"]
            and item["active_pointer_version"] == STATE["pointer_version"]
            for item in batch["observations"]
        )
        batch["pins_rolled_back_incumbent"] = True
        return batch
    if phase == "assess-after-rollback":
        return await _assess("third_registry_id")
    if phase == "revoke-paper-demo-rights":
        return await _revoke_paper_demo_rights()
    if phase == "assess-after-rights-revocation":
        return await _assess_after_rights_revocation()
    if phase == "restart-check":
        return await _restart_check()
    if phase == "role-matrix":
        return await _role_matrix()
    if phase == "cross-workspace":
        return await _cross_workspace()
    raise ValueError(f"Phase không hỗ trợ: {phase}")


if __name__ == "__main__":
    import sys

    print(json.dumps(asyncio.run(_main(sys.argv[1])), ensure_ascii=False, sort_keys=True))
