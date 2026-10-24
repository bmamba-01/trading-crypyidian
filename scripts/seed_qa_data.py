"""Gieo bộ dữ liệu QA (tài khoản kiểm thử, bot, settings, nến) vào PostgreSQL local/test.

Hai lệnh:

- `seed` (mặc định, chạy trên máy dev): kiểm tra chốt an toàn, tạo hoặc đọc lại
  `.secrets/qa_accounts.json`, rồi chạy service Compose `qa-seed` và truyền mật khẩu/TOTP
  seed qua stdin (không qua tham số dòng lệnh hay biến môi trường).
- `apply` (chạy trong container `qa-seed`): kiểm tra môi trường local/PAPER, băm mật khẩu
  bằng `PasswordManager`, mã hóa TOTP bằng `LocalMfaSecretProvider` và ghi dataset idempotent.

Không bao giờ in mật khẩu/TOTP seed ra stdout/log. Logic ghi DB theo lớp nằm ở
`scripts/qa_seed_layers.py`. Xem `docs/testing/qa-test-data.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    # Cho phép `python scripts/seed_qa_data.py` import `scripts.*` và `tests.fixtures.*`.
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.qa_seed_layers import (  # noqa: E402
    AccountCredential,
    SeedGuardError,
    apply_dataset,
)
from tests.fixtures.qa_dataset import QaDataset, build_qa_dataset  # noqa: E402

from trading_bot.adapters.database.engine import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider  # noqa: E402
from trading_bot.auth.security import TotpManager  # noqa: E402

CREDENTIALS_FORMAT = "crypidian-qa-accounts/v1"
DEFAULT_CREDENTIALS_FILE = Path(".secrets") / "qa_accounts.json"
DEFAULT_PROJECT_NAME = "crypidian"
QA_SEED_SERVICE = "qa-seed"
CONTAINER_SCRIPT = "/qa/scripts/seed_qa_data.py"
LOCAL_APP_ENVIRONMENTS = frozenset({"local", "test"})
LOCAL_DATABASE_HOSTS = frozenset({"postgres", "localhost", "127.0.0.1", "::1"})
LOCAL_HOST_SUFFIXES = (".localhost", ".test")
LOOPBACK_DOCKER_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REQUIRED_APPLY_VARIABLES = (
    "TRADING_BOT_DATABASE_USER",
    "TRADING_BOT_DATABASE_PASSWORD_FILE",
    "TRADING_BOT_MFA_ENCRYPTION_KEY_FILE",
    "TRADING_BOT_MFA_SECRET_DIRECTORY",
)
_PASSWORD_MIN_LENGTH = 14
_PASSWORD_MAX_LENGTH = 128
_TOTP_PATTERN = re.compile(r"[A-Z2-7]{16,128}")
_PROJECT_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_CREDENTIALS_WARNING_VI = (
    "Tệp chứa mật khẩu và TOTP seed của tài khoản QA local. Chỉ dùng cho môi trường "
    "local/test; không commit, không chia sẻ, không dán vào issue/log."
)


# ---------------------------------------------------------------------------
# Chốt an toàn (seed guard)
# ---------------------------------------------------------------------------


def require_local_only(local_only: bool) -> None:
    if not local_only:
        raise SeedGuardError(
            "Từ chối chạy: phải truyền --local-only để xác nhận đích là môi trường local/test."
        )


def is_local_database_host(host: str) -> bool:
    """Chỉ chấp nhận host Compose nội bộ, loopback hoặc TLD dành riêng cho kiểm thử."""
    normalized = host.strip().casefold()
    return normalized in LOCAL_DATABASE_HOSTS or any(
        normalized.endswith(suffix) and len(normalized) > len(suffix)
        for suffix in LOCAL_HOST_SUFFIXES
    )


def validate_apply_environment(environ: Mapping[str, str], *, local_only: bool) -> None:
    """Kiểm tra môi trường container trước khi kết nối database."""
    require_local_only(local_only)
    app_env = environ.get("TRADING_BOT_APP_ENV", "").strip().casefold()
    if app_env not in LOCAL_APP_ENVIRONMENTS:
        raise SeedGuardError(
            f"Từ chối chạy: TRADING_BOT_APP_ENV='{app_env or '<trống>'}', chỉ cho local/test."
        )
    mode = environ.get("TRADING_BOT_TRADING_MODE", "").strip().upper()
    if mode != "PAPER":
        raise SeedGuardError(
            f"Từ chối chạy: TRADING_BOT_TRADING_MODE='{mode or '<trống>'}', chỉ cho PAPER."
        )
    host = environ.get("TRADING_BOT_DATABASE_HOST", "")
    if not is_local_database_host(host):
        raise SeedGuardError(
            f"Từ chối chạy: database host '{host or '<trống>'}' không thuộc danh sách local "
            "(postgres, localhost, loopback, *.localhost, *.test)."
        )
    missing = [name for name in REQUIRED_APPLY_VARIABLES if not environ.get(name, "").strip()]
    if missing:
        raise SeedGuardError(f"Thiếu biến môi trường cho seed: {', '.join(missing)}.")


def validate_docker_endpoint(endpoint: str) -> None:
    """Chỉ cho Docker engine trên chính máy này (socket, named pipe hoặc TCP loopback)."""
    value = endpoint.strip()
    if value.startswith(("unix://", "npipe://")):
        return
    if value.startswith("tcp://") and urlsplit(value).hostname in LOOPBACK_DOCKER_HOSTS:
        return
    raise SeedGuardError(
        f"Từ chối chạy: Docker endpoint '{value or '<trống>'}' không phải engine local."
    )


def validate_credentials_path(path: Path, repository_root: Path) -> Path:
    """Tệp credentials phải là JSON nằm trong `.secrets/` (thư mục đã gitignore)."""
    secrets_directory = (repository_root / ".secrets").resolve()
    candidate = path if path.is_absolute() else repository_root / path
    resolved = candidate.resolve()
    if (
        resolved.suffix != ".json"
        or resolved == secrets_directory
        or not resolved.is_relative_to(secrets_directory)
    ):
        raise SeedGuardError(
            "Từ chối ghi credentials: đường dẫn phải là tệp .json bên trong thư mục .secrets/."
        )
    return resolved


def validate_project_name(project_name: str) -> None:
    if _PROJECT_NAME_PATTERN.fullmatch(project_name) is None:
        raise SeedGuardError(f"Tên Compose project không hợp lệ: '{project_name}'.")


# ---------------------------------------------------------------------------
# Credentials (chỉ nằm trong .secrets/ và stdin của container)
# ---------------------------------------------------------------------------


def _parse_credential(entry: object) -> AccountCredential | None:
    if not isinstance(entry, Mapping):
        return None
    password = entry.get("password")
    totp_secret = entry.get("totp_secret")
    if not isinstance(password, str) or not (
        _PASSWORD_MIN_LENGTH <= len(password) <= _PASSWORD_MAX_LENGTH
    ):
        return None
    if not isinstance(totp_secret, str) or _TOTP_PATTERN.fullmatch(totp_secret) is None:
        return None
    return AccountCredential(password=password, totp_secret=totp_secret)


def generate_credential(totp: TotpManager) -> AccountCredential:
    """Mật khẩu ngẫu nhiên 32 ký tự và TOTP seed Base32 từ TotpManager của dự án."""
    return AccountCredential(password=secrets.token_urlsafe(24), totp_secret=totp.generate_secret())


def write_credentials_file(path: Path, document: Mapping[str, Any]) -> None:
    """Ghi nguyên tử với quyền 0600 (POSIX); Windows dựa vào ACL hồ sơ người dùng."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def load_or_create_credentials(
    path: Path, dataset: QaDataset, *, totp: TotpManager | None = None
) -> dict[str, AccountCredential]:
    """Tái sử dụng credential hợp lệ; chỉ sinh mới cho tài khoản thiếu hoặc hỏng."""
    totp_manager = totp or TotpManager()
    previous: object = None
    stored_accounts: Mapping[str, object] = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SeedGuardError(
                f"Không đọc được tệp credentials QA '{path.name}'; kiểm tra hoặc xóa để tạo lại."
            ) from error
        if (
            not isinstance(previous, dict)
            or previous.get("format") != CREDENTIALS_FORMAT
            or not isinstance(previous.get("accounts"), dict)
        ):
            raise SeedGuardError(
                f"Tệp credentials QA '{path.name}' sai định dạng {CREDENTIALS_FORMAT}."
            )
        stored_accounts = previous["accounts"]

    credentials: dict[str, AccountCredential] = {}
    entries: dict[str, dict[str, Any]] = {}
    for member in dataset.members:
        credential = _parse_credential(stored_accounts.get(member.key)) or generate_credential(
            totp_manager
        )
        credentials[member.key] = credential
        entries[member.key] = {
            "workspace_slug": dataset.workspace(member.workspace_key).slug,
            "email": member.email,
            "roles": [role.value for role in member.roles],
            "membership_status": member.membership_status,
            "password": credential.password,
            "totp_secret": credential.totp_secret,
        }
    document = {
        "format": CREDENTIALS_FORMAT,
        "dataset_version": dataset.version,
        "warning_vi": _CREDENTIALS_WARNING_VI,
        "accounts": entries,
    }
    if document != previous:
        write_credentials_file(path, document)
    return credentials


def build_apply_payload(credentials: Mapping[str, AccountCredential]) -> str:
    """Gói stdin cho container: chỉ mật khẩu và TOTP seed theo khóa thành viên."""
    return json.dumps(
        {
            "format": CREDENTIALS_FORMAT,
            "accounts": {
                key: {"password": credential.password, "totp_secret": credential.totp_secret}
                for key, credential in sorted(credentials.items())
            },
        },
        separators=(",", ":"),
    )


def parse_apply_payload(payload: str, dataset: QaDataset) -> dict[str, AccountCredential]:
    """Kiểm tra gói stdin khớp đúng tập thành viên; lỗi không chứa giá trị bí mật."""
    try:
        document = json.loads(payload)
    except ValueError:
        # `from None`: thông báo lỗi JSON có thể trích một phần payload chứa bí mật.
        raise SeedGuardError("Gói credentials từ stdin không phải JSON hợp lệ.") from None
    if (
        not isinstance(document, dict)
        or document.get("format") != CREDENTIALS_FORMAT
        or not isinstance(document.get("accounts"), dict)
    ):
        raise SeedGuardError(f"Gói credentials từ stdin sai định dạng {CREDENTIALS_FORMAT}.")
    accounts = document["accounts"]
    expected = {member.key for member in dataset.members}
    if set(accounts) != expected:
        raise SeedGuardError(
            "Gói credentials không khớp dataset: thiếu "
            f"{sorted(expected - set(accounts))}, thừa {sorted(set(accounts) - expected)}."
        )
    result: dict[str, AccountCredential] = {}
    for key in sorted(expected):
        credential = _parse_credential(accounts[key])
        if credential is None:
            raise SeedGuardError(
                f"Credential của '{key}' không hợp lệ (mật khẩu 14-128 ký tự, TOTP Base32)."
            )
        result[key] = credential
    return result


def redact_secrets(value: str, secret_values: Iterable[str]) -> str:
    """Che mọi giá trị bí mật đã biết trước khi in output của tiến trình con."""
    redacted = value
    for secret_value in sorted({item for item in secret_values if item}, key=len, reverse=True):
        redacted = redacted.replace(secret_value, "***")
    return redacted


async def _apply_from_environment(
    environ: Mapping[str, str],
    dataset: QaDataset,
    credentials: Mapping[str, AccountCredential],
) -> dict[str, Any]:
    engine = create_database_engine(
        database_url_from_secret(
            username=environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=environ["TRADING_BOT_DATABASE_HOST"],
            database=environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    try:
        provider = LocalMfaSecretProvider(
            key_file=Path(environ["TRADING_BOT_MFA_ENCRYPTION_KEY_FILE"]),
            storage_directory=Path(environ["TRADING_BOT_MFA_SECRET_DIRECTORY"]),
        )
        return await apply_dataset(
            create_session_factory(engine), dataset, credentials, secret_provider=provider
        )
    finally:
        await engine.dispose()


def run_apply(arguments: argparse.Namespace, *, stdin: TextIO, stdout: TextIO) -> int:
    environ = dict(os.environ)
    validate_apply_environment(environ, local_only=arguments.local_only)
    dataset = build_qa_dataset()
    credentials = parse_apply_payload(stdin.read(), dataset)
    report = asyncio.run(_apply_from_environment(environ, dataset, credentials))
    print(json.dumps(report, sort_keys=True), file=stdout)
    return 0


# ---------------------------------------------------------------------------
# Điều phối trên máy dev (lệnh seed)
# ---------------------------------------------------------------------------


def repository_root() -> Path:
    return _REPOSITORY_ROOT


def resolve_docker_endpoint(environ: Mapping[str, str]) -> str:
    """DOCKER_HOST nếu có, nếu không thì endpoint của Docker context hiện hành."""
    configured = environ.get("DOCKER_HOST", "").strip()
    if configured:
        return configured
    result = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=dict(environ),
    )
    if result.returncode != 0:
        raise SeedGuardError("Không đọc được Docker context hiện hành; kiểm tra Docker đang chạy.")
    try:
        endpoint = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise SeedGuardError("Docker context trả endpoint không đọc được.") from error
    if not isinstance(endpoint, str):
        raise SeedGuardError("Docker context không có endpoint docker.")
    return endpoint


def ensure_git_ignored(path: Path, root: Path) -> None:
    """Từ chối ghi bí mật vào tệp git đang theo dõi hoặc không bị ignore."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except OSError, subprocess.TimeoutExpired:
        # Không có git (vd. bản tải nén): đường dẫn đã bị ép nằm trong .secrets/.
        return
    if result.returncode == 1:
        raise SeedGuardError(f"Tệp credentials {path.name} không được git ignore; từ chối ghi.")


def _parse_summary(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            summary = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(summary, dict) and "dataset_version" in summary:
            return summary
    raise SeedGuardError("Không đọc được tóm tắt JSON từ container qa-seed.")


def _print_summary(
    summary: Mapping[str, Any], *, project: str, credentials_label: str, stdout: TextIO
) -> None:
    print(
        f"Đã seed dataset {summary['dataset_version']} vào Compose project '{project}'.",
        file=stdout,
    )
    for key, workspace in summary.get("workspaces", {}).items():
        print(f"- Workspace {key}: slug={workspace['slug']} id={workspace['id']}", file=stdout)
    for key, bot_id in summary.get("bots", {}).items():
        print(f"- Bot của account {key}: {bot_id}", file=stdout)
    counts = ", ".join(f"{name}={value}" for name, value in summary.get("counts", {}).items())
    print(f"- Số hàng QA: {counts}", file=stdout)
    changes = summary.get("changes") or {}
    changed = ", ".join(f"{name}={value}" for name, value in changes.items())
    print(f"- Thay đổi lần chạy này: {changed or 'không có (đã đúng baseline)'}", file=stdout)
    print(
        f"- Mật khẩu và TOTP seed chỉ nằm trong {credentials_label} (không in ra đây).",
        file=stdout,
    )


def run_seed(arguments: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    require_local_only(arguments.local_only)
    validate_project_name(arguments.project_name)
    root = repository_root()
    environ = dict(os.environ)
    validate_docker_endpoint(resolve_docker_endpoint(environ))
    credentials_path = validate_credentials_path(
        Path(arguments.credentials_file or DEFAULT_CREDENTIALS_FILE), root
    )
    ensure_git_ignored(credentials_path, root)
    dataset = build_qa_dataset()
    credentials = load_or_create_credentials(credentials_path, dataset)
    secret_values = [
        value
        for credential in credentials.values()
        for value in (credential.password, credential.totp_secret)
    ]
    command = [
        "docker",
        "compose",
        "--project-name",
        arguments.project_name,
        "--file",
        arguments.compose_file,
        "run",
        "--rm",
        "-T",
    ]
    if arguments.no_deps:
        command.append("--no-deps")
    command += [QA_SEED_SERVICE, "python", CONTAINER_SCRIPT, "apply", "--local-only"]
    result = subprocess.run(
        command,
        input=build_apply_payload(credentials),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=root,
        env=environ,
        timeout=arguments.timeout,
    )
    if result.returncode != 0:
        detail = redact_secrets(
            (result.stderr or "")[-4000:] + "\n" + (result.stdout or "")[-2000:], secret_values
        )
        print(f"[seed-qa] Container qa-seed thất bại (mã {result.returncode}):", file=stderr)
        print(detail.strip(), file=stderr)
        return 1
    summary = _parse_summary(redact_secrets(result.stdout, secret_values))
    if arguments.json:
        print(json.dumps(summary, sort_keys=True), file=stdout)
    else:
        label = credentials_path.relative_to(root).as_posix()
        _print_summary(
            summary, project=arguments.project_name, credentials_label=label, stdout=stdout
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_qa_data.py",
        description="Gieo bộ dữ liệu QA vào PostgreSQL local/test (chỉ PAPER).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser(
        "seed", help="Chạy trên máy dev: tạo credentials trong .secrets/ rồi chạy qa-seed."
    )
    seed.add_argument(
        "--local-only",
        action="store_true",
        help="Bắt buộc: xác nhận đích là Docker/PostgreSQL local hoặc test.",
    )
    seed.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    seed.add_argument("--compose-file", default="compose.yaml")
    seed.add_argument(
        "--credentials-file",
        default=None,
        help="Tệp JSON trong .secrets/ (mặc định .secrets/qa_accounts.json).",
    )
    seed.add_argument(
        "--no-deps", action="store_true", help="Không khởi động lại dependency Compose."
    )
    seed.add_argument("--json", action="store_true", help="In tóm tắt dạng JSON (không bí mật).")
    seed.add_argument("--timeout", type=int, default=900)
    apply = commands.add_parser(
        "apply", help="Chạy trong container qa-seed; đọc credentials từ stdin."
    )
    apply.add_argument("--local-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments_list = list(sys.argv[1:] if argv is None else argv)
    if not arguments_list or arguments_list[0] not in {"seed", "apply"}:
        arguments_list.insert(0, "seed")
    arguments = build_parser().parse_args(arguments_list)
    try:
        if arguments.command == "apply":
            return run_apply(arguments, stdin=sys.stdin, stdout=sys.stdout)
        return run_seed(arguments, stdout=sys.stdout, stderr=sys.stderr)
    except SeedGuardError as error:
        print(f"[seed-qa] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    raise SystemExit(main())
