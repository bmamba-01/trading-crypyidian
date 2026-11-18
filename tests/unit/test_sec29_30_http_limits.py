"""SEC-29/SEC-30: TrustedHost, IP client an toàn, rate limit chung và giới hạn body."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from trading_bot.app import create_app
from trading_bot.auth.router import client_address, get_auth_service
from trading_bot.auth.router import router as auth_router
from trading_bot.auth.service import AuthenticationFailure
from trading_bot.common.http_limits import (
    SlidingWindowRateLimiter,
    parse_trusted_proxies,
    resolve_client_ip,
)
from trading_bot.config.settings import AppSettings

PROXY = "10.0.0.5"


def _settings(**overrides: object) -> AppSettings:
    values: dict[str, object] = {"app_env": "test", "allowed_hosts": "app.example.test"}
    values.update(overrides)
    return AppSettings(**values)  # type: ignore[arg-type]


def _app(settings: AppSettings) -> FastAPI:
    app = create_app(settings)

    @app.get("/probe/ip")
    async def probe_ip(request: Request) -> JSONResponse:
        return JSONResponse({"ip": request.client.host if request.client else None})

    @app.post("/probe/mutate")
    async def probe_mutate(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"size": len(body)})

    @app.get("/probe/heavy/report")
    async def probe_heavy() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def _client(
    settings: AppSettings, *, client: tuple[str, int] = ("203.0.113.9", 50000)
) -> TestClient:
    return TestClient(
        _app(settings),
        base_url="http://app.example.test",
        client=client,
        raise_server_exceptions=True,
    )


# ---------------------------------------------------------------- resolve_client_ip


def test_resolve_ignores_forwarded_header_from_untrusted_peer() -> None:
    trusted = parse_trusted_proxies(PROXY)
    assert resolve_client_ip("198.51.100.7", "1.2.3.4", trusted) == "198.51.100.7"


def test_resolve_uses_rightmost_untrusted_hop_and_skips_spoofed_left_entries() -> None:
    trusted = parse_trusted_proxies("10.0.0.0/24")
    header = "6.6.6.6, 198.51.100.7, 10.0.0.9"
    assert resolve_client_ip("10.0.0.5", header, trusted) == "198.51.100.7"


def test_resolve_falls_back_to_peer_on_malformed_chain() -> None:
    trusted = parse_trusted_proxies(PROXY)
    assert resolve_client_ip(PROXY, "not-an-ip", trusted) == PROXY
    assert resolve_client_ip(PROXY, None, trusted) == PROXY
    assert resolve_client_ip(None, "1.2.3.4", trusted) == "unknown"


def test_resolve_supports_ipv6_and_normalizes() -> None:
    trusted = parse_trusted_proxies("::1")
    assert resolve_client_ip("::1", "2001:DB8::1", trusted) == "2001:db8::1"


def test_parse_trusted_proxies_rejects_invalid_and_wildcard() -> None:
    with pytest.raises(ValueError, match="không hợp lệ"):
        parse_trusted_proxies("10.0.0.1, banana")
    with pytest.raises(ValueError, match="không được dùng '\\*'"):
        parse_trusted_proxies("*")
    with pytest.raises(ValueError, match="/0"):
        parse_trusted_proxies("0.0.0.0/0")


# ---------------------------------------------------------------- settings


def test_production_admin_web_requires_explicit_allowed_hosts() -> None:
    with pytest.raises(ValidationError):
        AppSettings(app_env="production")
    with pytest.raises(ValidationError):
        AppSettings(app_env="production", allowed_hosts="*")
    AppSettings(app_env="production", allowed_hosts="crypidian.example")


def test_settings_reject_invalid_trusted_proxy_ips() -> None:
    with pytest.raises(ValidationError):
        AppSettings(trusted_proxy_ips="0.0.0.0/0")


# ---------------------------------------------------------------- TrustedHost


def test_unknown_host_header_is_rejected_with_400() -> None:
    client = _client(_settings())
    response = client.get("/health/live", headers={"Host": "evil.example"})
    assert response.status_code == 400
    # Response lỗi vẫn mang header bảo mật (SecurityHeaders là lớp ngoài cùng).
    assert response.headers["x-content-type-options"] == "nosniff"


def test_allowed_host_is_served_including_port_suffix() -> None:
    client = _client(_settings())
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live", headers={"Host": "app.example.test:8443"}).status_code == 200


def test_wildcard_subdomain_allowed_host() -> None:
    client = _client(_settings(allowed_hosts="*.example.test"))
    assert client.get("/health/live", headers={"Host": "a.example.test"}).status_code == 200
    assert client.get("/health/live", headers={"Host": "example.org"}).status_code == 400


def test_local_without_allowed_hosts_keeps_existing_behavior() -> None:
    client = TestClient(create_app(AppSettings(app_env="local")))
    assert client.get("/health/live").status_code == 200


# ---------------------------------------------------------------- forwarded IP


def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored() -> None:
    client = _client(_settings(trusted_proxy_ips=PROXY))
    response = client.get("/probe/ip", headers={"X-Forwarded-For": "1.2.3.4"})
    assert response.json() == {"ip": "203.0.113.9"}


def test_forwarded_for_from_trusted_proxy_is_used() -> None:
    client = _client(_settings(trusted_proxy_ips=PROXY), client=(PROXY, 40000))
    response = client.get("/probe/ip", headers={"X-Forwarded-For": "198.51.100.7"})
    assert response.json() == {"ip": "198.51.100.7"}


def test_without_trusted_proxies_forwarded_for_is_never_used() -> None:
    client = _client(_settings(), client=(PROXY, 40000))
    response = client.get("/probe/ip", headers={"X-Forwarded-For": "198.51.100.7"})
    assert response.json() == {"ip": PROXY}


# ---------------------------------------------------------------- body size


def test_body_over_limit_by_content_length_is_413() -> None:
    client = _client(_settings(request_body_max_bytes=1024))
    response = client.post("/probe/mutate", content=b"x" * 2048)
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


def test_body_over_limit_when_streamed_without_content_length_is_413() -> None:
    client = _client(_settings(request_body_max_bytes=1024))

    def chunks() -> object:
        for _ in range(4):
            yield b"y" * 512

    response = client.post("/probe/mutate", content=chunks())  # type: ignore[arg-type]
    assert response.status_code == 413


def test_body_within_limit_passes() -> None:
    client = _client(_settings(request_body_max_bytes=1024))
    response = client.post("/probe/mutate", content=b"z" * 1024)
    assert response.status_code == 200
    assert response.json() == {"size": 1024}


# ---------------------------------------------------------------- rate limit


def test_mutation_over_limit_returns_429_with_retry_after() -> None:
    client = _client(_settings(rate_limit_mutation_per_minute=3))
    codes = [client.post("/probe/mutate", content=b"a").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    response = client.post("/probe/mutate", content=b"a")
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-content-type-options"] == "nosniff"


def test_reads_are_not_limited_by_mutation_quota() -> None:
    client = _client(_settings(rate_limit_mutation_per_minute=1))
    assert all(client.get("/health/live").status_code == 200 for _ in range(10))


def test_heavy_read_paths_are_limited() -> None:
    client = _client(
        _settings(rate_limit_heavy_read_per_minute=2, rate_limit_heavy_read_paths="/probe/heavy/")
    )
    codes = [client.get("/probe/heavy/report").status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    assert "retry-after" in client.get("/probe/heavy/report").headers


def test_rate_limit_is_per_client_ip_not_global() -> None:
    settings = _settings(rate_limit_mutation_per_minute=1)
    app = _app(settings)
    first = TestClient(app, base_url="http://app.example.test", client=("203.0.113.1", 1))
    second = TestClient(app, base_url="http://app.example.test", client=("203.0.113.2", 1))
    assert first.post("/probe/mutate", content=b"a").status_code == 200
    assert first.post("/probe/mutate", content=b"a").status_code == 429
    assert second.post("/probe/mutate", content=b"a").status_code == 200


def test_rate_limit_is_per_session_even_when_ip_changes() -> None:
    settings = _settings(rate_limit_mutation_per_minute=1)
    app = _app(settings)
    cookie = {"Cookie": "crypidian_session=session-token-value"}
    first = TestClient(app, base_url="http://app.example.test", client=("203.0.113.1", 1))
    second = TestClient(app, base_url="http://app.example.test", client=("203.0.113.2", 1))
    assert first.post("/probe/mutate", content=b"a", headers=cookie).status_code == 200
    assert second.post("/probe/mutate", content=b"a", headers=cookie).status_code == 429


def test_rate_limit_uses_resolved_ip_so_spoofed_xff_cannot_evade() -> None:
    client = _client(_settings(rate_limit_mutation_per_minute=1))
    first = client.post("/probe/mutate", content=b"a", headers={"X-Forwarded-For": "1.1.1.1"})
    second = client.post("/probe/mutate", content=b"a", headers={"X-Forwarded-For": "2.2.2.2"})
    assert (first.status_code, second.status_code) == (200, 429)


def test_telegram_webhook_and_health_are_exempt_from_mutation_limit() -> None:
    client = _client(_settings(rate_limit_mutation_per_minute=1))
    codes = [client.post("/api/v1/telegram/webhook", content=b"{}").status_code for _ in range(4)]
    assert 429 not in codes


# ---------------------------------------------------------------- limiter unit


def test_sliding_window_limiter_reports_retry_after_and_recovers() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60, max_keys=10)
    assert limiter.hit("k", now=0.0) is None
    assert limiter.hit("k", now=10.0) is None
    retry_after = limiter.hit("k", now=20.0)
    assert retry_after == 40
    assert limiter.hit("k", now=61.0) is None


def test_sliding_window_limiter_bounds_tracked_keys() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60, max_keys=3)
    for index in range(10):
        limiter.hit(f"k{index}", now=float(index))
    assert len(limiter) <= 3


def test_bare_app_helper_does_not_break_when_middleware_absent() -> None:
    # Bảo đảm test helper dùng được với app thuần (không phụ thuộc create_app).
    app = FastAPI()
    assert TestClient(app).get("/nope").status_code == 404


# ---------------------------------------------------------------- edge cases & integration


def test_resolve_normalizes_ipv4_mapped_ipv6_peer() -> None:
    trusted = parse_trusted_proxies("10.0.0.5")
    assert resolve_client_ip("::ffff:10.0.0.5", "198.51.100.7", trusted) == "198.51.100.7"


@pytest.mark.parametrize("pattern", ["https://app.test", "foo.*.test", "app.test/x", "bad host"])
def test_settings_reject_invalid_allowed_hosts_patterns(pattern: str) -> None:
    with pytest.raises(ValidationError):
        AppSettings(allowed_hosts=pattern)


def test_non_admin_workers_in_production_do_not_require_allowed_hosts() -> None:
    settings = AppSettings(
        app_env="production",
        process_role="trading-runtime",
        runtime_workspace_id=uuid4(),
    )
    assert settings.allowed_host_list == ()


def test_auth_login_rate_limiter_uses_safe_client_ip_on_app_and_bare_router() -> None:
    @dataclass
    class RejectingAuth:
        calls: int = 0

        async def login(self, **_: object) -> object:
            self.calls += 1
            raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

    service = RejectingAuth()
    app = create_app(_settings(trusted_proxy_ips=PROXY, rate_limit_mutation_per_minute=50))
    app.dependency_overrides[get_auth_service] = lambda: service
    payload = {
        "workspace_slug": "paper-workspace",
        "email": "owner@example.test",
        "password": "wrong password value",
        "code": "000000",
    }

    # Peer không tin cậy đổi X-Forwarded-For liên tục vẫn bị chặn ở lần 6 (ip_max=5).
    untrusted = TestClient(app, base_url="http://app.example.test", client=("203.0.113.9", 50000))
    codes = [
        untrusted.post(
            "/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": f"1.1.1.{i}"}
        ).status_code
        for i in range(6)
    ]
    assert codes == [401] * 5 + [429]
    assert service.calls == 5

    # Proxy tin cậy chuyển tiếp 2 IP thật khác nhau → bộ đếm LoginRateLimiter tách biệt.
    trusted_client = TestClient(app, base_url="http://app.example.test", client=(PROXY, 40000))
    first = trusted_client.post(
        "/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": "198.51.100.1"}
    )
    second = trusted_client.post(
        "/api/v1/auth/login", json=payload, headers={"X-Forwarded-For": "198.51.100.2"}
    )
    assert (first.status_code, second.status_code) == (401, 401)

    # Router gắn độc lập không có ClientIPMiddleware vẫn bỏ qua X-Forwarded-For.
    bare = FastAPI()
    bare.include_router(auth_router)

    @bare.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        return JSONResponse({"ip": client_address(request)})

    bare_resp = TestClient(bare, client=("203.0.113.7", 1)).get(
        "/probe", headers={"X-Forwarded-For": "1.2.3.4"}
    )
    assert bare_resp.json() == {"ip": "203.0.113.7"}


def test_put_patch_delete_and_default_heavy_read_paths_are_rate_limited() -> None:
    app = _app(
        _settings(
            app_env="production",
            allowed_hosts="app.example.test",
            rate_limit_mutation_per_minute=3,
            rate_limit_heavy_read_per_minute=2,
        )
    )

    @app.put("/probe/item")
    @app.patch("/probe/item")
    @app.delete("/probe/item")
    async def mutate_item() -> JSONResponse:
        return JSONResponse({"ok": True})

    client = TestClient(app, base_url="https://app.example.test", client=("203.0.113.10", 1))
    assert client.put("/probe/item").status_code == 200
    assert client.patch("/probe/item").status_code == 200
    assert client.delete("/probe/item").status_code == 200
    blocked = client.delete("/probe/item")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1

    # Kiểm tra đường dẫn đọc nặng mặc định + cookie __Host- ở production khi đổi IP.
    cookie = {"Cookie": "__Host-crypidian_session=prod-session-token"}
    client_ip1 = TestClient(app, base_url="https://app.example.test", client=("203.0.113.11", 1))
    client_ip2 = TestClient(app, base_url="https://app.example.test", client=("203.0.113.12", 1))
    assert client_ip1.get("/admin/reports", headers=cookie).status_code in {200, 303, 401, 503}
    assert client_ip1.get("/admin/reports/export.csv", headers=cookie).status_code in {
        200,
        303,
        401,
        503,
    }
    heavy_blocked = client_ip2.get("/admin/api/chart/history", headers=cookie)
    assert heavy_blocked.status_code == 429
    assert int(heavy_blocked.headers["retry-after"]) >= 1


def test_oversized_real_route_and_multi_content_length_return_413() -> None:
    client = _client(_settings(request_body_max_bytes=512))
    oversized_login = client.post(
        "/api/v1/auth/login",
        content=b'{"pad":"' + b"x" * 1024 + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert oversized_login.status_code == 413

    spoofed_cl = client.post(
        "/probe/mutate",
        content=b"ok",
        headers={"Content-Length": "2, 2048"},
    )
    assert spoofed_cl.status_code == 413


def test_deploy_artifacts_and_uvicorn_flags_are_configured(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "--forwarded-allow-ips" in dockerfile
    assert "--limit-concurrency" in dockerfile
    assert "--no-server-header" in dockerfile

    caddyfile = (root / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "max_size 1MB" in caddyfile
    assert "149.154.160.0/20" in caddyfile
    assert "91.108.4.0/22" in caddyfile
    assert "reverse_proxy admin-web:8000" in caddyfile

    nginx = (root / "deploy" / "nginx" / "crypidian.conf").read_text(encoding="utf-8")
    assert "TLSv1.2 TLSv1.3" in nginx
    assert "client_max_body_size 1m;" in nginx
    assert "limit_req_zone" in nginx
    assert "X-Forwarded-For $remote_addr" in nginx
    assert "149.154.160.0/20" in nginx

    runbook = (root / "docs" / "operations" / "reverse-proxy-tls-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "TrustedHostMiddleware" in runbook
    assert "413" in runbook
    assert "429" in runbook
    assert "Retry-After" in runbook

    # Xác nhận Docker Compose merge giữa compose.yaml và deploy/compose.tls.yaml hợp lệ.
    if shutil.which("docker") is None:
        return
    shutil.copy2(root / "compose.yaml", tmp_path / "compose.yaml")
    shutil.copytree(root / "deploy", tmp_path / "deploy")
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir()
    for name in (
        "binance_api_key",
        "binance_api_secret",
        "garage_rpc_secret",
        "postgres_password",
        "database_admin_web_password",
        "mfa_encryption_key",
        "bootstrap_token",
        "database_trading_runtime_password",
        "database_execution_gateway_password",
        "database_ingestion_worker_password",
        "database_candidate_builder_password",
        "database_trainer_worker_password",
        "database_learning_evaluator_password",
        "database_scorer_history_password",
        "database_learning_shadow_observer_password",
        "database_replay_writer_password",
        "database_signal_worker_password",
        "database_market_signal_worker_password",
        "database_backtest_worker_password",
        "telegram_bot_token",
        "garage.env",
    ):
        (secrets_dir / name).write_text("placeholder\n", encoding="utf-8")

    env = os.environ | {
        "CRYPIDIAN_PUBLIC_HOST": "admin.crypidian.example",
        "TRADING_BOT_ALLOWED_HOSTS": "admin.crypidian.example",
        "TRADING_BOT_TRUSTED_PROXY_IPS": "172.18.0.0/16",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "deploy/compose.tls.yaml",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
    )
    model = json.loads(result.stdout)
    services = model["services"]
    assert "reverse-proxy" in services
    assert "web_ingress" in services["reverse-proxy"]["networks"]
    admin_web = services["admin-web"]
    assert admin_web["environment"]["TRADING_BOT_APP_ENV"] == "production"
    assert admin_web["environment"]["TRADING_BOT_ALLOWED_HOSTS"] == "admin.crypidian.example"
    assert admin_web["environment"]["TRADING_BOT_TRUSTED_PROXY_IPS"] == "172.18.0.0/16"
    assert not admin_web.get("ports")
    assert "--forwarded-allow-ips" in admin_web["command"]
    assert "--limit-concurrency" in admin_web["command"]
    assert "--no-server-header" in admin_web["command"]


def test_uvicorn_proxy_headers_middleware_composes_cleanly_with_app() -> None:
    app = _app(_settings(trusted_proxy_ips=PROXY, rate_limit_mutation_per_minute=2))
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=PROXY)

    trusted_client = TestClient(wrapped, base_url="https://app.example.test", client=(PROXY, 443))
    spoofing_client = TestClient(
        wrapped, base_url="https://app.example.test", client=("203.0.113.55", 443)
    )

    assert trusted_client.get("/probe/ip", headers={"X-Forwarded-For": "198.51.100.88"}).json() == {
        "ip": "198.51.100.88"
    }
    assert spoofing_client.get("/probe/ip", headers={"X-Forwarded-For": "1.2.3.4"}).json() == {
        "ip": "203.0.113.55"
    }
