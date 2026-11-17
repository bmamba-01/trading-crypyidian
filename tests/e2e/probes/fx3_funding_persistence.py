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


# --- FX-3 lát 3b: bundle replay schema 2 mang funding đã chốt ---------------------------------
BUNDLE_START = FUNDING_TIME - timedelta(hours=4)
BUNDLE_EVALUATION = FUNDING_TIME - timedelta(hours=2)
BUNDLE_END = FUNDING_TIME + timedelta(hours=4)


def _bundle_inputs() -> tuple[Any, ...]:
    """Input replay tổng hợp (1h) xác định, dựng giống nhau ở mọi pha."""
    import hashlib

    from trading_bot.application.backtest_dataset_manifest import build_backtest_dataset_manifest
    from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
    from trading_bot.application.binance_rest_backfill import (
        BackfillRightsEvidence,
        BackfillRightsPurpose,
        RestBackfillRequest,
    )
    from trading_bot.domain.market.models import OHLCVBar

    step = timedelta(hours=1)
    request = RestBackfillRequest(
        symbol="BTCUSDT",
        interval="1h",
        start=BUNDLE_START,
        end=BUNDLE_END,
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
    )
    bars = tuple(
        OHLCVBar(
            symbol="BTCUSDT",
            timeframe="1h",
            open_time=BUNDLE_START + step * index,
            close_time=BUNDLE_START + step * (index + 1) - timedelta(milliseconds=1),
            event_at=BUNDLE_START + step * (index + 1) - timedelta(milliseconds=1),
            received_at=BUNDLE_END,
            available_at=BUNDLE_END,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1"),
            is_closed=True,
            source="BINANCE_USDM_REST_BACKFILL",
            source_cursor=f"fx3:{index}",
            checksum=hashlib.sha256(f"fx3:{index}".encode()).hexdigest(),
        )
        for index in range(8)
    )
    rights = BackfillRightsEvidence(
        workspace_id=WORKSPACE,
        provider="BINANCE_USDM_REST",
        purpose=BackfillRightsPurpose.BACKTEST_RESEARCH,
        terms_url="https://example.invalid/fx3-terms",
        policy_fingerprint=hashlib.sha256(b"fx3-bundle-policy").hexdigest(),
        verified_at=BUNDLE_START - timedelta(days=1),
        checked_at=BUNDLE_END,
        valid_until=BUNDLE_END + timedelta(days=1),
    )
    manifest = build_backtest_dataset_manifest(
        request,
        evaluation_start=BUNDLE_EVALUATION,
        warmup_bars=2,
        bars=bars,
        rights_evidence=rights,
        prepared_at=BUNDLE_END,
    )
    return (BacktestDatasetReplayInput(manifest, bars, rights),)


async def _bind(session: Any) -> None:
    await session.execute(
        text("SELECT pg_catalog.set_config('app.workspace_id', :w, true)"),
        {"w": str(WORKSPACE)},
    )


async def bundle_store() -> dict[str, Any]:
    """admin-web (role ghi bundle) dựng bundle có funding đọc từ DB rồi lưu schema 2."""
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.funding_settlements import (
        FundingSettlementRepository,
    )
    from trading_bot.application.backtest_dataset_funding import load_funding_series
    from trading_bot.application.backtest_dataset_replay_bundle import (
        build_backtest_dataset_replay_bundle,
    )

    engine, sessions = _sessions()
    try:
        async with sessions() as session, session.begin():
            await _bind(session)
            funding = await load_funding_series(
                FundingSettlementRepository(session),
                workspace_id=WORKSPACE,
                symbol="BTCUSDT",
                window_start=BUNDLE_EVALUATION,
                window_end=BUNDLE_END,
                as_of=BUNDLE_END,
            )
            before = await FundingSettlementRepository(session).settled_in_window(
                workspace_id=WORKSPACE,
                symbol="BTCUSDT",
                window_start=BUNDLE_EVALUATION,
                window_end=BUNDLE_END,
                as_of=FUNDING_TIME + timedelta(minutes=1),
            )
            bundle = build_backtest_dataset_replay_bundle(_bundle_inputs(), funding=funding)
            repository = BacktestDatasetReplayBundleRepository(session)
            first = await repository.append(workspace_id=WORKSPACE, bundle=bundle)
            again = await repository.append(workspace_id=WORKSPACE, bundle=bundle)
            schema = await session.scalar(
                text("SELECT schema_version FROM backtest_dataset_replay_bundle")
            )
            role = await session.scalar(text("SELECT current_user"))
    finally:
        await engine.dispose()
    STATE["bundle_hash"] = bundle.dataset_hash
    return {
        "role": role,
        "settlements": [
            [str(item.funding_rate), str(item.mark_price)] for item in funding.settlements
        ],
        "before_available": len(before),
        "stored": [first.is_duplicate, again.is_duplicate],
        "schema_version": schema,
        "bundle_hash": bundle.dataset_hash,
    }


async def bundle_load() -> dict[str, Any]:
    """backtest-worker nạp lại bundle schema 2: đọc lại funding từ DB và kiểm hash."""
    from trading_bot.adapters.database.repositories.backtest_dataset_replay_bundles import (
        BacktestDatasetReplayBundleRepository,
    )
    from trading_bot.adapters.database.repositories.funding_settlements import (
        FundingSettlementRepository,
    )
    from trading_bot.application.backtest_dataset_replay_bundle_loader import (
        BacktestDatasetReplayBundleLoader,
    )

    inputs = {item.manifest.dataset_hash: item for item in _bundle_inputs()}

    class _Children:
        """Manifest con đã được E2E WP-07 kiểm riêng; ở đây chỉ kiểm phần funding."""

        async def load_for_replay(self, *, workspace_id: UUID, dataset_hash: str, now: Any) -> Any:
            del workspace_id, now
            return inputs[dataset_hash]

    engine, sessions = _sessions()
    try:
        async with sessions() as session, session.begin():
            await _bind(session)
            loader = BacktestDatasetReplayBundleLoader(
                repository=BacktestDatasetReplayBundleRepository(session),
                replay_loader=_Children(),  # type: ignore[arg-type]
                funding_source=FundingSettlementRepository(session),
            )
            bundle = await loader.load_for_replay(
                workspace_id=WORKSPACE, bundle_hash=STATE["bundle_hash"], now=BUNDLE_END
            )
            role = await session.scalar(text("SELECT current_user"))
    finally:
        await engine.dispose()
    assert bundle.funding is not None
    return {
        "role": role,
        "hash_matches": bundle.dataset_hash == STATE["bundle_hash"],
        "funding_event_ids": [item.event_id for item in bundle.funding.settlements],
    }


if PHASE == "seed":
    output = seed()
elif PHASE == "write":
    output = asyncio.run(write())
elif PHASE == "read":
    output = asyncio.run(read())
elif PHASE == "bundle_store":
    output = asyncio.run(bundle_store())
elif PHASE == "bundle_load":
    output = asyncio.run(bundle_load())
else:
    raise AssertionError(PHASE)
print(json.dumps({**output, "_state": STATE}, default=str))
