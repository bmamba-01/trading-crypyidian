"""Probe FX-3 lát 1: ghi/đọc funding đã chốt kèm mark price bằng role thật (CRY-198).

Pha ``seed`` chạy bằng migrator (admin) tạo workspace + instrument; pha ``write`` chạy trong
container ``ingestion-worker`` (role ghi duy nhất của dữ liệu thị trường); pha ``read`` chạy trong
``trading-runtime`` (chỉ đọc). Không gọi mạng.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy import text

STATE = json.loads(os.environ["FX3_STATE"])
PHASE = os.environ["FX3_PHASE"]
WORKSPACE = UUID(STATE["workspace_id"])
OTHER_WORKSPACE = UUID(STATE["other_workspace_id"])
INSTRUMENT = UUID(STATE["instrument_id"])
FUNDING_TIME = datetime(2026, 11, 16, 8, tzinfo=UTC)


def _password() -> str:
    return Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()


def _sessions() -> tuple[Any, Any]:
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
    return engine, create_session_factory(engine)


def seed() -> dict[str, Any]:
    with psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=_password(),
        autocommit=True,
    ) as connection:
        for workspace, slug in ((WORKSPACE, "fx3-a"), (OTHER_WORKSPACE, "fx3-b")):
            connection.execute(
                "INSERT INTO workspace (id,name,slug,mode,status) "
                "VALUES (%s,'FX-3 funding E2E',%s,'PAPER','ACTIVE')",
                (workspace, f"{slug}-{workspace.hex[:8]}"),
            )
        connection.execute(
            "INSERT INTO market_instrument (id,workspace_id,source,idempotency_key,symbol,"
            "base_asset,quote_asset,tick_size,quantity_step,min_notional,status,revision,checksum,"
            "supersedes_id,event_at,received_at,available_at) VALUES (%s,%s,'BINANCE_USDM',"
            "'fx3-instrument','BTCUSDT','BTC','USDT',0.1,0.001,5,'TRADING',1,%s,NULL,%s,%s,%s)",
            (INSTRUMENT, WORKSPACE, "a" * 64, *([FUNDING_TIME - timedelta(days=1)] * 3)),
        )
    return {"seeded": True}


def _observation(mark: str) -> Any:
    from trading_bot.ports.market_data import FundingRateObservation

    received = FUNDING_TIME + timedelta(minutes=2)
    return FundingRateObservation(
        symbol="BTCUSDT",
        funding_time=FUNDING_TIME,
        funding_rate=Decimal("0.0001"),
        mark_price=Decimal(mark),
        rate_type="Regular",
        received_at=received,
        available_at=received,
    )


async def write() -> dict[str, Any]:
    from trading_bot.adapters.database.repositories.market_data import (
        MarketDataRepository,
        MarketRepositoryConflict,
    )
    from trading_bot.application.funding_rate_collector import funding_record_version

    engine, sessions = _sessions()

    async def append(mark: str) -> str:
        observation = _observation(mark)
        version, mark_price = funding_record_version(WORKSPACE, observation)
        async with sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :w, true)"),
                {"w": str(WORKSPACE)},
            )
            result = await MarketDataRepository(session).append_funding_rate(
                workspace_id=WORKSPACE,
                instrument_id=INSTRUMENT,
                version=version,
                funding_time=observation.funding_time,
                funding_rate=observation.funding_rate,
                mark_price=mark_price,
                rate_type=observation.rate_type,
            )
            return str(result.disposition.value)

    try:
        first = await append("90500.12")
        again = await append("90500.12")
        try:
            await append("90600")
            changed = "accepted"
        except MarketRepositoryConflict:
            changed = "conflict"
        async with sessions() as session:
            role = await session.scalar(text("SELECT current_user"))
    finally:
        await engine.dispose()
    return {"role": role, "first": first, "again": again, "changed_mark": changed}


async def read() -> dict[str, Any]:
    engine, sessions = _sessions()
    rows: dict[str, Any] = {}
    try:
        for name, workspace in (("own", WORKSPACE), ("other", OTHER_WORKSPACE)):
            async with sessions() as session, session.begin():
                await session.execute(
                    text("SELECT pg_catalog.set_config('app.workspace_id', :w, true)"),
                    {"w": str(workspace)},
                )
                rows[name] = (
                    await session.execute(
                        text(
                            "SELECT funding_rate::text, mark_price::text, rate_type "
                            "FROM market_funding ORDER BY funding_time"
                        )
                    )
                ).all()
        async with sessions() as session:
            role = await session.scalar(text("SELECT current_user"))
    finally:
        await engine.dispose()
    return {
        "role": role,
        "own": [list(row) for row in rows["own"]],
        "other_workspace_rows": len(rows["other"]),
    }


if PHASE == "seed":
    output = seed()
elif PHASE == "write":
    output = asyncio.run(write())
elif PHASE == "read":
    output = asyncio.run(read())
else:
    raise AssertionError(PHASE)
print(json.dumps(output, default=str))
