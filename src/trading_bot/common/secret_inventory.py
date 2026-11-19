"""Kiểm kê danh mục secret hệ thống và xác minh quyền truy cập POSIX 0600/0700 (SEC-44)."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REQUIRED_DIRECTORY_MODE = 0o700
REQUIRED_FILE_MODE = 0o600
_GROUP_OTHER_MASK = 0o077
_REQUIRED_INVENTORY_NAMES: frozenset[str] = frozenset(
    {
        "postgres_password",
        "database_admin_web_password",
        "database_trading_runtime_password",
        "database_execution_gateway_password",
        "database_ingestion_worker_password",
        "database_signal_worker_password",
        "database_market_signal_worker_password",
        "database_trainer_worker_password",
        "database_learning_evaluator_password",
        "database_learning_shadow_observer_password",
        "database_replay_writer_password",
        "database_candidate_builder_password",
        "database_backtest_worker_password",
        "database_scorer_history_password",
        "mfa_encryption_key",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "binance_api_key",
        "binance_api_secret",
        "bootstrap_token",
    }
)


@dataclass(frozen=True, slots=True)
class SecretInventorySpec:
    """Đặc tả kiểm kê, quyền sở hữu và chu kỳ xoay vòng tối đa của một secret."""

    name: str
    category: str
    owner_services: tuple[str, ...]
    description_vi: str
    max_rotation_days: int
    storage_local: str
    storage_production: str
    rotation_procedure: str
    requires_key_id: bool = False
    required_in_local: bool = True
    allow_empty_in_local: bool = False


@dataclass(frozen=True, slots=True)
class SecretFileCheck:
    """Kết quả kiểm tra từng tệp secret trong thư mục bí mật."""

    name: str
    category: str
    exists: bool
    mode_octal: str | None
    permission_ok: bool
    max_rotation_days: int
    key_id: str | None = None


@dataclass(frozen=True, slots=True)
class SecretInventoryReport:
    """Báo cáo kiểm tra danh mục secret, key_id MFA và quyền tệp 0600."""

    passed: bool
    directory_mode_octal: str | None
    checked_count: int
    mfa_key_id: str | None
    items: tuple[SecretFileCheck, ...]
    violations: tuple[str, ...]
    rotation_warnings: tuple[str, ...]


def derive_mfa_key_id(key_bytes: bytes) -> str:
    """Sinh định danh key_id tất định không làm lộ nội dung khóa AES-256."""
    if len(key_bytes) != 32:
        raise ValueError("Khóa mã hóa MFA phải dài chính xác 32 byte.")
    digest = hashlib.sha256(key_bytes).hexdigest()[:12]
    return f"mfa-v1-{digest}"


def _db_role_spec(role_suffix: str, service_name: str, description: str) -> SecretInventorySpec:
    return SecretInventorySpec(
        name=f"database_{role_suffix}_password",
        category="database_role",
        owner_services=(service_name, "db-provisioner"),
        description_vi=description,
        max_rotation_days=90,
        storage_local=f".secrets/database_{role_suffix}_password",
        storage_production="Vault / Secrets Manager động hoặc secret mount tách theo service",
        rotation_procedure=(
            "Cập nhật secret mới (0600), chạy scripts/provision_database_roles.py (ALTER ROLE) "
            f"rồi recreate {service_name}."
        ),
    )


SYSTEM_SECRET_INVENTORY: tuple[SecretInventorySpec, ...] = (
    SecretInventorySpec(
        name="postgres_password",
        category="infrastructure",
        owner_services=("postgres", "migrator", "db-provisioner", "qa-seed"),
        description_vi="Mật khẩu tài khoản quản trị PostgreSQL crypidian_admin.",
        max_rotation_days=90,
        storage_local=".secrets/postgres_password",
        storage_production="Vault / KMS / Managed PostgreSQL Secret",
        rotation_procedure=(
            "ALTER USER crypidian_admin, cập nhật secret cho migrator/db-provisioner "
            "và kiểm tra kết nối trước khi xóa mật khẩu cũ."
        ),
    ),
    SecretInventorySpec(
        name="garage_rpc_secret",
        category="infrastructure",
        owner_services=("object-store",),
        description_vi="Khóa RPC nội bộ của cụm lưu trữ đối tượng Garage S3.",
        max_rotation_days=90,
        storage_local=".secrets/garage_rpc_secret",
        storage_production="Vault / KMS secret cho dịch vụ object store",
        rotation_procedure="Cập nhật secret RPC mới, recreate object-store và kiểm tra /health.",
    ),
    SecretInventorySpec(
        name="garage_s3_access_key",
        category="infrastructure",
        owner_services=("object-store",),
        description_vi="Access key ID truy cập bucket crypidian-artifacts trên Garage S3.",
        max_rotation_days=90,
        storage_local=".secrets/garage_s3_access_key",
        storage_production="Vault / IAM Role (IRSA/Workload Identity)",
        rotation_procedure="Tạo cặp S3 key mới, cập nhật garage.env và thu hồi key cũ.",
    ),
    SecretInventorySpec(
        name="garage_s3_secret_key",
        category="infrastructure",
        owner_services=("object-store",),
        description_vi="Secret access key truy cập Garage S3.",
        max_rotation_days=90,
        storage_local=".secrets/garage_s3_secret_key",
        storage_production="Vault / IAM Role (IRSA/Workload Identity)",
        rotation_procedure="Tạo cặp S3 key mới, cập nhật garage.env và thu hồi key cũ.",
    ),
    SecretInventorySpec(
        name="garage.env",
        category="infrastructure",
        owner_services=("object-store",),
        description_vi="Tệp biến môi trường khởi tạo S3 mặc định cho container object-store.",
        max_rotation_days=90,
        storage_local=".secrets/garage.env",
        storage_production="Vault agent / KMS env injector không lưu trên đĩa host",
        rotation_procedure="Cập nhật cặp S3 key mới rồi chạy lại prepare_local_secrets.py.",
    ),
    _db_role_spec("admin_web", "admin-web", "Mật khẩu role PostgreSQL crypidian_admin_web."),
    _db_role_spec(
        "trading_runtime",
        "trading-runtime",
        "Mật khẩu role PostgreSQL crypidian_trading_runtime.",
    ),
    _db_role_spec(
        "execution_gateway",
        "execution-gateway",
        "Mật khẩu role PostgreSQL crypidian_execution_gateway.",
    ),
    _db_role_spec(
        "ingestion_worker",
        "ingestion-worker",
        "Mật khẩu role PostgreSQL crypidian_ingestion_worker.",
    ),
    _db_role_spec(
        "signal_worker",
        "telegram-signal-worker",
        "Mật khẩu role PostgreSQL crypidian_signal_worker.",
    ),
    _db_role_spec(
        "market_signal_worker",
        "market-signal-worker",
        "Mật khẩu role PostgreSQL crypidian_market_signal_worker.",
    ),
    _db_role_spec(
        "trainer_worker",
        "trainer-worker",
        "Mật khẩu role PostgreSQL crypidian_trainer_worker.",
    ),
    _db_role_spec(
        "learning_evaluator",
        "learning-evaluator",
        "Mật khẩu role PostgreSQL crypidian_learning_evaluator.",
    ),
    _db_role_spec(
        "learning_shadow_observer",
        "learning-shadow-observer",
        "Mật khẩu role PostgreSQL crypidian_learning_shadow_observer.",
    ),
    _db_role_spec(
        "replay_writer",
        "backtest-worker",
        "Mật khẩu role PostgreSQL crypidian_replay_writer.",
    ),
    _db_role_spec(
        "candidate_builder",
        "candidate-proof-builder",
        "Mật khẩu role PostgreSQL crypidian_candidate_builder.",
    ),
    _db_role_spec(
        "backtest_worker",
        "backtest-worker",
        "Mật khẩu role PostgreSQL crypidian_backtest_worker.",
    ),
    _db_role_spec(
        "scorer_history",
        "admin-web",
        "Mật khẩu role PostgreSQL chỉ đọc lịch sử scorer crypidian_scorer_history.",
    ),
    SecretInventorySpec(
        name="mfa_encryption_key",
        category="mfa_crypto",
        owner_services=("admin-web", "qa-seed"),
        description_vi="Khóa AES-256-GCM mã hóa seed TOTP kèm key_id/version trong ciphertext.",
        max_rotation_days=365,
        storage_local=".secrets/mfa_encryption_key",
        storage_production="Cloud KMS / HashiCorp Vault Transit keyring có key_id phiên bản",
        rotation_procedure=(
            "Nạp khóa mới với key_id mới, giữ khóa cũ trong previous_keys, chạy "
            "rotate_mfa_secret cho toàn bộ ciphertext rồi gỡ khóa cũ."
        ),
        requires_key_id=True,
    ),
    SecretInventorySpec(
        name="telegram_bot_token",
        category="telegram",
        owner_services=("admin-web", "telegram-signal-worker"),
        description_vi="Token Bot API Telegram (nạp qua SecretProvider lease).",
        max_rotation_days=365,
        storage_local=".secrets/telegram_bot_token",
        storage_production="Secret Vault tham chiếu qua TRADING_BOT_TELEGRAM_BOT_TOKEN_REFERENCE",
        rotation_procedure=(
            "Thu hồi trên @BotFather (/revoke), cập nhật SecretProvider và setWebhook."
        ),
        allow_empty_in_local=True,
    ),
    SecretInventorySpec(
        name="telegram_webhook_secret",
        category="telegram",
        owner_services=("admin-web",),
        description_vi="Mã bí mật X-Telegram-Bot-Api-Secret-Token xác thực webhook Telegram.",
        max_rotation_days=90,
        storage_local=".secrets/telegram_webhook_secret",
        storage_production="Secret Vault / mount chỉ đọc riêng cho admin-web",
        rotation_procedure="Sinh token >= 32 ký tự mới, gọi setWebhook với secret_token mới.",
    ),
    SecretInventorySpec(
        name="binance_api_key",
        category="exchange",
        owner_services=("execution-gateway",),
        description_vi=(
            "API Key Binance USD-M (Ed25519/RSA, IP allowlist, cấm withdrawal/transfer)."
        ),
        max_rotation_days=90,
        storage_local=".secrets/binance_api_key",
        storage_production="Vault / KMS lease ngắn chỉ cấp cho execution-gateway",
        rotation_procedure=(
            "Tạo cặp key Ed25519 mới kèm IP allowlist, kiểm apiRestrictions, chuyển gateway rồi "
            "xóa key cũ."
        ),
        allow_empty_in_local=True,
    ),
    SecretInventorySpec(
        name="binance_api_secret",
        category="exchange",
        owner_services=("execution-gateway",),
        description_vi="Private key / API Secret Binance USD-M gắn riêng với execution-gateway.",
        max_rotation_days=90,
        storage_local=".secrets/binance_api_secret",
        storage_production="Vault / KMS lease ngắn chỉ cấp cho execution-gateway",
        rotation_procedure=(
            "Thay thế đồng bộ cùng binance_api_key, xác minh ký Ed25519/apiRestrictions và xóa cũ."
        ),
        allow_empty_in_local=True,
    ),
    SecretInventorySpec(
        name="bootstrap_token",
        category="bootstrap",
        owner_services=("admin-web",),
        description_vi="Token dùng một lần khởi tạo Owner đầu tiên khi hệ thống mới dựng.",
        max_rotation_days=1,
        storage_local=".secrets/bootstrap_token",
        storage_production="Token dùng một lần, xóa ngay sau khi khởi tạo Owner đầu tiên",
        rotation_procedure="Sau khi hoàn tất bootstrap Owner, xóa tệp và gỡ mount khỏi compose.",
    ),
)


def _format_mode(mode: int) -> str:
    return f"0o{mode:03o}"


def _validate_inventory_metadata(inventory: Sequence[SecretInventorySpec]) -> list[str]:
    violations: list[str] = []
    by_name = {spec.name: spec for spec in inventory}
    missing = sorted(_REQUIRED_INVENTORY_NAMES - by_name.keys())
    if missing:
        violations.append("Danh mục kiểm kê secret thiếu các khóa bắt buộc: " + ", ".join(missing))
    mfa_spec = by_name.get("mfa_encryption_key")
    if mfa_spec is not None and not mfa_spec.requires_key_id:
        violations.append("Danh mục mfa_encryption_key phải bật requires_key_id=True.")
    for spec in inventory:
        if spec.max_rotation_days < 1:
            violations.append(f"Chu kỳ xoay vòng tối đa của '{spec.name}' phải >= 1 ngày.")
        if not spec.owner_services:
            violations.append(f"Secret '{spec.name}' chưa khai báo owner_services.")
    return violations


def _read_mfa_key_id(path: Path) -> str:
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError("Khóa mã hóa MFA không được rỗng.")
    decoded = base64.urlsafe_b64decode(raw_text.encode("ascii"))
    return derive_mfa_key_id(decoded)


def verify_secret_inventory_and_permissions(
    secrets_dir: Path,
    *,
    inventory: Sequence[SecretInventorySpec] = SYSTEM_SECRET_INVENTORY,
    require_all_files: bool = True,
    check_rotation_age: bool = False,
    now_utc: datetime | None = None,
) -> SecretInventoryReport:
    """Kiểm tra danh mục secret hệ thống, key_id MFA, chu kỳ xoay và quyền 0700/0600."""
    violations = _validate_inventory_metadata(inventory)
    rotation_warnings: list[str] = []
    items: list[SecretFileCheck] = []
    mfa_key_id: str | None = None

    if not secrets_dir.exists():
        violations.append(f"Thư mục bí mật '{secrets_dir}' không tồn tại.")
        return SecretInventoryReport(
            passed=False,
            directory_mode_octal=None,
            checked_count=0,
            mfa_key_id=None,
            items=(),
            violations=tuple(violations),
            rotation_warnings=(),
        )

    dir_stat = os.lstat(secrets_dir)
    if stat.S_ISLNK(dir_stat.st_mode) or not stat.S_ISDIR(dir_stat.st_mode):
        violations.append(f"Đường dẫn '{secrets_dir}' phải là thư mục thật, không phải symlink.")
        return SecretInventoryReport(
            passed=False,
            directory_mode_octal=None,
            checked_count=0,
            mfa_key_id=None,
            items=(),
            violations=tuple(violations),
            rotation_warnings=(),
        )

    dir_mode = stat.S_IMODE(dir_stat.st_mode)
    directory_mode_octal = _format_mode(dir_mode)
    if dir_mode != REQUIRED_DIRECTORY_MODE:
        violations.append(
            f"Thư mục '{secrets_dir}' có quyền {directory_mode_octal}, bắt buộc là 0o700."
        )

    reference_now = now_utc or datetime.now(UTC)
    known_names = {spec.name for spec in inventory}

    for spec in inventory:
        target = secrets_dir / spec.name
        if not target.exists():
            if require_all_files and spec.required_in_local:
                violations.append(f"Thiếu tệp bí mật bắt buộc '{spec.name}' trong '{secrets_dir}'.")
            items.append(
                SecretFileCheck(
                    name=spec.name,
                    category=spec.category,
                    exists=False,
                    mode_octal=None,
                    permission_ok=not (require_all_files and spec.required_in_local),
                    max_rotation_days=spec.max_rotation_days,
                )
            )
            continue

        file_stat = os.lstat(target)
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            violations.append(f"Tệp bí mật '{spec.name}' không được là symlink hoặc thư mục.")
            items.append(
                SecretFileCheck(
                    name=spec.name,
                    category=spec.category,
                    exists=True,
                    mode_octal=None,
                    permission_ok=False,
                    max_rotation_days=spec.max_rotation_days,
                )
            )
            continue

        file_mode = stat.S_IMODE(file_stat.st_mode)
        mode_octal = _format_mode(file_mode)
        permission_ok = file_mode == REQUIRED_FILE_MODE
        if file_mode & _GROUP_OTHER_MASK:
            violations.append(
                f"Tệp bí mật '{spec.name}' có quyền {mode_octal} cho phép group/other "
                "đọc/ghi (bắt buộc 0o600)."
            )
        elif not permission_ok:
            violations.append(f"Tệp bí mật '{spec.name}' có quyền {mode_octal} (bắt buộc 0o600).")

        file_key_id: str | None = None
        if spec.requires_key_id:
            try:
                file_key_id = _read_mfa_key_id(target)
                mfa_key_id = file_key_id
            except OSError, ValueError, UnicodeEncodeError:
                violations.append(f"Khóa MFA '{spec.name}' không hợp lệ hoặc không thể gán key_id.")
        elif not spec.allow_empty_in_local:
            try:
                if not target.read_text(encoding="utf-8").strip():
                    violations.append(f"Tệp bí mật '{spec.name}' không được để trống.")
            except OSError:
                violations.append(f"Không thể đọc tệp bí mật '{spec.name}'.")

        if check_rotation_age:
            modified_at = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)
            age_days = (reference_now - modified_at).total_seconds() / 86_400.0
            if age_days > spec.max_rotation_days:
                rotation_warnings.append(
                    f"Secret '{spec.name}' đã {age_days:.1f} ngày chưa xoay "
                    f"(vượt ngưỡng {spec.max_rotation_days} ngày)."
                )

        items.append(
            SecretFileCheck(
                name=spec.name,
                category=spec.category,
                exists=True,
                mode_octal=mode_octal,
                permission_ok=permission_ok,
                max_rotation_days=spec.max_rotation_days,
                key_id=file_key_id,
            )
        )

    for entry in sorted(secrets_dir.iterdir(), key=lambda p: p.name):
        if entry.name in known_names:
            continue
        entry_stat = os.lstat(entry)
        if stat.S_ISLNK(entry_stat.st_mode):
            violations.append(f"Tệp '{entry.name}' trong '{secrets_dir}' không được là symlink.")
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            violations.append(f"Mục '{entry.name}' trong '{secrets_dir}' không phải tệp thường.")
            continue
        entry_mode = stat.S_IMODE(entry_stat.st_mode)
        if entry_mode & _GROUP_OTHER_MASK or entry_mode != REQUIRED_FILE_MODE:
            violations.append(
                f"Tệp bí mật bổ sung '{entry.name}' có quyền {_format_mode(entry_mode)} "
                "(bắt buộc 0o600)."
            )

    return SecretInventoryReport(
        passed=len(violations) == 0,
        directory_mode_octal=directory_mode_octal,
        checked_count=len(items),
        mfa_key_id=mfa_key_id,
        items=tuple(items),
        violations=tuple(violations),
        rotation_warnings=tuple(rotation_warnings),
    )


__all__ = [
    "REQUIRED_DIRECTORY_MODE",
    "REQUIRED_FILE_MODE",
    "SYSTEM_SECRET_INVENTORY",
    "SecretFileCheck",
    "SecretInventoryReport",
    "SecretInventorySpec",
    "derive_mfa_key_id",
    "verify_secret_inventory_and_permissions",
]
