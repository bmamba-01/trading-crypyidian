"""E2E TLS Compose cho giới hạn upload bằng chứng quyền qua Caddy và ASGI."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from scripts.docker_image import build_image_reference, ensure_image_available

from trading_bot.auth.cookies import session_cookie_name
from trading_bot.config.settings import AppSettings

PROBE_BOOTSTRAP = (
    "import json,sys; p=json.load(sys.stdin); "
    "namespace={'__name__':'__main__','PROBE_PAYLOAD':{k:v for k,v in p.items() if k!='code'}}; "
    "exec(compile(p['code'],'wp06_license_upload_proxy.py','exec'),namespace)"
)
PROXY_TEST_SUBNET = "198.18.254.0/24"


def _post(opener: urllib.request.OpenerDirector, url: str, body: bytes, headers: dict[str, str]):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=45) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.headers, response.read()


def _post_expect_continue(
    *, host: str, port: int, path: str, body: bytes, headers: dict[str, str]
) -> tuple[int, http.client.HTTPMessage]:
    connection = http.client.HTTPSConnection(
        host,
        port,
        context=ssl._create_unverified_context(),
        timeout=45,
    )
    try:
        connection.putrequest("POST", path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Expect", "100-continue")
        connection.endheaders()
        assert connection.sock is not None
        response_stream = connection.sock.makefile("rb")
        try:
            status, response_headers = _read_response_head(response_stream)
            if status == 100:
                try:
                    connection.send(body)
                except OSError:
                    # Server có thể đóng socket ngay khi phát hiện body vượt giới hạn.
                    pass
                status, response_headers = _read_response_head(response_stream)
            return status, response_headers
        finally:
            response_stream.close()
    finally:
        connection.close()


def _read_response_head(stream) -> tuple[int, http.client.HTTPMessage]:
    status_line = stream.readline().decode("iso-8859-1").strip()
    parts = status_line.split(" ", maxsplit=2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise AssertionError(f"HTTP status line không hợp lệ: {status_line!r}")
    return int(parts[1]), http.client.parse_headers(stream)


def test_license_evidence_upload_limits_through_tls_compose_proxy() -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    project = f"crypidian-wp06-upload-proxy-{uuid.uuid4().hex[:10]}"
    image = build_image_reference(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        proxy_port = listener.getsockname()[1]
    telegram_token_file = root / ".secrets" / f"wp06-proxy-telegram-{project}.txt"
    telegram_token_file.write_text("123456789:wp06_proxy_test_token_not_real", encoding="utf-8")
    telegram_token_file.chmod(0o600)
    override = root / "tests" / "e2e" / "fixtures" / "wp06_license_upload_proxy.compose.yaml"
    environment = os.environ | {
        "CRYPIDIAN_APP_IMAGE": image,
        "CRYPIDIAN_PUBLIC_HOST": "localhost",
        "CRYPIDIAN_HEALTHCHECK_HOST": "localhost",
        "TRADING_BOT_ALLOWED_HOSTS": "localhost",
        # Chỉ tin dải nhỏ đã ghim cho web_ingress của Compose E2E.
        "TRADING_BOT_TRUSTED_PROXY_IPS": PROXY_TEST_SUBNET,
        "TRADING_BOT_REQUEST_BODY_MAX_BYTES": "1048576",
        "TRADING_BOT_AUTH_TEST_TOTP_ENABLED": "false",
        "WP06_PROXY_TELEGRAM_TOKEN_FILE": str(telegram_token_file),
        "WP06_PROXY_PORT": str(proxy_port),
    }
    compose_base = [
        "docker",
        "compose",
        "--profile",
        "paper",
        "--project-name",
        project,
        "-f",
        "compose.yaml",
        "-f",
        "deploy/compose.tls.yaml",
        "-f",
        str(override),
    ]

    def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*compose_base, *args],
            cwd=root,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if result.returncode != 0:
            app_logs = subprocess.run(
                [*compose_base, "logs", "--no-color", "--tail", "100", "admin-web"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            result.stderr += (
                "\nadmin-web startup logs:\n" + app_logs.stdout[-5000:] + app_logs.stderr[-3000:]
            )
        assert result.returncode == 0, (
            f"Compose {args!r} thất bại; stderr:\n{result.stderr[-5000:]}"
        )
        return result

    probe_code = (root / "tests/e2e/probes/wp06_license_upload_proxy.py").read_text(
        encoding="utf-8"
    )

    def run_probe(payload: dict[str, object]) -> dict[str, Any]:
        result = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--volume",
            f"{root / 'tests'}:/e2e-tests:ro",
            "db-provisioner",
            "python",
            "-c",
            PROBE_BOOTSTRAP,
            stdin=json.dumps(payload | {"code": probe_code}),
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    try:
        config = json.loads(compose("config", "--format", "json").stdout)
        assert (
            config["services"]["admin-web"]["environment"]["TRADING_BOT_TRUSTED_PROXY_IPS"]
            == PROXY_TEST_SUBNET
        )
        ingress = next(
            network for name, network in config["networks"].items() if name.endswith("web_ingress")
        )
        assert ingress["ipam"]["config"][0]["subnet"] == PROXY_TEST_SUBNET
        assert "web_ingress" in config["services"]["reverse-proxy"]["networks"]
        ensure_image_available(root, image)
        compose("up", "--detach", "--wait", "--wait-timeout", "180", "postgres")
        compose("run", "--rm", "--no-deps", "db-provisioner")
        compose("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head")
        compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "admin-web",
            "reverse-proxy",
        )
        published_proxy_port = compose("port", "reverse-proxy", "443").stdout.strip()
        assert published_proxy_port == f"127.0.0.1:{proxy_port}", published_proxy_port
        state = run_probe({"mode": "seed"})
        address = f"https://localhost:{proxy_port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                opener.open(address + "/health/live", timeout=2).close()
                break
            except OSError, urllib.error.URLError:
                if time.monotonic() >= deadline:
                    logs = subprocess.run(
                        [*compose_base, "logs", "--no-color", "--tail", "100", "reverse-proxy"],
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=15,
                    )
                    raise AssertionError(
                        "Caddy proxy không sẵn sàng; "
                        f"port={published_proxy_port}; logs:\n{logs.stdout[-5000:]}"
                        f"{logs.stderr[-3000:]}"
                    ) from None
                time.sleep(0.25)

        upload_path = (
            f"/api/v1/research-sources/{state['source_id']}/revisions/"
            f"{state['policy_id']}/license-evidence"
        )
        production_cookie_name = session_cookie_name(
            AppSettings(_env_file=None, app_env="production", allowed_hosts="localhost")
        )
        headers = {
            "Cookie": f"{production_cookie_name}={state['cookie']}",
            "X-CSRF-Token": str(state["csrf"]),
            "Content-Type": "text/plain",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        accepted_body = b"l" * (1024 * 1024 + 1)
        accepted_status, accepted_headers, accepted_content = _post(
            opener, address + upload_path, accepted_body, headers
        )
        assert accepted_status == 201, accepted_content[:300]
        accepted = json.loads(accepted_content)
        assert accepted["byte_count"] == len(accepted_body)
        assert accepted["digest"] == hashlib.sha256(accepted_body).hexdigest()
        assert accepted_headers.get("Cache-Control") == "no-store"
        assert accepted_body.decode() not in accepted_content.decode()

        rejected_status, rejected_headers = _post_expect_continue(
            host="localhost",
            port=proxy_port,
            path=upload_path,
            body=b"x" * (10 * 1024 * 1024 + 1),
            headers=headers | {"Idempotency-Key": str(uuid.uuid4())},
        )
        assert rejected_status == 413
        assert rejected_headers.get("Cache-Control") == "no-store"

        ordinary_status, _ = _post_expect_continue(
            host="localhost",
            port=proxy_port,
            path="/api/v1/research-sources",
            body=b"x" * (1024 * 1024 + 1),
            headers=headers | {"Idempotency-Key": str(uuid.uuid4())},
        )
        assert ordinary_status == 413

        persisted = run_probe(
            {
                "mode": "verify",
                "state": {key: state[key] for key in ("workspace_id", "source_id", "policy_id")},
            }
        )
        assert persisted == {
            "evidence_count": 1,
            "digest": accepted["digest"],
            "byte_count": len(accepted_body),
        }
    finally:
        try:
            subprocess.run(
                [*compose_base, "down", "--volumes", "--remove-orphans"],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=60,
                check=False,
            )
        finally:
            telegram_token_file.unlink(missing_ok=True)
