"""Manifest bất biến và canonical cho tập sample học máy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from trading_bot.domain.learning.labels import _sha256, _timestamp
from trading_bot.domain.research.source_policy import SourceUsePurpose

_SOURCE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_POLICY_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


@dataclass(frozen=True, slots=True)
class DatasetSourceReference:
    """Ghép một revision nguồn với policy, mục đích và mã băm quyền cụ thể."""

    source_id: str
    item_id: str
    revision_id: str
    policy_id: str
    policy_sequence: int
    purpose: SourceUsePurpose
    rights_hash: str

    def __post_init__(self) -> None:
        for name in ("source_id", "item_id", "revision_id"):
            if not isinstance(getattr(self, name), str) or not _SOURCE_IDENTIFIER.fullmatch(
                getattr(self, name)
            ):
                raise ValueError(f"{name} nguồn không hợp lệ.")
        if not isinstance(self.policy_id, str) or not _POLICY_IDENTIFIER.fullmatch(self.policy_id):
            raise ValueError("policy_id nguồn không hợp lệ.")
        if type(self.policy_sequence) is not int or self.policy_sequence < 1:
            raise ValueError("policy_sequence phải là số nguyên dương.")
        if type(self.purpose) is not SourceUsePurpose:
            raise ValueError("purpose phải là SourceUsePurpose tường minh.")
        if self.purpose is not SourceUsePurpose.TRAINING:
            raise ValueError("Dataset manifest chỉ ghim quyền TRAINING.")
        _sha256(self.rights_hash, "rights_hash")

    @property
    def identity(self) -> tuple[str, str, str, str, int, str]:
        """Khóa quyền ổn định để phát hiện cùng policy nhưng mang hai mã băm."""
        return (
            self.source_id,
            self.item_id,
            self.revision_id,
            self.policy_id,
            self.policy_sequence,
            self.purpose.value,
        )


class DatasetSplit(StrEnum):
    """Phân hoạch cố định v1 hoặc tập phát triển/holdout của v2."""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    HOLDOUT = "holdout"
    DEVELOPMENT = "development"


@dataclass(frozen=True, slots=True)
class DatasetSample:
    """Thành viên đã qua kiểm tra point-in-time của dataset."""

    sample_id: str
    decision_at: datetime
    feature_available_at: datetime
    outcome_start: datetime
    outcome_end: datetime
    feature_snapshot_hash: str
    outcome_id: str
    outcome_hash: str
    label_hash: str
    label_value: int
    label_version: str
    split: DatasetSplit
    source_dependent: bool
    source_rights_hashes: tuple[str, ...]
    source_revision_refs: tuple[str, ...]
    source_references: tuple[DatasetSourceReference, ...] = ()

    def __post_init__(self) -> None:
        for name in ("sample_id", "outcome_id", "label_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")
        for name in ("feature_snapshot_hash", "outcome_hash", "label_hash"):
            _sha256(getattr(self, name), name)
        object.__setattr__(self, "decision_at", _timestamp(self.decision_at, "decision_at"))
        object.__setattr__(
            self,
            "feature_available_at",
            _timestamp(self.feature_available_at, "feature_available_at"),
        )
        object.__setattr__(self, "outcome_start", _timestamp(self.outcome_start, "outcome_start"))
        object.__setattr__(self, "outcome_end", _timestamp(self.outcome_end, "outcome_end"))
        if self.feature_available_at > self.decision_at:
            raise ValueError("Feature phải khả dụng không muộn hơn decision_at.")
        if self.outcome_start < self.decision_at or self.outcome_end <= self.outcome_start:
            raise ValueError("Khoảng outcome phải bắt đầu sau decision và có độ dài dương.")
        if type(self.label_value) is not int or self.label_value not in {-1, 0, 1}:
            raise ValueError("label_value phải là một trong -1, 0 hoặc 1.")
        if type(self.split) is not DatasetSplit:
            raise ValueError("split phải là DatasetSplit tường minh.")
        if type(self.source_dependent) is not bool:
            raise ValueError("source_dependent phải là boolean tường minh.")
        rights = tuple(sorted(set(self.source_rights_hashes)))
        for rights_hash in rights:
            _sha256(rights_hash, "source_rights_hash")
        object.__setattr__(self, "source_rights_hashes", rights)
        revisions = tuple(sorted(set(self.source_revision_refs)))
        if any(not ref.strip() for ref in revisions):
            raise ValueError("source_revision_refs không được chứa giá trị rỗng.")
        object.__setattr__(self, "source_revision_refs", revisions)
        raw_references = tuple(self.source_references)
        if any(type(item) is not DatasetSourceReference for item in raw_references):
            raise ValueError("source_references phải chứa DatasetSourceReference.")
        references = tuple(
            sorted(set(raw_references), key=lambda item: (*item.identity, item.rights_hash))
        )
        identities: dict[tuple[str, str, str, str, int, str], str] = {}
        for item in references:
            previous_hash = identities.setdefault(item.identity, item.rights_hash)
            if previous_hash != item.rights_hash:
                raise ValueError("Cùng source reference có conflicting rights hash.")
        object.__setattr__(self, "source_references", references)
        if self.source_dependent and not references and (not rights or not revisions):
            raise ValueError("Sample phụ thuộc nguồn phải ghim rights hash và source revision.")
        if self.source_dependent and references and (rights or revisions):
            if rights != tuple(sorted({item.rights_hash for item in references})):
                raise ValueError("Rights hash cũ không khớp typed source references.")
            raise ValueError("Không trộn legacy refs với typed source references.")
        if not self.source_dependent and (rights or revisions or references):
            raise ValueError("Sample không phụ thuộc nguồn không được mang lineage nguồn.")


@dataclass(frozen=True, slots=True)
class DatasetExclusion:
    """Sample bị loại cùng danh sách lý do ổn định."""

    sample_id: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ValueError("sample_id exclusion không được để trống.")
        reasons = tuple(sorted(set(self.reasons)))
        if not reasons or any(not reason.strip() for reason in reasons):
            raise ValueError("Exclusion phải có ít nhất một lý do không rỗng.")
        object.__setattr__(self, "reasons", reasons)


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """Manifest pin phạm vi truy vấn, versions, membership và exclusions."""

    workspace_id: UUID
    query_hash: str
    symbols: tuple[str, ...]
    data_start: datetime
    data_end: datetime
    as_of: datetime
    feature_schema_version: str
    feature_version: str
    label_version: str
    sample_schema_version: str
    split_policy_version: str
    samples: tuple[DatasetSample, ...]
    exclusions: tuple[DatasetExclusion, ...]
    holdout_sealed_at: datetime | None
    manifest_version: str = "dataset-manifest-v1"
    retention_policy_ref: str = "unspecified-offline-fixture"
    canonical_json: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _sha256(self.query_hash, "query_hash")
        start = _timestamp(self.data_start, "data_start")
        end = _timestamp(self.data_end, "data_end")
        as_of = _timestamp(self.as_of, "as_of")
        object.__setattr__(self, "data_start", start)
        object.__setattr__(self, "data_end", end)
        object.__setattr__(self, "as_of", as_of)
        if start >= end:
            raise ValueError("data_start phải trước data_end.")
        if not self.symbols or any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("symbols phải chứa ít nhất một mã hợp lệ.")
        object.__setattr__(self, "symbols", tuple(sorted(set(self.symbols))))
        for name in (
            "feature_schema_version",
            "feature_version",
            "label_version",
            "sample_schema_version",
            "split_policy_version",
            "manifest_version",
            "retention_policy_ref",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} không được để trống.")

        samples = tuple(sorted(self.samples, key=lambda item: item.sample_id))
        exclusions = tuple(sorted(self.exclusions, key=lambda item: item.sample_id))
        sample_ids = [item.sample_id for item in samples]
        excluded_ids = [item.sample_id for item in exclusions]
        if len(sample_ids) != len(set(sample_ids)) or len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("Dataset không được có sample_id duplicate.")
        if set(sample_ids) & set(excluded_ids):
            raise ValueError("Sample không thể đồng thời hợp lệ và bị loại.")
        for item in samples:
            if self.manifest_version == "dataset-manifest-v2":
                if item.split not in {DatasetSplit.DEVELOPMENT, DatasetSplit.HOLDOUT}:
                    raise ValueError("Manifest v2 chỉ nhận split development hoặc holdout.")
            elif item.split is DatasetSplit.DEVELOPMENT:
                raise ValueError("Split development chỉ được dùng với dataset-manifest-v2.")
            if self.manifest_version == "dataset-manifest-v3":
                if item.source_dependent and not item.source_references:
                    raise ValueError(
                        "Manifest v3 yêu cầu typed source reference cho sample phụ thuộc nguồn."
                    )
                if item.source_references and not item.source_dependent:
                    raise ValueError(
                        "Typed source reference chỉ hợp lệ cho sample phụ thuộc nguồn."
                    )
            elif item.source_references:
                raise ValueError("Typed source reference chỉ được lưu trong manifest v3.")
            if not start <= item.decision_at < end:
                raise ValueError("Sample decision nằm ngoài data range.")
            if item.decision_at > as_of:
                raise ValueError("Sample decision nằm sau as_of.")
            if item.label_version != self.label_version:
                raise ValueError("Label version của sample không khớp manifest.")
            if item.outcome_end > as_of:
                raise ValueError("outcome_end của sample không được sau as_of.")
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "exclusions", exclusions)

        has_holdout = any(item.split is DatasetSplit.HOLDOUT for item in samples)
        if self.holdout_sealed_at is not None:
            object.__setattr__(
                self, "holdout_sealed_at", _timestamp(self.holdout_sealed_at, "holdout_sealed_at")
            )
        if has_holdout and self.holdout_sealed_at is None:
            raise ValueError("Holdout sample yêu cầu trạng thái sealed và thời điểm niêm phong.")
        if not has_holdout and self.holdout_sealed_at is not None:
            raise ValueError("Không được niêm phong holdout khi manifest không có holdout sample.")

        payload = self._payload()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        object.__setattr__(self, "canonical_json", canonical)
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        object.__setattr__(self, "content_hash", content_hash)

    def _payload(self) -> dict[str, object]:
        samples = []
        for item in self.samples:
            sample_payload: dict[str, object] = {
                "sample_id": item.sample_id,
                "decision_at": item.decision_at.isoformat(),
                "feature_available_at": item.feature_available_at.isoformat(),
                "outcome_start": item.outcome_start.isoformat(),
                "outcome_end": item.outcome_end.isoformat(),
                "feature_snapshot_hash": item.feature_snapshot_hash,
                "outcome_id": item.outcome_id,
                "outcome_hash": item.outcome_hash,
                "label_hash": item.label_hash,
                "label_value": item.label_value,
                "label_version": item.label_version,
                "split": item.split.value,
                "source_dependent": item.source_dependent,
            }
            if self.manifest_version == "dataset-manifest-v3":
                sample_payload["source_references"] = [
                    {
                        "source_id": reference.source_id,
                        "item_id": reference.item_id,
                        "revision_id": reference.revision_id,
                        "policy_id": reference.policy_id,
                        "policy_sequence": reference.policy_sequence,
                        "purpose": reference.purpose.value,
                        "rights_hash": reference.rights_hash,
                    }
                    for reference in item.source_references
                ]
            else:
                sample_payload["source_rights_hashes"] = list(item.source_rights_hashes)
                sample_payload["source_revision_refs"] = list(item.source_revision_refs)
            samples.append(sample_payload)
        return {
            "manifest_version": self.manifest_version,
            "workspace_id": str(self.workspace_id),
            "query_hash": self.query_hash,
            "symbols": list(self.symbols),
            "data_range": [self.data_start.isoformat(), self.data_end.isoformat()],
            "as_of": self.as_of.isoformat(),
            "feature_schema_version": self.feature_schema_version,
            "feature_version": self.feature_version,
            "label_version": self.label_version,
            "sample_schema_version": self.sample_schema_version,
            "split_policy_version": self.split_policy_version,
            "samples": samples,
            "exclusions": [
                {"sample_id": item.sample_id, "reasons": list(item.reasons)}
                for item in self.exclusions
            ],
            "holdout_sealed_at": (
                self.holdout_sealed_at.isoformat() if self.holdout_sealed_at else None
            ),
            "retention_policy_ref": self.retention_policy_ref,
        }

    @classmethod
    def from_canonical_json(
        cls,
        canonical_json: str,
        *,
        expected_content_hash: str,
    ) -> DatasetManifest:
        """Tái dựng manifest đã lưu và xác minh schema, canonical bytes cùng content hash."""
        if not isinstance(canonical_json, str) or not canonical_json:
            raise ValueError("canonical_json phải là chuỗi không rỗng.")
        _sha256(expected_content_hash, "expected_content_hash")
        try:
            payload = json.loads(canonical_json)
        except (TypeError, ValueError) as error:
            raise ValueError("canonical manifest JSON không hợp lệ.") from error
        common_keys = {
            "manifest_version",
            "workspace_id",
            "query_hash",
            "symbols",
            "data_range",
            "as_of",
            "feature_schema_version",
            "feature_version",
            "label_version",
            "sample_schema_version",
            "split_policy_version",
            "samples",
            "exclusions",
            "holdout_sealed_at",
            "retention_policy_ref",
        }
        if not isinstance(payload, dict) or set(payload) != common_keys:
            raise ValueError("canonical manifest có schema không hỗ trợ.")
        manifest_version = payload.get("manifest_version")
        if not isinstance(manifest_version, str) or not manifest_version.strip():
            raise ValueError("manifest_version không hợp lệ.")

        def parse_timestamp(value: object, name: str) -> datetime:
            if not isinstance(value, str):
                raise ValueError(f"{name} phải là chuỗi ISO-8601.")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{name} phải là chuỗi ISO-8601.") from error
            return _timestamp(parsed, name)

        try:
            data_range = payload["data_range"]
            if not isinstance(data_range, list) or len(data_range) != 2:
                raise ValueError("data_range phải có đúng hai mốc.")
            raw_samples = payload["samples"]
            raw_exclusions = payload["exclusions"]
            symbols = payload["symbols"]
            if not isinstance(raw_samples, list) or not isinstance(raw_exclusions, list):
                raise ValueError("samples/exclusions phải là danh sách.")
            if not isinstance(symbols, list) or any(not isinstance(item, str) for item in symbols):
                raise ValueError("symbols phải là danh sách chuỗi.")
            sample_base_keys = {
                "sample_id",
                "decision_at",
                "feature_available_at",
                "outcome_start",
                "outcome_end",
                "feature_snapshot_hash",
                "outcome_id",
                "outcome_hash",
                "label_hash",
                "label_value",
                "label_version",
                "split",
                "source_dependent",
            }
            legacy_lineage_keys = {"source_rights_hashes", "source_revision_refs"}

            def parse_sample(item: object) -> DatasetSample:
                if not isinstance(item, dict):
                    raise ValueError("Sample canonical phải là object.")
                lineage_keys = (
                    {"source_references"}
                    if manifest_version == "dataset-manifest-v3"
                    else legacy_lineage_keys
                )
                if set(item) != sample_base_keys | lineage_keys:
                    raise ValueError("Sample canonical có schema không hỗ trợ.")
                references: tuple[DatasetSourceReference, ...] = ()
                rights_hashes: tuple[str, ...] = ()
                revision_refs: tuple[str, ...] = ()
                if manifest_version == "dataset-manifest-v3":
                    raw_references = item["source_references"]
                    if not isinstance(raw_references, list):
                        raise ValueError("source_references phải là danh sách.")
                    reference_keys = {
                        "source_id",
                        "item_id",
                        "revision_id",
                        "policy_id",
                        "policy_sequence",
                        "purpose",
                        "rights_hash",
                    }
                    parsed_references = []
                    for reference in raw_references:
                        if not isinstance(reference, dict) or set(reference) != reference_keys:
                            raise ValueError("Typed source reference có schema không hỗ trợ.")
                        parsed_references.append(
                            DatasetSourceReference(
                                source_id=reference["source_id"],
                                item_id=reference["item_id"],
                                revision_id=reference["revision_id"],
                                policy_id=reference["policy_id"],
                                policy_sequence=reference["policy_sequence"],
                                purpose=SourceUsePurpose(reference["purpose"]),
                                rights_hash=reference["rights_hash"],
                            )
                        )
                    references = tuple(parsed_references)
                else:
                    rights_hashes = tuple(item["source_rights_hashes"])
                    revision_refs = tuple(item["source_revision_refs"])
                return DatasetSample(
                    sample_id=item["sample_id"],
                    decision_at=parse_timestamp(item["decision_at"], "decision_at"),
                    feature_available_at=parse_timestamp(
                        item["feature_available_at"], "feature_available_at"
                    ),
                    outcome_start=parse_timestamp(item["outcome_start"], "outcome_start"),
                    outcome_end=parse_timestamp(item["outcome_end"], "outcome_end"),
                    feature_snapshot_hash=item["feature_snapshot_hash"],
                    outcome_id=item["outcome_id"],
                    outcome_hash=item["outcome_hash"],
                    label_hash=item["label_hash"],
                    label_value=item["label_value"],
                    label_version=item["label_version"],
                    split=DatasetSplit(item["split"]),
                    source_dependent=item["source_dependent"],
                    source_rights_hashes=rights_hashes,
                    source_revision_refs=revision_refs,
                    source_references=references,
                )

            samples = tuple(parse_sample(item) for item in raw_samples)
            exclusions = tuple(
                DatasetExclusion(item["sample_id"], tuple(item["reasons"]))
                for item in raw_exclusions
                if isinstance(item, dict) and set(item) == {"sample_id", "reasons"}
            )
            if len(exclusions) != len(raw_exclusions):
                raise ValueError("Exclusion canonical có schema không hỗ trợ.")
            holdout = payload["holdout_sealed_at"]
            if holdout is not None and not isinstance(holdout, str):
                raise ValueError("holdout_sealed_at phải là chuỗi hoặc null.")
            manifest = cls(
                workspace_id=UUID(payload["workspace_id"]),
                query_hash=payload["query_hash"],
                symbols=tuple(symbols),
                data_start=parse_timestamp(data_range[0], "data_start"),
                data_end=parse_timestamp(data_range[1], "data_end"),
                as_of=parse_timestamp(payload["as_of"], "as_of"),
                feature_schema_version=payload["feature_schema_version"],
                feature_version=payload["feature_version"],
                label_version=payload["label_version"],
                sample_schema_version=payload["sample_schema_version"],
                split_policy_version=payload["split_policy_version"],
                samples=samples,
                exclusions=exclusions,
                holdout_sealed_at=parse_timestamp(holdout, "holdout_sealed_at")
                if holdout is not None
                else None,
                manifest_version=payload["manifest_version"],
                retention_policy_ref=payload["retention_policy_ref"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("canonical manifest fields are invalid.") from error
        if manifest.canonical_json != canonical_json:
            raise ValueError("canonical manifest JSON is not in canonical form.")
        if manifest.content_hash != expected_content_hash:
            raise ValueError("canonical manifest hash does not match.")
        return manifest
