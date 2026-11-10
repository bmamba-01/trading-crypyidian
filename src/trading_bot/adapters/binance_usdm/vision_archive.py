"""Backfill USDⓈ-M klines từ Binance Vision với chặn quyền và kiểm tra dữ liệu."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx2

from trading_bot.adapters.database.repositories.market_data import MarketAppendResult
from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
from trading_bot.domain.market.models import OHLCVBar

VISION_HOST = "data.binance.vision"
VISION_TERMS_URL = (
    "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
)
VISION_DATASET_LICENSE = "CC-BY-NC-SA-4.0"
VISION_DATASET_ROOT = "https://data.binance.vision/data/futures/um"
VISION_SOURCE = "BINANCE_VISION"
_SYMBOL = re.compile(r"[A-Z0-9]{3,32}\Z")
_INTERVALS: Mapping[str, timedelta] = {
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
}
_SHA256 = re.compile(r"(?<![0-9a-f])[0-9a-fA-F]{64}(?![0-9a-f])")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_RANGE = timedelta(days=366)
_MAX_CHECKPOINT_URLS = 100_000
PARSER_VERSION = "BINANCE_VISION_KLINE_CSV_V1"


class HistoricalBackfillError(RuntimeError):
    """Archive thiếu/sai contract hoặc vượt giới hạn backfill."""


class DatasetRightsError(PermissionError):
    """Quyền dataset không cho phép tác vụ yêu cầu."""


class ArchiveRightsPurpose(StrEnum):
    """Mục đích sử dụng được kiểm soát trước khi tải dataset."""

    PERSONAL_NONCOMMERCIAL_RESEARCH = "PERSONAL_NONCOMMERCIAL_RESEARCH"
    PRODUCT_OR_DEMO = "PRODUCT_OR_DEMO"
    COMMERCIAL_TRADING = "COMMERCIAL_TRADING"


@dataclass(frozen=True, slots=True)
class ArchiveRightsGrant:
    """Xác nhận rõ provider, mục đích và phiên bản terms được phép dùng."""

    provider: str
    purpose: ArchiveRightsPurpose
    terms_url: str
    terms_checked_at: datetime
    verified_at: datetime
    enabled: bool
    personal_noncommercial_acknowledged: bool
    approval_reference: str
    license_id: str = VISION_DATASET_LICENSE


def require_vision_archive_rights(
    grant: ArchiveRightsGrant | None,
    *,
    purpose: ArchiveRightsPurpose,
    now: datetime,
    max_terms_age: timedelta = timedelta(days=30),
) -> ArchiveRightsGrant:
    """Chỉ cho tải khi grant tường minh cho nghiên cứu phi thương mại cá nhân còn mới."""
    _require_utc(now, "now")
    if max_terms_age <= timedelta(0):
        raise ValueError("max_terms_age phải lớn hơn 0.")
    if grant is None or not grant.enabled:
        raise DatasetRightsError("Nguồn Binance Vision chưa được bật theo rights policy.")
    _require_utc(grant.terms_checked_at, "terms_checked_at")
    _require_utc(grant.verified_at, "verified_at")
    if grant.provider != "BINANCE_VISION" or grant.terms_url != VISION_TERMS_URL:
        raise DatasetRightsError("Rights grant không khớp provider/terms Binance Vision.")
    if grant.license_id != VISION_DATASET_LICENSE:
        raise DatasetRightsError("Rights grant license không khớp điều khoản Binance Vision.")
    if purpose is not ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH:
        raise DatasetRightsError(
            "Terms Binance Vision hiện không cho phép mục đích sản phẩm/demo hoặc thương mại."
        )
    if grant.purpose is not purpose or not grant.personal_noncommercial_acknowledged:
        raise DatasetRightsError(
            "Dataset Vision chỉ được dùng cho nghiên cứu phi thương mại cá nhân đã xác nhận."
        )
    if not grant.approval_reference.strip():
        raise DatasetRightsError("Rights grant cần mã phê duyệt/ghi nhận mục đích.")
    if grant.verified_at > now or grant.terms_checked_at > now:
        raise DatasetRightsError("Rights grant không được ghi nhận ở thời điểm tương lai.")
    if now - grant.terms_checked_at > max_terms_age:
        raise DatasetRightsError("Terms dataset đã cũ; cần kiểm tra lại trước khi tải.")
    if now - grant.verified_at > max_terms_age:
        raise DatasetRightsError("Xác nhận quyền dataset đã cũ; cần rà soát lại trước khi tải.")
    return grant


@dataclass(frozen=True, slots=True)
class BackfillRequest:
    """Một khoảng nạp klines UTC, tối đa một năm và không bao gồm bar chưa đóng."""

    symbol: str
    interval: str
    start: datetime
    end: datetime
    now: datetime
    archive_period: str = "daily"

    def __post_init__(self) -> None:
        if _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("symbol phải là mã USD-M viết hoa, không chứa separator.")
        if self.interval not in _INTERVALS:
            raise ValueError("interval chưa được hỗ trợ trong parser backfill.")
        if self.archive_period not in {"daily", "monthly"}:
            raise ValueError("archive_period chỉ nhận daily hoặc monthly.")
        for name, value in (("start", self.start), ("end", self.end), ("now", self.now)):
            _require_utc(value, name)
        if self.start >= self.end:
            raise ValueError("start phải trước end.")
        if self.end > self.now:
            raise ValueError("end không được nằm trong tương lai; chỉ nạp bar đã đóng.")
        if self.end - self.start > _MAX_RANGE:
            raise ValueError("Mỗi backfill request tối đa 366 ngày.")
        interval = _INTERVALS[self.interval]
        if not _is_aligned(self.start, interval) or not _is_aligned(self.end, interval):
            raise ValueError("start/end phải căn theo biên interval UTC.")


def build_trailing_365_day_request(
    symbol: str,
    interval: str,
    *,
    now: datetime,
    archive_period: str = "daily",
) -> BackfillRequest:
    """Tạo request 365 ngày tới bar đóng gần nhất, tính thuần từ UTC ``now``."""
    _require_utc(now, "now")
    duration = _INTERVALS.get(interval)
    if duration is None:
        raise ValueError("interval chưa được hỗ trợ trong parser backfill.")
    elapsed = now - _EPOCH
    elapsed_microseconds = (
        elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    )
    interval_microseconds = (
        duration.days * 86_400_000_000 + duration.seconds * 1_000_000 + duration.microseconds
    )
    floored_microseconds = elapsed_microseconds // interval_microseconds * interval_microseconds
    end = _EPOCH + timedelta(microseconds=floored_microseconds)
    start = end - timedelta(days=365)
    return BackfillRequest(
        symbol=symbol,
        interval=interval,
        start=start,
        end=end,
        now=now,
        archive_period=archive_period,
    )


@dataclass(frozen=True, slots=True)
class ArchiveCheckpoint:
    """Receipt để resume theo từng archive đã xác minh."""

    url: str
    publisher_sha256: str
    downloaded_sha256: str
    byte_length: int
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if not self.url.strip() or self.byte_length < 1:
            raise ValueError("Archive checkpoint cần URL và byte_length dương.")
        for name, value in (
            ("publisher_sha256", self.publisher_sha256),
            ("downloaded_sha256", self.downloaded_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} phải là SHA-256 chữ thường.")
        if self.publisher_sha256 != self.downloaded_sha256:
            raise ValueError("Publisher checksum và SHA-256 tải về không khớp.")
        _require_utc(self.retrieved_at, "retrieved_at")


@dataclass(frozen=True, slots=True)
class BackfillCheckpoint:
    """Cursor có thể lưu bên ngoài để tiếp tục tải batch tiếp theo."""

    request_fingerprint: str | None = None
    completed: tuple[ArchiveCheckpoint, ...] = ()
    next_index: int = 0

    def __post_init__(self) -> None:
        if self.next_index < 0 or self.next_index > _MAX_CHECKPOINT_URLS:
            raise ValueError("next_index vượt giới hạn checkpoint.")
        if (
            self.request_fingerprint is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.request_fingerprint) is None
        ):
            raise ValueError("request_fingerprint phải là SHA-256 dạng hex.")
        if self.next_index > 0 and self.request_fingerprint is None:
            raise ValueError("Checkpoint đã tiến triển cần request_fingerprint.")
        if self.next_index != len(self.completed):
            raise ValueError("next_index phải bằng số archive đã hoàn tất.")
        urls = tuple(item.url for item in self.completed)
        if len(urls) != len(set(urls)):
            raise ValueError("Checkpoint không được chứa archive URL trùng.")

    @property
    def completed_urls(self) -> tuple[str, ...]:
        return tuple(item.url for item in self.completed)

    def to_json(self) -> bytes:
        """Serialize cursor để caller ghi atomically vào manifest/workspace riêng."""
        payload = {
            "request_fingerprint": self.request_fingerprint,
            "next_index": self.next_index,
            "completed": [
                {
                    "url": item.url,
                    "publisher_sha256": item.publisher_sha256,
                    "downloaded_sha256": item.downloaded_sha256,
                    "byte_length": item.byte_length,
                    "retrieved_at": item.retrieved_at.isoformat(),
                }
                for item in self.completed
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, payload: bytes, *, max_bytes: int = 4 * 1024 * 1024) -> BackfillCheckpoint:
        """Đọc cursor đã lưu với giới hạn kích thước và contract đóng."""
        if not payload or len(payload) > max_bytes:
            raise HistoricalBackfillError("Checkpoint rỗng hoặc vượt giới hạn byte.")
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict) or set(raw) != {
                "request_fingerprint",
                "next_index",
                "completed",
            }:
                raise ValueError("checkpoint fields không hợp lệ")
            completed_raw = raw["completed"]
            if not isinstance(completed_raw, list):
                raise ValueError("completed phải là danh sách")
            completed: list[ArchiveCheckpoint] = []
            for item in completed_raw:
                if not isinstance(item, dict) or set(item) != {
                    "url",
                    "publisher_sha256",
                    "downloaded_sha256",
                    "byte_length",
                    "retrieved_at",
                }:
                    raise ValueError("archive receipt fields không hợp lệ")
                completed.append(
                    ArchiveCheckpoint(
                        url=_json_string(item["url"], "url"),
                        publisher_sha256=_json_string(item["publisher_sha256"], "publisher_sha256"),
                        downloaded_sha256=_json_string(
                            item["downloaded_sha256"], "downloaded_sha256"
                        ),
                        byte_length=_json_int(item["byte_length"], "byte_length"),
                        retrieved_at=_parse_utc_datetime(item["retrieved_at"], "retrieved_at"),
                    )
                )
            fingerprint = raw["request_fingerprint"]
            if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                raise ValueError("request_fingerprint không hợp lệ")
            next_index = _json_int(raw["next_index"], "next_index")
            return cls(fingerprint, tuple(completed), next_index)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as error:
            raise HistoricalBackfillError("Checkpoint JSON sai cấu trúc.") from error


@dataclass(frozen=True, slots=True)
class BackfillRequestResult:
    """Một batch download cùng cursor kế tiếp và số archive còn lại."""

    archives: tuple[VisionArchive, ...]
    checkpoint: BackfillCheckpoint
    total_archives: int

    @property
    def is_complete(self) -> bool:
        return self.checkpoint.next_index >= self.total_archives


@dataclass(frozen=True, slots=True)
class VisionArchive:
    """Archive ZIP đã đối chiếu SHA-256 với sidecar của nhà cung cấp."""

    url: str
    checksum: str
    payload: bytes
    retrieved_at: datetime
    publisher_checksum: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.retrieved_at, "retrieved_at")
        actual = hashlib.sha256(self.payload).hexdigest()
        if actual != self.checksum:
            raise ValueError("checksum không khớp payload archive.")
        if self.publisher_checksum is not None and self.publisher_checksum != actual:
            raise ValueError("publisher_checksum không khớp payload archive.")


@dataclass(frozen=True, slots=True)
class ArchivedKline:
    """Một bar CSV gốc; timestamp nhận/available được tạo rõ ở bước replay."""

    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    open_time_ms: int
    checksum: str

    def __post_init__(self) -> None:
        if _SYMBOL.fullmatch(self.symbol) is None or self.interval not in _INTERVALS:
            raise ValueError("Kline cần symbol và interval được hỗ trợ.")
        _require_utc(self.open_time, "open_time")
        _require_utc(self.close_time, "close_time")
        if _from_milliseconds(self.open_time_ms) != self.open_time:
            raise ValueError("open_time_ms không khớp open_time.")
        if self.close_time != self.open_time + _INTERVALS[self.interval] - timedelta(
            milliseconds=1
        ):
            raise ValueError("close_time không khớp interval.")
        if re.fullmatch(r"[0-9a-f]{64}", self.checksum) is None:
            raise ValueError("checksum kline phải là SHA-256 dạng hex.")
        _validate_kline_values(
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.quote_volume,
        )

    def to_simulated_backtest_bar(
        self,
        *,
        feed_delay: timedelta = timedelta(0),
        source: str = VISION_SOURCE,
    ) -> OHLCVBar:
        """Dựng OHLCV có clock mô phỏng; không giả làm thời điểm archive được tải thật."""
        if feed_delay < timedelta(0) or feed_delay > timedelta(hours=1):
            raise ValueError("feed_delay mô phỏng phải nằm trong 0–1 giờ.")
        if source != VISION_SOURCE:
            raise ValueError("source archive không hợp lệ.")
        available_at = self.close_time + feed_delay
        return OHLCVBar(
            symbol=self.symbol,
            timeframe=self.interval,
            open_time=self.open_time,
            close_time=self.close_time,
            event_at=self.close_time,
            received_at=self.close_time,
            available_at=available_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            quote_volume=self.quote_volume,
            is_closed=True,
            source=source,
            source_cursor=str(self.open_time_ms),
            checksum=self.checksum,
            raw_reference=None,
        )


@dataclass(frozen=True, slots=True)
class KlineCoverage:
    """Coverage report phân biệt gap, trùng và row ngoài khoảng yêu cầu."""

    range_start: datetime
    range_end: datetime
    expected_count: int
    actual_count: int
    missing_open_times: tuple[datetime, ...]
    duplicate_open_times: tuple[datetime, ...]
    unexpected_open_times: tuple[datetime, ...]

    @property
    def is_complete(self) -> bool:
        return (
            self.expected_count == self.actual_count
            and not self.missing_open_times
            and not self.duplicate_open_times
            and not self.unexpected_open_times
        )


@dataclass(frozen=True, slots=True)
class BackfillManifest:
    """Lineage tĩnh cho kế hoạch/tập bar, gồm quyền dữ liệu và chất lượng."""

    manifest_sha256: str
    dataset_sha256: str
    status: str
    provider: str
    purpose: ArchiveRightsPurpose
    terms_url: str
    license_id: str
    terms_checked_at: datetime
    approval_reference: str
    symbol: str
    interval: str
    start: datetime
    end: datetime
    archive_period: str
    generated_at: datetime
    parser_version: str
    assumed_feed_delay_ms: int
    archive_receipts: tuple[ArchiveCheckpoint, ...]
    coverage: KlineCoverage

    def __post_init__(self) -> None:
        if self.status not in {"IN_PROGRESS", "INCOMPLETE", "COMPLETE"}:
            raise ValueError("Manifest status không hợp lệ.")
        if self.status == "COMPLETE" and not self.coverage.is_complete:
            raise ValueError("Manifest COMPLETE phải có coverage đầy đủ.")
        for name, timestamp in (
            ("terms_checked_at", self.terms_checked_at),
            ("start", self.start),
            ("end", self.end),
            ("generated_at", self.generated_at),
        ):
            _require_utc(timestamp, name)
        for name, digest in (
            ("manifest_sha256", self.manifest_sha256),
            ("dataset_sha256", self.dataset_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError(f"{name} phải là SHA-256 dạng hex.")

    @property
    def can_ingest(self) -> bool:
        return self.status == "COMPLETE"

    def to_json(self) -> bytes:
        """Serialize manifest canonical để persist cùng bộ dữ liệu backtest."""
        payload = _backfill_manifest_payload(self, include_hash=True)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ArchiveRepository(Protocol):
    """Phần repository cần thiết để nối raw archive và bars bất biến."""

    async def append_raw_reference(self, **kwargs: object) -> MarketAppendResult: ...

    async def append_bar(self, **kwargs: object) -> MarketAppendResult: ...


class BinanceVisionArchiveDownloader:
    """Tải tuần tự, có giới hạn kích thước và resume; không bật nếu thiếu rights grant."""

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_archive_bytes: int = 256 * 1024 * 1024,
        max_archives_per_call: int = 31,
        max_attempts: int = 3,
        max_retry_after_seconds: float = 30.0,
    ) -> None:
        if max_archive_bytes < 1 or max_archives_per_call < 1 or max_attempts < 1:
            raise ValueError("Download limits phải là số dương.")
        if max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds không được âm.")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._max_archive_bytes = max_archive_bytes
        self._max_archives_per_call = max_archives_per_call
        self._max_attempts = max_attempts
        self._max_retry_after_seconds = max_retry_after_seconds
        self._client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            auth=None,
            headers={"Accept": "application/zip, text/plain", "User-Agent": "Crypidian/0.1"},
        )

    async def __aenter__(self) -> BinanceVisionArchiveDownloader:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def download(
        self,
        request: BackfillRequest,
        *,
        grant: ArchiveRightsGrant | None,
        checkpoint: BackfillCheckpoint,
        purpose: ArchiveRightsPurpose = ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
    ) -> BackfillRequestResult:
        actual_now = self._clock()
        _require_utc(actual_now, "clock")
        if request.now > actual_now or request.end > actual_now:
            raise HistoricalBackfillError(
                "Request vượt thời điểm hiện tại; từ chối tải bar tương lai."
            )
        require_vision_archive_rights(grant, purpose=purpose, now=actual_now)
        urls = build_vision_archive_urls(request)
        if len(urls) > _MAX_CHECKPOINT_URLS:
            raise HistoricalBackfillError("Backfill cần chia nhỏ: số archive vượt giới hạn.")
        fingerprint = backfill_request_fingerprint(request)
        if checkpoint.request_fingerprint not in {None, fingerprint}:
            raise HistoricalBackfillError(
                "Checkpoint thuộc request khác; từ chối resume nhầm dataset."
            )
        if checkpoint.next_index > 0 and checkpoint.request_fingerprint != fingerprint:
            raise HistoricalBackfillError(
                "Checkpoint đã tiến triển thiếu fingerprint đúng request."
            )
        if checkpoint.next_index > len(urls):
            raise HistoricalBackfillError("Checkpoint index vượt số archive của request.")
        expected_prefix = tuple(urls[: checkpoint.next_index])
        if checkpoint.completed_urls != expected_prefix:
            raise HistoricalBackfillError("Checkpoint archive không khớp thứ tự request.")
        stop_index = min(len(urls), checkpoint.next_index + self._max_archives_per_call)
        downloaded: list[VisionArchive] = []
        completed = list(checkpoint.completed)
        for url in urls[checkpoint.next_index : stop_index]:
            checksum_body = await self._get_limited(f"{url}.CHECKSUM", text=True)
            publisher_checksum = parse_vision_checksum(checksum_body)
            payload = await self._get_limited(url, text=False)
            downloaded_checksum = hashlib.sha256(payload).hexdigest()
            if downloaded_checksum != publisher_checksum:
                raise HistoricalBackfillError("SHA-256 tải về không khớp checksum nhà cung cấp.")
            retrieved_at = self._clock()
            _require_utc(retrieved_at, "clock")
            archive = VisionArchive(
                url=url,
                checksum=downloaded_checksum,
                publisher_checksum=publisher_checksum,
                payload=payload,
                retrieved_at=retrieved_at,
            )
            downloaded.append(archive)
            completed.append(
                ArchiveCheckpoint(
                    url=url,
                    publisher_sha256=publisher_checksum,
                    downloaded_sha256=downloaded_checksum,
                    byte_length=len(payload),
                    retrieved_at=retrieved_at,
                )
            )
        next_checkpoint = BackfillCheckpoint(
            request_fingerprint=fingerprint,
            completed=tuple(completed),
            next_index=len(completed),
        )
        return BackfillRequestResult(tuple(downloaded), next_checkpoint, len(urls))

    async def _get_limited(self, url: str, *, text: bool) -> bytes:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != VISION_HOST
            or parsed.port not in {None, 443}
        ):
            raise HistoricalBackfillError("Chỉ cho phép HTTPS trên host data.binance.vision.")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise HistoricalBackfillError("URL archive chứa thành phần không được phép.")
        for attempt in range(self._max_attempts):
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        raise HistoricalBackfillError(
                            "Từ chối redirect khỏi host archive đã cho phép."
                        )
                    if response.status_code == 429 or response.status_code == 418:
                        delay_raw = response.headers.get("Retry-After", "")
                        try:
                            delay = float(delay_raw)
                        except ValueError:
                            delay = min(0.25 * (2**attempt), self._max_retry_after_seconds)
                        if (
                            delay > self._max_retry_after_seconds
                            or attempt + 1 >= self._max_attempts
                        ):
                            raise HistoricalBackfillError(
                                "Archive rate limit; dừng để không vượt policy."
                            )
                        await self._sleep(max(0.0, delay))
                        continue
                    if response.status_code >= 500 and attempt + 1 < self._max_attempts:
                        await self._sleep(min(0.25 * (2**attempt), self._max_retry_after_seconds))
                        continue
                    if not response.is_success:
                        raise HistoricalBackfillError(f"Archive trả HTTP {response.status_code}.")
                    content_length = response.headers.get("Content-Length")
                    declared_length: int | None = None
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as error:
                            raise HistoricalBackfillError("Content-Length không hợp lệ.") from error
                        if declared_length < 0:
                            raise HistoricalBackfillError("Content-Length không hợp lệ.")
                    limit = 64 * 1024 if text else self._max_archive_bytes
                    if declared_length is not None and declared_length > limit:
                        raise HistoricalBackfillError("Response vượt giới hạn byte đã cấu hình.")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > limit:
                            raise HistoricalBackfillError(
                                "Response vượt giới hạn byte đã cấu hình."
                            )
                    return bytes(body)
            except (httpx2.TimeoutException, httpx2.NetworkError) as error:
                if attempt + 1 >= self._max_attempts:
                    raise HistoricalBackfillError(
                        "Tải archive gặp lỗi mạng sau retry giới hạn."
                    ) from error
                await self._sleep(min(0.25 * (2**attempt), self._max_retry_after_seconds))
                continue
        raise AssertionError("Retry loop kết thúc ngoài dự kiến.")


def build_vision_archive_urls(request: BackfillRequest) -> tuple[str, ...]:
    """Tạo URL official daily/monthly cho đúng symbol, interval và khoảng UTC."""
    folder = (
        f"{VISION_DATASET_ROOT}/{request.archive_period}/klines/{request.symbol}/{request.interval}"
    )
    if request.archive_period == "daily":
        day = request.start.date()
        last_day = (request.end - timedelta(milliseconds=1)).date()
        dates: list[date] = []
        while day <= last_day:
            dates.append(day)
            day += timedelta(days=1)
        names = tuple(f"{request.symbol}-{request.interval}-{day.isoformat()}.zip" for day in dates)
    else:
        month = date(request.start.year, request.start.month, 1)
        last = (request.end - timedelta(milliseconds=1)).date()
        months: list[date] = []
        while month <= date(last.year, last.month, 1):
            months.append(month)
            month = date(
                month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1
            )
        names = tuple(f"{request.symbol}-{request.interval}-{month:%Y-%m}.zip" for month in months)
    return tuple(f"{folder}/{name}" for name in names)


def backfill_request_fingerprint(request: BackfillRequest) -> str:
    """Hash deterministic của symbol, khoảng UTC, interval và URLs archive."""
    return _request_fingerprint(request, build_vision_archive_urls(request))


def parse_vision_checksum(payload: bytes) -> str:
    """Đọc một SHA-256 từ sidecar official; từ chối sidecar mơ hồ."""
    try:
        text = payload.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise HistoricalBackfillError("Sidecar checksum không phải ASCII.") from error
    matches = _SHA256.findall(text)
    if len(matches) != 1:
        raise HistoricalBackfillError("Sidecar phải chứa đúng một giá trị SHA-256.")
    digest = matches[0]
    if not isinstance(digest, str):
        raise HistoricalBackfillError("Sidecar SHA-256 không hợp lệ.")
    return digest.lower()


def parse_kline_archive(
    archive: VisionArchive,
    *,
    symbol: str,
    interval: str,
    max_uncompressed_bytes: int = 512 * 1024 * 1024,
    max_rows: int = 2_000_000,
    max_members: int = 8,
) -> tuple[ArchivedKline, ...]:
    """Đọc CSV 12 trường, giữ Decimal, kiểm tra ZIP path và giới hạn giải nén."""
    if _SYMBOL.fullmatch(symbol) is None or interval not in _INTERVALS:
        raise ValueError("symbol hoặc interval không hợp lệ.")
    archive_source, archive_symbol, archive_interval, expected_csv_name = _archive_identity(
        archive.url
    )
    if archive_symbol != symbol or archive_interval != interval:
        raise HistoricalBackfillError("Archive URL không khớp symbol/interval được parse.")
    if archive_source != VISION_SOURCE:
        raise HistoricalBackfillError("Archive không phải USD-M klines.")
    actual_checksum = hashlib.sha256(archive.payload).hexdigest()
    if actual_checksum != archive.checksum:
        raise HistoricalBackfillError("Checksum archive đổi sau khi tải.")
    try:
        with zipfile.ZipFile(io.BytesIO(archive.payload)) as zipped:
            members = zipped.infolist()
            if not members or len(members) > max_members:
                raise HistoricalBackfillError("Archive có số member rỗng hoặc vượt giới hạn.")
            csv_members = []
            total_uncompressed = 0
            seen_names: set[str] = set()
            for member in members:
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.filename
                    or member.filename in seen_names
                ):
                    raise HistoricalBackfillError("Archive có đường dẫn member không an toàn.")
                seen_names.add(member.filename)
                total_uncompressed += member.file_size
                if total_uncompressed > max_uncompressed_bytes:
                    raise HistoricalBackfillError("Archive vượt giới hạn kích thước giải nén.")
                if not member.is_dir() and path.suffix.lower() == ".csv":
                    csv_members.append(member)
            if len(csv_members) != 1:
                raise HistoricalBackfillError("Archive cần đúng một file CSV.")
            csv_member = csv_members[0]
            if PurePosixPath(csv_member.filename).name != expected_csv_name:
                raise HistoricalBackfillError("Tên CSV bên trong ZIP không khớp archive URL.")
            rows: list[ArchivedKline] = []
            with (
                zipped.open(csv_member) as compressed,
                io.TextIOWrapper(compressed, encoding="utf-8-sig", newline="") as stream,
            ):
                reader = csv.reader(stream)
                for row_number, fields in enumerate(reader, start=1):
                    if row_number > max_rows:
                        raise HistoricalBackfillError("Archive vượt giới hạn số bar.")
                    parsed_row = _parse_kline_row(
                        fields, row_number=row_number, symbol=symbol, interval=interval
                    )
                    if rows and parsed_row.open_time <= rows[-1].open_time:
                        raise HistoricalBackfillError(
                            f"Kline dòng {row_number} không tăng theo thời gian."
                        )
                    rows.append(parsed_row)
            if not rows:
                raise HistoricalBackfillError("Archive CSV không có bar.")
            return tuple(rows)
    except zipfile.BadZipFile as error:
        raise HistoricalBackfillError("Payload không phải ZIP hợp lệ.") from error


def inspect_kline_coverage(
    bars: Iterable[ArchivedKline],
    *,
    start: datetime,
    end: datetime,
    interval: str,
    max_expected_bars: int = 5_000_000,
) -> KlineCoverage:
    """So sánh đúng open-time grid; giữ gap/trùng/ngoài-range để người dùng xử lý."""
    _require_utc(start, "start")
    _require_utc(end, "end")
    if start >= end or interval not in _INTERVALS:
        raise ValueError("Khoảng coverage hoặc interval không hợp lệ.")
    rows = tuple(bars)
    if any(bar.interval != interval for bar in rows):
        raise ValueError("Coverage bars chứa interval khác khoảng được yêu cầu.")
    if len({bar.symbol for bar in rows}) > 1:
        raise ValueError("Coverage bars chứa nhiều symbol.")
    duration = _INTERVALS[interval]
    if not _is_aligned(start, duration) or not _is_aligned(end, duration):
        raise ValueError("Khoảng coverage phải căn theo biên interval UTC.")
    expected_count = int((end - start) // duration)
    if expected_count > max_expected_bars:
        raise HistoricalBackfillError("Coverage request vượt giới hạn số bar dự kiến.")
    expected = {start + duration * index for index in range(expected_count)}
    grouped: dict[datetime, int] = {}
    for bar in rows:
        grouped[bar.open_time] = grouped.get(bar.open_time, 0) + 1
    duplicates = tuple(sorted(open_time for open_time, count in grouped.items() if count > 1))
    actual = set(grouped)
    return KlineCoverage(
        range_start=start,
        range_end=end,
        expected_count=expected_count,
        actual_count=len(actual),
        missing_open_times=tuple(sorted(expected - actual)),
        duplicate_open_times=duplicates,
        unexpected_open_times=tuple(sorted(actual - expected)),
    )


def build_backfill_manifest(
    request: BackfillRequest,
    *,
    grant: ArchiveRightsGrant | None,
    checkpoint: BackfillCheckpoint,
    bars: Iterable[ArchivedKline],
    generated_at: datetime,
    feed_delay: timedelta = timedelta(0),
    parser_version: str = PARSER_VERSION,
    purpose: ArchiveRightsPurpose = ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
) -> BackfillManifest:
    """Tạo manifest băm xác định, kể cả trạng thái đang tải hoặc thiếu coverage."""
    _require_utc(generated_at, "generated_at")
    if request.now > generated_at or request.end > generated_at:
        raise HistoricalBackfillError("Manifest không được xác nhận dữ liệu tương lai.")
    require_vision_archive_rights(grant, purpose=purpose, now=generated_at)
    if feed_delay < timedelta(0) or feed_delay > timedelta(hours=1):
        raise ValueError("feed_delay mô phỏng phải nằm trong 0–1 giờ.")
    if not parser_version.strip():
        raise ValueError("parser_version không được để trống.")
    urls = build_vision_archive_urls(request)
    fingerprint = backfill_request_fingerprint(request)
    if checkpoint.request_fingerprint not in {None, fingerprint}:
        raise HistoricalBackfillError("Checkpoint thuộc request khác.")
    if checkpoint.next_index > 0 and checkpoint.request_fingerprint != fingerprint:
        raise HistoricalBackfillError("Checkpoint đã tiến triển thiếu fingerprint đúng request.")
    if checkpoint.next_index > len(urls):
        raise HistoricalBackfillError("Checkpoint index vượt số archive.")
    if checkpoint.completed_urls != tuple(urls[: checkpoint.next_index]):
        raise HistoricalBackfillError("Checkpoint receipts không theo thứ tự archive.")
    if any(item.retrieved_at > generated_at for item in checkpoint.completed):
        raise HistoricalBackfillError("Manifest không được chứa receipt tải ở tương lai.")
    archive_rows = tuple(bars)
    if any(
        bar.symbol != request.symbol or bar.interval != request.interval for bar in archive_rows
    ):
        raise HistoricalBackfillError("Manifest bars không khớp symbol/interval yêu cầu.")
    coverage = inspect_kline_coverage(
        archive_rows,
        start=request.start,
        end=request.end,
        interval=request.interval,
    )
    if checkpoint.next_index < len(urls):
        status = "IN_PROGRESS"
    elif coverage.is_complete:
        status = "COMPLETE"
    else:
        status = "INCOMPLETE"
    dataset_sha256 = hashlib.sha256(
        "\n".join(f"{item.url} {item.downloaded_sha256}" for item in checkpoint.completed).encode(
            "utf-8"
        )
    ).hexdigest()
    manifest = BackfillManifest(
        manifest_sha256="0" * 64,
        dataset_sha256=dataset_sha256,
        status=status,
        provider="BINANCE_VISION",
        purpose=purpose,
        terms_url=grant.terms_url if grant is not None else "",
        license_id=grant.license_id if grant is not None else "",
        terms_checked_at=grant.terms_checked_at if grant is not None else generated_at,
        approval_reference=grant.approval_reference if grant is not None else "",
        symbol=request.symbol,
        interval=request.interval,
        start=request.start,
        end=request.end,
        archive_period=request.archive_period,
        generated_at=generated_at,
        parser_version=parser_version,
        assumed_feed_delay_ms=int(feed_delay.total_seconds() * 1000),
        archive_receipts=checkpoint.completed,
        coverage=coverage,
    )
    digest = hashlib.sha256(
        json.dumps(
            _backfill_manifest_payload(manifest, include_hash=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return BackfillManifest(
        manifest_sha256=digest,
        dataset_sha256=manifest.dataset_sha256,
        status=manifest.status,
        provider=manifest.provider,
        purpose=manifest.purpose,
        terms_url=manifest.terms_url,
        license_id=manifest.license_id,
        terms_checked_at=manifest.terms_checked_at,
        approval_reference=manifest.approval_reference,
        symbol=manifest.symbol,
        interval=manifest.interval,
        start=manifest.start,
        end=manifest.end,
        archive_period=manifest.archive_period,
        generated_at=manifest.generated_at,
        parser_version=manifest.parser_version,
        assumed_feed_delay_ms=manifest.assumed_feed_delay_ms,
        archive_receipts=manifest.archive_receipts,
        coverage=manifest.coverage,
    )


async def append_archive_to_market_repository(
    repository: ArchiveRepository,
    *,
    workspace_id: UUID,
    instrument_id: UUID,
    archive: VisionArchive,
    bars: Iterable[ArchivedKline],
    raw_object_key: str,
    grant: ArchiveRightsGrant | None,
    request_now: datetime,
    coverage: KlineCoverage,
    feed_delay: timedelta = timedelta(0),
    purpose: ArchiveRightsPurpose = ArchiveRightsPurpose.PERSONAL_NONCOMMERCIAL_RESEARCH,
    source: str | None = None,
) -> tuple[MarketAppendResult, ...]:
    """Append raw archive và bars sau rights/coverage gate, giữ clock mô phỏng tường minh."""
    require_vision_archive_rights(grant, purpose=purpose, now=request_now)
    _require_utc(request_now, "request_now")
    if archive.retrieved_at > request_now:
        raise HistoricalBackfillError(
            "Archive retrieval không được ghi nhận ở thời điểm tương lai."
        )
    if archive.publisher_checksum is None or archive.publisher_checksum != archive.checksum:
        raise HistoricalBackfillError(
            "Không append archive nếu thiếu checksum publisher đã xác minh."
        )
    if not raw_object_key.strip():
        raise ValueError("raw_object_key phải trỏ tới archive đã lưu bất biến.")
    bar_rows = tuple(bars)
    if not bar_rows:
        raise HistoricalBackfillError("Không append archive không có bar.")
    symbols = {bar.symbol for bar in bar_rows}
    intervals = {bar.interval for bar in bar_rows}
    if len(symbols) != 1 or len(intervals) != 1:
        raise HistoricalBackfillError("Archive chứa nhiều symbol hoặc interval.")
    _, archive_symbol, archive_interval, _ = _archive_identity(archive.url)
    if symbols != {archive_symbol} or intervals != {archive_interval}:
        raise HistoricalBackfillError("Bars không khớp symbol/interval trong archive URL.")
    verified_coverage = inspect_kline_coverage(
        bar_rows,
        start=coverage.range_start,
        end=coverage.range_end,
        interval=bar_rows[0].interval,
    )
    if coverage != verified_coverage or not verified_coverage.is_complete:
        raise HistoricalBackfillError("Không append dataset có gap, trùng hoặc bar ngoài range.")
    archive_source = _archive_source(archive.url)
    actual_source = source or archive_source
    if actual_source != archive_source:
        raise HistoricalBackfillError("Source ingestion không khớp loại archive trong URL.")
    if actual_source != VISION_SOURCE:
        raise ValueError("source archive không hợp lệ.")
    raw = await repository.append_raw_reference(
        workspace_id=workspace_id,
        source=actual_source,
        idempotency_key=archive.url,
        object_key=raw_object_key,
        checksum=archive.checksum,
        byte_length=len(archive.payload),
        content_type="application/zip",
    )
    output: list[MarketAppendResult] = []
    for archived_bar in bar_rows:
        bar = archived_bar.to_simulated_backtest_bar(feed_delay=feed_delay, source=actual_source)
        if bar.open_time < coverage.range_start or bar.open_time >= coverage.range_end:
            raise HistoricalBackfillError("Bar ngoài khoảng coverage không được append.")
        logical_key = f"{bar.symbol}:{bar.timeframe}:{bar.open_time.isoformat()}"
        idempotency_key = f"{archive.url}:{archived_bar.open_time_ms}"
        record_checksum = _kline_checksum(archived_bar)
        bar = OHLCVBar(
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            open_time=bar.open_time,
            close_time=bar.close_time,
            event_at=bar.event_at,
            received_at=bar.received_at,
            available_at=bar.available_at,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            quote_volume=bar.quote_volume,
            is_closed=True,
            source=actual_source,
            source_cursor=f"{archive.url}:{archived_bar.open_time_ms}",
            checksum=record_checksum,
            raw_reference=raw_object_key,
        )
        version = MarketRecordVersion(
            record_id=uuid5(NAMESPACE_URL, f"{actual_source}:{idempotency_key}:{record_checksum}"),
            stream=MarketStream(source=actual_source, symbol=bar.symbol, timeframe=bar.timeframe),
            logical_key=logical_key,
            idempotency_key=idempotency_key,
            revision=1,
            content_checksum=record_checksum,
            event_at=bar.event_at,
            received_at=bar.received_at,
            available_at=bar.available_at,
        )
        output.append(
            await repository.append_bar(
                workspace_id=workspace_id,
                instrument_id=instrument_id,
                bar=bar,
                version=version,
                raw_reference_id=raw.record_id,
            )
        )
    return tuple(output)


def _parse_kline_row(
    fields: list[str], *, row_number: int, symbol: str, interval: str
) -> ArchivedKline:
    if len(fields) != 12:
        raise HistoricalBackfillError(f"Kline dòng {row_number} phải có đúng 12 cột.")
    try:
        open_ms = int(fields[0])
        open_time = _from_milliseconds(open_ms)
        close_time = _from_milliseconds(int(fields[6]))
        open_price, high, low, close, volume = (Decimal(fields[index]) for index in range(1, 6))
        quote_volume = Decimal(fields[7])
    except (ValueError, InvalidOperation, IndexError, OverflowError) as error:
        raise HistoricalBackfillError(
            f"Kline dòng {row_number} có timestamp/Decimal sai."
        ) from error
    _validate_kline_values(open_price, high, low, close, volume, quote_volume)
    expected_close = open_time + _INTERVALS[interval] - timedelta(milliseconds=1)
    if close_time != expected_close:
        raise HistoricalBackfillError(f"Kline dòng {row_number} có close time không khớp interval.")
    if not _is_aligned(open_time, _INTERVALS[interval]):
        raise HistoricalBackfillError(f"Kline dòng {row_number} lệch lưới UTC của interval.")
    canonical = ",".join(fields).encode("utf-8")
    return ArchivedKline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        quote_volume=quote_volume,
        open_time_ms=open_ms,
        checksum=hashlib.sha256(canonical).hexdigest(),
    )


def _validate_kline_values(
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    quote_volume: Decimal,
) -> None:
    values = (open_price, high, low, close, volume, quote_volume)
    if any(not value.is_finite() for value in values):
        raise HistoricalBackfillError("Kline phải chứa Decimal hữu hạn.")
    if (
        any(value <= 0 for value in (open_price, high, low, close))
        or volume < 0
        or quote_volume < 0
    ):
        raise HistoricalBackfillError("Kline có giá không dương hoặc volume âm.")
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        raise HistoricalBackfillError("Kline vi phạm quan hệ OHLC.")


def _from_milliseconds(value: int) -> datetime:
    if value < 0:
        raise HistoricalBackfillError("Timestamp millisecond không được âm.")
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError as error:
        raise HistoricalBackfillError("Timestamp nằm ngoài miền hỗ trợ.") from error


def _is_aligned(value: datetime, interval: timedelta) -> bool:
    since_epoch = value - _EPOCH
    value_microseconds = (
        since_epoch.days * 86_400_000_000
        + since_epoch.seconds * 1_000_000
        + since_epoch.microseconds
    )
    interval_microseconds = (
        interval.days * 86_400_000_000 + interval.seconds * 1_000_000 + interval.microseconds
    )
    return value_microseconds % interval_microseconds == 0


def _json_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} phải là chuỗi")
    return value


def _json_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} phải là số nguyên")
    return value


def _parse_utc_datetime(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_json_string(value, name))
    except ValueError as error:
        raise ValueError(f"{name} phải là ISO-8601") from error
    _require_utc(parsed, name)
    return parsed


def _backfill_manifest_payload(
    manifest: BackfillManifest, *, include_hash: bool
) -> dict[str, object]:
    sample_limit = 100
    quality = {
        "range_start": manifest.coverage.range_start.isoformat(),
        "range_end": manifest.coverage.range_end.isoformat(),
        "expected_count": manifest.coverage.expected_count,
        "actual_count": manifest.coverage.actual_count,
        "complete": manifest.coverage.is_complete,
        "missing_count": len(manifest.coverage.missing_open_times),
        "missing_open_times_sample": [
            value.isoformat() for value in manifest.coverage.missing_open_times[:sample_limit]
        ],
        "missing_sample_truncated": len(manifest.coverage.missing_open_times) > sample_limit,
        "duplicate_count": len(manifest.coverage.duplicate_open_times),
        "duplicate_open_times_sample": [
            value.isoformat() for value in manifest.coverage.duplicate_open_times[:sample_limit]
        ],
        "duplicate_sample_truncated": len(manifest.coverage.duplicate_open_times) > sample_limit,
        "unexpected_count": len(manifest.coverage.unexpected_open_times),
        "unexpected_open_times_sample": [
            value.isoformat() for value in manifest.coverage.unexpected_open_times[:sample_limit]
        ],
        "unexpected_sample_truncated": len(manifest.coverage.unexpected_open_times) > sample_limit,
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": manifest.status,
        "provider": manifest.provider,
        "purpose": manifest.purpose.value,
        "rights": {
            "terms_url": manifest.terms_url,
            "license_id": manifest.license_id,
            "terms_checked_at": manifest.terms_checked_at.isoformat(),
            "approval_reference": manifest.approval_reference,
        },
        "request": {
            "symbol": manifest.symbol,
            "interval": manifest.interval,
            "start": manifest.start.isoformat(),
            "end": manifest.end.isoformat(),
            "archive_period": manifest.archive_period,
        },
        "generated_at": manifest.generated_at.isoformat(),
        "parser_version": manifest.parser_version,
        "assumed_feed_delay_ms": manifest.assumed_feed_delay_ms,
        "dataset_sha256": manifest.dataset_sha256,
        "archives": [
            {
                "url": item.url,
                "publisher_sha256": item.publisher_sha256,
                "downloaded_sha256": item.downloaded_sha256,
                "byte_length": item.byte_length,
                "retrieved_at": item.retrieved_at.isoformat(),
            }
            for item in manifest.archive_receipts
        ],
        "coverage": quality,
    }
    if include_hash:
        payload["manifest_sha256"] = manifest.manifest_sha256
    return payload


def _archive_source(url: str) -> str:
    return _archive_identity(url)[0]


def _archive_identity(url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != VISION_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HistoricalBackfillError("Archive URL không thuộc host Binance Vision.")
    parts = PurePosixPath(parsed.path).parts
    if len(parts) != 9 or parts[:4] != ("/", "data", "futures", "um") or parts[5] != "klines":
        raise HistoricalBackfillError("Archive URL không phải USD-M kline daily/monthly.")
    period, symbol, interval, filename = parts[4], parts[6], parts[7], parts[8]
    if period not in {"daily", "monthly"}:
        raise HistoricalBackfillError("Archive URL không phải USD-M kline daily/monthly.")
    if _SYMBOL.fullmatch(symbol) is None or interval not in _INTERVALS:
        raise HistoricalBackfillError("Archive URL có symbol/interval không hợp lệ.")
    name_pattern = (
        rf"{re.escape(symbol)}-{re.escape(interval)}-\d{{4}}-\d{{2}}-\d{{2}}\.zip"
        if period == "daily"
        else rf"{re.escape(symbol)}-{re.escape(interval)}-\d{{4}}-\d{{2}}\.zip"
    )
    if re.fullmatch(name_pattern, filename) is None:
        raise HistoricalBackfillError("Archive URL có tên file không khớp symbol/interval.")
    return VISION_SOURCE, symbol, interval, filename[:-4] + ".csv"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} phải có timezone UTC.")


def _request_fingerprint(request: BackfillRequest, urls: tuple[str, ...]) -> str:
    payload = "\n".join(
        (
            request.symbol,
            request.interval,
            request.start.isoformat(),
            request.end.isoformat(),
            request.archive_period,
            *urls,
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _kline_checksum(bar: ArchivedKline) -> str:
    canonical = "|".join(
        (
            bar.symbol,
            bar.interval,
            str(bar.open_time_ms),
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            str(bar.volume),
            str(bar.quote_volume),
        )
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
