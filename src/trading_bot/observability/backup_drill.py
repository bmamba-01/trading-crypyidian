"""Công cụ diễn tập Backup, Restore cô lập và đo RPO/RTO thực tế (OPS07C)."""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading_bot.observability.redaction import contains_secret_canary

_FORBIDDEN_PROD_TARGETS: frozenset[str] = frozenset({"production", "prod", "live"})


class RestoreVerificationError(RuntimeError):
    """Ngoại lệ khi phát hiện lộ bí mật trong backup, sai checksum hoặc mục tiêu không cô lập."""


@dataclass(frozen=True)
class BackupArtifactInput:
    """Thông tin tệp đầu vào cần đưa vào manifest sao lưu."""

    name: str
    path: Path


@dataclass(frozen=True)
class BackupArtifactRecord:
    """Bản ghi bất biến của một tệp sao lưu kèm kích thước và SHA-256."""

    name: str
    filename: str
    byte_count: int
    sha256_hex: str


@dataclass(frozen=True)
class BackupManifest:
    """Manifest sao lưu có checksum cho PostgreSQL dump và object-store."""

    backup_id: str
    created_at: datetime
    operator_id: str
    artifacts: tuple[BackupArtifactRecord, ...]


@dataclass(frozen=True)
class RestoreDrillPolicy:
    """Ngưỡng SLA cho diễn tập phục hồi thảm họa (giây)."""

    max_rpo_seconds: int = 900
    max_rto_seconds: int = 300


@dataclass(frozen=True)
class RestoreDrillReport:
    """Báo cáo kết quả diễn tập phục hồi cô lập kèm số liệu RPO/RTO thực đo."""

    backup_id: str
    target_environment: str
    verified: bool
    rpo_seconds: float
    rto_seconds: float
    rpo_within_sla: bool
    rto_within_sla: bool
    restored_files: tuple[str, ...]
    detail_vi: str


def _sha256_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            total += len(chunk)
            hasher.update(chunk)
    return total, hasher.hexdigest()


def _check_no_plain_secrets(path: Path, secret_canaries: Iterable[str]) -> None:
    canaries = [c for c in secret_canaries if c]
    if not canaries:
        return
    text = path.read_text(encoding="utf-8", errors="ignore")
    if contains_secret_canary(text, canaries):
        raise RestoreVerificationError(
            f"Tệp sao lưu '{path.name}' chứa chuỗi bí mật chưa mã hóa; từ chối đóng gói."
        )


def create_backup_manifest(
    *,
    backup_id: str,
    created_at: datetime,
    operator_id: str,
    artifacts: Iterable[BackupArtifactInput],
    secret_canaries: Iterable[str] = (),
) -> BackupManifest:
    """Kiểm tra an toàn bí mật và tạo BackupManifest kèm checksum SHA-256."""
    if created_at.tzinfo is None:
        raise ValueError("created_at phải có timezone.")
    canary_list = tuple(secret_canaries)
    records: list[BackupArtifactRecord] = []
    for item in artifacts:
        if not item.path.is_file():
            raise RestoreVerificationError(f"Không tìm thấy tệp sao lưu: {item.path}")
        _check_no_plain_secrets(item.path, canary_list)
        byte_count, digest = _sha256_file(item.path)
        records.append(
            BackupArtifactRecord(
                name=item.name,
                filename=item.path.name,
                byte_count=byte_count,
                sha256_hex=digest,
            )
        )
    if not records:
        raise RestoreVerificationError("Backup manifest phải có ít nhất một artifact.")
    return BackupManifest(
        backup_id=backup_id,
        created_at=created_at,
        operator_id=operator_id,
        artifacts=tuple(records),
    )


def execute_isolated_restore_drill(
    *,
    manifest: BackupManifest,
    artifact_sources: Mapping[str, Path],
    restore_dir: Path,
    target_environment: str,
    incident_at: datetime,
    policy: RestoreDrillPolicy | None = None,
) -> RestoreDrillReport:
    """Chạy phục hồi vào thư mục cô lập, xác minh checksum và đo RPO/RTO."""
    if incident_at.tzinfo is None:
        raise ValueError("incident_at phải có timezone.")
    normalized_env = target_environment.strip().lower()
    if not normalized_env or normalized_env in _FORBIDDEN_PROD_TARGETS:
        raise RestoreVerificationError(
            f"Từ chối phục hồi lên '{target_environment}'; chỉ cho phép môi trường cô lập."
        )

    active_policy = policy or RestoreDrillPolicy()
    started = time.monotonic()
    restore_dir.mkdir(parents=True, exist_ok=True)
    restored_names: list[str] = []

    for expected in manifest.artifacts:
        source_path = artifact_sources.get(expected.name)
        if source_path is None or not source_path.is_file():
            raise RestoreVerificationError(f"Thiếu nguồn artifact '{expected.name}' khi phục hồi.")
        actual_bytes, actual_digest = _sha256_file(source_path)
        if actual_bytes != expected.byte_count or actual_digest != expected.sha256_hex:
            raise RestoreVerificationError(
                f"Sai lệch checksum cho '{expected.name}': phát hiện dữ liệu bị hỏng."
            )
        destination = restore_dir / expected.filename
        shutil.copy2(source_path, destination)
        restored_names.append(expected.filename)

    rto_seconds = round(max(0.0, time.monotonic() - started), 4)
    rpo_seconds = max(0.0, (incident_at - manifest.created_at).total_seconds())
    rpo_ok = rpo_seconds <= active_policy.max_rpo_seconds
    rto_ok = rto_seconds <= active_policy.max_rto_seconds

    return RestoreDrillReport(
        backup_id=manifest.backup_id,
        target_environment=target_environment,
        verified=True,
        rpo_seconds=rpo_seconds,
        rto_seconds=rto_seconds,
        rpo_within_sla=rpo_ok,
        rto_within_sla=rto_ok,
        restored_files=tuple(restored_names),
        detail_vi=(
            f"Diễn tập phục hồi cô lập thành công ({len(restored_names)} tệp); "
            f"RPO={rpo_seconds}s, RTO={rto_seconds}s."
        ),
    )
