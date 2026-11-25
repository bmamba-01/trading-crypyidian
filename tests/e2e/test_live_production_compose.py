"""E2E kiểm chứng overlay Compose production (OPS07F / Task 7 / CRY-465).

Chứng minh bằng `docker compose config --format json` (không khởi động container, không secret):
1. Overlay `compose.yaml + deploy/compose.tls.yaml + deploy/compose.production.yaml` render sạch.
2. Chỉ duy nhất `execution-gateway` có network `exchange_egress_prod` và secret Binance Live.
3. Các service khác (`admin-web`, `trading-runtime`, `migrator`, `ingestion-worker`,
   `trainer-worker`, `backtest-worker`, `report-reader`) không có egress sàn hay secret Binance.
4. Volume và network production tách biệt hoàn toàn khỏi local/PAPER/Demo; Live mặc định bị khóa.
5. Thiếu `CRYPIDIAN_PRODUCTION_IMAGE` sẽ fail-closed ngay khi render config.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_IMAGE = (
    "ghcr.io/crypidian/app@sha256:1111111111111111111111111111111111111111111111111111111111111111"
)


def _render_compose_json(
    files: list[str],
    extra_env: dict[str, str],
    *,
    profile: str | None = None,
) -> dict:
    env = os.environ.copy()
    env.update(extra_env)
    cmd = ["docker", "compose"]
    if profile:
        cmd.extend(["--profile", profile])
    for f in files:
        cmd.extend(["-f", f])
    cmd.extend(["config", "--format", "json"])
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_production_compose_overlay_enforces_gateway_only_egress_and_secret_boundary(
    tmp_path: Path,
) -> None:
    """Chỉ `execution-gateway` có `exchange_egress_prod` và secret Live; tách volume/network."""
    fake_secret_dir = tmp_path / "fake_live_secrets"
    fake_secret_dir.mkdir()
    (fake_secret_dir / "binance_api_key").write_text("SYNTHETIC_KEY_ONLY", encoding="utf-8")
    (fake_secret_dir / "binance_api_secret").write_text("SYNTHETIC_SECRET_ONLY", encoding="utf-8")

    cfg = _render_compose_json(
        [
            "compose.yaml",
            "deploy/compose.tls.yaml",
            "deploy/compose.production.yaml",
        ],
        {
            "CRYPIDIAN_PUBLIC_HOST": "admin.crypidian.example",
            "TRADING_BOT_ALLOWED_HOSTS": "admin.crypidian.example",
            "TRADING_BOT_TRUSTED_PROXY_IPS": "172.28.0.0/16",
            "CRYPIDIAN_PRODUCTION_IMAGE": SYNTHETIC_IMAGE,
            "CRYPIDIAN_LIVE_SECRET_DIR": str(fake_secret_dir),
        },
        profile="production-locked",
    )

    services = cfg["services"]
    networks = cfg["networks"]
    assert networks["backend_prod"]["internal"] is True

    # 1. Chỉ duy nhất execution-gateway được nối vào exchange_egress_prod và có secret Live
    egress_services = [
        name
        for name, svc in services.items()
        if "exchange_egress_prod" in (svc.get("networks") or {})
        or "exchange_egress" in (svc.get("networks") or {})
    ]
    assert egress_services == ["execution-gateway"], egress_services

    binance_secret_services = []
    for name, svc in services.items():
        secret_sources = {
            (s["source"] if isinstance(s, dict) else s) for s in (svc.get("secrets") or [])
        }
        if any("binance" in src for src in secret_sources):
            binance_secret_services.append(name)
    assert binance_secret_services == ["execution-gateway"], binance_secret_services

    # 2. Kiểm tra non-root, read_only, cap_drop ALL và LIVE dispatch bị khóa trên các app services
    hardened_services = [
        "admin-web",
        "migrator",
        "trading-runtime",
        "execution-gateway",
        "ingestion-worker",
        "trainer-worker",
        "backtest-worker",
        "report-reader",
    ]
    for svc_name in hardened_services:
        svc = services[svc_name]
        assert svc["image"] == SYNTHETIC_IMAGE
        assert svc["read_only"] is True
        assert svc["user"] == "10001:10001"
        assert "ALL" in (svc.get("cap_drop") or [])
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
        env_map = svc.get("environment") or {}
        assert env_map.get("TRADING_BOT_LIVE_DISPATCH_LOCKED") == "true"
        assert env_map.get("TRADING_BOT_TRADING_MODE") == "PAPER"
        assert env_map.get("TZ") == "UTC"

    # 3. Volume production tách riêng hoàn toàn khỏi postgres_data local
    pg_volumes = services["postgres"].get("volumes") or []
    pg_sources = [v["source"] for v in pg_volumes if isinstance(v, dict)]
    assert "postgres_production_data" in pg_sources
    assert "postgres_data" not in pg_sources
    assert not services["postgres"].get("ports")


def test_default_local_compose_cannot_accidentally_enable_live_or_live_secrets() -> None:
    """Profile mặc định của `compose.yaml` không chứa secret Live hay bật mode LIVE."""
    cfg = _render_compose_json(["compose.yaml"], {})
    assert "binance_live_api_key" not in (cfg.get("secrets") or {})
    for name, svc in (cfg.get("services") or {}).items():
        mode = (svc.get("environment") or {}).get("TRADING_BOT_TRADING_MODE")
        assert mode != "LIVE", f"Service {name} không được phép đặt LIVE ở compose mặc định"


def test_production_compose_fails_closed_without_pinned_image(tmp_path: Path) -> None:
    """Thiếu CRYPIDIAN_PRODUCTION_IMAGE phải fail-closed ngay khi render config."""
    env = os.environ.copy()
    env.pop("CRYPIDIAN_PRODUCTION_IMAGE", None)
    env.update(
        {
            "CRYPIDIAN_PUBLIC_HOST": "admin.crypidian.example",
            "TRADING_BOT_ALLOWED_HOSTS": "admin.crypidian.example",
            "TRADING_BOT_TRUSTED_PROXY_IPS": "172.28.0.0/16",
            "CRYPIDIAN_LIVE_SECRET_DIR": str(tmp_path),
        }
    )
    res = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "deploy/compose.tls.yaml",
            "-f",
            "deploy/compose.production.yaml",
            "config",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "CRYPIDIAN_PRODUCTION_IMAGE" in res.stderr
