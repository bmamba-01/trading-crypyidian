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
# Chính sách mark của workspace fixture: venue root và timeframe root (fix round 2, I2).
MARK_SOURCE = "BINANCE_USDM"
MARK_TIMEFRAMES = ["1h", "4h"]
MARK_AGE_SECONDS = 3600
_AGGREGATE_V2 = "public.get_workspace_risk_equity_aggregate(uuid,timestamptz,integer,text,text[])"
_STATE_V2 = "public.workspace_account_risk_state(uuid,timestamptz,integer,text,text[])"


def _has_function(conn: psycopg.Connection, signature: str) -> bool:
    return bool(conn.execute("SELECT to_regprocedure(%s) IS NOT NULL", (signature,)).fetchone()[0])


def _aggregate_signature(conn: psycopg.Connection) -> str:
    if _has_function(conn, _AGGREGATE_V2):
        return _AGGREGATE_V2
    return "public.get_workspace_risk_equity_aggregate(uuid,timestamptz,integer)"


def _aggregate(
    conn: psycopg.Connection, columns: str, workspace_id: UUID, as_of: datetime
) -> tuple[Any, ...]:
    """Gọi aggregate theo chữ ký hiện có (v2 nhận nguồn/timeframe mark; v1 cho bằng chứng RED)."""
    if _has_function(conn, _AGGREGATE_V2):
        row = conn.execute(
            f"SELECT {columns} FROM get_workspace_risk_equity_aggregate(%s, %s, %s, %s, %s)",
            (workspace_id, as_of, MARK_AGE_SECONDS, MARK_SOURCE, MARK_TIMEFRAMES),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {columns} FROM get_workspace_risk_equity_aggregate(%s, %s, %s)",
            (workspace_id, as_of, MARK_AGE_SECONDS),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _account_states(
    conn: psycopg.Connection, columns: str, workspace_id: UUID, as_of: datetime
) -> list[tuple[Any, ...]] | None:
    if _has_function(conn, _STATE_V2):
        rows = conn.execute(
            f"SELECT {columns} FROM public.workspace_account_risk_state(%s, %s, %s, %s, %s)",
            (workspace_id, as_of, MARK_AGE_SECONDS, MARK_SOURCE, MARK_TIMEFRAMES),
        ).fetchall()
    elif _has_function(conn, "public.workspace_account_risk_state(uuid,timestamptz,integer)"):
        rows = conn.execute(
            f"SELECT {columns} FROM public.workspace_account_risk_state(%s, %s, %s)",
            (workspace_id, as_of, MARK_AGE_SECONDS),
        ).fetchall()
    else:
        return None
    return [tuple(row) for row in rows]


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
            # Fixture dùng nến 1h; mark tươi tối đa 1 giờ theo close_time (fix round 2, I2).
            "max_mark_age_seconds": MARK_AGE_SECONDS,
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
            settings=Jsonb(
                {
                    "schema_version": 1,
                    "hard_caps": {
                        "max_aggregate_open_risk": "2500",
                        "max_notional": "100000",
                        "max_leverage": "10",
                        "max_positions": 10,
                        "max_symbol_notional": "50000",
                        "max_concentration_fraction": "1",
                        "max_daily_loss": "5000",
                        "max_rolling_loss": "10000",
                        "max_account_age_seconds": 86400,
                        "max_data_age_seconds": 3600,
                    },
                }
            ),
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
            created_at=NOW - timedelta(seconds=1),
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
            created_at=NOW - timedelta(seconds=1),
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

                coordinator = WorkspaceRiskCoordinator(session)
                req = ReservationRequest(
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=acc_id,
                    idempotency_key=f"e2e-barrier-bot-{bot_idx}",
                    requested_risk_usdt=Decimal("2000"),
                    requested_notional=Decimal("5000"),
                    requested_initial_margin=Decimal("1000"),
                    as_of=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                    symbol="BTCUSDT",
                    plan_hash=f"plan-hash-{bot_idx}",
                )

                # Đồng bộ barrier thật sự trước khi gọi reserve
                barrier.wait(timeout=30)

                res = await coordinator.reserve(req)
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

    # Tổng pending qua aggregate SQL (role runtime); tổng đọc thẳng bảng chạy ở pha
    # `direct_reserved_sum` bằng superuser (RLS runtime chỉ thấy bot của mình).
    with _get_db_connection() as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(ws_id),))
        row = _aggregate(conn, "total_pending_risk_usdt", ws_id, NOW)
        reserved_sum = row[0]

    return {
        "results": results,
        "db_reserved_sum": str(reserved_sum),
    }


def direct_reserved_sum_phase(state: dict[str, str]) -> dict[str, str]:
    """Tổng reservation đang giữ đọc thẳng `risk_reservation` (không qua hàm đang được kiểm)."""
    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT coalesce(sum(reserved_risk), 0), count(*) FROM risk_reservation "
            "WHERE workspace_id = %s AND status IN ('RESERVED', 'UNKNOWN')",
            (UUID(state["workspace_id"]),),
        ).fetchone()
    return {"reserved_sum": str(row[0]), "reserved_count": str(row[1])}


def replay_dead_reservation_phase(state: dict[str, str]) -> dict[str, object]:
    """I6: replay idempotent không được trả approved cho reservation đã RELEASED/hết hạn."""
    ws_id = UUID(state["workspace_id"])
    acc2_id, bot2_id = UUID(state["acc2_id"]), UUID(state["bot2_id"])

    async def _run() -> dict[str, object]:
        engine = create_async_engine(_get_async_url(), pool_pre_ping=True)
        out: dict[str, object] = {}
        try:
            cases = (("released", timedelta(minutes=10)), ("expired", timedelta(seconds=2)))
            for case, ttl in cases:
                now = datetime.now(UTC)
                request = ReservationRequest(
                    workspace_id=ws_id,
                    bot_id=bot2_id,
                    account_id=acc2_id,
                    idempotency_key=f"i6-{case}",
                    requested_risk_usdt=Decimal("100"),
                    requested_notional=Decimal("1000"),
                    requested_initial_margin=Decimal("100"),
                    as_of=now,
                    expires_at=now + ttl,
                    symbol="BTCUSDT",
                    plan_hash="plan-hash-2",
                )
                async with AsyncSession(engine) as session:
                    await _bind(session, ws_id, bot2_id)
                    first = await WorkspaceRiskCoordinator(session).reserve(request)
                    await session.commit()
                out[f"{case}_first"] = [first.approved, first.reason_code.value]
                if case == "released":
                    async with AsyncSession(engine) as session:
                        await _bind(session, ws_id, bot2_id)
                        assert first.reservation_id is not None
                        await WorkspaceRiskCoordinator(session).release_reservation(
                            workspace_id=ws_id,
                            account_id=acc2_id,
                            reservation_id=first.reservation_id,
                        )
                        await session.commit()
                else:
                    await asyncio.sleep(3)
                async with AsyncSession(engine) as session:
                    await _bind(session, ws_id, bot2_id)
                    try:
                        replay = await WorkspaceRiskCoordinator(session).reserve(request)
                        out[f"{case}_replay"] = [replay.approved, replay.is_replay]
                    except Exception as error:
                        out[f"{case}_replay"] = type(error).__name__
                    await session.rollback()
        finally:
            await engine.dispose()
        return out

    return asyncio.run(_run())


async def _bind(session: AsyncSession, workspace_id: UUID, bot_id: UUID) -> None:
    await session.execute(
        text(
            "SELECT set_config('app.workspace_id', :ws, false), "
            "set_config('app.bot_id', :bot, false)"
        ),
        {"ws": str(workspace_id), "bot": str(bot_id)},
    )


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
            # Không ghi rủi ro vào lineage (ledger thật cũng không): coordinator phải lấy 1.200 từ
            # reservation liên kết (review C2).
            lineage=Jsonb({}),
        )
        _insert(
            conn,
            "market_instrument",
            id=uuid4(),
            workspace_id=ws_id,
            source="e2e-fixture",
            idempotency_key=f"instrument-{ws_id.hex[:8]}",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_notional=Decimal("5"),
            status="ACTIVE",
            revision=1,
            checksum="e" * 64,
            supersedes_id=None,
            event_at=NOW,
            received_at=NOW,
            available_at=NOW,
        )
        instrument_id = conn.execute(
            "SELECT id FROM market_instrument WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
            (ws_id,),
        ).fetchone()[0]
        _insert(
            conn,
            "market_bar",
            id=uuid4(),
            workspace_id=ws_id,
            instrument_id=instrument_id,
            symbol="BTCUSDT",
            timeframe="1h",
            source=MARK_SOURCE,
            logical_key=f"bar-{ws_id.hex[:8]}",
            idempotency_key=f"bar-{ws_id.hex[:8]}",
            revision=1,
            checksum="f" * 64,
            supersedes_id=None,
            raw_reference_id=None,
            source_cursor=None,
            open_time=NOW - timedelta(hours=1),
            close_time=NOW,
            event_at=NOW,
            received_at=NOW,
            available_at=NOW,
            open=Decimal("10000"),
            high=Decimal("10100"),
            low=Decimal("9900"),
            close=Decimal("10000"),
            volume=Decimal("1"),
            is_closed=True,
        )
        _insert(
            conn,
            "fill",
            id=uuid4(),
            workspace_id=ws_id,
            bot_id=bot1_id,
            account_id=acc1_id,
            mode="PAPER",
            intent_id=intent_id,
            fill_role="ENTRY",
            fill_key=f"fill-{ws_id.hex[:8]}",
            bar_key=f"bar-{ws_id.hex[:8]}",
            fill_policy_version="e2e-v1",
            quantity=Decimal("1"),
            price=Decimal("10000"),
            fee_amount=Decimal("0"),
            slippage_amount=Decimal("0"),
            occurred_at=NOW,
            lineage=Jsonb({}),
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
    acc2_id = UUID(state["acc2_id"])
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
            req = ReservationRequest(
                workspace_id=ws_id,
                bot_id=bot2_id,
                account_id=acc2_id,
                idempotency_key="bot2-tightening-entry",
                requested_risk_usdt=Decimal("1000"),
                requested_notional=Decimal("5000"),
                requested_initial_margin=Decimal("1000"),
                as_of=NOW,
                expires_at=NOW + timedelta(minutes=10),
                symbol="BTCUSDT",
                plan_hash="plan-tightening-2",
            )
            res = await coordinator.reserve(req)
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
        agg_row = _aggregate(
            conn,
            "workspace_id, total_equity_usdt, total_open_risk_usdt, total_pending_risk_usdt, "
            "active_bot_count, has_unknown_exposure, has_unknown_equity",
            ws_id,
            NOW,
        )
        unknown_equity = _aggregate(conn, "has_unknown_equity", ws_id, NOW + timedelta(hours=3))[0]
        signature = _aggregate_signature(conn)
        function_execute = conn.execute(
            "SELECT has_function_privilege('crypidian_trading_runtime', %s, 'EXECUTE')",
            (signature,),
        ).fetchone()[0]
        public_execute = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, "
            "acldefault('f', p.proowner))) acl WHERE p.oid = %s::regprocedure "
            "AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
            "FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (signature, signature),
        ).fetchone()[0]

        agg_result = {
            "workspace_id": str(agg_row[0]),
            "total_equity_usdt": str(agg_row[1]),
            "total_open_risk_usdt": str(agg_row[2]),
            "total_pending_risk_usdt": str(agg_row[3]),
            "active_bot_count": int(agg_row[4]),
            "has_unknown_exposure": bool(agg_row[5]),
            "has_unknown_equity": bool(agg_row[6]),
            "unknown_after_mark_removal": bool(unknown_equity),
            "function_execute": bool(function_execute),
            "public_execute": bool(public_execute),
        }

    return {
        "has_table_privilege": bool(has_select and has_insert and has_update),
        "rolbypassrls": bool(bypass_rls),
        "bot2_reservations_count": int(bot2_reservations_count),
        "aggregate": agg_result,
    }


def equity_edge_cases_phase(state: dict[str, str]) -> dict[str, bool]:
    """Kiểm tra realized PnL, short UPNL, seed/fill thiếu và quote không hỗ trợ."""
    ws_id = UUID(state["workspace_id"])
    with _get_db_connection() as conn:
        conn.execute("BEGIN")
        try:
            # Tạm tắt trigger append-only trong transaction kiểm thử; ROLLBACK khôi phục cả DDL.
            conn.execute("ALTER TABLE trade DISABLE TRIGGER trade_close_guard")
            conn.execute("ALTER TABLE market_bar DISABLE TRIGGER market_bar_immutable")
            conn.execute("ALTER TABLE fill DISABLE TRIGGER fill_append_only")
            conn.execute(
                "ALTER TABLE paper_account_seed DISABLE TRIGGER paper_account_seed_immutable"
            )
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws_id),))
            trade = conn.execute(
                "SELECT id, bot_id, account_id, intent_id FROM trade "
                "WHERE workspace_id = %s AND mode = 'PAPER' AND status = 'OPEN' "
                "ORDER BY opened_at LIMIT 1",
                (ws_id,),
            ).fetchone()
            assert trade is not None, "Fixture phải có một vị thế PAPER đang mở"
            trade_id, bot_id, account_id, intent_id = trade

            def aggregate_equity(as_of: datetime = NOW) -> tuple[Decimal, bool]:
                row = _aggregate(conn, "total_equity_usdt, has_unknown_equity", ws_id, as_of)
                return Decimal(row[0]), bool(row[1])

            def aggregate_row(as_of: datetime) -> tuple[Decimal, Decimal, Decimal, bool]:
                row = _aggregate(
                    conn,
                    "total_equity_usdt, total_open_risk_usdt, total_pending_risk_usdt, "
                    "has_unknown_equity",
                    ws_id,
                    as_of,
                )
                return Decimal(row[0]), Decimal(row[1]), Decimal(row[2]), bool(row[3])

            def set_bar(open_time: datetime, close_time: datetime, close: str) -> None:
                conn.execute(
                    "UPDATE market_bar SET open_time = %s, close_time = %s, close = %s, "
                    "low = least(low, %s) WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
                    (open_time, close_time, Decimal(close), Decimal(close), ws_id),
                )

            # MB-04 fix C3b: trade OPEN mở sau as_of (opened_at = NOW) vẫn là exposure hiện hành.
            _, open_risk_before_open, _, _ = aggregate_row(NOW - timedelta(minutes=1))
            # MB-04 fix I2: nến cũ backfill muộn (available_at mới, close_time cũ 3 giờ) không
            # phải mark tươi: độ tươi đo theo thời điểm quan sát close_time.
            set_bar(NOW - timedelta(hours=4), NOW - timedelta(hours=3), "10000")
            _, backfilled_stale_mark_unknown = aggregate_equity()
            set_bar(NOW - timedelta(hours=1), NOW, "10000")

            # Fix round 2 (I2/R3): mark chỉ lấy từ timeframe/source đã cấu hình; độ dài nến
            # không được nới cửa sổ tươi (nến 1d/1w hoặc open_time sai không qua được).
            def mark_case(
                timeframe: str, source: str, open_time: datetime, close: datetime
            ) -> bool:
                conn.execute(
                    "UPDATE market_bar SET timeframe = %s, source = %s, open_time = %s, "
                    "close_time = %s WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
                    (timeframe, source, open_time, close, ws_id),
                )
                return aggregate_equity()[1]

            day_bar_mark_unknown = mark_case(
                "1d", MARK_SOURCE, NOW - timedelta(hours=26), NOW - timedelta(hours=2)
            )
            week_bar_mark_unknown = mark_case(
                "1w", MARK_SOURCE, NOW - timedelta(days=9), NOW - timedelta(days=2)
            )
            malformed_open_time_unknown = mark_case(
                "1h", MARK_SOURCE, NOW - timedelta(hours=5), NOW - timedelta(minutes=10)
            )
            foreign_source_mark_unknown = mark_case(
                "1h", "UNTRUSTED_FEED", NOW - timedelta(hours=1), NOW
            )
            configured_mark_known = not mark_case("1h", MARK_SOURCE, NOW - timedelta(hours=1), NOW)
            # Minor: vốn âm của một account là unknown (khớp domain), không phải một số âm hợp lệ.
            conn.execute(
                "UPDATE paper_account_seed SET initial_equity = 1 WHERE workspace_id = %s "
                "AND bot_id = %s",
                (ws_id, bot_id),
            )
            set_bar(NOW - timedelta(hours=1), NOW, "5000")
            _, negative_account_equity_unknown = aggregate_equity()
            conn.execute(
                "UPDATE paper_account_seed SET initial_equity = 10000 WHERE workspace_id = %s "
                "AND bot_id = %s",
                (ws_id, bot_id),
            )
            set_bar(NOW - timedelta(hours=1), NOW, "10000")
            # C3c: RESERVED còn hạn theo đồng hồ thật vẫn giữ budget khi as_of ở tương lai.
            bot2_id, acc2_id = UUID(state["bot2_id"]), UUID(state["acc2_id"])
            c3c_decision = uuid4()
            _insert(
                conn,
                "risk_decision",
                id=c3c_decision,
                workspace_id=ws_id,
                bot_id=bot2_id,
                account_id=acc2_id,
                decision_key="c" * 64,
                plan_hash="plan-tightening-2",
                snapshot_id=state["feature_snapshot_id"],
                approved=True,
                reason_code="APPROVED",
                detail_vi="C3c fixture",
                payload=Jsonb({"approved": True}),
            )
            conn.execute(
                "INSERT INTO risk_reservation (id, workspace_id, bot_id, account_id, "
                "risk_decision_id, reserved_risk, reserved_notional, reserved_initial_margin, "
                "status, idempotency_key, symbol, plan_hash, expires_at) VALUES "
                "(%s, %s, %s, %s, %s, 100, 1000, 100, 'RESERVED', 'c3c-live', 'BTCUSDT', "
                "'plan-tightening-2', now() + interval '5 minutes')",
                (uuid4(), ws_id, bot2_id, acc2_id, c3c_decision),
            )
            _, _, pending_at_future_as_of, _ = aggregate_row(NOW + timedelta(minutes=30))
            # I3: vốn từng account (hàm invoker dùng chung) cộng lại đúng bằng aggregate.
            account_state_matches_aggregate: object = None
            rows = _account_states(conn, "is_enabled, equity_usdt, has_unknown_equity", ws_id, NOW)
            if rows is not None:
                total, _, _, unknown_total = aggregate_row(NOW)
                account_state_matches_aggregate = (
                    sum((Decimal(row[1]) for row in rows if row[0]), Decimal("0")) == total
                    and not unknown_total
                    and not any(row[2] for row in rows if row[0])
                )

            # Giá tăng 1.000 USDT tạo UPNL dương cho long 1 BTC.
            conn.execute(
                "UPDATE market_bar SET close = 11000, high = 11100 "
                "WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
                (ws_id,),
            )
            long_equity, long_unknown = aggregate_equity()

            # Giá giảm 1.000 USDT tạo UPNL dương cho short 1 BTC.
            conn.execute("UPDATE trade SET side = 'SHORT' WHERE id = %s", (trade_id,))
            conn.execute(
                "UPDATE market_bar SET close = 9000, low = 8900 "
                "WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
                (ws_id,),
            )
            short_equity, short_unknown = aggregate_equity()

            # Mark đến sau as_of không được dùng; thiếu mark khả dụng phải fail-closed.
            conn.execute(
                "UPDATE market_bar SET received_at = %s, available_at = %s "
                "WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
                (NOW + timedelta(seconds=1), NOW + timedelta(seconds=1), ws_id),
            )
            _, future_mark_unknown = aggregate_equity()
            conn.execute(
                "UPDATE market_bar SET received_at = %s, available_at = %s "
                "WHERE workspace_id = %s AND symbol = 'BTCUSDT'",
                (NOW, NOW, ws_id),
            )
            conn.execute("UPDATE trade SET side = 'LONG' WHERE id = %s", (trade_id,))
            conn.execute(
                "UPDATE market_bar SET close = 11000 WHERE workspace_id = %s "
                "AND symbol = 'BTCUSDT'",
                (ws_id,),
            )

            # Mark-to-market phải cộng phần đã chốt một phần, phần còn mở và trừ cost đã ghi.
            exit_fill_key = f"partial-exit-{ws_id.hex[:8]}"
            _insert(
                conn,
                "fill",
                id=uuid4(),
                workspace_id=ws_id,
                bot_id=bot_id,
                account_id=account_id,
                mode="PAPER",
                intent_id=intent_id,
                fill_role="EXIT",
                fill_key=exit_fill_key,
                bar_key=f"bar-{ws_id.hex[:8]}",
                fill_policy_version="e2e-v1",
                quantity=Decimal("0.25"),
                price=Decimal("10500"),
                fee_amount=Decimal("1"),
                slippage_amount=Decimal("2"),
                occurred_at=NOW,
                lineage=Jsonb({}),
            )
            for cost_type, amount in (("FEE", "1"), ("SLIPPAGE", "2"), ("FUNDING", "5")):
                _insert(
                    conn,
                    "trade_cost",
                    id=uuid4(),
                    workspace_id=ws_id,
                    bot_id=bot_id,
                    account_id=account_id,
                    mode="PAPER",
                    trade_id=trade_id,
                    cost_key=f"{exit_fill_key}:{cost_type.lower()}",
                    cost_type=cost_type,
                    amount=Decimal(amount),
                    currency="USDT",
                    occurred_at=NOW,
                    source_event_id=exit_fill_key,
                    lineage=Jsonb({}),
                )
            partial_equity, partial_unknown = aggregate_equity()

            # PnL đã chốt được cộng đúng một lần vào vốn gốc.
            conn.execute(
                "UPDATE trade SET status = 'CLOSED', closed_at = %s, gross_pnl = 250, "
                "net_pnl = 250 WHERE id = %s",
                (NOW, trade_id),
            )
            realized_equity, realized_unknown = aggregate_equity()

            # Vị thế mở thiếu seed hoặc entry fill phải khiến equity thành unknown.
            conn.execute(
                "UPDATE trade SET status = 'OPEN', closed_at = NULL, gross_pnl = 0, "
                "net_pnl = 0 WHERE id = %s",
                (trade_id,),
            )
            seed = conn.execute(
                "SELECT id, initial_equity, quote_asset, source_settings_version_id, "
                "source_settings_hash, idempotency_key, created_at FROM paper_account_seed "
                "WHERE workspace_id = %s AND bot_id = %s AND trading_account_id = %s",
                (ws_id, bot_id, account_id),
            ).fetchone()
            assert seed is not None
            conn.execute("DELETE FROM paper_account_seed WHERE id = %s", (seed[0],))
            _, missing_seed_unknown = aggregate_equity()
            conn.execute(
                "INSERT INTO paper_account_seed "
                "(id, workspace_id, bot_id, trading_account_id, initial_equity, quote_asset, "
                "source_settings_version_id, source_settings_hash, idempotency_key, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (seed[0], ws_id, bot_id, account_id, *seed[1:]),
            )
            conn.execute(
                "UPDATE paper_account_seed SET created_at = %s WHERE id = %s",
                (NOW + timedelta(seconds=1), seed[0]),
            )
            _, future_seed_unknown = aggregate_equity()
            conn.execute(
                "UPDATE paper_account_seed SET created_at = %s WHERE id = %s",
                (seed[-1], seed[0]),
            )
            conn.execute(
                "DELETE FROM fill WHERE workspace_id = %s AND intent_id = %s "
                "AND fill_role = 'ENTRY'",
                (ws_id, intent_id),
            )
            _, missing_fill_unknown = aggregate_equity()

            # Không cộng trực tiếp tài sản định giá bằng đơn vị khác vào USDT.
            conn.execute("UPDATE trade SET quote_asset = 'BTC' WHERE id = %s", (trade_id,))
            _, unsupported_quote_unknown = aggregate_equity()
            conn.execute(
                "UPDATE trade SET status = 'CLOSED', closed_at = %s, gross_pnl = 250, "
                "net_pnl = 250 WHERE id = %s",
                (NOW, trade_id),
            )
            _, unsupported_realized_quote_unknown = aggregate_equity()

            return {
                "long_pnl_correct": long_equity == Decimal("21000") and not long_unknown,
                "short_pnl_correct": short_equity == Decimal("21000") and not short_unknown,
                "partial_exit_and_costs_correct": (
                    partial_equity == Decimal("20867") and not partial_unknown
                ),
                "future_mark_unknown": future_mark_unknown,
                "realized_pnl_correct": (
                    realized_equity == Decimal("20250") and not realized_unknown
                ),
                "missing_seed_unknown": missing_seed_unknown,
                "future_seed_unknown": future_seed_unknown,
                "missing_fill_unknown": missing_fill_unknown,
                "unsupported_quote_unknown": unsupported_quote_unknown,
                "unsupported_realized_quote_unknown": unsupported_realized_quote_unknown,
                "open_risk_counts_trade_opened_after_as_of": (
                    open_risk_before_open == Decimal("1200")
                ),
                "backfilled_stale_mark_unknown": backfilled_stale_mark_unknown,
                "negative_account_equity_unknown": negative_account_equity_unknown,
                "future_as_of_keeps_live_reservation": pending_at_future_as_of == Decimal("100"),
                "account_state_matches_aggregate": account_state_matches_aggregate,
                "day_bar_mark_unknown": day_bar_mark_unknown,
                "week_bar_mark_unknown": week_bar_mark_unknown,
                "malformed_open_time_unknown": malformed_open_time_unknown,
                "foreign_source_mark_unknown": foreign_source_mark_unknown,
                "configured_mark_known": configured_mark_known,
            }
        finally:
            conn.execute("ROLLBACK")


AGGREGATE_FN = _AGGREGATE_V2
STATE_FN = _STATE_V2
PROCESS_ROLES = (
    "crypidian_trading_runtime",
    "crypidian_admin_web",
    "crypidian_execution_gateway",
    "crypidian_ingestion_worker",
    "crypidian_trainer_worker",
    "crypidian_app",
)


def security_checks_phase(state: dict[str, str]) -> dict[str, object]:
    """I1: owner/role/search_path/grant của hàm SECURITY DEFINER đọc từ catalog (superuser)."""
    del state
    with _get_db_connection() as conn:

        def scalar(query: str, *params: object) -> object:
            row = conn.execute(query, params).fetchone()
            return None if row is None else row[0]

        def function_facts(signature: str) -> dict[str, object] | None:
            if scalar("SELECT to_regprocedure(%s) IS NOT NULL", signature) is not True:
                return None
            row = conn.execute(
                "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig::text[] "
                "FROM pg_proc p WHERE p.oid = %s::regprocedure",
                (signature,),
            ).fetchone()
            execute = {
                role: bool(
                    scalar("SELECT has_function_privilege(%s, %s, 'EXECUTE')", role, signature)
                )
                for role in PROCESS_ROLES
            }
            public_execute = bool(
                scalar(
                    "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode("
                    "coalesce(p.proacl, acldefault('f', p.proowner))) acl WHERE p.oid = "
                    "%s::regprocedure AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE')",
                    signature,
                )
            )
            return {
                "owner": row[0],
                "security_definer": row[1],
                "config": list(row[2] or []),
                "execute": execute,
                "public_execute": public_execute,
            }

        owner_role = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolinherit, rolcreaterole, rolcreatedb, "
            "rolreplication, EXISTS (SELECT 1 FROM pg_auth_members a WHERE a.roleid = r.oid "
            "OR a.member = r.oid) FROM pg_roles r WHERE rolname = 'crypidian_risk_aggregator'"
        ).fetchone()
        aggregator_writes = None
        if owner_role is not None:
            aggregator_writes = any(
                bool(
                    scalar(
                        "SELECT has_table_privilege('crypidian_risk_aggregator', %s, %s)",
                        table,
                        privilege,
                    )
                )
                for table in (
                    "trade",
                    "risk_reservation",
                    "fill",
                    "paper_account_seed",
                    "market_bar",
                    "bot_instance",
                    "trading_account",
                    "trade_cost",
                )
                for privilege in ("INSERT", "UPDATE", "DELETE")
            )
        return {
            "aggregate": function_facts(AGGREGATE_FN),
            "account_state": function_facts(STATE_FN),
            "legacy_aggregate_present": scalar(
                "SELECT to_regprocedure("
                "'public.get_workspace_risk_aggregate(uuid,timestamptz)') IS NOT NULL"
            ),
            "owner_role": None if owner_role is None else list(owner_role),
            "aggregator_has_writes": aggregator_writes,
        }


def precreate_unsafe_aggregator_phase(state: dict[str, str]) -> dict[str, bool]:
    """Role cấp cluster tạo sẵn với LOGIN phải làm migration từ chối (như MB-01 provisioner)."""
    del state
    with _get_db_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'crypidian_risk_aggregator'"
        ).fetchone()
        assert exists is None, "Downgrade phải xóa role aggregator"
        conn.execute("CREATE ROLE crypidian_risk_aggregator LOGIN")
    return {"precreated": True}


def harden_aggregator_phase(state: dict[str, str]) -> dict[str, bool]:
    del state
    with _get_db_connection() as conn:
        conn.execute("ALTER ROLE crypidian_risk_aggregator NOLOGIN NOINHERIT")
    return {"hardened": True}


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
    elif phase == "direct_reserved_sum":
        res = direct_reserved_sum_phase(state)
    elif phase == "replay_dead_reservation":
        res = replay_dead_reservation_phase(state)
    elif phase == "security_checks":
        res = security_checks_phase(state)
    elif phase == "precreate_unsafe_aggregator":
        res = precreate_unsafe_aggregator_phase(state)
    elif phase == "harden_aggregator":
        res = harden_aggregator_phase(state)
    elif phase == "admin_tighten":
        res = admin_tighten_phase(state)
    elif phase == "bot2_breach":
        res = bot2_breach_phase(state)
    elif phase == "tighten_and_breach":
        res = tighten_and_breach_phase(state)
    elif phase == "rls_checks":
        res = rls_checks_phase(state)
    elif phase == "equity_edge_cases":
        res = equity_edge_cases_phase(state)
    elif phase == "insert_custom_profile_guard":
        res = insert_custom_profile_guard_phase(state)
    elif phase == "cleanup_custom_profile_guard":
        res = cleanup_custom_profile_guard_phase(state)
    else:
        raise ValueError(f"Unknown phase: {phase}")

    print(json.dumps(res))


if __name__ == "__main__":
    main()
