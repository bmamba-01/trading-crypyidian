"""Ghép replay input đa timeframe sau khi từng manifest được kiểm quyền."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from trading_bot.application.backtest_dataset_manifest import (
    backtest_dataset_bars_hash,
)
from trading_bot.application.backtest_dataset_replay import BacktestDatasetReplayInput
from trading_bot.application.binance_rest_backfill import BackfillRightsEvidence


class BacktestDatasetBundleMismatch(ValueError):
    """Các manifest con không thể ghép an toàn thành cùng một lần replay."""


_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_FIELDS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "symbol",
        "evaluation_start",
        "range_end",
        "rights_provider",
        "rights_purpose",
        "policy_fingerprint",
        "components",
    }
)
_COMPONENT_REFERENCE_FIELDS = frozenset({"interval", "dataset_hash", "bars_content_hash"})


@dataclass(frozen=True, slots=True)
class BacktestDatasetReplayBundleComponentReference:
    """Tham chiếu bất biến tới manifest con và nội dung bars đã băm."""

    interval: str
    dataset_hash: str
    bars_content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.interval, str) or not self.interval:
            raise ValueError("interval của component phải là chuỗi không rỗng.")
        if not _is_sha256(self.dataset_hash) or not _is_sha256(self.bars_content_hash):
            raise ValueError("Hash của manifest và bars phải là SHA-256 dạng hex thường.")


@dataclass(frozen=True, slots=True)
class BacktestDatasetReplayBundleReference:
    """Biểu diễn canonical bất biến để lưu bundle replay theo workspace."""

    schema_version: int
    workspace_id: UUID
    symbol: str
    evaluation_start: datetime
    range_end: datetime
    rights_provider: str
    rights_purpose: str
    policy_fingerprint: str
    components: tuple[BacktestDatasetReplayBundleComponentReference, ...]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "components", tuple(self.components))
        except TypeError as exc:
            raise ValueError("Bundle reference cần component là dãy hợp lệ.") from exc
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version của bundle reference phải bằng 1.")
        if type(self.workspace_id) is not UUID:
            raise ValueError("workspace_id của bundle reference không hợp lệ.")
        if not all(
            isinstance(value, str) and value
            for value in (self.symbol, self.rights_provider, self.rights_purpose)
        ):
            raise ValueError("Scope bundle phải chứa chuỗi không rỗng.")
        if not _is_sha256(self.policy_fingerprint):
            raise ValueError("policy_fingerprint phải là SHA-256 dạng hex thường.")
        if (
            not isinstance(self.evaluation_start, datetime)
            or not isinstance(self.range_end, datetime)
            or self.evaluation_start.tzinfo is None
            or self.range_end.tzinfo is None
            or self.evaluation_start >= self.range_end
        ):
            raise ValueError("Evaluation window của bundle reference không hợp lệ.")
        if not self.components or any(
            type(component) is not BacktestDatasetReplayBundleComponentReference
            for component in self.components
        ):
            raise ValueError("Bundle reference cần component hợp lệ và không rỗng.")
        intervals = tuple(component.interval for component in self.components)
        if intervals != tuple(sorted(intervals)) or len(intervals) != len(set(intervals)):
            raise ValueError("Component phải duy nhất và được sắp theo interval.")

    @classmethod
    def from_bundle(
        cls, bundle: BacktestDatasetReplayBundle
    ) -> BacktestDatasetReplayBundleReference:
        """Tạo tham chiếu từ bundle đã xác thực, bỏ qua thời điểm lease quyền."""
        if type(bundle) is not BacktestDatasetReplayBundle:
            raise TypeError("bundle phải là BacktestDatasetReplayBundle đã xác thực.")
        return cls(
            schema_version=1,
            workspace_id=bundle.workspace_id,
            symbol=bundle.symbol,
            evaluation_start=bundle.evaluation_start,
            range_end=bundle.range_end,
            rights_provider=bundle.rights_provider,
            rights_purpose=bundle.rights_purpose,
            policy_fingerprint=bundle.policy_fingerprint,
            components=tuple(
                BacktestDatasetReplayBundleComponentReference(
                    interval=item.manifest.interval,
                    dataset_hash=item.manifest.dataset_hash,
                    bars_content_hash=item.manifest.bars_content_hash,
                )
                for item in bundle.components
            ),
        )

    @property
    def canonical_json(self) -> str:
        """Trả JSON UTF-8 canonical tương thích payload hash bundle hiện hữu."""
        return json.dumps(
            _reference_payload(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def bundle_hash(self) -> str:
        """Hash SHA-256 của payload canonical, tương thích dataset_hash cũ."""
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(
        cls, canonical_json: str, *, expected_bundle_hash: str
    ) -> BacktestDatasetReplayBundleReference:
        """Đọc JSON canonical và từ chối hash, schema hoặc cấu trúc sai lệch."""
        if not isinstance(canonical_json, str) or not _is_sha256(expected_bundle_hash):
            raise ValueError("Canonical JSON hoặc expected bundle hash không hợp lệ.")
        try:
            payload = json.loads(canonical_json)
            if type(payload) is not dict or set(payload) != _REFERENCE_FIELDS:
                raise ValueError("Canonical payload có field thiếu hoặc không nhận diện.")
            if type(payload["components"]) is not list or not payload["components"]:
                raise ValueError("Canonical payload cần danh sách component không rỗng.")
            components: list[BacktestDatasetReplayBundleComponentReference] = []
            for item in payload["components"]:
                if type(item) is not dict or set(item) != _COMPONENT_REFERENCE_FIELDS:
                    raise ValueError("Component có field thiếu hoặc không nhận diện.")
                components.append(BacktestDatasetReplayBundleComponentReference(**item))
            reference = cls(
                schema_version=payload["schema_version"],
                workspace_id=UUID(payload["workspace_id"]),
                symbol=payload["symbol"],
                evaluation_start=datetime.fromisoformat(payload["evaluation_start"]),
                range_end=datetime.fromisoformat(payload["range_end"]),
                rights_provider=payload["rights_provider"],
                rights_purpose=payload["rights_purpose"],
                policy_fingerprint=payload["policy_fingerprint"],
                components=tuple(components),
            )
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Canonical bundle reference không hợp lệ.") from exc
        if reference.canonical_json != canonical_json:
            raise ValueError("Canonical JSON không ở dạng canonical hoặc có scope sai lệch.")
        if reference.bundle_hash != expected_bundle_hash:
            raise ValueError("Canonical bundle hash không khớp expected hash.")
        return reference


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _HASH_PATTERN.fullmatch(value) is not None


def _reference_payload(reference: BacktestDatasetReplayBundleReference) -> dict[str, object]:
    return {
        "schema_version": reference.schema_version,
        "workspace_id": str(reference.workspace_id),
        "symbol": reference.symbol,
        "evaluation_start": reference.evaluation_start.isoformat(),
        "range_end": reference.range_end.isoformat(),
        "rights_provider": reference.rights_provider,
        "rights_purpose": reference.rights_purpose,
        "policy_fingerprint": reference.policy_fingerprint,
        "components": [
            {
                "interval": component.interval,
                "dataset_hash": component.dataset_hash,
                "bars_content_hash": component.bars_content_hash,
            }
            for component in reference.components
        ],
    }


@dataclass(frozen=True, slots=True)
class BacktestDatasetReplayBundle:
    """Snapshot tổng hợp các timeframe đã được kiểm quyền và hash riêng."""

    components: tuple[BacktestDatasetReplayInput, ...]
    workspace_id: UUID
    symbol: str
    evaluation_start: datetime
    range_end: datetime
    rights_provider: str
    rights_purpose: str
    policy_fingerprint: str
    dataset_hash: str

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("Bundle cần ít nhất một manifest con.")
        if any(type(item) is not BacktestDatasetReplayInput for item in self.components):
            raise BacktestDatasetBundleMismatch("Bundle chứa replay input không hợp lệ.")
        ordered = tuple(sorted(self.components, key=lambda item: item.manifest.interval))
        if ordered != self.components:
            raise ValueError("Manifest con trong bundle phải được sắp theo timeframe.")
        if len({item.manifest.interval for item in self.components}) != len(self.components):
            raise BacktestDatasetBundleMismatch("Timeframe bị trùng trong replay bundle.")
        first_rights = self.components[0].current_rights
        for item in self.components:
            manifest = item.manifest
            manifest.require_ready()
            if (
                manifest.rights_workspace_id != self.workspace_id
                or manifest.symbol != self.symbol
                or manifest.evaluation_start != self.evaluation_start
                or manifest.range_end != self.range_end
                or manifest.rights_provider != self.rights_provider
                or manifest.rights_purpose != self.rights_purpose
                or manifest.policy_fingerprint != self.policy_fingerprint
            ):
                raise BacktestDatasetBundleMismatch(
                    "Manifest con phải cùng workspace, symbol, evaluation window và rights policy."
                )
            rights = item.current_rights
            if (
                type(rights) is not BackfillRightsEvidence
                or rights.workspace_id != manifest.rights_workspace_id
                or rights.provider != manifest.rights_provider
                or rights.purpose.value != manifest.rights_purpose
                or rights.policy_fingerprint != manifest.policy_fingerprint
            ):
                raise BacktestDatasetBundleMismatch(
                    "Rights evidence hiện hành không khớp manifest con."
                )
            if (
                rights.workspace_id != first_rights.workspace_id
                or rights.provider != first_rights.provider
                or rights.purpose != first_rights.purpose
                or rights.policy_fingerprint != first_rights.policy_fingerprint
            ):
                raise BacktestDatasetBundleMismatch(
                    "Rights evidence giữa các timeframe không khớp."
                )
            if len(item.bars) != manifest.expected_bar_count:
                raise BacktestDatasetBundleMismatch("Số lượng bars của manifest con bị thay đổi.")
            if (
                backtest_dataset_bars_hash(item.bars, schema_version=manifest.schema_version)
                != manifest.bars_content_hash
            ):
                raise BacktestDatasetBundleMismatch("Hash bars của manifest con bị thay đổi.")
        expected_hash = _bundle_hash(
            components=self.components,
            workspace_id=self.workspace_id,
            symbol=self.symbol,
            evaluation_start=self.evaluation_start,
            range_end=self.range_end,
            rights_provider=self.rights_provider,
            rights_purpose=self.rights_purpose,
            policy_fingerprint=self.policy_fingerprint,
        )
        if self.dataset_hash != expected_hash:
            raise ValueError("dataset_hash của bundle không khớp nội dung.")


def build_backtest_dataset_replay_bundle(
    inputs: Sequence[BacktestDatasetReplayInput],
) -> BacktestDatasetReplayBundle:
    """Ghép child manifest cùng symbol/window/rights thành hash ổn định."""
    if not isinstance(inputs, Sequence) or not inputs:
        raise ValueError("inputs phải là dãy replay input không rỗng.")
    if any(type(item) is not BacktestDatasetReplayInput for item in inputs):
        raise ValueError("Mọi bundle component phải là BacktestDatasetReplayInput.")

    components = tuple(sorted(inputs, key=lambda item: item.manifest.interval))
    manifests = tuple(item.manifest for item in components)
    intervals = tuple(manifest.interval for manifest in manifests)
    if len(intervals) != len(set(intervals)):
        raise BacktestDatasetBundleMismatch("Timeframe bị trùng trong replay bundle.")

    first = manifests[0]

    dataset_hash = _bundle_hash(
        components=components,
        workspace_id=first.rights_workspace_id,
        symbol=first.symbol,
        evaluation_start=first.evaluation_start,
        range_end=first.range_end,
        rights_provider=first.rights_provider,
        rights_purpose=first.rights_purpose,
        policy_fingerprint=first.policy_fingerprint,
    )
    return BacktestDatasetReplayBundle(
        components=components,
        workspace_id=first.rights_workspace_id,
        symbol=first.symbol,
        evaluation_start=first.evaluation_start,
        range_end=first.range_end,
        rights_provider=first.rights_provider,
        rights_purpose=first.rights_purpose,
        policy_fingerprint=first.policy_fingerprint,
        dataset_hash=dataset_hash,
    )


def _bundle_hash(
    *,
    components: tuple[BacktestDatasetReplayInput, ...],
    workspace_id: UUID,
    symbol: str,
    evaluation_start: datetime,
    range_end: datetime,
    rights_provider: str,
    rights_purpose: str,
    policy_fingerprint: str,
) -> str:
    payload = {
        "schema_version": 1,
        "workspace_id": str(workspace_id),
        "symbol": symbol,
        "evaluation_start": evaluation_start.isoformat(),
        "range_end": range_end.isoformat(),
        "rights_provider": rights_provider,
        "rights_purpose": rights_purpose,
        "policy_fingerprint": policy_fingerprint,
        "components": [
            {
                "interval": item.manifest.interval,
                "dataset_hash": item.manifest.dataset_hash,
                "bars_content_hash": item.manifest.bars_content_hash,
            }
            for item in components
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
