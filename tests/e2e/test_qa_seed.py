"""Seed dataset QA vào Compose cô lập rồi kiểm qua ranh giới thật (API, RLS, git)."""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pyotp
from scripts.docker_image import build_image_reference, ensure_image_available
from tests.fixtures.qa_dataset import build_qa_dataset

# Bootstrap ngắn đọc gói JSON từ stdin: Windows giới hạn dòng lệnh 32.767 ký tự.
PROBE_BOOTSTRAP = (
    "import json,os,sys;p=json.load(sys.stdin);"
    "os.environ.update(QA_PHASE=p['phase'],QA_CONTEXT=json.dumps(p['context']));"
    "exec(compile(p['code'],'qa_seed_probe.py','exec'),{'__name__':'__main__'})"
)
EXPECTED_COUNTS = {
    "workspace": 2,
    "user_account": 7,
    "user_membership": 7,
    "role_binding": 7,
    "mfa_factor": 7,
    "secret_reference": 7,
    "trading_account": 4,
    "bot_instance": 4,
    "bot_grant": 6,
    "risk_profile_version": 2,
    "workspace_settings_version": 2,
    "paper_account_seed": 4,
    "market_instrument": 2,
    "market_bar": 920,
}
# Oracle độc lập với code seed: bot (theo account) mà từng thành viên được thấy qua RLS.
EXPECTED_VISIBLE_BOTS = {
    "qa.owner@qa": ["A", "B", "C"],
    "qa.strategy_admin@qa": ["A", "B", "C"],
    "qa.risk_admin@qa": ["A", "B", "C"],
    "qa.operator@qa": ["A"],
    "qa.analyst_reviewer@qa": ["A"],
    "qa.suspended@qa": [],
    "qa_other.owner@qa": [],
    "qa_other.owner@qa_other": ["OTHER"],
    "anonymous@qa": [],
}
EXPECTED_GRANT_ROWS = {
    "qa.owner@qa": 6,
    "qa.operator@qa": 3,
    "qa.analyst_reviewer@qa": 2,
    "qa.strategy_admin@qa": 0,
    "qa.risk_admin@qa": 0,
    # Policy `bot_grant_read` cho thấy grant của chính membership mà không xét status;
    # thành viên bị đình chỉ vẫn không có phiên vì login/authenticate yêu cầu ACTIVE.
    "qa.suspended@qa": 1,
}
REQUIRE_CHECKS = {
    ("qa.operator", "A", "bot:runtime:pause"): True,
    ("qa.operator", "A", "bot:runtime:resume"): True,
    ("qa.operator", "B", "bot:runtime:pause"): False,
    ("qa.operator", "A", "bot:config:write"): False,
    ("qa.operator", "A", "bot:report:read"): False,
    ("qa.analyst_reviewer", "A", "bot:report:read"): True,
    ("qa.analyst_reviewer", "B", "bot:read"): False,
    ("qa.analyst_reviewer", "A", "bot:runtime:pause"): False,
    ("qa.risk_admin", "B", "bot:runtime:pause"): True,
    ("qa.risk_admin", "B", "bot:config:publish"): False,
    ("qa.strategy_admin", "C", "bot:config:publish"): True,
    ("qa.owner", "C", "bot:runtime:resume"): True,
}


def _login(port: int, account: dict[str, object], *, slug: str | None = None):
    """Đăng nhập qua Admin API thật; trả (status, body, opener giữ cookie phiên)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = json.dumps(
        {
            "workspace_slug": slug or account["workspace_slug"],
            "email": account["email"],
            "password": account["password"],
            "code": pyotp.TOTP(str(account["totp_secret"])).now(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read()), opener
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}"), opener


def test_qa_seed_is_idempotent_and_accounts_work_through_real_boundaries() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-qa-e2e-{uuid.uuid4().hex[:10]}"
    credentials_file = root / ".secrets" / f"qa_accounts-{project}.json"
    image = build_image_reference(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        web_port = listener.getsockname()[1]
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image, "TRADING_BOT_WEB_PORT": str(web_port)}
    base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
    ]

    def run(command, *, stdin=None, timeout=300):
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        assert result.returncode == 0, (command[:8], result.stdout[-5000:], result.stderr[-5000:])
        return result

    probe_code = (root / "tests/e2e/probes/qa_seed.py").read_text(encoding="utf-8")
    dataset = build_qa_dataset()
    context = {
        "workspaces": {workspace.key: str(workspace.id) for workspace in dataset.workspaces},
        "members": {
            member.key: {
                "workspace": member.workspace_key,
                "user_id": str(member.user_id),
                "membership_id": str(member.membership_id),
                "roles": [role.value for role in member.roles],
            }
            for member in dataset.members
        },
        "accounts": {
            account.key: {"workspace": account.workspace_key, "id": str(account.id)}
            for account in dataset.accounts
        },
        "require_checks": [list(check) for check in REQUIRE_CHECKS],
    }

    def probe(phase, service):
        payload = json.dumps({"phase": phase, "context": context, "code": probe_code})
        result = run(
            base + ["run", "--rm", "--no-deps", "-T", service, "python", "-c", PROBE_BOOTSTRAP],
            stdin=payload,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def seed():
        return run(
            [
                sys.executable,
                "scripts/seed_qa_data.py",
                "seed",
                "--local-only",
                "--project-name",
                project,
                "--credentials-file",
                str(credentials_file),
                "--json",
            ],
            timeout=900,
        )

    ensure_image_available(root, image)
    try:
        run(base + ["up", "--detach", "--wait", "--wait-timeout", "180"])
        run(base + ["run", "--rm", "--no-deps", "migrator", "alembic", "check"])

        first = seed()
        first_summary = json.loads(first.stdout.strip().splitlines()[-1])
        context["bots"] = first_summary["bots"]
        first_snapshot = probe("snapshot", "migrator")
        second = seed()
        second_summary = json.loads(second.stdout.strip().splitlines()[-1])
        second_snapshot = probe("snapshot", "migrator")

        # Idempotency: lần hai không đổi số hàng, nội dung hay bot ID, không ghi gì mới.
        assert {table: count for table, (count, _) in first_snapshot.items()} == EXPECTED_COUNTS
        assert second_snapshot == first_snapshot
        assert first_summary["counts"] == second_summary["counts"] == EXPECTED_COUNTS
        assert first_summary["changes"]["market_bar.created"] == 920
        assert second_summary["changes"] == {}
        assert second_summary["bots"] == first_summary["bots"]

        # Bí mật chỉ nằm trong tệp .secrets, không lộ qua output CLI/container.
        credentials = json.loads(credentials_file.read_text(encoding="utf-8"))["accounts"]
        secret_values = [
            value
            for account in credentials.values()
            for value in (account["password"], account["totp_secret"])
        ]
        for result in (first, second):
            for value in secret_values:
                assert value not in result.stdout
                assert value not in result.stderr

        # (a) Đăng nhập thật qua Admin API với mật khẩu/TOTP vừa sinh; role khớp dataset.
        for member in dataset.members:
            status, body, opener = _login(web_port, credentials[member.key])
            if member.membership_status != "ACTIVE":
                assert status == 401, (member.key, status, body)
                continue
            assert status == 200, (member.key, status, body)
            assert body["roles"] == [role.value for role in member.roles]
            assert body["workspace_id"] == str(dataset.workspace(member.workspace_key).id)
            with opener.open(f"http://127.0.0.1:{web_port}/api/v1/auth/me", timeout=20) as me:
                assert json.loads(me.read())["email"] == member.email
        foreign_status, _, _ = _login(
            web_port, credentials["qa.owner"], slug=dataset.workspace("qa_other").slug
        )
        assert foreign_status == 401

        # (b) Ma trận RLS/grant dưới login crypidian_admin_web thật.
        admin = probe("admin_matrix", "admin-web")
        assert admin["identity"] == ["crypidian_admin_web", "crypidian_admin_web"]
        for scenario, expected in EXPECTED_VISIBLE_BOTS.items():
            observed = admin["matrix"][scenario]
            assert observed["bot_instance"] == expected, scenario
            assert observed["paper_account_seed"] == expected, scenario
        for scenario, expected in EXPECTED_GRANT_ROWS.items():
            assert admin["matrix"][scenario]["bot_grant"] == expected, scenario
        for member_key in ("qa.owner", "qa.operator", "qa.analyst_reviewer", "qa.risk_admin"):
            assert admin["matrix"][f"{member_key}@qa_other"]["bot_instance"] == []
            assert admin["matrix"][f"{member_key}@qa_other"]["bot_grant"] == 0
        assert admin["require"] == {
            ":".join(check): allowed for check, allowed in REQUIRE_CHECKS.items()
        }

        # (c) Login crypidian_trading_runtime với scope bot A không đọc được hàng của bot B.
        runtime = probe("runtime_scope", "trading-runtime")
        assert runtime["identity"] == ["crypidian_trading_runtime", "crypidian_trading_runtime"]
        for account_key in ("A", "B", "C"):
            assert runtime[f"qa:{account_key}"] == [account_key]
            assert runtime[f"qa_other:{account_key}"] == []
        assert runtime["qa_other:OTHER"] == ["OTHER"]
        assert runtime["qa:OTHER"] == []
        assert runtime["bot_a_reads_account_b"] == 0
        assert runtime["bot_a_sees_qa_bots"] == 3

        # (d) Tệp credentials bị git ignore và không có tệp .secrets nào được theo dõi.
        for path in (credentials_file, root / ".secrets" / "qa_accounts.json"):
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--quiet", "--", str(path)],
                check=False,
                capture_output=True,
            )
            assert ignored.returncode == 0, path
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--", ".secrets"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert tracked.stdout.strip() == ""
    finally:
        subprocess.run(
            base + ["--profile", "qa", "down", "--volumes", "--remove-orphans"],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=300,
        )
        credentials_file.unlink(missing_ok=True)
