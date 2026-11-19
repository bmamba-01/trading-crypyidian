import base64
import os
import secrets
import stat
import sys
from pathlib import Path

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_DATABASE_ROLES: tuple[str, ...] = (
    "admin_web",
    "trading_runtime",
    "execution_gateway",
    "ingestion_worker",
    "signal_worker",
    "market_signal_worker",
    "trainer_worker",
    "learning_evaluator",
    "learning_shadow_observer",
    "replay_writer",
    "candidate_builder",
    "backtest_worker",
    "scorer_history",
)


def _write_secret_file(path: Path, content: str, *, overwrite: bool) -> None:
    """Ghi tệp bí mật với quyền POSIX 0600 ngay từ lúc tạo và ép chmod 0600 nếu đã tồn tại."""
    if path.is_symlink():
        raise PermissionError(f"Từ chối ghi đè symlink tại '{path}'.")
    if path.exists() and not overwrite:
        os.chmod(path, _FILE_MODE)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, _FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if path.exists() and not path.is_symlink():
            os.chmod(path, _FILE_MODE)


def prepare_local_secrets(secret_directory: Path = Path(".secrets")) -> tuple[Path, ...]:
    """Tạo secret hạ tầng local, ép thư mục 0700 và mọi tệp secret về quyền 0600."""
    if secret_directory.is_symlink():
        raise PermissionError(f"Thư mục bí mật '{secret_directory}' không được là symlink.")
    secret_directory.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
    os.chmod(secret_directory, _DIRECTORY_MODE)

    generated_secrets = {
        "postgres_password": secrets.token_urlsafe(48),
        "garage_rpc_secret": secrets.token_hex(32),
        "garage_s3_access_key": f"GK{secrets.token_hex(16)}",
        "garage_s3_secret_key": secrets.token_hex(32),
        "mfa_encryption_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "bootstrap_token": secrets.token_urlsafe(48),
        "telegram_webhook_secret": secrets.token_urlsafe(32),
        **{f"database_{role}_password": secrets.token_urlsafe(48) for role in _DATABASE_ROLES},
        "telegram_bot_token": "",
        "binance_api_key": "",
        "binance_api_secret": "",
    }
    for name, value in generated_secrets.items():
        _write_secret_file(secret_directory / name, value, overwrite=False)

    garage_access_key = (secret_directory / "garage_s3_access_key").read_text(encoding="utf-8")
    garage_secret_key = (secret_directory / "garage_s3_secret_key").read_text(encoding="utf-8")
    _write_secret_file(
        secret_directory / "garage.env",
        "\n".join(
            (
                f"GARAGE_DEFAULT_ACCESS_KEY={garage_access_key}",
                f"GARAGE_DEFAULT_SECRET_KEY={garage_secret_key}",
                "GARAGE_DEFAULT_BUCKET=crypidian-artifacts",
                "",
            )
        ),
        overwrite=True,
    )

    hardened_files: list[Path] = []
    for entry in sorted(secret_directory.iterdir(), key=lambda p: p.name):
        entry_stat = os.lstat(entry)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise PermissionError(
                f"Phát hiện symlink không hợp lệ trong '{secret_directory}': {entry.name}"
            )
        if stat.S_ISREG(entry_stat.st_mode):
            os.chmod(entry, _FILE_MODE)
            hardened_files.append(entry)
    return tuple(hardened_files)


def main() -> None:
    """Tạo secret hạ tầng local; không tự tạo hoặc gọi Binance API key."""
    prepare_local_secrets(Path(".secrets"))
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if reconfigure_stdout is not None:
        reconfigure_stdout(encoding="utf-8")
    print("Đã chuẩn bị secret local; Binance API key/secret đang để trống.")


if __name__ == "__main__":
    main()
