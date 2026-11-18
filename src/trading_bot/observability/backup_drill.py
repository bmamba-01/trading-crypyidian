"""Công cụ diễn tập Backup, Restore cô lập và đo RPO/RTO thực tế (OPS07C)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from trading_bot.observability.redaction import contains_secret_canary

_FORBIDDEN_PROD_TARGETS: frozenset[str] = frozenset({"production", "prod", "live"})
_ENCRYPTED_MAGIC = b"CRYPBKP1"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_PBKDF2_ITERATIONS = 200_000


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
    encrypted: bool = False
    plaintext_sha256_hex: str | None = None


@dataclass(frozen=True)
class EncryptedArtifactResult:
    """Kết quả mã hóa tệp sao lưu bằng AES-256-GCM."""

    source_path: Path
    encrypted_path: Path
    plaintext_sha256_hex: str
    ciphertext_sha256_hex: str
    ciphertext_bytes: int


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
    db_verification: Mapping[str, object] | None = None


def _derive_aes_key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise RestoreVerificationError("Passphrase mã hóa sao lưu không được để trống.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def check_disk_space_available(target_path: Path, *, min_free_bytes: int) -> int:
    """Kiểm tra dung lượng đĩa trống tối thiểu trước khi backup/restore."""
    probe = target_path if target_path.exists() else target_path.parent
    probe.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(probe).free
    if free_bytes < min_free_bytes:
        raise RestoreVerificationError(
            f"Cảnh báo đầy đĩa tại '{probe}': còn {free_bytes} bytes, "
            f"yêu cầu tối thiểu {min_free_bytes} bytes."
        )
    return free_bytes


def validate_isolated_target(
    target_environment: str,
    *,
    source_project: str | None = None,
    target_project: str | None = None,
) -> None:
    """Đảm bảo mục tiêu phục hồi luôn cô lập, không đè production hoặc project nguồn."""
    normalized_env = target_environment.strip().lower()
    if not normalized_env or normalized_env in _FORBIDDEN_PROD_TARGETS:
        raise RestoreVerificationError(
            f"Từ chối phục hồi lên '{target_environment}'; chỉ cho phép môi trường cô lập."
        )
    if source_project and target_project and source_project.strip() == target_project.strip():
        raise RestoreVerificationError(
            f"Từ chối phục hồi đè lên cùng Compose project nguồn '{source_project}'."
        )


def encrypt_backup_artifact(
    source_path: Path,
    encrypted_path: Path,
    *,
    passphrase: str,
    secret_canaries: Iterable[str] = (),
) -> EncryptedArtifactResult:
    """Kiểm tra secret canary rồi mã hóa tệp sao lưu bằng AES-256-GCM."""
    if not source_path.is_file():
        raise RestoreVerificationError(f"Không tìm thấy tệp nguồn để mã hóa: {source_path}")
    _check_no_plain_secrets(source_path, secret_canaries)
    _, plaintext_sha256 = _sha256_file(source_path)
    plaintext = source_path.read_bytes()
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    key = _derive_aes_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _ENCRYPTED_MAGIC)

    encrypted_path.parent.mkdir(parents=True, exist_ok=True)
    encrypted_path.write_bytes(_ENCRYPTED_MAGIC + salt + nonce + ciphertext)
    ciphertext_bytes, ciphertext_sha256 = _sha256_file(encrypted_path)
    return EncryptedArtifactResult(
        source_path=source_path,
        encrypted_path=encrypted_path,
        plaintext_sha256_hex=plaintext_sha256,
        ciphertext_sha256_hex=ciphertext_sha256,
        ciphertext_bytes=ciphertext_bytes,
    )


def decrypt_backup_artifact(
    encrypted_path: Path,
    destination_path: Path,
    *,
    passphrase: str,
) -> Path:
    """Giải mã tệp sao lưu AES-256-GCM; phát hiện sai passphrase hoặc dữ liệu bị hỏng."""
    if not encrypted_path.is_file():
        raise RestoreVerificationError(f"Không tìm thấy tệp mã hóa: {encrypted_path}")
    raw = encrypted_path.read_bytes()
    header_len = len(_ENCRYPTED_MAGIC) + _SALT_BYTES + _NONCE_BYTES
    if len(raw) <= header_len or not raw.startswith(_ENCRYPTED_MAGIC):
        raise RestoreVerificationError(
            f"Định dạng tệp sao lưu mã hóa không hợp lệ: '{encrypted_path.name}'."
        )
    salt_start = len(_ENCRYPTED_MAGIC)
    nonce_start = salt_start + _SALT_BYTES
    salt = raw[salt_start:nonce_start]
    nonce = raw[nonce_start:header_len]
    ciphertext = raw[header_len:]
    key = _derive_aes_key(passphrase, salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _ENCRYPTED_MAGIC)
    except InvalidTag as exc:
        raise RestoreVerificationError(
            f"Giải mã '{encrypted_path.name}' thất bại: sai khóa hoặc dữ liệu bị hỏng."
        ) from exc

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(plaintext)
    return destination_path


def serialize_backup_manifest(manifest: BackupManifest) -> str:
    """Xuất BackupManifest sang chuỗi JSON để lưu trữ và kiểm tra đối chiếu."""
    payload = {
        "schema_version": 1,
        "backup_id": manifest.backup_id,
        "created_at": manifest.created_at.isoformat(),
        "operator_id": manifest.operator_id,
        "artifacts": [
            {
                "name": item.name,
                "filename": item.filename,
                "byte_count": item.byte_count,
                "sha256_hex": item.sha256_hex,
                "encrypted": item.encrypted,
                "plaintext_sha256_hex": item.plaintext_sha256_hex,
            }
            for item in manifest.artifacts
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_backup_manifest(path: Path) -> BackupManifest:
    """Đọc BackupManifest từ tệp JSON."""
    if not path.is_file():
        raise RestoreVerificationError(f"Không tìm thấy manifest sao lưu: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    created_at = datetime.fromisoformat(str(data["created_at"]))
    records = tuple(
        BackupArtifactRecord(
            name=str(item["name"]),
            filename=str(item["filename"]),
            byte_count=int(item["byte_count"]),
            sha256_hex=str(item["sha256_hex"]),
            encrypted=bool(item.get("encrypted", False)),
            plaintext_sha256_hex=(
                str(item["plaintext_sha256_hex"])
                if item.get("plaintext_sha256_hex") is not None
                else None
            ),
        )
        for item in data.get("artifacts", [])
    )
    if not records:
        raise RestoreVerificationError("Manifest sao lưu không có artifact nào.")
    return BackupManifest(
        backup_id=str(data["backup_id"]),
        created_at=created_at,
        operator_id=str(data["operator_id"]),
        artifacts=records,
    )


def format_restore_drill_report_json(report: RestoreDrillReport) -> str:
    """Xuất báo cáo diễn tập phục hồi (kèm RPO/RTO thực đo) sang JSON."""
    payload = {
        "backup_id": report.backup_id,
        "target_environment": report.target_environment,
        "verified": report.verified,
        "rpo_seconds": report.rpo_seconds,
        "rto_seconds": report.rto_seconds,
        "rpo_within_sla": report.rpo_within_sla,
        "rto_within_sla": report.rto_within_sla,
        "restored_files": list(report.restored_files),
        "detail_vi": report.detail_vi,
        "db_verification": dict(report.db_verification) if report.db_verification else None,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
    encrypted: bool = False,
    passphrase: str | None = None,
    output_dir: Path | None = None,
) -> BackupManifest:
    """Kiểm tra an toàn bí mật, tùy chọn mã hóa AES-256-GCM và tạo BackupManifest."""
    if created_at.tzinfo is None:
        raise ValueError("created_at phải có timezone.")
    canary_list = tuple(secret_canaries)
    records: list[BackupArtifactRecord] = []
    for item in artifacts:
        if not item.path.is_file():
            raise RestoreVerificationError(f"Không tìm thấy tệp sao lưu: {item.path}")
        _check_no_plain_secrets(item.path, canary_list)
        if encrypted:
            if not passphrase:
                raise RestoreVerificationError("Cần passphrase khi tạo backup mã hóa.")
            enc_dir = output_dir or item.path.parent
            enc_path = enc_dir / f"{item.path.name}.enc"
            enc_res = encrypt_backup_artifact(
                item.path,
                enc_path,
                passphrase=passphrase,
                secret_canaries=canary_list,
            )
            records.append(
                BackupArtifactRecord(
                    name=item.name,
                    filename=enc_path.name,
                    byte_count=enc_res.ciphertext_bytes,
                    sha256_hex=enc_res.ciphertext_sha256_hex,
                    encrypted=True,
                    plaintext_sha256_hex=enc_res.plaintext_sha256_hex,
                )
            )
        else:
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
    passphrase: str | None = None,
    source_project: str | None = None,
    target_project: str | None = None,
    min_free_bytes: int = 0,
) -> RestoreDrillReport:
    """Chạy phục hồi vào thư mục cô lập, giải mã nếu cần, xác minh checksum và đo RPO/RTO."""
    if incident_at.tzinfo is None:
        raise ValueError("incident_at phải có timezone.")
    validate_isolated_target(
        target_environment,
        source_project=source_project,
        target_project=target_project,
    )
    if min_free_bytes > 0:
        check_disk_space_available(restore_dir, min_free_bytes=min_free_bytes)

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
        if expected.encrypted:
            if not passphrase:
                raise RestoreVerificationError(
                    f"Cần passphrase để giải mã artifact '{expected.name}'."
                )
            plain_name = expected.filename.removesuffix(".enc")
            destination = restore_dir / plain_name
            decrypt_backup_artifact(source_path, destination, passphrase=passphrase)
            if expected.plaintext_sha256_hex is not None:
                _, plain_digest = _sha256_file(destination)
                if plain_digest != expected.plaintext_sha256_hex:
                    raise RestoreVerificationError(
                        f"Sai lệch checksum bản rõ sau giải mã cho '{expected.name}'."
                    )
            restored_names.append(plain_name)
        else:
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
