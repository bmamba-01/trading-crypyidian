"""Seed dataset QA vào Compose cô lập rồi kiểm qua ranh giới thật (API, RLS, git)."""

import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pyotp
from scripts.docker_image import build_image_reference, ensure_image_available
from tests.fixtures.qa_dataset import build_qa_dataset, qa_id

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
    "strategy_release": 4,
    "bot_config_version": 2,
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
# Seed lại sau khi probe `mutate` làm lệch baseline: mỗi bộ đếm phải đúng một thay đổi.
EXPECTED_RESET_CHANGES = {
    "bot_config_version.published": 1,
    "bot_grant.created": 1,
    "bot_grant.revoked": 1,
    "mfa_factor.deleted": 1,
    "mfa_factor.rotated": 1,
    "role_binding.deleted": 1,
    "secret_reference.updated": 1,
    "trading_account.updated": 1,
    "user_account.password_rotated": 1,
    "user_membership.updated": 1,
}


def _wait_for_next_totp_step(step: int) -> None:
    """Login chặn phát lại counter TOTP đã dùng; đăng nhập lại cần chu kỳ 30 giây mới."""
    deadline = time.monotonic() + 45
    while int(time.time()) // 30 <= step:
        assert time.monotonic() < deadline, "Đồng hồ không sang chu kỳ TOTP mới"
        time.sleep(0.5)


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
                "mfa_factor_id": str(member.mfa_factor_id),
                "secret_reference_id": str(member.secret_reference_id),
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
    baseline_configs = {config.account_key: config for config in dataset.bot_configs}

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
        assert first_summary["changes"]["strategy_release.created"] == 4
        assert first_summary["changes"]["bot_config_version.published"] == 2
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
        first_login_step = int(time.time()) // 30

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

        # (e) Reset về baseline: làm lệch trạng thái như thao tác thủ công rồi seed lại.
        mutation = probe("mutate", "migrator")
        assert mutation["revoked"] == 1
        # Config bot B bị publish tay sang release khác baseline (v2); A giữ v1.
        assert mutation["bot_config_heads"]["A"][1:3] == [1, baseline_configs["A"].overlay_hash]
        assert mutation["bot_config_heads"]["B"][1] == 2
        assert mutation["bot_config_heads"]["B"][2] != baseline_configs["B"].overlay_hash
        # Trigger MB-01 đồng bộ bot theo account ngay khi account bị tắt/đổi tên.
        assert mutation["bot_a"] == ["DISABLED", "qa-paper-a-mutated"]
        suspended_status, _, _ = _login(web_port, credentials["qa.analyst_reviewer"])
        assert suspended_status == 401
        document = json.loads(credentials_file.read_text(encoding="utf-8"))
        old_risk_admin = dict(document["accounts"]["qa.risk_admin"])
        new_password, new_totp = secrets.token_urlsafe(24), pyotp.random_base32()
        document["accounts"]["qa.risk_admin"] |= {"password": new_password, "totp_secret": new_totp}
        credentials_file.write_text(json.dumps(document), encoding="utf-8")

        reset = seed()
        reset_summary = json.loads(reset.stdout.strip().splitlines()[-1])
        assert reset_summary["changes"] == EXPECTED_RESET_CHANGES
        # Lịch sử grant bất biến: thêm grant lạ (đã thu hồi) và grant cấp lại.
        # Lịch sử config bất biến: baseline của B được publish lại thành v3 nối sau v2.
        assert reset_summary["counts"] == EXPECTED_COUNTS | {
            "bot_grant": 8,
            "bot_config_version": 4,
        }
        assert reset_summary["bots"] == first_summary["bots"]
        for value in (new_password, new_totp):
            assert value not in reset.stdout
            assert value not in reset.stderr
        restored = probe("verify_reset", "migrator")
        heads = restored["bot_config_heads"]
        assert heads["A"] == mutation["bot_config_heads"]["A"]
        assert heads["B"][1:] == [
            3,
            baseline_configs["B"].overlay_hash,
            mutation["drift_config"],
        ]
        assert restored["account_a"] == ["ENABLED", "qa-paper-a"]
        assert restored["bot_a"] == ["ENABLED", "qa-paper-a", "A"]
        assert restored["membership_status"] == {
            member.key: member.membership_status for member in dataset.members
        }
        assert restored["operator_roles"] == ["operator"]
        assert restored["strategy_factors"] == [
            str(dataset.member("qa.strategy_admin").mfa_factor_id)
        ]
        grants = restored["operator_grants"]
        assert {(account, permission) for _, account, permission, live in grants if live} == {
            ("A", "bot:read"),
            ("A", "bot:runtime:pause"),
            ("A", "bot:runtime:resume"),
        }
        revoked_grants = sorted(
            (account, permission) for _, account, permission, live in grants if not live
        )
        assert revoked_grants == [("A", "bot:runtime:pause"), ("B", "bot:read")]
        reissued = str(qa_id("bot_grant", "A:qa.operator:bot:runtime:pause:reissue:1"))
        assert [
            grant_id
            for grant_id, _, permission, live in grants
            if live and permission == "bot:runtime:pause"
        ] == [reissued]
        before, after = mutation["references"], restored["references"]
        assert after["qa.risk_admin"] != before["qa.risk_admin"]
        assert restored["risk_admin_reference_key"] == after["qa.risk_admin"]
        assert restored["counters"]["qa.risk_admin"] is None
        unchanged = set(before) - {"qa.risk_admin"}
        assert {key: after[key] for key in unchanged} == {key: before[key] for key in unchanged}
        # Tệp TOTP bị thay phải bị xóa; mọi tệp còn được tham chiếu phải còn nguyên.
        context["mfa_files"] = {f"current:{key}": value for key, value in after.items()} | {
            "superseded:qa.risk_admin": before["qa.risk_admin"]
        }
        assert probe("mfa_files", "admin-web") == {f"current:{key}": True for key in after} | {
            "superseded:qa.risk_admin": False
        }

        # Đăng nhập lại sau reset qua API thật (chờ chu kỳ TOTP mới để tránh chặn phát lại).
        _wait_for_next_totp_step(first_login_step)
        rotated = json.loads(credentials_file.read_text(encoding="utf-8"))["accounts"]
        assert rotated["qa.risk_admin"]["password"] == new_password
        stale_status, _, _ = _login(web_port, old_risk_admin)
        assert stale_status == 401
        for member in dataset.members:
            status, body, _ = _login(web_port, rotated[member.key])
            if member.membership_status != "ACTIVE":
                assert status == 401, (member.key, status, body)
                continue
            assert status == 200, (member.key, status, body)
            assert body["roles"] == [role.value for role in member.roles]
        idle = seed()
        assert json.loads(idle.stdout.strip().splitlines()[-1])["changes"] == {}

        # (f) Dataset QA dẫn fan-out MB-03: một shared evidence → quyết định cho bot A và B
        # (config PUBLISHED, release khác nhau); C DISABLED không được liệt kê, OTHER khác
        # workspace. Context resolve tại mốc dữ liệu nên bot B dùng v1 baseline.
        probe("feature_snapshot", "migrator")
        fanout = probe("fanout", "trading-runtime")
        assert fanout["identity"] == "crypidian_trading_runtime"
        assert fanout["exclusions"] == []
        root_settings = str(dataset.settings_for("qa").id)
        expected_releases = {key: config.release_id for key, config in baseline_configs.items()}
        assert expected_releases["A"] != expected_releases["B"]
        assert {key: item["release"] for key, item in fanout["contexts"].items()} == (
            expected_releases
        )
        assert {key: item["release"] for key, item in fanout["decisions"].items()} == (
            expected_releases
        )
        decisions = fanout["decisions"]
        assert len({item["evidence_id"] for item in decisions.values()}) == 1
        assert decisions["A"]["bot_config_version_id"] != decisions["B"]["bot_config_version_id"]
        for key, item in decisions.items():
            assert item["root_settings_version_id"] == root_settings
            assert item["bot_config_version_id"] == fanout["contexts"][key]["bot_config_version_id"]
            assert item["status"] in {"CANDIDATE", "ABSTAIN"}
        assert fanout["retry_identical"] is True

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
