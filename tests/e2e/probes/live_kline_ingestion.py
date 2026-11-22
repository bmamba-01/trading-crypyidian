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
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

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


def _mark_message(at: datetime) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "E": _ms(at),
                "s": "BTCUSDT",
                "p": "100.50000000",
                "ap": "100.50000000",
                "P": "100.60000000",
                "i": "100.40000000",
                "r": "0.00010000",
                "T": _ms(M + timedelta(hours=8)),
                "st": 1,
            },
        }
    )


def _book_message(at: datetime, *, update_id: int, bid: str, ask: str) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "e": "bookTicker",
                "u": update_id,
                "E": _ms(at),
                "T": _ms(at),
                "s": "BTCUSDT",
                "b": bid,
                "B": "1.500",
                "a": ask,
                "A": "2.250",
            },
        }
    )


def _liquidation_message(at: datetime) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@forceOrder",
            "data": {
                "e": "forceOrder",
                "E": _ms(at) + 5,
                "o": {
                    "s": "BTCUSDT",
                    "S": "SELL",
                    "o": "LIMIT",
                    "f": "IOC",
                    "q": "0.500",
                    "p": "99.00",
                    "ap": "99.50",
                    "X": "FILLED",
                    "l": "0.500",
                    "z": "0.500",
                    "T": _ms(at),
                    "ps": "BTCUSDT",
                    "st": 1,
                },
            },
        }
    )


def _agg_message(aggregate_id: int, at: datetime) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@aggTrade",
            "data": {
                "e": "aggTrade",
                "E": _ms(at) + 5,
                "a": aggregate_id,
                "s": "BTCUSDT",
                "p": "100.00",
                "q": "0.500",
                "nq": "0.500",
                "f": aggregate_id * 10,
                "l": aggregate_id * 10,
                "T": _ms(at),
                # id chẵn là taker mua, id lẻ là taker bán.
                "m": aggregate_id % 2 == 1,
                "st": 1,
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
        BinanceUsdMEventStream,
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
        MarketLiquidation,
        MarketLiveQuote,
        MarketMarkPrice,
        MarketTradeFlow,
    )
    from trading_bot.adapters.database.repositories.bot_reports import BotReportRepository
    from trading_bot.adapters.database.repositories.source_registry import SourceRegistryRepository
    from trading_bot.application.ingestion_worker_heartbeat import (
        IngestionWorkerHeartbeatPublisher,
        WorkerHeartbeatReadService,
        WorkerHeartbeatSource,
    )
    from trading_bot.application.live_kline_ingestion import (
        LiveKlineIngestionService,
        LiveKlineSubscription,
    )
    from trading_bot.application.live_kline_ingestion_composition import workspace_transactions
    from trading_bot.application.live_market_events import LiveMarketEventService
    from trading_bot.application.market_signal_composition import _latest_bar_rows
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.domain.research.license_scope import (
        SourceLicenseScope,
        canonical_scope_json,
        scope_sha256,
    )
    from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
    from trading_bot.domain.research.source_registry import SourceBudget, SourceRegistryEntry
    from trading_bot.workers.live_market_ingestion_worker import (
        LiveMarketEventWorker,
        LiveMarketIngestionWorker,
        LiveQuoteWorker,
    )

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
    # Admin-web (CK-1) chỉ đọc bảng giá trực tiếp và mẫu mark price bằng role của nó.
    web_engine = create_database_engine(url)

    @event.listens_for(web_engine.sync_engine, "begin")
    def _use_admin_web_role(connection: Any) -> None:
        connection.exec_driver_sql("SET LOCAL ROLE crypidian_admin_web")

    web_sessions = async_sessionmaker(web_engine, expire_on_commit=False)

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

    # 6. Stream sự kiện: mark price lấy mẫu mỗi phút, mọi lệnh thanh lý, dòng lệnh theo bucket.
    event_service = LiveMarketEventService(
        workspace_id=WORKSPACE_ID,
        transactions=workspace_transactions(worker_sessions, workspace_id=WORKSPACE_ID),
    )
    event_worker = LiveMarketEventWorker(
        service=event_service,
        stream_factory=lambda: BinanceUsdMEventStream(
            symbols=("BTCUSDT",),
            kinds=("mark_price", "liquidation", "trade_flow"),
            client=stream_client,
            clock=lambda: clock["now"],
        ),
        symbols=("BTCUSDT",),
        now=lambda: clock["now"],
        mark_price_interval_seconds=60,
        flush_seconds=0,
    )
    event_messages = [
        _mark_message(M + timedelta(seconds=1)),
        _mark_message(M + timedelta(seconds=2)),
        _liquidation_message(M + timedelta(seconds=5)),
        _agg_message(100, M + timedelta(seconds=10)),
        _mark_message(M + timedelta(seconds=61)),
        _agg_message(101, M + timedelta(seconds=65)),
        _agg_message(102, M + timedelta(seconds=70)),
        _agg_message(103, M + timedelta(seconds=125)),
    ]

    async def event_session() -> str:
        clock["now"] = M + timedelta(minutes=3)
        binance.stream_messages = event_messages
        try:
            await event_worker.run_session()
        except KlineStreamDisconnected:
            return "DISCONNECTED"
        return "ENDED"

    async def event_counts(sessions: Any, workspace_id: UUID) -> tuple[int, int, int, int]:
        async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
            totals = []
            for model in (MarketMarkPrice, MarketLiquidation, MarketTradeFlow):
                totals.append(
                    await uow.session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.workspace_id == workspace_id)
                    )
                )
            complete = await uow.session.scalar(
                select(func.count())
                .select_from(MarketTradeFlow)
                .where(
                    MarketTradeFlow.workspace_id == workspace_id,
                    MarketTradeFlow.is_complete.is_(True),
                )
            )
        return (*(int(total or 0) for total in totals), int(complete or 0))

    event_outcome = await event_session()
    events_first = await event_counts(admin_sessions, WORKSPACE_ID)
    events_written_first = event_worker.events_written
    await event_session()
    events_replayed = await event_counts(admin_sessions, WORKSPACE_ID)
    events_other_workspace = await event_counts(worker_sessions, OTHER_WORKSPACE_ID)
    async with UnitOfWork(admin_sessions, workspace_id=WORKSPACE_ID) as uow:
        complete_flow = await uow.session.scalar(
            select(MarketTradeFlow).where(
                MarketTradeFlow.workspace_id == WORKSPACE_ID,
                MarketTradeFlow.is_complete.is_(True),
            )
        )
    events_append_only = False
    try:
        async with UnitOfWork(worker_sessions, workspace_id=WORKSPACE_ID) as uow:
            await uow.session.execute(text("UPDATE market_liquidation SET status = 'EDITED'"))
    except DBAPIError:
        # Role ingestion không có quyền UPDATE; trigger append-only là lớp chặn thứ hai.
        events_append_only = True

    # 7. CK-1: bookTicker trên route /public → một dòng giá trực tiếp, chỉ nhận update_id mới hơn.
    quote_worker = LiveQuoteWorker(
        service=event_service,
        stream_factory=lambda: BinanceUsdMEventStream(
            symbols=("BTCUSDT",),
            kinds=("book_ticker",),
            client=stream_client,
            clock=lambda: clock["now"],
        ),
        symbols=("BTCUSDT",),
        now=lambda: clock["now"],
        interval_seconds=2.0,
        monotonic=lambda: 0.0,
    )

    async def quote_session(messages: list[str]) -> str:
        clock["now"] = M + timedelta(minutes=3)
        binance.stream_messages = messages
        try:
            await quote_worker.run_session()
        except Exception as error:  # noqa: BLE001 - probe chỉ ghi lại loại kết thúc phiên
            return type(error).__name__
        return "ENDED"

    async def quote_rows(sessions: Any, workspace_id: UUID) -> list[tuple[int, str, str]]:
        async with UnitOfWork(sessions, workspace_id=workspace_id) as uow:
            found = (
                await uow.session.scalars(
                    select(MarketLiveQuote).where(MarketLiveQuote.workspace_id == workspace_id)
                )
            ).all()
        return [(row.update_id, str(row.bid_price), str(row.ask_price)) for row in found]

    quote_outcome = await quote_session(
        [
            _book_message(M + timedelta(minutes=2), update_id=5, bid="100.10", ask="100.20"),
            _book_message(
                M + timedelta(minutes=2, seconds=1), update_id=7, bid="100.30", ask="100.40"
            ),
        ]
    )
    quotes_first = await quote_rows(admin_sessions, WORKSPACE_ID)
    # Phát lại bản cũ hơn (u=6) không được ghi đè bản u=7.
    await quote_session(
        [_book_message(M + timedelta(minutes=2, seconds=2), update_id=6, bid="99.00", ask="99.10")]
    )
    quotes_after_stale = await quote_rows(admin_sessions, WORKSPACE_ID)
    quotes_other_workspace = await quote_rows(worker_sessions, OTHER_WORKSPACE_ID)
    quote_delete_denied = False
    try:
        async with UnitOfWork(worker_sessions, workspace_id=WORKSPACE_ID) as uow:
            await uow.session.execute(text("DELETE FROM market_live_quote"))
    except DBAPIError:
        quote_delete_denied = True
    async with UnitOfWork(web_sessions, workspace_id=WORKSPACE_ID) as uow:
        web_reports = BotReportRepository(uow.session)
        web_quote = await web_reports.latest_live_quote(workspace_id=WORKSPACE_ID, symbol="BTCUSDT")
        web_mark = await web_reports.latest_mark_price(
            workspace_id=WORKSPACE_ID, symbol="BTCUSDT", as_of=REAL_NOW + timedelta(minutes=1)
        )
    async with UnitOfWork(web_sessions, workspace_id=OTHER_WORKSPACE_ID) as uow:
        web_other = await BotReportRepository(uow.session).latest_live_quote(
            workspace_id=WORKSPACE_ID, symbol="BTCUSDT"
        )
    web_write_denied = False
    try:
        async with UnitOfWork(web_sessions, workspace_id=WORKSPACE_ID) as uow:
            await uow.session.execute(text("UPDATE market_live_quote SET update_id = 999"))
    except DBAPIError:
        web_write_denied = True

    # 8. CK-9: publisher ghi heartbeat bằng role ingestion, Admin-web chỉ đọc theo RLS.
    heartbeat_wait = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat_wait.wait())
    heartbeat_publisher = IngestionWorkerHeartbeatPublisher(
        sessions=worker_sessions,
        workspace_id=WORKSPACE_ID,
        sources=(
            WorkerHeartbeatSource("live_market_ingestion", worker, heartbeat_task),
            WorkerHeartbeatSource("live_market_events", event_worker, heartbeat_task),
            WorkerHeartbeatSource("live_market_quotes", quote_worker, heartbeat_task),
        ),
    )
    await heartbeat_publisher.publish_once(now=REAL_NOW)
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    heartbeat_reader = WorkerHeartbeatReadService(web_sessions)
    heartbeat_reports = await heartbeat_reader.list_readiness(
        workspace_id=WORKSPACE_ID, now=REAL_NOW + timedelta(seconds=1)
    )
    heartbeat_other = await heartbeat_reader.list_readiness(
        workspace_id=OTHER_WORKSPACE_ID, now=REAL_NOW + timedelta(seconds=1)
    )
    heartbeat_by_component = {report.component: report for report in heartbeat_reports}
    heartbeat_web_write_denied = False
    try:
        async with UnitOfWork(web_sessions, workspace_id=WORKSPACE_ID) as uow:
            await uow.session.execute(text("UPDATE worker_heartbeat SET state = 'degraded'"))
    except DBAPIError:
        heartbeat_web_write_denied = True

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
    await web_engine.dispose()
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
        "stream_uses_market_route": {
            path for path in binance.stream_paths if "/public/" not in path
        }
        == {
            "/market/stream?streams=btcusdt@kline_1m",
            "/market/stream?streams=btcusdt@markPrice@1s/btcusdt@forceOrder/btcusdt@aggTrade",
        },
        "public_requests_only": all(
            request.method == "GET"
            and not any(marker in request.headers for marker in secret_markers)
            and "signature" not in request.url.params
            for request in binance.rest_requests
        ),
        "event_session_outcome": event_outcome,
        "event_rows_mark_liquidation_flow_complete": list(events_first),
        "event_worker_written": events_written_first,
        "event_replay_adds_nothing": events_replayed == events_first,
        "complete_flow_bucket": complete_flow is not None
        and complete_flow.bucket_start == M + timedelta(minutes=1)
        and complete_flow.trade_count == 2
        and complete_flow.buy_quantity == Decimal("0.5")
        and complete_flow.sell_quantity == Decimal("0.5"),
        "events_hidden_from_other_workspace": events_other_workspace == (0, 0, 0, 0),
        "events_append_only": events_append_only,
        "quote_session_outcome": quote_outcome,
        "quote_rows_after_stream": quotes_first,
        "quote_stale_update_ignored": quotes_after_stale == quotes_first,
        "quote_hidden_from_other_workspace": quotes_other_workspace == [],
        "quote_delete_denied_for_ingestion": quote_delete_denied,
        "quote_stream_uses_public_route": "/public/stream?streams=btcusdt@bookTicker"
        in binance.stream_paths,
        "admin_web_reads_quote_and_mark": web_quote is not None
        and web_quote["bid_price"] == Decimal("100.30")
        and web_mark is not None
        and web_mark["mark_price"] == Decimal("100.5"),
        "admin_web_quote_rls_isolated": web_other is None,
        "admin_web_cannot_write_quote": web_write_denied,
        "worker_heartbeat_components": sorted(heartbeat_by_component),
        "worker_heartbeat_ingestion_counters_valid": heartbeat_by_component[
            "live_market_ingestion"
        ].metadata.get("bars_appended", 0)
        >= WARMUP
        and heartbeat_by_component["live_market_ingestion"].metadata.get("reconnect_count") == 0
        and heartbeat_by_component["live_market_ingestion"].metadata.get("heartbeat_stale")
        is False,
        "worker_heartbeat_admin_rls_isolated": heartbeat_other == [],
        "worker_heartbeat_admin_read_only": heartbeat_web_write_denied,
        "exchange_info_requests": sum(
            request.url.path == "/fapi/v1/exchangeInfo" for request in binance.rest_requests
        ),
    }


print(json.dumps(asyncio.run(_exercise()), sort_keys=True))
