"""Probe Compose: ingest nến live qua stream + REST giả lập vào PostgreSQL bằng role ingestion."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from httpx2.websockets import ASGIWebSocketTransport
from sqlalchemy import event, func, select

STATE = json.loads(os.environ["LIVE_INGEST_STATE"])
WORKSPACE_ID = UUID(STATE["workspace_id"])
OTHER_WORKSPACE_ID = UUID(STATE["other_workspace_id"])
EXCHANGE_INFO = STATE["exchange_info"].encode("utf-8")
REAL_NOW = datetime.fromisoformat(STATE["now"]).astimezone(UTC)
INTERVAL = timedelta(minutes=1)
# Lùi 10 phút để mọi mốc thời gian đều nằm trước đồng hồ PostgreSQL.
M = REAL_NOW.replace(second=0, microsecond=0) - timedelta(minutes=10)
WARMUP = 200
INGESTION_ROLE = "crypidian_ingestion_worker"


def _seed_workspaces() -> None:
    password = Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]).read_text().strip()
    with psycopg.connect(
        host="postgres",
        dbname="crypidian",
        user=os.environ["TRADING_BOT_DATABASE_USER"],
        password=password,
        autocommit=True,
    ) as connection:
        for workspace_id, label in ((WORKSPACE_ID, "main"), (OTHER_WORKSPACE_ID, "other")):
            connection.execute(
                "INSERT INTO workspace (id,name,slug,mode,status) "
                "VALUES (%s,%s,%s,'PAPER','ACTIVE')",
                (
                    workspace_id,
                    f"Live ingest E2E {label}",
                    f"live-ingest-{label}-" + workspace_id.hex[:12],
                ),
            )


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _kline_row(open_time: datetime) -> list[Any]:
    return [
        _ms(open_time),
        "100.0",
        "102.0",
        "99.0",
        "101.0",
        "1.25",
        _ms(open_time + INTERVAL) - 1,
        "126.25",
        2,
        "0.75",
        "75.75",
        "0",
    ]


def _stream_message(open_time: datetime, *, closed: bool) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@kline_1m",
            "data": {
                "e": "kline",
                "E": _ms(open_time + INTERVAL),
                "s": "BTCUSDT",
                "k": {
                    "t": _ms(open_time),
                    "T": _ms(open_time + INTERVAL) - 1,
                    "s": "BTCUSDT",
                    "i": "1m",
                    "o": "100.0",
                    "c": "101.0",
                    "h": "102.0",
                    "l": "99.0",
                    "v": "1.25",
                    "n": 2,
                    "x": closed,
                    "q": "126.25",
                    "V": "0.75",
                    "Q": "75.75",
                    "B": "0",
                },
            },
        }
    )


class FakeBinance:
    """Giả lập REST và WebSocket công khai; ghi lại mọi request để kiểm tra không có credential."""

    def __init__(self) -> None:
        self.rest_requests: list[httpx2.Request] = []
        self.stream_paths: list[str] = []
        self.rest_closed_until = M
        self.stream_messages: list[str] = []

    def rest(self, request: httpx2.Request) -> httpx2.Response:
        self.rest_requests.append(request)
        if request.url.host != "fapi.binance.com" or request.method != "GET":
            raise AssertionError("REST ngoài allowlist fixture.")
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx2.Response(200, content=EXCHANGE_INFO)
        if request.url.path != "/fapi/v1/klines":
            raise AssertionError("Endpoint REST ngoài fixture.")
        start = datetime.fromtimestamp(int(request.url.params["startTime"]) / 1000, UTC)
        end = datetime.fromtimestamp(int(request.url.params["endTime"]) / 1000, UTC)
        limit = int(request.url.params["limit"])
        rows = []
        open_time = start
        while open_time <= end and open_time < self.rest_closed_until and len(rows) < limit:
            rows.append(_kline_row(open_time))
            open_time += INTERVAL
        return httpx2.Response(200, json=rows)

    async def websocket(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "websocket":
            raise AssertionError("Fixture chỉ phục vụ WebSocket.")
        self.stream_paths.append(scope["path"] + "?" + scope["query_string"].decode("ascii"))
        await receive()
        await send({"type": "websocket.accept"})
        for message in self.stream_messages:
            await send({"type": "websocket.send", "text": message})
        await send({"type": "websocket.close", "code": 1000, "reason": ""})


async def _exercise() -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.binance_usdm.market_data import BinanceUsdMMarketDataAdapter
    from trading_bot.adapters.binance_usdm.market_stream import (
        BinanceUsdMKlineStream,
        KlineStreamDisconnected,
    )
    from trading_bot.adapters.database.engine import (
        create_database_engine,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import SourceLicenseEvidenceRecord
    from trading_bot.adapters.database.models.market_entities import (
        IngestionCursor as CursorRow,
    )
    from trading_bot.adapters.database.models.market_entities import (
        MarketBar,
        MarketInstrument,
    )
    from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
    from trading_bot.application.live_kline_ingestion import (
        LiveKlineIngestionService,
        LiveKlineSubscription,
    )
    from trading_bot.application.live_kline_ingestion_composition import workspace_transactions
    from trading_bot.application.market_signal_composition import _latest_bar_rows
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.research.license_scope import (
        SourceLicenseScope,
        canonical_scope_json,
        scope_sha256,
    )
    from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
    from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
    from trading_bot.workers.live_market_ingestion_worker import LiveMarketIngestionWorker

    url = database_url_from_secret(
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
        host="postgres",
        database="crypidian",
    )
    admin_engine = create_database_engine(url)
    admin_sessions = async_sessionmaker(admin_engine, expire_on_commit=False)
    # Mọi transaction của worker chạy bằng đúng role least-privilege của ingestion-worker.
    worker_engine = create_database_engine(url)

    @event.listens_for(worker_engine.sync_engine, "begin")
    def _use_ingestion_role(connection: Any) -> None:
        connection.exec_driver_sql(f"SET LOCAL ROLE {INGESTION_ROLE}")

    worker_sessions = async_sessionmaker(worker_engine, expire_on_commit=False)

    _seed_workspaces()
    binance = FakeBinance()
    clock = {"now": M + timedelta(seconds=5)}
    subscription = LiveKlineSubscription("BTCUSDT", "1m")

    async def no_wait(_: float) -> None:
        return None

    market_data = BinanceUsdMMarketDataAdapter(
        transport=httpx2.MockTransport(binance.rest),
        clock=lambda: clock["now"],
        sleep=no_wait,
    )
    stream_client = httpx2.AsyncClient(transport=ASGIWebSocketTransport(binance.websocket))
    # Transport ASGI chỉ khởi tạo task group khi client được mở như context manager.
    await stream_client.__aenter__()
    service = LiveKlineIngestionService(
        workspace_id=WORKSPACE_ID,
        market_data=market_data,
        transactions=workspace_transactions(worker_sessions, workspace_id=WORKSPACE_ID),
        warmup_bars=WARMUP,
    )
    worker = LiveMarketIngestionWorker(
        service=service,
        stream_factory=lambda: BinanceUsdMKlineStream(
            subscriptions=(("BTCUSDT", "1m"),),
            client=stream_client,
            clock=lambda: clock["now"],
        ),
        subscriptions=(subscription,),
        now=lambda: clock["now"],
        catch_up_min_interval_seconds=0,
    )

    async def session(*, now: datetime, messages: list[str], rest_until: datetime) -> str:
        clock["now"] = now
        binance.stream_messages = messages
        binance.rest_closed_until = rest_until
        try:
            await worker.run_session()
        except KlineStreamDisconnected:
            return "DISCONNECTED"
        except PermissionError:
            return "RIGHTS_DENIED"
        return "ENDED"

    async def counts(sessions: Any, workspace_id: UUID) -> tuple[int, int, int]:
        async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
            bars = await uow.session.scalar(
                select(func.count())
                .select_from(MarketBar)
                .where(MarketBar.workspace_id == workspace_id)
            )
            cursors = await uow.session.scalar(
                select(func.count())
                .select_from(CursorRow)
                .where(CursorRow.workspace_id == workspace_id)
            )
            instruments = await uow.session.scalar(
                select(func.count())
                .select_from(MarketInstrument)
                .where(MarketInstrument.workspace_id == workspace_id)
            )
        return int(bars or 0), int(cursors or 0), int(instruments or 0)

    # 1. Chưa có policy quyền dữ liệu: không request mạng, không ghi DB.
    denied_outcome = await session(now=M + timedelta(seconds=5), messages=[], rest_until=M)
    denied_counts = await counts(admin_sessions, WORKSPACE_ID)
    denied_requests = len(binance.rest_requests) + len(binance.stream_paths)

    # 2. Cấp policy PAPER_DEMO đã verified bằng luồng registry thật.
    policy_at = REAL_NOW - timedelta(days=3)
    entry = SourceRegistryEntry.discover(
        source_id="BINANCE_USDM_REST",
        publisher="Provider fixture",
        endpoint_url="https://example.invalid/klines",
        access_method="GET",
        auth_scope="public fixture",
        budget=SourceBudget(
            window=timedelta(hours=1),
            max_requests=100,
            max_bytes=1024 * 1024,
            max_cost=Decimal("0"),
            cost_unit="USD",
        ),
        actor_id="fixture-operator",
        at=policy_at,
        reason="Tạo policy quyền fixture cho E2E live ingest.",
    )
    scope = SourceLicenseScope(
        source_id="BINANCE_USDM_REST",
        provider="BINANCE",
        product="USDM_KLINES",
        venue="BINANCE_USDM",
        contract="FIXTURE",
        symbols=("BTCUSDT",),
        intervals=("1m",),
        data_start=REAL_NOW - timedelta(days=365),
        data_end=REAL_NOW + timedelta(days=365),
        allowed_purposes=(SourceUsePurpose.PAPER_DEMO,),
        allowed_outputs=("aggregate_report", "derived_features"),
        derived_use=("aggregate_reporting", "feature_extraction"),
        attribution="Provider fixture",
        max_retention_days=None,
    )
    pending = entry.transition(
        policy_id="BINANCE_USDM_REST:live-pending",
        state=SourcePolicyState.RIGHTS_PENDING,
        actor_id="fixture-operator",
        at=REAL_NOW - timedelta(days=2),
        reason="Chờ xác minh policy fixture.",
        evidence_ref="https://example.invalid/test-fixture-terms",
        provider=scope.provider,
        product=scope.product,
        venue=scope.venue,
        contract=scope.contract,
        license_scope=scope,
        attribution=scope.attribution,
    )
    evidence_id = uuid4()
    verified = pending.transition(
        policy_id="BINANCE_USDM_REST:live-verified",
        state=SourcePolicyState.VERIFIED,
        actor_id="fixture-operator",
        at=REAL_NOW - timedelta(hours=1),
        reason="Cho phép fixture PAPER demo.",
        license_scope=scope,
        license_evidence_id=evidence_id,
        license_digest="a" * 64,
        license_scope_hash=scope_sha256(scope),
        allowed_purposes=frozenset({SourceUsePurpose.PAPER_DEMO}),
        evidence_ref="https://example.invalid/test-fixture-terms",
        reviewed_at=REAL_NOW - timedelta(hours=1),
        valid_until=REAL_NOW + timedelta(days=1),
        approved_by="fixture-reviewer",
    )
    for revision in verified.revisions:
        async with UnitOfWork(admin_sessions, workspace_id=WORKSPACE_ID) as uow:
            if revision.state is SourcePolicyState.VERIFIED:
                uow.session.add(
                    SourceLicenseEvidenceRecord(
                        id=evidence_id,
                        workspace_id=WORKSPACE_ID,
                        source_id="BINANCE_USDM_REST",
                        policy_id=pending.current.policy.policy_id,
                        idempotency_key=uuid4(),
                        evidence_ref="https://example.invalid/test-fixture-terms",
                        digest="a" * 64,
                        byte_count=123,
                        media_type="text/plain",
                        scope_version=scope.scope_version,
                        license_scope=json.loads(canonical_scope_json(scope)),
                        canonical_scope=canonical_scope_json(scope),
                        scope_hash=scope_sha256(scope),
                        actor_id="fixture-operator",
                        created_at=REAL_NOW - timedelta(days=2),
                    )
                )
                await uow.session.flush()
            await SourceRegistryRepository(uow.session).append_revision(
                workspace_id=WORKSPACE_ID, revision=revision
            )
        async with UnitOfWork(admin_sessions, workspace_id=WORKSPACE_ID) as uow:
            await SourceRegistryRepository(uow.session).finalize_policy(
                workspace_id=WORKSPACE_ID,
                source_id="BINANCE_USDM_REST",
                policy_id=revision.policy.policy_id,
            )

    # 3. Phiên đầu: warm-up 200 nến bằng REST; nến M còn đang chạy nên chưa được ghi.
    first_outcome = await session(
        now=M + timedelta(seconds=1),
        messages=[_stream_message(M, closed=False)],
        rest_until=M,
    )
    after_warmup = await counts(admin_sessions, WORKSPACE_ID)
    # REST chưa công bố nến M; sự kiện đóng đến qua stream và được ghi ngay.
    second_outcome = await session(
        now=M + INTERVAL + timedelta(seconds=1),
        messages=[_stream_message(M, closed=True)],
        rest_until=M,
    )
    after_stream_bar = await counts(admin_sessions, WORKSPACE_ID)

    # 4. Rớt kết nối vài phút: REST lấp M+1..M+3; stream đưa tiếp nến M+4.
    third_outcome = await session(
        now=M + 5 * INTERVAL + timedelta(seconds=1),
        messages=[_stream_message(M + 4 * INTERVAL, closed=True)],
        rest_until=M + 4 * INTERVAL,
    )
    after_gap = await counts(admin_sessions, WORKSPACE_ID)

    # 5. Phát lại cùng dữ liệu: không nhân bản, không xung đột idempotency.
    replay_outcome = await session(
        now=M + 5 * INTERVAL + timedelta(seconds=2),
        messages=[_stream_message(M + 4 * INTERVAL, closed=True)],
        rest_until=M + 5 * INTERVAL,
    )
    after_replay = await counts(admin_sessions, WORKSPACE_ID)

    as_of = M + 5 * INTERVAL + timedelta(seconds=3)
    async with UnitOfWork(admin_sessions, workspace_id=WORKSPACE_ID) as uow:
        rows = (
            await uow.session.scalars(
                select(MarketBar)
                .where(MarketBar.workspace_id == WORKSPACE_ID)
                .order_by(MarketBar.open_time)
            )
        ).all()
        cursor = await uow.session.scalar(
            select(CursorRow).where(CursorRow.workspace_id == WORKSPACE_ID)
        )
        # Scanner signal đọc đúng các nến live này (nguồn BINANCE_USDM, đã đóng, point-in-time).
        scanner_rows = await _latest_bar_rows(
            uow.session,
            workspace_id=WORKSPACE_ID,
            as_of=as_of,
            symbols=("BTCUSDT",),
            timeframes=("1m",),
            max_bars_per_stream=256,
        )
    other_counts = await counts(worker_sessions, OTHER_WORKSPACE_ID)
    opens = [row.open_time for row in rows]

    await stream_client.__aexit__(None, None, None)
    await market_data.aclose()
    await worker_engine.dispose()
    await admin_engine.dispose()

    secret_markers = ("x-mbx-apikey", "authorization")
    return {
        "denied_without_policy": denied_outcome == "RIGHTS_DENIED"
        and denied_counts == (0, 0, 0)
        and denied_requests == 0,
        "warmup_bars": after_warmup[0],
        "bars_after_stream_close": after_stream_bar[0],
        "bars_after_gap_fill": after_gap[0],
        "bars_after_replay": after_replay[0],
        "session_outcomes": [first_outcome, second_outcome, third_outcome, replay_outcome],
        "single_cursor_and_instrument": after_replay[1:] == (1, 1),
        "contiguous_closed_live_bars": opens
        == [M - WARMUP * INTERVAL + index * INTERVAL for index in range(len(opens))]
        and all(row.is_closed and row.source == "BINANCE_USDM" for row in rows),
        "cursor_points_to_next_bar": cursor is not None
        and cursor.covered_until == M + 5 * INTERVAL
        and cursor.source == "BINANCE_USDM",
        "scanner_reads_live_bars": len(scanner_rows) == len(rows)
        and {row.source for row in scanner_rows} == {"BINANCE_USDM"},
        "other_workspace_sees_nothing": other_counts == (0, 0, 0),
        "stream_uses_market_route": set(binance.stream_paths)
        == {"/market/stream?streams=btcusdt@kline_1m"},
        "public_requests_only": all(
            request.method == "GET"
            and not any(marker in request.headers for marker in secret_markers)
            and "signature" not in request.url.params
            for request in binance.rest_requests
        ),
        "exchange_info_requests": sum(
            request.url.path == "/fapi/v1/exchangeInfo" for request in binance.rest_requests
        ),
    }


print(json.dumps(asyncio.run(_exercise()), sort_keys=True))
