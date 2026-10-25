"""Probe MB04 E2E: Concurrency barrier, Admin cap tightening, DB roles/RLS, and downgrade guard.

Chạy trong container Compose; mã probe được truyền qua stdin
để tương thích giới hạn dòng lệnh Windows.
Hỗ trợ các phase:
- `seed`: Tạo workspace, 2 PAPER accounts, 2 bot instances, settings và initial seeds.
- `concurrency_barrier`: 2 Bot chạy đồng thời xin 2,000 USDT (tổng 4,000 > 3,000 cap).
- `tighten_and_breach`: Admin siết Root cap xuống 5% khi Bot 1 có vị thế mở 1,200 USDT.
- `rls_checks`: Kiểm tra has_table_privilege, rolbypassrls, RLS cross-bot và SQL aggregate function.
- `insert_custom_profile_guard`: Chèn risk profile với max_open_risk_fraction = 0.5
  để kích hoạt downgrade guard.
- `cleanup_custom_profile_guard`: Xóa risk profile thử nghiệm để cho phép downgrade an toàn.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trading_bot.adapters.database.repositories.workspace_settings import (
    canonical_settings,
    settings_hash,
)
from trading_bot.application.workspace_risk import (
    ReservationRequest,
    WorkspaceRiskCoordinator,
)
from trading_bot.config.workspace_settings import WorkspaceSettings
from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    PaperEquityBasis,
    RiskConversionObservation,
    mark_to_market_equity,
)

NOW = datetime.now(UTC)
ZERO = Decimal("0")


def _get_db_connection() -> psycopg.Connection:
    """Tạo kết nối psycopg trực tiếp tới PostgreSQL bằng secret credentials."""
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return psycopg.connect(
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        dbname=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    )


def _get_async_url() -> URL:
    """Tạo SQLAlchemy async URL từ secret credentials."""
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    return URL.create(
        "postgresql+psycopg",
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )


def _insert(connection: psycopg.Connection, table: str, **values: object) -> None:
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values),
    )
    connection.execute(query, tuple(values.values()))


def _create_conversion(workspace_id: UUID, as_of: datetime = NOW) -> RiskConversionObservation:
    return RiskConversionObservation(
        workspace_id=str(workspace_id),
        base_asset="USDT",
        quote_asset="USDT",
        rate=Decimal("1.0"),
        as_of=as_of,
        available_at=as_of,
        source_ref="identity",
        source_version="v1",
        source_hash="c" * 64,
        quality="ACCEPTED",
        policy_version_id="pol_v1",
        known=True,
        route="DIRECT",
        approved=True,
    )


def _create_snapshot(
    workspace_id: UUID,
    bot_id: UUID,
    account_id: UUID,
    equity: Decimal,
    as_of: datetime = NOW,
) -> AccountEquitySnapshot:
    basis = PaperEquityBasis(
        workspace_id=str(workspace_id),
        bot_id=str(bot_id),
        account_id=str(account_id),
        asset="USDT",
        as_of=as_of,
        captured_at=as_of,
        available_at=as_of,
        source="paper_seed",
        source_version="v1",
        max_mark_age=timedelta(hours=24),
        max_equity_age=timedelta(hours=24),
        initial_capital_before_realized_pnl=equity,
        cumulative_realized_pnl=ZERO,
    )
    return mark_to_market_equity(basis, (), {}, as_of)


def _make_workspace_payload(profile_id: UUID, workspace_cap: str = "0.15") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime": {"mode": "PAPER", "timezone": "UTC"},
        "market_data": {
            "venue": "BINANCE_USDM",
            "symbols": ["BTCUSDT"],
            "timeframes": ["1h", "4h"],
            "max_age_seconds": 3600,
        },
        "features": {"formula_version": "crypidian-features-v1", "sessions_timezone": "UTC"},
        "strategy": {
            "default_release": "h01-v1",
            "parameters": {},
            "entry_timeframe": "1h",
            "confirmation_timeframe": "4h",
        },
        "risk": {
            "profile_version_id": str(profile_id),
            "workspace_max_open_risk_fraction": workspace_cap,
            "max_equity_age_seconds": 3600,
            "max_conversion_age_seconds": 3600,
        },
        "paper_execution": {
            "quote_asset": "USDT",
            "starting_equity": "10000",
            "fee_schedule_version": "paper-fees-v1",
            "fill_model": "next_bar_with_adverse_costs",
        },
    }


def seed_phase() -> dict[str, str]:
    """Khởi tạo workspace, 2 tài khoản PAPER (10,000 USDT mỗi tài khoản) và 2 bots."""
    ws_id = uuid4()
    slug = f"ws-mb04-{ws_id.hex[:8]}"

    acc1_id, acc2_id = uuid4(), uuid4()
    user_id = uuid4()
    membership_id = uuid4()
    profile_id = uuid4()
    release_id = uuid4()
    settings_id = uuid4()

    with _get_db_connection() as conn:
        # 1. Workspace
        _insert(
            conn, "workspace", id=ws_id, name="MB04 E2E", slug=slug, mode="PAPER", status="ACTIVE"
        )

        # 2. User & Membership (Owner)
        _insert(
            conn,
            "user_account",
            id=user_id,
            email_normalized=f"admin-{ws_id.hex[:6]}@example.test",
            password_hash="unused",
            status="ACTIVE",
        )
        _insert(
            conn,
            "user_membership",
            id=membership_id,
            workspace_id=ws_id,
            user_id=user_id,
            status="ACTIVE",
        )
        _insert(
            conn,
            "role_binding",
            id=uuid4(),
            workspace_id=ws_id,
            membership_id=membership_id,
            role="owner",
        )

        # 3. Hai tài khoản giao dịch PAPER
        _insert(
            conn,
            "trading_account",
            id=acc1_id,
            workspace_id=ws_id,
            name=f"acc-1-{ws_id.hex[:6]}",
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status="ENABLED",
            max_notional=Decimal("100000"),
        )
        _insert(
            conn,
            "trading_account",
            id=acc2_id,
            workspace_id=ws_id,
            name=f"acc-2-{ws_id.hex[:6]}",
            venue="binance_usdm",
            mode="PAPER",
            environment="local",
            status="ENABLED",
            max_notional=Decimal("100000"),
        )

        # 4. Bot instance được tự động provision bởi trigger trading_account_provision_bot
        bot1_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws_id, acc1_id),
        ).fetchone()[0]
        bot2_id = conn.execute(
            "SELECT id FROM bot_instance WHERE workspace_id = %s AND trading_account_id = %s",
            (ws_id, acc2_id),
        ).fetchone()[0]

        # 5. Risk Profile Version: Account Cap = 25% (0.25)
        _insert(
            conn,
            "risk_profile_version",
            id=profile_id,
            workspace_id=ws_id,
            name="MB04 Risk Profile",
            version=1,
            max_risk_fraction=Decimal("0.02"),
            max_open_risk_fraction=Decimal("0.25"),
            settings=Jsonb({"hard_caps": {"max_aggregate_open_risk": "2500"}}),
            is_active=True,
        )

        # 6. Strategy Release & Feature Snapshot
        _insert(
            conn,
            "strategy_release",
            id=release_id,
            workspace_id=ws_id,
            name="h01",
            version=1,
            status="ACTIVE",
            recipe=Jsonb({}),
            content_hash="a" * 64,
        )
        snap_hash = "a" * 64
        _insert(
            conn,
            "feature_snapshot",
            id=uuid4(),
            workspace_id=ws_id,
            snapshot_id=snap_hash,
            content_hash=snap_hash,
            symbol="BTCUSDT",
            timeframe="1h",
            as_of=NOW,
            computed_at=NOW,
            schema_hash="b" * 64,
            formula_hash="c" * 64,
            code_hash="d" * 64,
            input_bar_ids=Jsonb([]),
            input_event_ids=Jsonb([]),
            features=Jsonb({"atr_14": 2.0}),
            warmup=Jsonb({}),
            quality_flags=Jsonb([]),
            data_freshness="FRESH",
            data_coverage=Decimal("1.0"),
            regime=Jsonb({"label": "TREND"}),
            is_usable=True,
            canonical_json="{}",
        )

        # 7. Workspace Settings: Root Cap = 15% (0.15)
        raw_settings = _make_workspace_payload(profile_id, "0.15")
        validated = WorkspaceSettings.model_validate(raw_settings)
        canonical = canonical_settings(validated)
        s_hash = settings_hash(canonical)

        _insert(
            conn,
            "workspace_settings_version",
            id=settings_id,
            workspace_id=ws_id,
            version=1,
            revision=1,
            schema_version=1,
            status="PUBLISHED",
            published_at=NOW,
            effective_at=NOW,
            settings_json=Jsonb(canonical),
            settings_hash=s_hash,
            parent_version_id=None,
            created_by=membership_id,
        )

        # 8. Seed vốn PAPER: 10,000 USDT cho mỗi tài khoản (tổng 20,000 USDT)
        _insert(
            conn,
            "paper_account_seed",
            id=uuid4(),
            workspace_id=ws_id,
            bot_id=bot1_id,
            trading_account_id=acc1_id,
            initial_equity=Decimal("10000"),
            quote_asset="USDT",
            source_settings_version_id=settings_id,
            source_settings_hash=s_hash,
            idempotency_key=f"seed-acc1-{ws_id.hex[:6]}",
        )
        _insert(
            conn,
            "paper_account_seed",
            id=uuid4(),
            workspace_id=ws_id,
            bot_id=bot2_id,
            trading_account_id=acc2_id,
            initial_equity=Decimal("10000"),
            quote_asset="USDT",
            source_settings_version_id=settings_id,
            source_settings_hash=s_hash,
            idempotency_key=f"seed-acc2-{ws_id.hex[:6]}",
        )

        # 9. Seed Trade Plans cho Bot 1 và Bot 2 (phục vụ concurrency barrier)
        for b_idx, b_id, a_id in [(1, bot1_id, acc1_id), (2, bot2_id, acc2_id)]:
            _insert(
                conn,
                "trade_plan",
                id=uuid4(),
                workspace_id=ws_id,
                bot_id=b_id,
                account_id=a_id,
                plan_hash=f"plan-hash-{b_idx}",
                symbol="BTCUSDT",
                notional=Decimal("10000"),
                payload=Jsonb({"plan_hash": f"plan-hash-{b_idx}"}),
            )

    return {
        "workspace_id": str(ws_id),
        "acc1_id": str(acc1_id),
        "acc2_id": str(acc2_id),
        "bot1_id": str(bot1_id),
        "bot2_id": str(bot2_id),
        "profile_id": str(profile_id),
        "release_id": str(release_id),
        "feature_snapshot_id": snap_hash,
        "settings_id": str(settings_id),
        "settings_hash": s_hash,
        "membership_id": str(membership_id),
    }


def concurrency_barrier_phase(state: dict[str, str]) -> dict[str, object]:
    """Kịch bản 1: Concurrency barrier thử nghiệm Headroom Trap.

    Bot 1 và Bot 2 đồng thời xin reserve 2,000 USDT.
    - Workspace equity: 20,000 USDT.
    - Root cap: 15% (3,000 USDT).
    - Account cap: 25% (2,500 USDT).
    - Yêu cầu mỗi bot: 2,000 USDT <= 2,500 USDT (account OK).
    - Tổng gộp: 4,000 USDT > 3,000 USDT.
    Đúng 1 bot nhận APPROVED và 1 bot nhận DENY_NEW_ENTRY (WORKSPACE_CAP_EXCEEDED).
    """
    ws_id = UUID(state["workspace_id"])
    acc1_id, acc2_id = UUID(state["acc1_id"]), UUID(state["acc2_id"])
    bot1_id, bot2_id = UUID(state["bot1_id"]), UUID(state["bot2_id"])

    barrier = threading.Barrier(2)
    results: list[dict[str, object]] = []
    lock = threading.Lock()

    def run_bot(
        bot_idx: int, bot_id: UUID, acc_id: UUID, other_bot_id: UUID, other_acc_id: UUID
    ) -> None:
        async def _async_runner() -> dict[str, object]:
            engine = create_async_engine(_get_async_url(), pool_pre_ping=True)
            async with AsyncSession(engine) as session:
                # Đặt context session cho RLS
                await session.execute(
                    text(
                        "SELECT set_config('app.workspace_id', :ws, false), "
                        "set_config('app.bot_id', :bot, false)"
                    ),
                    {"ws": str(ws_id), "bot": str(bot_id)},
                )

                # Dựng scope cho other account bằng coordinator.build_account_scope
                # (tránh cross-bot DB query bị RLS chặn)
                coordinator = WorkspaceRiskCoordinator(session)
                other_scope = coordinator.build_account_scope(
                    workspace_id=ws_id,
                    bot_id=other_bot_id,
                    account_id=other_acc_id,
                    account_cap=Decimal("0.25"),
                    snapshot=_create_snapshot(
                        ws_id, other_bot_id, other_acc_id, Decimal("10000"), NOW
                    ),
                    conversion=_create_conversion(ws_id, NOW),
                )

                coordinator = WorkspaceRiskCoordinator(session)
                req = ReservationRequest(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    idempotency_key=f"e2e-barrier-bot-{bot_idx}",
                    requested_risk_usdt=Decimal("2000"),
                    as_of=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                    symbol="BTCUSDT",
                    plan_hash=f"plan-hash-{bot_idx}",
                )

                # Đồng bộ barrier thật sự trước khi gọi reserve
                barrier.wait(timeout=30)

                res = await coordinator.reserve(
                    req,
                    account_max_open_risk_fraction=Decimal("0.25"),
                    other_account_scopes=[other_scope],
                )
                await session.commit()
                return {
                    "bot_idx": bot_idx,
                    "approved": res.approved,
                    "reason_code": res.reason_code.value,
                    "detail_vi": res.detail_vi,
                }

        out = asyncio.run(_async_runner())
        with lock:
            results.append(out)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run_bot, 1, bot1_id, acc1_id, bot2_id, acc2_id)
        f2 = pool.submit(run_bot, 2, bot2_id, acc2_id, bot1_id, acc1_id)
        f1.result()
        f2.result()

    # Query DB xác nhận sum(reserved_risk) của status='RESERVED' qua get_workspace_risk_aggregate
    with _get_db_connection() as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws_id),))
        row = conn.execute(
            "SELECT total_pending_risk_usdt FROM get_workspace_risk_aggregate(%s, %s)",
            (ws_id, NOW),
        ).fetchone()
        reserved_sum = row[0]

    return {
        "results": results,
        "db_reserved_sum": str(reserved_sum),
    }


def admin_tighten_phase(state: dict[str, str]) -> dict[str, str]:
    """Admin publish siết Root cap xuống 5% và seed vị thế mở cho Bot 1 (1,200 USDT)."""
    ws_id = UUID(state["workspace_id"])
    acc1_id = UUID(state["acc1_id"])
    bot1_id = UUID(state["bot1_id"])
    profile_id = UUID(state["profile_id"])

    trade_id = uuid4()
    plan_id = uuid4()
    decision_id = uuid4()
    reservation_id = uuid4()
    intent_id = uuid4()

    with _get_db_connection() as conn:
        # Giải phóng các reservation RESERVED cũ từ phase 1 để tránh cộng dồn
        conn.execute(
            "UPDATE risk_reservation SET status = 'RELEASED', "
            "resolution_reason = 'EXPIRED_BEFORE_SUBMIT' "
            "WHERE workspace_id = %s AND status = 'RESERVED'",
            (ws_id,),
        )

        plan_hash = "a" * 64
        decision_key = "d" * 64
        release_id = UUID(state["release_id"])
        snap_id = state["feature_snapshot_id"]
        cand_id = "0" * 64
        rel_hash = "a" * 64

        # 1. Tạo vị thế mở (Trade OPEN) với stop risk 1,200 USDT cho Bot 1
        _insert(
            conn,
            "trade_plan",
            id=plan_id,
            workspace_id=ws_id,
            bot_id=bot1_id,
            account_id=acc1_id,
            plan_hash=plan_hash,
            symbol="BTCUSDT",
            notional=Decimal("10000"),
            payload=Jsonb({"symbol": "BTCUSDT"}),
        )
        _insert(
            conn,
            "trade_plan",
            id=uuid4(),
            workspace_id=ws_id,
            bot_id=UUID(state["bot2_id"]),
            account_id=UUID(state["acc2_id"]),
            plan_hash="plan-tightening-2",
            symbol="BTCUSDT",
            notional=Decimal("10000"),
            payload=Jsonb({"symbol": "BTCUSDT"}),
        )
        _insert(
            conn,
            "risk_decision",
            id=decision_id,
            workspace_id=ws_id,
            bot_id=bot1_id,
            account_id=acc1_id,
            decision_key=decision_key,
            plan_hash=plan_hash,
            snapshot_id=snap_id,
            approved=True,
            reason_code="APPROVED",
            detail_vi="Duyệt lệnh ban đầu",
            payload=Jsonb({"approved": True}),
        )
        _insert(
            conn,
            "risk_reservation",
            id=reservation_id,
            workspace_id=ws_id,
            bot_id=bot1_id,
            account_id=acc1_id,
            risk_decision_id=decision_id,
            reserved_risk=Decimal("1200"),
            reserved_notional=Decimal("10000"),
            reserved_initial_margin=Decimal("1000"),
            status="TRANSFERRED",
            idempotency_key="bot1-pos-res",
            symbol="BTCUSDT",
            plan_hash=plan_hash,
            expires_at=NOW + timedelta(hours=1),
            resolution_reason="OPEN_RISK_RECORDED",
        )
        _insert(
            conn,
            "order_intent",
            id=intent_id,
            workspace_id=ws_id,
            bot_id=bot1_id,
            account_id=acc1_id,
            strategy_release_id=release_id,
            strategy_release_hash=rel_hash,
            feature_snapshot_id=snap_id,
            candidate_id=cand_id,
            risk_decision_id=decision_id,
            risk_reservation_id=reservation_id,
            settings_version_id=UUID(state["settings_id"]),
            plan_hash=plan_hash,
            symbol="BTCUSDT",
            quote_asset="USDT",
            side="LONG",
            quantity=Decimal("1"),
            filled_quantity=Decimal("1"),
            status="FILLED",
            idempotency_key="intent-1",
            created_at=NOW,
            evidence=Jsonb({}),
        )
        _insert(
            conn,
            "trade",
            id=trade_id,
            workspace_id=ws_id,
            bot_id=bot1_id,
            account_id=acc1_id,
            intent_id=intent_id,
            symbol="BTCUSDT",
            quote_asset="USDT",
            side="LONG",
            settings_version_id=UUID(state["settings_id"]),
            strategy_release_id=release_id,
            strategy_release_hash=rel_hash,
            feature_snapshot_id=snap_id,
            candidate_id=cand_id,
            risk_decision_id=decision_id,
            risk_reservation_id=reservation_id,
            gross_pnl=Decimal("0"),
            fee_total=Decimal("0"),
            funding_total=Decimal("0"),
            slippage_total=Decimal("0"),
            total_cost=Decimal("0"),
            net_pnl=Decimal("0"),
            status="OPEN",
            mode="PAPER",
            opened_at=NOW,
            lineage=Jsonb({"worst_case_loss_usdt": "1200"}),
        )

        # 2. Admin publish siết Root cap xuống 5% (0.05 * 20,000 = 1,000 USDT)
        new_settings = _make_workspace_payload(profile_id, "0.05")
        validated = WorkspaceSettings.model_validate(new_settings)
        canonical = canonical_settings(validated)
        s_hash = settings_hash(canonical)

        _insert(
            conn,
            "workspace_settings_version",
            id=uuid4(),
            workspace_id=ws_id,
            version=2,
            revision=2,
            schema_version=1,
            status="PUBLISHED",
            published_at=NOW,
            effective_at=NOW,
            settings_json=Jsonb(canonical),
            settings_hash=s_hash,
            parent_version_id=UUID(state["settings_id"]),
            created_by=UUID(state["membership_id"]),
        )

    res_state = dict(state)
    res_state["trade_id"] = str(trade_id)
    return res_state


def bot2_breach_phase(state: dict[str, str]) -> dict[str, object]:
    """Bot 2 xin reserve 1,000 USDT -> bị từ chối bởi CAPACITY_BREACH_AFTER_TIGHTENING."""
    ws_id = UUID(state["workspace_id"])
    acc1_id, acc2_id = UUID(state["acc1_id"]), UUID(state["acc2_id"])
    bot1_id, bot2_id = UUID(state["bot1_id"]), UUID(state["bot2_id"])
    trade_id = UUID(state["trade_id"])

    async def _reserve_bot2() -> dict[str, object]:
        engine = create_async_engine(_get_async_url(), pool_pre_ping=True)
        async with AsyncSession(engine) as session:
            await session.execute(
                text(
                    "SELECT set_config('app.workspace_id', :ws, false), "
                    "set_config('app.bot_id', :bot, false)"
                ),
                {"ws": str(ws_id), "bot": str(bot2_id)},
            )
            coordinator = WorkspaceRiskCoordinator(session)
            scope1 = coordinator.build_account_scope(
                workspace_id=ws_id,
                bot_id=bot1_id,
                account_id=acc1_id,
                account_cap=Decimal("0.25"),
                snapshot=_create_snapshot(ws_id, bot1_id, acc1_id, Decimal("10000"), NOW),
                conversion=_create_conversion(ws_id, NOW),
            )
            req = ReservationRequest(
                workspace_id=ws_id,
                bot_id=bot2_id,
                account_id=acc2_id,
                idempotency_key="bot2-tightening-entry",
                requested_risk_usdt=Decimal("1000"),
                as_of=NOW,
                expires_at=NOW + timedelta(minutes=10),
                symbol="BTCUSDT",
                plan_hash="plan-tightening-2",
            )
            res = await coordinator.reserve(
                req,
                account_max_open_risk_fraction=Decimal("0.25"),
                other_account_scopes=[scope1],
            )
            await session.commit()
            return {
                "approved": res.approved,
                "reason_code": res.reason_code.value,
                "detail_vi": res.detail_vi,
            }

    bot2_result = asyncio.run(_reserve_bot2())

    # Kiểm tra vị thế của Bot 1 KHÔNG bị force-close/flatten
    with _get_db_connection() as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws_id),))
        conn.execute("SELECT set_config('app.bot_id', %s, false)", (str(bot1_id),))
        bot1_trade_status = conn.execute(
            "SELECT status FROM trade WHERE id = %s", (trade_id,)
        ).fetchone()[0]

    return {
        "bot2_result": bot2_result,
        "bot1_trade_status": bot1_trade_status,
    }


def tighten_and_breach_phase(state: dict[str, str]) -> dict[str, object]:
    """Chạy liên tiếp hai phase nếu được gọi chung."""
    tightened = admin_tighten_phase(state)
    return bot2_breach_phase(tightened)


def rls_checks_phase(state: dict[str, str]) -> dict[str, object]:
    """Kịch bản 3: DB Roles & Cross-Bot RLS Isolation dưới role crypidian_trading_runtime."""
    ws_id = UUID(state["workspace_id"])
    bot1_id, bot2_id = UUID(state["bot1_id"]), UUID(state["bot2_id"])

    with _get_db_connection() as conn:
        # 1. Kiểm tra quyền thao tác trên bảng risk_reservation
        has_select = conn.execute(
            "SELECT has_table_privilege('crypidian_trading_runtime', 'risk_reservation', 'SELECT')"
        ).fetchone()[0]
        has_insert = conn.execute(
            "SELECT has_table_privilege('crypidian_trading_runtime', 'risk_reservation', 'INSERT')"
        ).fetchone()[0]
        has_update = conn.execute(
            "SELECT has_table_privilege('crypidian_trading_runtime', 'risk_reservation', 'UPDATE')"
        ).fetchone()[0]

        # 2. Xác nhận crypidian_trading_runtime KHÔNG có rolbypassrls
        bypass_rls = conn.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'crypidian_trading_runtime'"
        ).fetchone()[0]

        # 3. Đặt context cho Bot 1 và query reservation của Bot 2 -> Phải bị RLS chặn (0 rows)
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws_id),))
        conn.execute("SELECT set_config('app.bot_id', %s, false)", (str(bot1_id),))
        bot2_reservations_count = conn.execute(
            "SELECT count(*) FROM risk_reservation WHERE bot_id = %s", (bot2_id,)
        ).fetchone()[0]

        # 4. Gọi SQL function get_workspace_risk_aggregate(ws_id, as_of)
        # Hàm SECURITY DEFINER phải trả aggregate scalar mà không rò rỉ bot_id / symbol
        agg_row = conn.execute(
            "SELECT workspace_id, total_open_risk_usdt, total_pending_risk_usdt, "
            "active_bot_count, has_unknown_exposure "
            "FROM get_workspace_risk_aggregate(%s, %s)",
            (ws_id, NOW),
        ).fetchone()

        agg_result = {
            "workspace_id": str(agg_row[0]),
            "total_open_risk_usdt": str(agg_row[1]),
            "total_pending_risk_usdt": str(agg_row[2]),
            "active_bot_count": int(agg_row[3]),
            "has_unknown_exposure": bool(agg_row[4]),
        }

    return {
        "has_table_privilege": bool(has_select and has_insert and has_update),
        "rolbypassrls": bool(bypass_rls),
        "bot2_reservations_count": int(bot2_reservations_count),
        "aggregate": agg_result,
    }


def insert_custom_profile_guard_phase(state: dict[str, str]) -> dict[str, str]:
    """Chèn risk_profile_version có max_open_risk_fraction = 0.5 để kích hoạt downgrade guard."""
    ws_id = UUID(state["workspace_id"])
    custom_profile_id = uuid4()
    with _get_db_connection() as conn:
        _insert(
            conn,
            "risk_profile_version",
            id=custom_profile_id,
            workspace_id=ws_id,
            name="Custom Cap Profile For Guard",
            version=99,
            max_risk_fraction=Decimal("0.02"),
            max_open_risk_fraction=Decimal("0.5"),
            settings=Jsonb({"hard_caps": {}}),
            is_active=False,
        )
    return {"custom_profile_id": str(custom_profile_id)}


def cleanup_custom_profile_guard_phase(state: dict[str, str]) -> dict[str, bool]:
    """Xóa profile thử nghiệm và reset max_open_risk_fraction về 1.0 để downgrade thành công."""
    profile_id = UUID(state["custom_profile_id"])
    with _get_db_connection() as conn:
        conn.execute("DELETE FROM risk_profile_version WHERE id = %s", (profile_id,))
        conn.execute("UPDATE risk_profile_version SET max_open_risk_fraction = 1.0")
    return {"cleaned": True}


def main() -> None:
    phase = os.environ.get("MB04_PHASE", "")
    state_str = os.environ.get("MB04_STATE", "{}")
    state = json.loads(state_str) if state_str else {}

    if phase == "seed":
        res = seed_phase()
    elif phase == "concurrency_barrier":
        res = concurrency_barrier_phase(state)
    elif phase == "admin_tighten":
        res = admin_tighten_phase(state)
    elif phase == "bot2_breach":
        res = bot2_breach_phase(state)
    elif phase == "tighten_and_breach":
        res = tighten_and_breach_phase(state)
    elif phase == "rls_checks":
        res = rls_checks_phase(state)
    elif phase == "insert_custom_profile_guard":
        res = insert_custom_profile_guard_phase(state)
    elif phase == "cleanup_custom_profile_guard":
        res = cleanup_custom_profile_guard_phase(state)
    else:
        raise ValueError(f"Unknown phase: {phase}")

    print(json.dumps(res))


if __name__ == "__main__":
    main()
