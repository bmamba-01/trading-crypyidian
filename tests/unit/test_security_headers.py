"""SEC-27: header bảo mật cho mọi response của app (SECT-32)."""

from collections.abc import Mapping

from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.common.http_security import security_headers
from trading_bot.config.settings import AppSettings

EXPECTED_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


def _assert_baseline_headers(headers: Mapping[str, str]) -> None:
    assert headers["content-security-policy"] == EXPECTED_CSP
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"


def test_local_responses_carry_security_headers_without_hsts() -> None:
    client = TestClient(create_app(AppSettings(app_env="local")))

    response = client.get("/health/live")

    assert response.status_code == 200
    _assert_baseline_headers(response.headers)
    assert "strict-transport-security" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_production_responses_add_hsts() -> None:
    client = TestClient(create_app(AppSettings(app_env="production")))

    response = client.get("/health/live")

    _assert_baseline_headers(response.headers)
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_error_and_unknown_routes_also_carry_headers_and_no_store() -> None:
    client = TestClient(create_app(AppSettings(app_env="test")))

    unauthenticated = client.get("/api/v1/auth/me")
    missing = client.get("/does-not-exist")
    invalid = client.post("/api/v1/auth/login", json={})

    for response in (unauthenticated, missing, invalid):
        assert response.status_code in {401, 404, 422, 503}
        _assert_baseline_headers(response.headers)
        assert response.headers["cache-control"] == "no-store"


def test_route_defined_cache_control_is_not_overridden() -> None:
    app = create_app(AppSettings(app_env="test"))

    @app.get("/cacheable-probe")
    def cacheable_probe() -> PlainTextResponse:
        return PlainTextResponse("ok", headers={"Cache-Control": "public, max-age=60"})

    response = TestClient(app).get("/cacheable-probe")

    assert response.headers["cache-control"] == "public, max-age=60"
    _assert_baseline_headers(response.headers)


def test_security_headers_helper_only_adds_hsts_for_production() -> None:
    local_names = {name for name, _ in security_headers(AppSettings(app_env="local"))}
    production_names = {name for name, _ in security_headers(AppSettings(app_env="production"))}

    assert "Strict-Transport-Security" not in local_names
    assert production_names - local_names == {"Strict-Transport-Security"}
