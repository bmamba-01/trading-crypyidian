"""Lineage dữ liệu OHLCV chính xác dùng trong một sample TRAINING."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9_.:-]{0,127}\Z")
_INTERVAL = re.compile(r"[1-9][0-9]*[mhdwM]\Z")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SourceUseLineage:
    """Ghim exact child, timeframe, khoảng nửa mở và MarketBar UUID đã xác minh."""

    source_id: str
    provider: str
    bundle_hash: str
    child_dataset_hash: str
    policy_fingerprint: str
    symbol: str
    interval: str
    data_start: datetime
    data_end: datetime
    bar_identity_hash: str
    market_bar_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("source_id", "provider", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
                raise ValueError(f"{name} source-use lineage không hợp lệ.")
        if self.source_id != self.provider:
            raise ValueError("source_id phải khớp provider được kiểm quyền.")
        if not isinstance(self.interval, str) or _INTERVAL.fullmatch(self.interval) is None:
            raise ValueError("interval source-use lineage không hợp lệ.")
        for name in (
            "bundle_hash",
            "child_dataset_hash",
            "policy_fingerprint",
            "bar_identity_hash",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} phải là SHA-256 hex thường.")
        start = _utc(self.data_start, "data_start")
        end = _utc(self.data_end, "data_end")
        if start >= end:
            raise ValueError("source-use data range phải là khoảng nửa mở không rỗng.")
        object.__setattr__(self, "data_start", start)
        object.__setattr__(self, "data_end", end)
        raw_ids = tuple(self.market_bar_ids)
        if not raw_ids or any(not isinstance(item, str) for item in raw_ids):
            raise ValueError("market_bar_ids phải là danh sách UUID không rỗng.")
        parsed: list[str] = []
        for item in raw_ids:
            prefix = "market-bar-record:"
            if not item.startswith(prefix):
                raise ValueError("MarketBar id phải dùng tiền tố market-bar-record:.")
            try:
                parsed.append(f"{prefix}{UUID(item.removeprefix(prefix))}")
            except ValueError as error:
                raise ValueError("MarketBar id phải chứa UUID chuẩn.") from error
        if len(parsed) != len(set(parsed)):
            raise ValueError("market_bar_ids không được trùng.")
        object.__setattr__(self, "market_bar_ids", tuple(parsed))

    @property
    def canonical_payload(self) -> dict[str, object]:
        """Trả metadata lineage canonical để manifest ghim theo hash."""
        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "bundle_hash": self.bundle_hash,
            "child_dataset_hash": self.child_dataset_hash,
            "policy_fingerprint": self.policy_fingerprint,
            "symbol": self.symbol,
            "interval": self.interval,
            "data_start": self.data_start.isoformat(),
            "data_end": self.data_end.isoformat(),
            "bar_identity_hash": self.bar_identity_hash,
            "market_bar_ids": list(self.market_bar_ids),
        }

    @classmethod
    def from_payload(cls, payload: object) -> SourceUseLineage:
        """Khôi phục object sau khi kiểm schema canonical."""
        keys = {
            "source_id",
            "provider",
            "bundle_hash",
            "child_dataset_hash",
            "policy_fingerprint",
            "symbol",
            "interval",
            "data_start",
            "data_end",
            "bar_identity_hash",
            "market_bar_ids",
        }
        if not isinstance(payload, dict) or set(payload) != keys:
            raise ValueError("SourceUseLineage canonical có schema không hỗ trợ.")
        if (
            not isinstance(payload["data_start"], str)
            or not isinstance(payload["data_end"], str)
            or not isinstance(payload["market_bar_ids"], list)
        ):
            raise ValueError("SourceUseLineage canonical có kiểu field không hợp lệ.")
        return cls(
            source_id=payload["source_id"],
            provider=payload["provider"],
            bundle_hash=payload["bundle_hash"],
            child_dataset_hash=payload["child_dataset_hash"],
            policy_fingerprint=payload["policy_fingerprint"],
            symbol=payload["symbol"],
            interval=payload["interval"],
            data_start=datetime.fromisoformat(payload["data_start"]),
            data_end=datetime.fromisoformat(payload["data_end"]),
            bar_identity_hash=payload["bar_identity_hash"],
            market_bar_ids=tuple(payload["market_bar_ids"]),
        )


def source_use_lineage_set_hash(lineage: tuple[SourceUseLineage, ...]) -> str:
    """Băm một lần tập lineage canonical dùng chung bởi sample của cùng replay run."""
    entries = tuple(lineage)
    if any(type(item) is not SourceUseLineage for item in entries):
        raise ValueError("Lineage set phải chứa SourceUseLineage.")
    ordered = tuple(sorted(entries, key=lambda item: item.child_dataset_hash))
    if not ordered or ordered != entries:
        raise ValueError("Lineage set phải không rỗng và sắp theo child hash.")
    if len({item.child_dataset_hash for item in ordered}) != len(ordered):
        raise ValueError("Lineage set không được lặp child.")
    canonical = json.dumps(
        [item.canonical_payload for item in ordered],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
