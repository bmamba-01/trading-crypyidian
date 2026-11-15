"""Tạo thành viên ``analyst_reviewer`` cho workspace local để duyệt quyền nguồn (local/PAPER).

Bối cảnh: duyệt policy Source Registry yêu cầu người duyệt khác người gửi; stack local thường chỉ
có một owner và chưa có chức năng mời thành viên (US-04). Script này tái dùng đúng các bước của
QA seed (`scripts/qa_seed_layers.py`): băm mật khẩu bằng `PasswordManager`, lưu TOTP qua
`LocalMfaSecretProvider`, ghi user/membership/role/MFA idempotent trong một transaction.

Hai lệnh:

- ``seed --workspace-slug <slug>`` (máy dev): chốt local, tạo hoặc đọc lại
  ``.secrets/local_reviewer.json`` (chmod 600, phải bị git ignore), chạy service Compose
  ``qa-seed`` với lệnh ``apply`` và truyền credential qua stdin.
- ``apply`` (trong container ``qa-seed``): kiểm môi trường local/PAPER rồi ghi.

Không in mật khẩu hay TOTP seed ra stdout/log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

CREDENTIALS_FILE = Path(".secrets") / "local_reviewer.json"
CREDENTIALS_FORMAT = "crypidian-local-reviewer/v1"
CONTAINER_SCRIPT = "/qa/scripts/seed_local_reviewer.py"
DEFAULT_EMAIL = "local.reviewer@example.test"


def _member_ids(workspace_id: UUID, email: str) -> dict[str, UUID]:
    """ID tất định theo workspace + email để chạy lại không tạo bản ghi trùng."""
    base = f"crypidian-local-reviewer:{workspace_id}:{email}"
    return {
        kind: uuid5(NAMESPACE_URL, f"{base}:{kind}")
        for kind in ("user", "membership", "mfa_factor", "secret_reference")
    }


# ---------------------------------------------------------------------------
# Trong container: ghi DB
# ---------------------------------------------------------------------------


async def _apply(payload: dict[str, str]) -> dict[str, Any]:
    from scripts.qa_seed_layers import (
        AccountCredential,
        SeedContext,
        _ensure_mfa,
        _ensure_role_bindings,
        _ensure_row,
        _ensure_user,
    )
    from sqlalchemy import select, text
    from tests.fixtures.qa_dataset.access import QaMember

    from trading_bot.adapters.database.engine import (
        create_database_engine,
        create_session_factory,
        database_url_from_secret,
    )
    from trading_bot.adapters.database.models import UserMembership, Workspace
    from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
    from trading_bot.auth.policy import Role
    from trading_bot.auth.security import PasswordManager

    environ = os.environ
    if environ.get("TRADING_BOT_APP_ENV") not in {"local", "test"}:
        raise SystemExit("Chỉ chạy ở môi trường local/test.")
    if environ.get("TRADING_BOT_TRADING_MODE") != "PAPER":
        raise SystemExit("Chỉ chạy ở chế độ PAPER.")
    engine = create_database_engine(
        database_url_from_secret(
            username=environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=environ["TRADING_BOT_DATABASE_HOST"],
            database=environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    provider = LocalMfaSecretProvider(
        key_file=Path(environ["TRADING_BOT_MFA_ENCRYPTION_KEY_FILE"]),
        storage_directory=Path(environ["TRADING_BOT_MFA_SECRET_DIRECTORY"]),
    )
    sessions = create_session_factory(engine)
    context: Any = None
    try:
        async with sessions() as session, session.begin():
            workspace = await session.scalar(
                select(Workspace).where(Workspace.slug == payload["workspace_slug"])
            )
            if workspace is None:
                raise SystemExit("Không tìm thấy workspace theo slug.")
            if workspace.mode != "PAPER":
                raise SystemExit("Workspace không ở chế độ PAPER.")
            await session.execute(
                text("SELECT pg_catalog.set_config('app.workspace_id', :w, true)"),
                {"w": str(workspace.id)},
            )
            ids = _member_ids(workspace.id, payload["email"])
            member = QaMember(
                key="local.reviewer",
                workspace_key="local",
                user_id=ids["user"],
                membership_id=ids["membership"],
                email=payload["email"],
                roles=(Role.ANALYST_REVIEWER,),
                membership_status="ACTIVE",
                user_status="ACTIVE",
                mfa_factor_id=ids["mfa_factor"],
                secret_reference_id=ids["secret_reference"],
            )
            context = SeedContext(
                session=session,
                dataset=None,  # type: ignore[arg-type] - chỉ dùng các hàm thành viên
                credentials={
                    member.key: AccountCredential(payload["password"], payload["totp_secret"])
                },
                secret_provider=provider,
                passwords=PasswordManager(),
                now=workspace.created_at,
            )
            await _ensure_user(context, member)
            await _ensure_row(
                context,
                UserMembership,
                member.membership_id,
                {
                    "workspace_id": workspace.id,
                    "user_id": member.user_id,
                    "status": "ACTIVE",
                    "created_at": workspace.created_at,
                },
                identity=("workspace_id", "user_id"),
                mutable=("status",),
            )
            await _ensure_role_bindings(context, member, workspace.id)
            await _ensure_mfa(context, member, workspace.id)
            workspace_id = workspace.id
    except BaseException:
        if context is not None:
            for stored_workspace, reference in context.stored_references:
                provider.delete_mfa_secret(stored_workspace, reference)
        raise
    finally:
        await engine.dispose()
    return {"workspace_id": str(workspace_id), "changes": dict(context.changes)}


# ---------------------------------------------------------------------------
# Trên máy dev: credential + điều phối Compose
# ---------------------------------------------------------------------------


def _load_or_create_credentials(path: Path, email: str) -> dict[str, str]:
    from trading_bot.auth.security import TotpManager

    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format") != CREDENTIALS_FORMAT:
            raise SystemExit(f"{path} không đúng định dạng {CREDENTIALS_FORMAT}.")
        return {key: document[key] for key in ("email", "password", "totp_secret")}
    path.parent.mkdir(parents=True, exist_ok=True)
    credentials = {
        "email": email,
        "password": secrets.token_urlsafe(24),
        "totp_secret": TotpManager().generate_secret(),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"format": CREDENTIALS_FORMAT, **credentials}, handle, indent=2)
    return credentials


def _seed(arguments: argparse.Namespace) -> int:
    root = _REPOSITORY_ROOT
    path = root / CREDENTIALS_FILE
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(CREDENTIALS_FILE)], cwd=root, check=False
    )
    if ignored.returncode != 0:
        raise SystemExit(f"{CREDENTIALS_FILE} phải được git ignore trước khi ghi credential.")
    credentials = _load_or_create_credentials(path, arguments.email)
    payload = json.dumps({"workspace_slug": arguments.workspace_slug, **credentials})
    command = [
        "docker",
        "compose",
        "-p",
        arguments.project,
        "-f",
        "compose.yaml",
        "--profile",
        "qa",
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "-v",
        f"{root / 'scripts' / 'seed_local_reviewer.py'}:{CONTAINER_SCRIPT}:ro",
        "qa-seed",
        "python",
        CONTAINER_SCRIPT,
        "apply",
    ]
    result = subprocess.run(
        command, cwd=root, input=payload, capture_output=True, text=True, check=False
    )
    secret_values = (credentials["password"], credentials["totp_secret"])
    output = result.stdout + result.stderr
    for value in secret_values:
        output = output.replace(value, "***")
    if result.returncode != 0:
        print(output[-3000:], file=sys.stderr)
        return result.returncode
    report = json.loads(result.stdout.strip().splitlines()[-1])
    print(
        json.dumps(
            {
                "workspace_id": report["workspace_id"],
                "email": credentials["email"],
                "role": "analyst_reviewer",
                "changes": report["changes"],
                "credentials_file": str(CREDENTIALS_FILE),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--workspace-slug", required=True)
    seed.add_argument("--email", default=DEFAULT_EMAIL)
    seed.add_argument("--project", default="crypidian-review")
    commands.add_parser("apply")
    arguments = parser.parse_args(argv)
    if arguments.command == "apply":
        payload = json.loads(sys.stdin.read())
        print(json.dumps(asyncio.run(_apply(payload)), sort_keys=True))
        return 0
    return _seed(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
