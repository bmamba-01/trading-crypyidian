"""Kiểm định quyền và độ phủ trước khi chuẩn bị dữ liệu cho backtest."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    BACKTEST_RIGHTS_PROVIDERS,
    BINANCE_USDM_REST_PROVIDER,
    CRYPIDIAN_FIRST_PARTY_QA_PROVIDER,
    BackfillRightsEvidence,
    BackfillRightsPurpose,
    RestBackfillRequest,
    market_source_for_rights_provider,
)
from trading_bot.application.first_party_qa_dataset import (
    CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF,
    FirstPartyQADatasetRequest,
)
from trading_bot.domain.market.models import MarketBarSourceIdentity, OHLCVBar

_SHA256_LENGTH = 64


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


class BacktestDatasetNotReady(RuntimeError):
    """Manifest thiếu độ phủ để chuyển sang replay/backtest."""


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class BacktestDatasetManifest:
    """Snapshot tái lập về quyền, khoảng dữ liệu, warm-up và độ phủ nến."""

    request_fingerprint: str
    policy_fingerprint: str
    rights_provider: str
    rights_purpose: str
    rights_workspace_id: UUID
    terms_url: str
    rights_verified_at: datetime
    rights_checked_at: datetime | None
    rights_valid_until: datetime
    prepared_at: datetime
    symbol: str
    interval: str
    warmup_start: datetime
    evaluation_start: datetime
    range_end: datetime
    warmup_bar_count: int
    evaluation_bar_count: int
    warmup_observed_bar_count: int
    evaluation_observed_bar_count: int
    expected_bar_count: int
    bar_count: int
    missing_open_times: tuple[datetime, ...]
    bars_content_hash: str
    dataset_hash: str
    bar_identity_hash: str | None = None
    schema_version: int = 3

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {2, 3, 4}:
            raise ValueError("schema_version chỉ hỗ trợ 2, 3 hoặc 4.")
        for name in (
            "prepared_at",
            "rights_verified_at",
            "rights_valid_until",
            "warmup_start",
            "evaluation_start",
            "range_end",
        ):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.rights_checked_at is not None:
            object.__setattr__(
                self, "rights_checked_at", _utc(self.rights_checked_at, "rights_checked_at")
            )
        gaps = tuple(_utc(item, "missing_open_time") for item in self.missing_open_times)
        if gaps != tuple(sorted(set(gaps))):
            raise ValueError("missing_open_times phải duy nhất và tăng dần.")
        object.__setattr__(self, "missing_open_times", gaps)
        for name in (
            "request_fingerprint",
            "policy_fingerprint",
            "bars_content_hash",
            "dataset_hash",
        ):
            digest = getattr(self, name)
            if (
                not isinstance(digest, str)
                or len(digest) != _SHA256_LENGTH
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{name} phải là SHA-256 hex chữ thường.")
        if self.schema_version == 4:
            if not _is_sha256(self.bar_identity_hash):
                raise ValueError("bar_identity_hash phải là SHA-256 khi schema v4.")
        elif self.bar_identity_hash is not None:
            raise ValueError("Manifest v2/v3 không được có bar_identity_hash.")
        if self.warmup_start > self.evaluation_start or self.evaluation_start >= self.range_end:
            raise ValueError("Manifest cần khoảng evaluation không rỗng sau warm-up.")
        if type(self.rights_workspace_id) is not UUID:
            raise ValueError("rights_workspace_id phải là UUID.")
        for name in ("rights_provider", "rights_purpose", "terms_url", "symbol", "interval"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        if self.rights_provider not in BACKTEST_RIGHTS_PROVIDERS:
            raise ValueError("rights_provider nằm ngoài allowlist backtest.")
        if self.rights_purpose != BackfillRightsPurpose.BACKTEST_RESEARCH.value:
            raise ValueError("rights_purpose không được duyệt cho backtest research.")
        terms = urlsplit(self.terms_url)
        if terms.scheme != "https" or not terms.netloc:
            raise ValueError("terms_url phải là HTTPS đầy đủ.")
        if (
            self.rights_provider == CRYPIDIAN_FIRST_PARTY_QA_PROVIDER
            and self.terms_url != CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF
        ):
            raise ValueError("QA rights chỉ nhận tài liệu fixture first-party đã ghim.")
        if not self.rights_verified_at <= self.prepared_at < self.rights_valid_until:
            raise ValueError("Rights evidence không hiệu lực tại prepared_at.")
        if self.rights_checked_at is not None and not (
            self.rights_verified_at <= self.rights_checked_at < self.rights_valid_until
        ):
            raise ValueError("rights_checked_at phải thuộc khoảng hiệu lực của policy.")
        counts = (
            self.warmup_bar_count,
            self.evaluation_bar_count,
            self.warmup_observed_bar_count,
            self.evaluation_observed_bar_count,
            self.expected_bar_count,
            self.bar_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Số nến trong manifest phải là số nguyên không âm.")
        if self.expected_bar_count != self.warmup_bar_count + self.evaluation_bar_count:
            raise ValueError("Tổng nến dự kiến phải bằng warm-up cộng evaluation.")
        if self.evaluation_bar_count < 1:
            raise ValueError("Manifest cần ít nhất một nến evaluation.")
        if self.range_end > self.prepared_at:
            raise ValueError("range_end phải không muộn hơn prepared_at.")
        if self.bar_count != self.warmup_observed_bar_count + self.evaluation_observed_bar_count:
            raise ValueError("Tổng nến quan sát phải bằng warm-up cộng evaluation.")
        if self.bar_count + len(gaps) != self.expected_bar_count:
            raise ValueError("Số nến thiếu không khớp coverage của manifest.")
        if self.bar_count > self.expected_bar_count:
            raise ValueError("Số nến nhận được vượt phạm vi manifest.")
        if self.warmup_observed_bar_count > self.warmup_bar_count:
            raise ValueError("Số nến warm-up quan sát vượt số mốc dự kiến.")
        if self.evaluation_observed_bar_count > self.evaluation_bar_count:
            raise ValueError("Số nến evaluation quan sát vượt số mốc dự kiến.")
        canonical = json.dumps(
            _manifest_payload(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.dataset_hash != expected_hash:
            raise ValueError("dataset_hash không khớp nội dung manifest.")

    @property
    def coverage_ratio(self) -> float:
        """Tỷ lệ mốc nến đã có trong toàn khoảng warm-up và evaluation."""
        return self.bar_count / self.expected_bar_count

    @property
    def is_ready(self) -> bool:
        """Chỉ sẵn sàng khi mọi mốc nến dự kiến đã có đúng một phiên bản."""
        return self.bar_count == self.expected_bar_count and not self.missing_open_times

    @property
    def has_pinned_source_identities(self) -> bool:
        """Cho biết manifest có ghim ordered MarketBar identity cho scorer parity."""
        return self.schema_version == 4 and self.bar_identity_hash is not None

    @property
    def status(self) -> str:
        """Trạng thái có thể lưu trong backtest run hoặc màn hình chuẩn bị dữ liệu."""
        return "READY" if self.is_ready else "INCOMPLETE"

    @property
    def canonical_json(self) -> str:
        """Manifest JSON ổn định để lưu cùng run mà không nhúng toàn bộ OHLCV."""
        value = _manifest_payload(self)
        value["dataset_hash"] = self.dataset_hash
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_canonical_json(
        cls,
        canonical_json: str,
        *,
        expected_dataset_hash: str,
    ) -> BacktestDatasetManifest:
        """Khôi phục manifest từ bytes chuẩn và xác minh hash/cột dẫn xuất."""
        if not isinstance(canonical_json, str) or not canonical_json:
            raise ValueError("canonical_json phải là chuỗi không rỗng.")
        try:
            payload = json.loads(canonical_json)
        except json.JSONDecodeError, TypeError:
            raise ValueError("canonical_json không phải JSON hợp lệ.") from None
        if not isinstance(payload, dict):
            raise ValueError("canonical_json phải là JSON object.")
        expected_fields = {
            "schema_version",
            "request_fingerprint",
            "policy_fingerprint",
            "rights_workspace_id",
            "provider",
            "purpose",
            "terms_url",
            "rights_verified_at",
            "rights_checked_at",
            "rights_valid_until",
            "prepared_at",
            "symbol",
            "interval",
            "warmup_start",
            "evaluation_start",
            "range_end",
            "warmup_bar_count",
            "evaluation_bar_count",
            "warmup_observed_bar_count",
            "evaluation_observed_bar_count",
            "expected_bar_count",
            "bar_count",
            "coverage_ratio",
            "missing_open_times",
            "bars_content_hash",
            "status",
            "dataset_hash",
        }
        schema_version = payload.get("schema_version")
        if schema_version == 4:
            expected_fields.add("bar_identity_hash")
        if set(payload) != expected_fields or schema_version not in {2, 3, 4}:
            raise ValueError("canonical_json fields/schema_version không hợp lệ.")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != canonical_json:
            raise ValueError("canonical_json không ở định dạng chuẩn.")
        dataset_hash = payload.pop("dataset_hash")
        if not isinstance(expected_dataset_hash, str) or dataset_hash != expected_dataset_hash:
            raise ValueError("expected_dataset_hash không khớp payload.")
        status = payload.pop("status")
        coverage_ratio = payload.pop("coverage_ratio")
        schema_version = payload.pop("schema_version")
        payload["schema_version"] = schema_version
        if schema_version < 4:
            payload["bar_identity_hash"] = None
        payload["rights_provider"] = payload.pop("provider")
        payload["rights_purpose"] = payload.pop("purpose")
        try:
            payload["rights_workspace_id"] = UUID(payload["rights_workspace_id"])
        except TypeError, ValueError:
            raise ValueError("rights_workspace_id không hợp lệ trong canonical_json.") from None
        for name in (
            "rights_verified_at",
            "rights_checked_at",
            "rights_valid_until",
            "prepared_at",
            "warmup_start",
            "evaluation_start",
            "range_end",
        ):
            value = payload[name]
            payload[name] = datetime.fromisoformat(value) if value is not None else None
        missing = payload["missing_open_times"]
        if not isinstance(missing, list) or any(not isinstance(value, str) for value in missing):
            raise ValueError("missing_open_times phải là danh sách timestamp.")
        payload["missing_open_times"] = tuple(datetime.fromisoformat(value) for value in missing)
        result = cls(**payload, dataset_hash=dataset_hash)
        if result.status != status or result.coverage_ratio != coverage_ratio:
            raise ValueError("status/coverage_ratio không khớp nội dung manifest.")
        if result.canonical_json != canonical_json:
            raise ValueError("canonical_json không khớp manifest đã dựng lại.")
        return result

    def require_ready(self) -> BacktestDatasetManifest:
        """Từ chối rõ ràng bộ dữ liệu chưa đủ coverage trước khi replay."""
        if not self.is_ready:
            first_gap = (
                self.missing_open_times[0].isoformat() if self.missing_open_times else "không rõ"
            )
            raise BacktestDatasetNotReady(
                f"Bộ dữ liệu chưa sẵn sàng: thiếu nến tại {len(self.missing_open_times)} mốc; "
                f"mốc đầu tiên {first_gap}."
            )
        return self


def build_backtest_dataset_manifest(
    request: RestBackfillRequest | FirstPartyQADatasetRequest,
    *,
    evaluation_start: datetime,
    warmup_bars: int,
    bars: Sequence[OHLCVBar],
    rights_evidence: BackfillRightsEvidence,
    prepared_at: datetime,
    schema_version: int = 3,
) -> BacktestDatasetManifest:
    """Tạo manifest chuẩn hóa; nến thiếu được liệt kê chứ không nội suy."""
    if type(request) not in {RestBackfillRequest, FirstPartyQADatasetRequest}:
        raise ValueError("request phải là request Binance REST hoặc fixture first-party QA.")
    if not isinstance(rights_evidence, BackfillRightsEvidence):
        raise PermissionError("Thiếu bằng chứng quyền dữ liệu cho backtest.")
    if type(rights_evidence.workspace_id) is not UUID:
        raise PermissionError("Rights evidence chưa được ràng buộc với workspace.")
    if request.purpose is not BackfillRightsPurpose.BACKTEST_RESEARCH:
        raise PermissionError("Request không được duyệt cho mục đích backtest research.")
    expected_provider = (
        request.provider
        if type(request) is FirstPartyQADatasetRequest
        else BINANCE_USDM_REST_PROVIDER
    )
    if rights_evidence.provider != expected_provider:
        raise PermissionError("Rights evidence không khớp provider request backtest.")
    if rights_evidence.purpose is not BackfillRightsPurpose.BACKTEST_RESEARCH:
        raise PermissionError("Rights evidence không khớp mục đích backtest research.")
    if (
        type(request) is FirstPartyQADatasetRequest
        and rights_evidence.terms_url != CRYPIDIAN_FIRST_PARTY_QA_EVIDENCE_REF
    ):
        raise PermissionError("QA rights phải trỏ đúng tài liệu fixture first-party.")

    instant = _utc(prepared_at, "prepared_at")
    if rights_evidence.checked_at is not None:
        instant = max(instant, rights_evidence.checked_at)
    if request.end > instant:
        raise ValueError("range_end phải không muộn hơn prepared_at.")
    if rights_evidence.verified_at > instant:
        raise PermissionError("Quyền dữ liệu chưa có hiệu lực tại thời điểm chuẩn bị manifest.")
    if instant >= rights_evidence.valid_until:
        raise PermissionError("Quyền dữ liệu đã hết hiệu lực tại thời điểm chuẩn bị manifest.")

    evaluation = _utc(evaluation_start, "evaluation_start")
    if type(warmup_bars) is not int or warmup_bars < 0:
        raise ValueError("warmup_bars phải là số nguyên không âm.")
    expected_bar_count = request.expected_bars
    if warmup_bars >= expected_bar_count:
        raise ValueError("warmup_bars phải nhỏ hơn số mốc trong request.")
    interval = request.interval_duration
    if request.start + interval * warmup_bars != evaluation:
        raise ValueError("evaluation_start phải cách warm-up đúng warmup_bars interval.")
    if evaluation >= request.end:
        raise ValueError("Cần ít nhất một nến evaluation sau warm-up.")
    evaluation_bar_count = expected_bar_count - warmup_bars
    if evaluation_bar_count <= 0:
        raise ValueError("Khoảng request không còn nến cho evaluation.")

    observed = tuple(bars)
    if any(not isinstance(item, OHLCVBar) for item in observed):
        raise ValueError("bars chỉ được chứa OHLCVBar.")
    ordered = tuple(sorted(observed, key=lambda item: item.open_time))
    seen: set[datetime] = set()
    for item in ordered:
        if not isinstance(item, OHLCVBar):
            raise ValueError("bars chỉ được chứa OHLCVBar.")
        open_time = item.open_time.astimezone(UTC)
        if open_time < request.start or open_time >= request.end:
            raise ValueError("Nến nằm ngoài khoảng request nửa mở.")
        if (open_time - request.start) % interval != timedelta(0):
            raise ValueError("open_time của nến không nằm trên lưới interval request.")
        if open_time in seen:
            raise ValueError("Nến bị trùng open_time trong cùng manifest.")
        seen.add(open_time)
        if item.symbol != request.symbol or item.timeframe != request.interval:
            raise ValueError("Nến không khớp symbol/timeframe của request.")
        expected_source = market_source_for_rights_provider(expected_provider)
        if item.source != expected_source:
            raise ValueError("Nến không khớp market source của provider đã được kiểm soát.")
        if type(request) is FirstPartyQADatasetRequest:
            if item.source_cursor is None:
                raise ValueError("Nến QA cần source cursor versioned.")
            request.validate_bar_identity(source=item.source, source_cursor=item.source_cursor)
        if not item.is_closed or item.close_time > instant:
            raise ValueError("Nến chưa đóng tại thời điểm chuẩn bị manifest.")
        if item.available_at > instant:
            raise ValueError("Nến chưa available tại thời điểm chuẩn bị manifest.")
        expected_close = open_time + interval - timedelta(milliseconds=1)
        if item.close_time.astimezone(UTC) != expected_close:
            raise ValueError("close_time không khớp biên nến REST của interval.")

    missing = tuple(
        request.start + interval * index
        for index in range(expected_bar_count)
        if request.start + interval * index not in seen
    )
    warmup_observed = sum(item.open_time.astimezone(UTC) < evaluation for item in ordered)
    evaluation_observed = len(ordered) - warmup_observed
    if type(schema_version) is not int or schema_version not in {2, 3, 4}:
        raise ValueError("schema_version chỉ hỗ trợ 2, 3 hoặc 4.")
    bars_hash = backtest_dataset_bars_hash(ordered, schema_version=schema_version)
    bar_identity_hash = (
        backtest_dataset_bar_identity_hash(ordered, workspace_id=rights_evidence.workspace_id)
        if schema_version == 4
        else None
    )
    manifest_payload = {
        "schema_version": schema_version,
        "request_fingerprint": request.fingerprint,
        "policy_fingerprint": rights_evidence.policy_fingerprint,
        "rights_workspace_id": str(rights_evidence.workspace_id),
        "provider": rights_evidence.provider,
        "purpose": rights_evidence.purpose.value,
        "terms_url": rights_evidence.terms_url,
        "rights_verified_at": rights_evidence.verified_at.isoformat(),
        "rights_checked_at": (
            rights_evidence.checked_at.isoformat() if rights_evidence.checked_at else None
        ),
        "rights_valid_until": rights_evidence.valid_until.isoformat(),
        "prepared_at": instant.isoformat(),
        "symbol": request.symbol,
        "interval": request.interval,
        "warmup_start": request.start.isoformat(),
        "evaluation_start": evaluation.isoformat(),
        "range_end": request.end.isoformat(),
        "warmup_bar_count": warmup_bars,
        "evaluation_bar_count": evaluation_bar_count,
        "warmup_observed_bar_count": warmup_observed,
        "evaluation_observed_bar_count": evaluation_observed,
        "expected_bar_count": expected_bar_count,
        "bar_count": len(ordered),
        "coverage_ratio": len(ordered) / expected_bar_count,
        "missing_open_times": [item.isoformat() for item in missing],
        "bars_content_hash": bars_hash,
        "status": "READY" if not missing else "INCOMPLETE",
    }
    if schema_version == 4:
        manifest_payload["bar_identity_hash"] = bar_identity_hash
    canonical = json.dumps(
        manifest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    dataset_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return BacktestDatasetManifest(
        request_fingerprint=request.fingerprint,
        policy_fingerprint=rights_evidence.policy_fingerprint,
        rights_provider=rights_evidence.provider,
        rights_purpose=rights_evidence.purpose.value,
        rights_workspace_id=rights_evidence.workspace_id,
        terms_url=rights_evidence.terms_url,
        rights_verified_at=rights_evidence.verified_at,
        rights_checked_at=rights_evidence.checked_at,
        rights_valid_until=rights_evidence.valid_until,
        prepared_at=instant,
        symbol=request.symbol,
        interval=request.interval,
        warmup_start=request.start,
        evaluation_start=evaluation,
        range_end=request.end,
        warmup_bar_count=warmup_bars,
        evaluation_bar_count=evaluation_bar_count,
        warmup_observed_bar_count=warmup_observed,
        evaluation_observed_bar_count=evaluation_observed,
        expected_bar_count=expected_bar_count,
        bar_count=len(ordered),
        missing_open_times=missing,
        bars_content_hash=bars_hash,
        bar_identity_hash=bar_identity_hash,
        dataset_hash=dataset_hash,
        schema_version=schema_version,
    )


@dataclass(frozen=True, slots=True)
class MarketBarIdentityMetadata:
    """Định danh và thời gian nguồn; không chứa giá, volume hay raw payload."""

    source_identity: MarketBarSourceIdentity
    source: str
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    available_at: datetime
    is_closed: bool
    raw_reference: str | None

    @property
    def checksum(self) -> str:
        """Checksum được ghim cùng identity authoritative."""
        return self.source_identity.checksum

    @classmethod
    def from_bar(cls, bar: OHLCVBar) -> MarketBarIdentityMetadata:
        """Tách metadata thật, không dựng bar hoặc giá giả."""
        if type(bar.source_identity) is not MarketBarSourceIdentity:
            raise ValueError("Identity nguồn authoritative bắt buộc cho manifest v4.")
        if bar.source_identity.checksum != bar.checksum:
            raise ValueError("Checksum bar không khớp identity nguồn.")
        return cls(
            bar.source_identity,
            bar.source,
            bar.symbol,
            bar.timeframe,
            bar.open_time,
            bar.close_time,
            bar.available_at,
            bar.is_closed,
            bar.raw_reference,
        )


def backtest_dataset_bar_identity_hash(bars: Sequence[OHLCVBar], *, workspace_id: UUID) -> str:
    """Ghim ordered định danh record/revision, giữ nguyên bytes manifest v4."""
    if not isinstance(bars, Sequence) or any(type(bar) is not OHLCVBar for bar in bars):
        raise ValueError("bars phải là dãy OHLCVBar hợp lệ.")
    return backtest_dataset_identity_metadata_hash(
        tuple(MarketBarIdentityMetadata.from_bar(bar) for bar in bars), workspace_id=workspace_id
    )


def backtest_dataset_identity_metadata_hash(
    bars: Sequence[MarketBarIdentityMetadata], *, workspace_id: UUID
) -> str:
    """Ghim ordered định danh record/revision đã đọc từ MarketBar."""
    if type(workspace_id) is not UUID:
        raise ValueError("workspace_id cho identity digest phải là UUID.")
    if not isinstance(bars, Sequence) or any(
        type(bar) is not MarketBarIdentityMetadata for bar in bars
    ):
        raise ValueError("bars phải là dãy metadata định danh hợp lệ.")
    ordered = tuple(
        sorted(
            bars,
            key=lambda bar: (
                bar.open_time.astimezone(UTC),
                bar.source,
                bar.symbol,
                bar.timeframe,
            ),
        )
    )
    payload: list[dict[str, object]] = []
    seen_record_ids: set[UUID] = set()
    seen_versions: set[tuple[str, int]] = set()
    for bar in ordered:
        identity = bar.source_identity
        if type(identity) is not MarketBarSourceIdentity:
            raise ValueError(
                "Identity nguồn authoritative của từng bar là bắt buộc cho manifest v4."
            )
        if identity.workspace_id != workspace_id:
            raise ValueError("Identity bar không thuộc workspace của manifest.")
        if identity.checksum != bar.checksum:
            raise ValueError("Checksum bar không khớp identity nguồn.")
        version_key = (identity.logical_key, identity.revision)
        if identity.record_id in seen_record_ids or version_key in seen_versions:
            raise ValueError("Identity record/revision bar bị trùng trong manifest.")
        seen_record_ids.add(identity.record_id)
        seen_versions.add(version_key)
        payload.append(
            {
                "workspace_id": str(identity.workspace_id),
                "record_id": str(identity.record_id),
                "logical_key": identity.logical_key,
                "revision": identity.revision,
                "supersedes_record_id": (
                    None
                    if identity.supersedes_record_id is None
                    else str(identity.supersedes_record_id)
                ),
                "checksum": identity.checksum,
                "source": bar.source,
                "symbol": bar.symbol,
                "timeframe": bar.timeframe,
                "open_time": bar.open_time.astimezone(UTC).isoformat(),
            }
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def backtest_dataset_bars_hash(bars: Sequence[OHLCVBar], *, schema_version: int = 3) -> str:
    """Tính hash ổn định theo thời gian mở nến cho snapshot dataset."""
    if type(schema_version) is not int or schema_version not in {2, 3, 4}:
        raise ValueError("schema_version chỉ hỗ trợ 2, 3 hoặc 4.")
    if not isinstance(bars, Sequence) or any(type(bar) is not OHLCVBar for bar in bars):
        raise ValueError("bars phải là dãy OHLCVBar hợp lệ.")
    ordered = tuple(
        sorted(
            bars,
            key=lambda bar: (
                bar.open_time.astimezone(UTC),
                bar.source,
                bar.symbol,
                bar.timeframe,
            ),
        )
    )
    digest = hashlib.sha256()
    for bar in ordered:
        payload = {
            "source": bar.source,
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "open_time": bar.open_time.astimezone(UTC).isoformat(),
            "close_time": bar.close_time.astimezone(UTC).isoformat(),
            "event_at": bar.event_at.astimezone(UTC).isoformat(),
            "received_at": bar.received_at.astimezone(UTC).isoformat(),
            "available_at": bar.available_at.astimezone(UTC).isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "is_closed": bar.is_closed,
            "source_cursor": bar.source_cursor,
            "checksum": bar.checksum,
            "raw_reference": bar.raw_reference,
        }
        if schema_version >= 3:
            payload["quote_volume"] = None if bar.quote_volume is None else str(bar.quote_volume)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _manifest_payload(manifest: BacktestDatasetManifest) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": manifest.schema_version,
        "request_fingerprint": manifest.request_fingerprint,
        "policy_fingerprint": manifest.policy_fingerprint,
        "rights_workspace_id": str(manifest.rights_workspace_id),
        "provider": manifest.rights_provider,
        "purpose": manifest.rights_purpose,
        "terms_url": manifest.terms_url,
        "rights_verified_at": manifest.rights_verified_at.isoformat(),
        "rights_checked_at": (
            manifest.rights_checked_at.isoformat() if manifest.rights_checked_at else None
        ),
        "rights_valid_until": manifest.rights_valid_until.isoformat(),
        "prepared_at": manifest.prepared_at.isoformat(),
        "symbol": manifest.symbol,
        "interval": manifest.interval,
        "warmup_start": manifest.warmup_start.isoformat(),
        "evaluation_start": manifest.evaluation_start.isoformat(),
        "range_end": manifest.range_end.isoformat(),
        "warmup_bar_count": manifest.warmup_bar_count,
        "evaluation_bar_count": manifest.evaluation_bar_count,
        "warmup_observed_bar_count": manifest.warmup_observed_bar_count,
        "evaluation_observed_bar_count": manifest.evaluation_observed_bar_count,
        "expected_bar_count": manifest.expected_bar_count,
        "bar_count": manifest.bar_count,
        "coverage_ratio": manifest.coverage_ratio,
        "missing_open_times": [item.isoformat() for item in manifest.missing_open_times],
        "bars_content_hash": manifest.bars_content_hash,
        "status": manifest.status,
    }
    if manifest.schema_version == 4:
        payload["bar_identity_hash"] = manifest.bar_identity_hash
    return payload
