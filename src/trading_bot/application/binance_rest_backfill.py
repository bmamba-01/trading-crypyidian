"""Phân trang klines USDⓈ-M theo năm, quyền dữ liệu và checkpoint có thể tiếp tục."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

from trading_bot.domain.market.models import OHLCVBar
from trading_bot.ports.market_data import MarketDataPage

_FIXED_INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(days=7),
}
_SYMBOL = re.compile(r"[A-Z0-9]{3,32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_WEEK_EPOCH = datetime(1970, 1, 5, tzinfo=UTC)
_SOURCE = "BINANCE_USDM_REST_BACKFILL"
_CHECKPOINT_LIMIT = 2 * 1024 * 1024


class RestBackfillError(RuntimeError):
    """REST backfill có request, checkpoint hoặc dữ liệu nguồn không hợp lệ."""


class BackfillRightsPurpose(StrEnum):
    """Mục đích sử dụng được duyệt riêng cho từng lần nạp bù."""

    BACKTEST_RESEARCH = "BACKTEST_RESEARCH"
    PAPER_DEMO = "PAPER_DEMO"


@dataclass(frozen=True, slots=True)
class RestBackfillRequest:
    """Khoảng klines nửa mở UTC; end là mốc kết thúc không bao gồm."""

    symbol: str
    interval: str
    start: datetime
    end: datetime
    purpose: BackfillRightsPurpose = BackfillRightsPurpose.BACKTEST_RESEARCH
    page_limit: int = 1500
    feed_delay: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("symbol phải là mã USD-M viết hoa, không chứa separator.")
        if self.interval not in _FIXED_INTERVALS:
            raise ValueError("interval phải có độ dài cố định được hỗ trợ cho backfill.")
        if not isinstance(self.purpose, BackfillRightsPurpose):
            raise ValueError("purpose backfill không hợp lệ.")
        if isinstance(self.page_limit, bool) or not 1 <= self.page_limit <= 1500:
            raise ValueError("page_limit phải nằm trong 1..1500.")
        if not isinstance(self.feed_delay, timedelta) or self.feed_delay < timedelta(0):
            raise ValueError("feed_delay phải là timedelta không âm.")
        start = _as_utc(self.start, "start")
        end = _as_utc(self.end, "end")
        if start >= end:
            raise ValueError("start phải trước end.")
        if end - start > timedelta(days=366):
            raise ValueError("Mỗi REST backfill tối đa 366 ngày.")
        if not _is_interval_aligned(start, self.interval) or not _is_interval_aligned(
            end, self.interval
        ):
            raise ValueError("start/end phải căn theo biên interval UTC.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def expected_bars(self) -> int:
        """Số mốc nến dự kiến trong khoảng nửa mở đã căn interval."""
        interval = _FIXED_INTERVALS[self.interval]
        return int((self.end - self.start) / interval)

    @property
    def interval_duration(self) -> timedelta:
        """Độ dài cố định của interval đã được kiểm tra ở constructor."""
        return _FIXED_INTERVALS[self.interval]

    @property
    def fingerprint(self) -> str:
        """Hash xác định gắn symbol, thời gian, mục đích và giới hạn trang."""
        payload = {
            "provider": "BINANCE_USDM_REST",
            "symbol": self.symbol,
            "interval": self.interval,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "purpose": self.purpose.value,
            "page_limit": self.page_limit,
            "feed_delay_microseconds": _duration_microseconds(self.feed_delay),
        }
        return _canonical_hash(payload)


def build_trailing_365_day_rest_request(
    symbol: str,
    interval: str,
    *,
    now: datetime,
    purpose: BackfillRightsPurpose = BackfillRightsPurpose.BACKTEST_RESEARCH,
    page_limit: int = 1500,
    feed_delay: timedelta = timedelta(0),
) -> RestBackfillRequest:
    """Tạo khoảng 365 ngày đến biên nến UTC đã đóng gần nhất."""
    instant = _as_utc(now, "now")
    duration = _FIXED_INTERVALS.get(interval)
    if duration is None:
        raise ValueError("interval phải có độ dài cố định được hỗ trợ cho backfill.")
    anchor = _interval_anchor(interval)
    elapsed = instant - anchor
    interval_microseconds = _duration_microseconds(duration)
    elapsed_microseconds = _duration_microseconds(elapsed)
    end = anchor + timedelta(
        microseconds=elapsed_microseconds // interval_microseconds * interval_microseconds
    )
    bar_count = _duration_microseconds(timedelta(days=365)) // interval_microseconds
    start = end - duration * bar_count
    return RestBackfillRequest(
        symbol=symbol,
        interval=interval,
        start=start,
        end=end,
        purpose=purpose,
        page_limit=page_limit,
        feed_delay=feed_delay,
    )


@dataclass(frozen=True, slots=True)
class BackfillRightsEvidence:
    """Bằng chứng quyền đã được policy bên ngoài xác nhận cho một purpose."""

    workspace_id: UUID
    provider: str
    purpose: BackfillRightsPurpose
    terms_url: str
    policy_fingerprint: str
    verified_at: datetime
    valid_until: datetime
    checked_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id rights evidence phải là UUID.")
        if self.provider != "BINANCE_USDM_REST":
            raise ValueError("Rights evidence không khớp provider REST USD-M.")
        if not isinstance(self.purpose, BackfillRightsPurpose):
            raise ValueError("Rights evidence cần purpose hợp lệ.")
        parsed = urlparse(self.terms_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("terms_url phải là HTTPS đầy đủ.")
        if _SHA256.fullmatch(self.policy_fingerprint) is None:
            raise ValueError("policy_fingerprint phải là SHA-256 dạng hex.")
        verified_at = _as_utc(self.verified_at, "verified_at")
        valid_until = _as_utc(self.valid_until, "valid_until")
        if valid_until <= verified_at:
            raise ValueError("valid_until phải sau verified_at.")
        checked_at = self.checked_at
        if checked_at is not None:
            checked_at = _as_utc(checked_at, "checked_at")
            if checked_at < verified_at or checked_at >= valid_until:
                raise ValueError("checked_at phải thuộc khoảng hiệu lực của rights evidence.")
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "checked_at", checked_at)


class BackfillRightsGate(Protocol):
    """Trả bằng chứng quyền đã được duyệt; implementation không có mặc định cho phép."""

    async def authorize(
        self,
        *,
        request: RestBackfillRequest,
        now: datetime,
    ) -> BackfillRightsEvidence: ...


class RestKlineProvider(Protocol):
    """Phần provider market-data cần thiết cho phân trang klines."""

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[OHLCVBar]: ...


@dataclass(frozen=True, slots=True)
class RestBackfillPageReceipt:
    """Receipt bất biến của một trang REST đã được kiểm checksum."""

    start_cursor: datetime
    next_cursor: datetime
    checksum: str
    bars_received: int
    retrieved_at: datetime

    def __post_init__(self) -> None:
        start_cursor = _as_utc(self.start_cursor, "start_cursor")
        next_cursor = _as_utc(self.next_cursor, "next_cursor")
        retrieved_at = _as_utc(self.retrieved_at, "retrieved_at")
        if next_cursor < start_cursor:
            raise ValueError("next_cursor không được lùi lại.")
        if _SHA256.fullmatch(self.checksum) is None:
            raise ValueError("checksum phải là SHA-256 dạng hex.")
        if self.bars_received < 0:
            raise ValueError("bars_received không được âm.")
        object.__setattr__(self, "start_cursor", start_cursor)
        object.__setattr__(self, "next_cursor", next_cursor)
        object.__setattr__(self, "retrieved_at", retrieved_at)


@dataclass(frozen=True, slots=True)
class RestBackfillCheckpoint:
    """Cursor cùng receipt để caller lưu bền vững và tiếp tục sau gián đoạn."""

    request_fingerprint: str
    policy_fingerprint: str
    next_open_time: datetime
    bars_received: int
    missing_bars: int
    page_receipts: tuple[RestBackfillPageReceipt, ...]
    started_at: datetime
    updated_at: datetime
    finished: bool = False

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.request_fingerprint) is None:
            raise ValueError("request_fingerprint không hợp lệ.")
        if _SHA256.fullmatch(self.policy_fingerprint) is None:
            raise ValueError("policy_fingerprint không hợp lệ.")
        if self.bars_received < 0 or self.missing_bars < 0:
            raise ValueError("Số bar và gap không được âm.")
        if isinstance(self.bars_received, bool) or isinstance(self.missing_bars, bool):
            raise ValueError("Số bar và gap phải là số nguyên.")
        next_open_time = _as_utc(self.next_open_time, "next_open_time")
        started_at = _as_utc(self.started_at, "started_at")
        updated_at = _as_utc(self.updated_at, "updated_at")
        receipts = tuple(self.page_receipts)
        if updated_at < started_at:
            raise ValueError("updated_at không được trước started_at.")
        if any(item.retrieved_at > updated_at for item in receipts):
            raise ValueError("Receipt không được nằm sau updated_at.")
        if sum(item.bars_received for item in receipts) != self.bars_received:
            raise ValueError("Tổng receipt không khớp bars_received.")
        if any(
            right.start_cursor != left.next_cursor
            for left, right in zip(receipts, receipts[1:], strict=False)
        ):
            raise ValueError("Receipt không liên tục theo cursor.")
        if receipts and receipts[-1].next_cursor != next_open_time:
            raise ValueError("Receipt cuối không khớp next_open_time.")
        object.__setattr__(self, "next_open_time", next_open_time)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "page_receipts", receipts)

    @classmethod
    def start(
        cls,
        request: RestBackfillRequest,
        *,
        policy_fingerprint: str,
        now: datetime,
    ) -> RestBackfillCheckpoint:
        instant = _as_utc(now, "now")
        return cls(
            request_fingerprint=request.fingerprint,
            policy_fingerprint=policy_fingerprint,
            next_open_time=request.start,
            bars_received=0,
            missing_bars=0,
            page_receipts=(),
            started_at=instant,
            updated_at=instant,
        )

    def to_json(self) -> bytes:
        """Serialize checkpoint canonical để ghi vào storage bền vững."""
        payload = {
            "request_fingerprint": self.request_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "next_open_time": self.next_open_time.isoformat(),
            "bars_received": self.bars_received,
            "missing_bars": self.missing_bars,
            "page_receipts": [
                {
                    "start_cursor": item.start_cursor.isoformat(),
                    "next_cursor": item.next_cursor.isoformat(),
                    "checksum": item.checksum,
                    "bars_received": item.bars_received,
                    "retrieved_at": item.retrieved_at.isoformat(),
                }
                for item in self.page_receipts
            ],
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "finished": self.finished,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(
        cls, payload: bytes, *, max_bytes: int = _CHECKPOINT_LIMIT
    ) -> RestBackfillCheckpoint:
        if not payload or len(payload) > max_bytes:
            raise RestBackfillError("Checkpoint rỗng hoặc vượt giới hạn byte.")
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict) or set(raw) != {
                "request_fingerprint",
                "policy_fingerprint",
                "next_open_time",
                "bars_received",
                "missing_bars",
                "page_receipts",
                "started_at",
                "updated_at",
                "finished",
            }:
                raise ValueError("Trường checkpoint không đúng.")
            if not isinstance(raw["page_receipts"], list):
                raise ValueError("page_receipts phải là array.")
            receipts: list[RestBackfillPageReceipt] = []
            for item in raw["page_receipts"]:
                if not isinstance(item, dict) or set(item) != {
                    "start_cursor",
                    "next_cursor",
                    "checksum",
                    "bars_received",
                    "retrieved_at",
                }:
                    raise ValueError("Receipt có cấu trúc sai.")
                receipts.append(
                    RestBackfillPageReceipt(
                        start_cursor=_parse_datetime(item["start_cursor"]),
                        next_cursor=_parse_datetime(item["next_cursor"]),
                        checksum=_require_string(item["checksum"]),
                        bars_received=_require_int(item["bars_received"]),
                        retrieved_at=_parse_datetime(item["retrieved_at"]),
                    )
                )
            checkpoint = cls(
                request_fingerprint=_require_string(raw["request_fingerprint"]),
                policy_fingerprint=_require_string(raw["policy_fingerprint"]),
                next_open_time=_parse_datetime(raw["next_open_time"]),
                bars_received=_require_int(raw["bars_received"]),
                missing_bars=_require_int(raw["missing_bars"]),
                page_receipts=tuple(receipts),
                started_at=_parse_datetime(raw["started_at"]),
                updated_at=_parse_datetime(raw["updated_at"]),
                finished=_require_bool(raw["finished"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RestBackfillError("Checkpoint JSON không hợp lệ.") from error
        if checkpoint.to_json() != payload:
            raise RestBackfillError("Checkpoint JSON không ở dạng canonical.")
        return checkpoint


@dataclass(frozen=True, slots=True)
class RestBackfillBatch:
    """Trang vừa tải và trạng thái tổng hợp để theo dõi tiếp hoặc chuẩn bị replay."""

    bars: tuple[OHLCVBar, ...]
    checkpoint: RestBackfillCheckpoint
    expected_bars: int

    @property
    def missing_bars(self) -> int:
        return self.checkpoint.missing_bars

    @property
    def coverage_complete(self) -> bool:
        return (
            self.checkpoint.finished
            and self.checkpoint.missing_bars == 0
            and self.checkpoint.bars_received == self.expected_bars
        )

    @property
    def coverage_ratio(self) -> float:
        if self.expected_bars == 0:
            return 1.0
        return self.checkpoint.bars_received / self.expected_bars


class BinanceRestBackfillRunner:
    """Đọc từng trang REST sau khi rights gate cho phép và ghi cursor kèm checksum."""

    def __init__(
        self,
        *,
        market_data: RestKlineProvider,
        rights_gate: BackfillRightsGate,
    ) -> None:
        self._market_data = market_data
        self._rights_gate = rights_gate

    async def run_batch(
        self,
        request: RestBackfillRequest,
        *,
        now: datetime,
        checkpoint: RestBackfillCheckpoint | None = None,
        max_pages: int = 10,
    ) -> RestBackfillBatch:
        instant = _as_utc(now, "now")
        if isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise ValueError("max_pages phải nằm trong 1..100.")
        interval = _FIXED_INTERVALS[request.interval]
        if request.end > instant:
            raise RestBackfillError("Request chứa mốc end trong tương lai.")
        state = checkpoint
        output: list[OHLCVBar] = []
        if state is not None:
            if state.request_fingerprint != request.fingerprint:
                raise RestBackfillError("Checkpoint request fingerprint không khớp request.")
            if state.updated_at > instant:
                raise RestBackfillError("Checkpoint updated_at nằm trong tương lai.")
            if state.finished:
                return RestBackfillBatch((), state, request.expected_bars)
            if state.next_open_time < request.start or state.next_open_time >= request.end:
                raise RestBackfillError("Checkpoint cursor nằm ngoài request chưa hoàn tất.")
            if not _is_interval_aligned(state.next_open_time, request.interval):
                raise RestBackfillError("Checkpoint cursor lệch lưới interval UTC.")

        for _ in range(max_pages):
            evidence = await self._rights_gate.authorize(request=request, now=instant)
            self._validate_rights(evidence, request=request, now=instant)
            if state is None:
                state = RestBackfillCheckpoint.start(
                    request, policy_fingerprint=evidence.policy_fingerprint, now=instant
                )
            elif state.policy_fingerprint != evidence.policy_fingerprint:
                raise RestBackfillError("Checkpoint policy fingerprint đã thay đổi.")

            cursor = state.next_open_time
            raw_page = await self._market_data.get_klines(
                symbol=request.symbol,
                interval=request.interval,
                start_time=cursor,
                end_time=request.end - timedelta(milliseconds=1),
                limit=request.page_limit,
            )
            self._validate_page_envelope(raw_page, now=instant)
            if len(raw_page.records) > request.page_limit:
                raise RestBackfillError("Trang REST vượt page_limit đã yêu cầu.")
            converted: list[OHLCVBar] = []
            next_cursor = cursor
            missing_bars = state.missing_bars
            last_open: datetime | None = None
            for item in raw_page.records:
                self._validate_bar(
                    item,
                    request=request,
                    cursor=cursor,
                    last_open=last_open,
                    page_received_at=raw_page.received_at,
                    now=instant,
                )
                if item.open_time > next_cursor:
                    missing_bars += int((item.open_time - next_cursor) / interval)
                next_cursor = item.open_time + interval
                last_open = item.open_time
                simulated_available_at = item.close_time + request.feed_delay
                if simulated_available_at > instant:
                    raise RestBackfillError(
                        "feed_delay khiến bar chưa khả dụng tại mốc backfill đã chọn."
                    )
                converted.append(
                    OHLCVBar(
                        symbol=item.symbol,
                        timeframe=item.timeframe,
                        open_time=item.open_time,
                        close_time=item.close_time,
                        event_at=item.close_time,
                        received_at=simulated_available_at,
                        available_at=simulated_available_at,
                        open=item.open,
                        high=item.high,
                        low=item.low,
                        close=item.close,
                        volume=item.volume,
                        quote_volume=item.quote_volume,
                        is_closed=True,
                        source=_SOURCE,
                        source_cursor=item.source_cursor or str(_milliseconds(item.open_time)),
                        checksum=raw_page.checksum,
                    )
                )
            if not raw_page.records and not raw_page.is_complete:
                raise RestBackfillError("Trang rỗng nhưng nguồn báo còn trang; cursor không tiến.")
            if raw_page.records and next_cursor <= cursor:
                raise RestBackfillError("Trang không làm cursor tiến về phía trước.")
            if len(converted) + state.bars_received > request.expected_bars:
                raise RestBackfillError("Số bar vượt số mốc kỳ vọng trong request.")

            finished = next_cursor >= request.end or raw_page.is_complete
            total_bars = state.bars_received + len(converted)
            if finished:
                missing_bars = request.expected_bars - total_bars
                next_cursor = request.end
            receipt = RestBackfillPageReceipt(
                start_cursor=cursor,
                next_cursor=next_cursor,
                checksum=raw_page.checksum,
                bars_received=len(converted),
                retrieved_at=raw_page.received_at,
            )
            state = RestBackfillCheckpoint(
                request_fingerprint=request.fingerprint,
                policy_fingerprint=evidence.policy_fingerprint,
                next_open_time=next_cursor,
                bars_received=total_bars,
                missing_bars=missing_bars,
                page_receipts=(*state.page_receipts, receipt),
                started_at=state.started_at,
                updated_at=instant,
                finished=finished,
            )
            output.extend(converted)
            if finished:
                break

        if state is None:
            raise RestBackfillError("Rights gate không tạo checkpoint.")
        return RestBackfillBatch(tuple(output), state, request.expected_bars)

    @staticmethod
    def _validate_rights(
        evidence: BackfillRightsEvidence,
        *,
        request: RestBackfillRequest,
        now: datetime,
    ) -> None:
        if evidence.provider != "BINANCE_USDM_REST" or evidence.purpose is not request.purpose:
            raise PermissionError("Rights evidence không cấp cho provider hoặc mục đích yêu cầu.")
        authorization_time = evidence.checked_at or now
        if evidence.verified_at > authorization_time or authorization_time >= evidence.valid_until:
            raise PermissionError("Rights evidence chưa hiệu lực hoặc đã hết hạn.")

    @staticmethod
    def _validate_page_envelope(page: MarketDataPage[OHLCVBar], *, now: datetime) -> None:
        if page.received_at > now or page.available_at > now:
            raise RestBackfillError("Trang REST có thời điểm nhận trong tương lai.")
        if page.available_at < page.received_at:
            raise RestBackfillError("Trang REST available_at nằm trước received_at.")
        if _SHA256.fullmatch(page.checksum) is None:
            raise RestBackfillError("Trang REST thiếu checksum SHA-256 hợp lệ.")
        if hashlib.sha256(page.raw_payload).hexdigest() != page.checksum:
            raise RestBackfillError("Checksum trang REST không khớp raw payload.")

    @staticmethod
    def _validate_bar(
        item: OHLCVBar,
        *,
        request: RestBackfillRequest,
        cursor: datetime,
        last_open: datetime | None,
        page_received_at: datetime,
        now: datetime,
    ) -> None:
        interval = _FIXED_INTERVALS[request.interval]
        if (item.symbol, item.timeframe) != (request.symbol, request.interval):
            raise RestBackfillError("Trang REST lẫn symbol hoặc interval ngoài request.")
        if item.open_time < cursor or item.open_time >= request.end:
            raise RestBackfillError("Trang REST có bar trùng hoặc ngoài khoảng nửa mở.")
        if not _is_interval_aligned(item.open_time, request.interval):
            raise RestBackfillError("Bar REST lệch lưới thời gian UTC.")
        if last_open is not None and item.open_time <= last_open:
            raise RestBackfillError("Trang REST có bar trùng hoặc không tăng theo thời gian.")
        if item.close_time != item.open_time + interval - timedelta(milliseconds=1):
            raise RestBackfillError("close_time không khớp interval được yêu cầu.")
        if not item.is_closed or item.close_time >= now:
            raise RestBackfillError("Backfill chỉ nhận bar đã đóng tại mốc yêu cầu.")
        if item.received_at > page_received_at or item.available_at > page_received_at:
            raise RestBackfillError("Timestamp của bar nằm sau receipt của trang REST.")


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _interval_anchor(interval: str) -> datetime:
    return _WEEK_EPOCH if interval == "1w" else _EPOCH


def _is_interval_aligned(value: datetime, interval: str) -> bool:
    elapsed = value.astimezone(UTC) - _interval_anchor(interval)
    return _duration_microseconds(elapsed) % _duration_microseconds(_FIXED_INTERVALS[interval]) == 0


def _duration_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp checkpoint phải là chuỗi.")
    parsed = datetime.fromisoformat(value)
    return _as_utc(parsed, "checkpoint timestamp")


def _require_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Giá trị checkpoint phải là chuỗi.")
    return value


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Giá trị checkpoint phải là số nguyên.")
    return value


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("finished checkpoint phải là boolean.")
    return value
