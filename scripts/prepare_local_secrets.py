import secrets
import sys
from pathlib import Path


def main() -> None:
    """Tạo secret hạ tầng local; không tự tạo hoặc gọi Binance API key."""
    secret_directory = Path(".secrets")
    secret_directory.mkdir(mode=0o700, exist_ok=True)
    generated_secrets = {
        "postgres_password": secrets.token_urlsafe(48),
        "garage_rpc_secret": secrets.token_hex(32),
        "garage_s3_access_key": f"GK{secrets.token_hex(16)}",
        "garage_s3_secret_key": secrets.token_hex(32),
    }
    for name, value in generated_secrets.items():
        path = secret_directory / name
        if not path.exists():
            path.write_text(value, encoding="utf-8")
    for name in ("binance_api_key", "binance_api_secret"):
        path = secret_directory / name
        if not path.exists():
            path.write_text("", encoding="utf-8")
    garage_access_key = (secret_directory / "garage_s3_access_key").read_text(encoding="utf-8")
    garage_secret_key = (secret_directory / "garage_s3_secret_key").read_text(encoding="utf-8")
    (secret_directory / "garage.env").write_text(
        "\n".join(
            (
                f"GARAGE_DEFAULT_ACCESS_KEY={garage_access_key}",
                f"GARAGE_DEFAULT_SECRET_KEY={garage_secret_key}",
                "GARAGE_DEFAULT_BUCKET=crypidian-artifacts",
                "",
            )
        ),
        encoding="utf-8",
    )
    sys.stdout.reconfigure(encoding="utf-8")
    print("Đã chuẩn bị secret local; Binance API key/secret đang để trống.")


if __name__ == "__main__":
    main()
