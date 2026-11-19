"""Unit tests TDD (RED -> GREEN) cho SEC-44, SEC-45 và SEC-46 (CRY-149)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider
from trading_bot.common.secret_inventory import (
    SYSTEM_SECRET_INVENTORY,
    verify_secret_inventory_and_permissions,
)
from trading_bot.observability.supply_chain import (
    scan_repository_for_leaked_secrets,
    verify_compose_container_hardening,
    verify_compose_egress_isolation,
    verify_pinned_container_images,
    verify_sbom_matches_lockfile,
    verify_supply_chain_ci_workflow,
)

ROOT = Path(__file__).parents[2]


# ---------------------------------------------------------------------------
# SEC-44: Kiểm kê secret, quyền 0700/0600, MFA key_id & runbook
# ---------------------------------------------------------------------------


def test_prepare_local_secrets_enforces_0700_dir_and_0600_files_including_existing(
    tmp_path: Path,
) -> None:
    """Thư mục .secrets phải có mode 0700 và mọi tệp secret phải ép mode 0600."""
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir(mode=0o755)
    os.chmod(secrets_dir, 0o755)

    existing_pg = secrets_dir / "postgres_password"
    existing_pg.write_text("keep-my-existing-secret", encoding="utf-8")
    os.chmod(existing_pg, 0o644)

    existing_extra = secrets_dir / "custom_operator_note"
    existing_extra.write_text("local-note", encoding="utf-8")
    os.chmod(existing_extra, 0o666)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_local_secrets.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Binance API key/secret đang để trống" in result.stdout

    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert existing_pg.read_text(encoding="utf-8") == "keep-my-existing-secret"
    assert stat.S_IMODE(existing_pg.stat().st_mode) == 0o600
    assert stat.S_IMODE(existing_extra.stat().st_mode) == 0o600

    for item in secrets_dir.iterdir():
        assert item.is_file()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600

    # Placeholder sàn và Telegram token mặc định trống; webhook secret >= 32 ký tự
    assert (secrets_dir / "binance_api_key").read_text(encoding="utf-8") == ""
    assert (secrets_dir / "binance_api_secret").read_text(encoding="utf-8") == ""
    assert (secrets_dir / "telegram_bot_token").read_text(encoding="utf-8") == ""
    assert len((secrets_dir / "telegram_webhook_secret").read_text(encoding="utf-8")) >= 32

    report = verify_secret_inventory_and_permissions(secrets_dir)
    assert report.passed is True
    assert report.violations == ()
    assert report.directory_mode_octal == "0o700"
    assert report.mfa_key_id is not None
    assert report.mfa_key_id.startswith("mfa-v1-")


def test_secret_inventory_declares_all_required_categories_and_rotation_cycles() -> None:
    """Danh mục secret phải đủ DB roles, MFA key_id, Telegram, Binance, bootstrap & rotation."""
    by_name = {spec.name: spec for spec in SYSTEM_SECRET_INVENTORY}
    expected_db_secrets = {
        "postgres_password",
        "database_admin_web_password",
        "database_trading_runtime_password",
        "database_execution_gateway_password",
        "database_ingestion_worker_password",
        "database_signal_worker_password",
        "database_market_signal_worker_password",
        "database_trainer_worker_password",
        "database_learning_evaluator_password",
        "database_learning_shadow_observer_password",
        "database_replay_writer_password",
        "database_candidate_builder_password",
        "database_backtest_worker_password",
        "database_scorer_history_password",
    }
    expected_app_secrets = {
        "mfa_encryption_key",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "binance_api_key",
        "binance_api_secret",
        "bootstrap_token",
        "garage_rpc_secret",
        "garage_s3_access_key",
        "garage_s3_secret_key",
        "garage.env",
    }
    assert expected_db_secrets | expected_app_secrets <= by_name.keys()
    assert by_name["mfa_encryption_key"].requires_key_id is True
    for spec in SYSTEM_SECRET_INVENTORY:
        assert spec.max_rotation_days > 0
        assert spec.owner_services


def test_verify_secret_inventory_rejects_loose_permissions_and_missing_files(
    tmp_path: Path,
) -> None:
    """Từ chối nếu thư mục khác 0700, file cho phép group/other đọc/ghi, hoặc thiếu secret."""
    secrets_dir = tmp_path / ".secrets"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_local_secrets.py")],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    # 1) Thư mục lỏng quyền 0755
    os.chmod(secrets_dir, 0o755)
    dir_bad = verify_secret_inventory_and_permissions(secrets_dir)
    assert dir_bad.passed is False
    assert any("0o700" in item for item in dir_bad.violations)
    os.chmod(secrets_dir, 0o700)

    # 2) File secret cho phép group/other đọc hoặc ghi (0640, 0604, 0644, 0666)
    for bad_mode in (0o640, 0o604, 0o644, 0o666):
        target = secrets_dir / "mfa_encryption_key"
        os.chmod(target, bad_mode)
        bad_report = verify_secret_inventory_and_permissions(secrets_dir)
        assert bad_report.passed is False
        assert any("mfa_encryption_key" in v and "0o600" in v for v in bad_report.violations)
    os.chmod(secrets_dir / "mfa_encryption_key", 0o600)

    # 3) File ngoài danh mục trong .secrets nhưng lỏng quyền vẫn bị từ chối
    stray = secrets_dir / "stray_secret"
    stray.write_text("x", encoding="utf-8")
    os.chmod(stray, 0o644)
    stray_report = verify_secret_inventory_and_permissions(secrets_dir)
    assert stray_report.passed is False
    assert any("stray_secret" in v for v in stray_report.violations)
    stray.unlink()

    # 4) Thiếu file secret bắt buộc
    (secrets_dir / "bootstrap_token").unlink()
    missing_report = verify_secret_inventory_and_permissions(secrets_dir)
    assert missing_report.passed is False
    assert any("bootstrap_token" in v for v in missing_report.violations)


def test_verify_secret_inventory_reports_overdue_rotation(tmp_path: Path) -> None:
    """Cảnh báo khi tuổi file secret vượt chu kỳ xoay tối đa."""
    secrets_dir = tmp_path / ".secrets"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_local_secrets.py")],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    future = datetime.now(UTC) + timedelta(days=120)
    report = verify_secret_inventory_and_permissions(
        secrets_dir, check_rotation_age=True, now_utc=future
    )
    assert report.passed is True
    assert any("postgres_password" in w for w in report.rotation_warnings)
    assert any("bootstrap_token" in w for w in report.rotation_warnings)


def test_local_mfa_provider_embeds_key_id_and_rotates_losslessly(tmp_path: Path) -> None:
    """Ciphertext MFA phải mang key_id/version và xoay khóa không làm mất dữ liệu TOTP."""
    key1 = tmp_path / "key_v1"
    key2 = tmp_path / "key_v2"
    key1.write_text(base64.urlsafe_b64encode(b"1" * 32).decode("ascii"), encoding="ascii")
    key2.write_text(base64.urlsafe_b64encode(b"2" * 32).decode("ascii"), encoding="ascii")
    vault = tmp_path / "vault"
    workspace_id = uuid4()
    totp_secret = "JBSWY3DPEHPK3PXP"

    provider_v1 = LocalMfaSecretProvider(key_file=key1, storage_directory=vault, key_id="k1")
    reference = provider_v1.store_mfa_secret(workspace_id, totp_secret)

    # Kiểm tra quyền vault 0700 và file .bin 0600, payload có key_id
    assert stat.S_IMODE(vault.stat().st_mode) == 0o700
    bin_files = list(vault.glob("*.bin"))
    assert len(bin_files) == 1
    assert stat.S_IMODE(bin_files[0].stat().st_mode) == 0o600
    payload = json.loads(bin_files[0].read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["key_id"] == "k1"
    assert provider_v1.inspect_ciphertext_key_id(reference) == "k1"

    # Tạo legacy v1 ciphertext không có key_id để kiểm tra tương thích ngược
    legacy_ref = f"local-mfa:{uuid4()}"
    legacy_id = legacy_ref.split(":")[1].replace("-", "")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = b"N" * 12
    legacy_ct = AESGCM(b"1" * 32).encrypt(
        nonce, totp_secret.encode("utf-8"), f"crypidian:mfa:{workspace_id}:{legacy_ref}".encode()
    )
    legacy_path = vault / f"{legacy_id}.bin"
    legacy_path.write_text(
        json.dumps(
            {
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(legacy_ct).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    # Xoay sang key2 với previous_keys={"k1": key1}
    provider_v2 = LocalMfaSecretProvider(
        key_file=key2,
        storage_directory=vault,
        key_id="k2",
        previous_keys={"k1": key1},
    )
    assert provider_v2.resolve_mfa_secret(workspace_id, reference) == totp_secret
    assert provider_v2.resolve_mfa_secret(workspace_id, legacy_ref) == totp_secret

    provider_v2.rotate_mfa_secret(workspace_id, reference)
    provider_v2.rotate_mfa_secret(workspace_id, legacy_ref)
    assert provider_v2.inspect_ciphertext_key_id(reference) == "k2"
    assert provider_v2.inspect_ciphertext_key_id(legacy_ref) == "k2"

    # Gỡ khóa cũ k1: chỉ giữ key2 vẫn giải mã được cả hai secret sau khi xoay
    provider_only_v2 = LocalMfaSecretProvider(key_file=key2, storage_directory=vault, key_id="k2")
    assert provider_only_v2.resolve_mfa_secret(workspace_id, reference) == totp_secret
    assert provider_only_v2.resolve_mfa_secret(workspace_id, legacy_ref) == totp_secret


def test_secret_inventory_runbook_exists_in_vietnamese() -> None:
    """Runbook kiểm kê và xoay secret tiếng Việt phải có đủ các nhóm secret SEC-44."""
    doc = (ROOT / "docs/operations/secret-inventory-and-rotation.md").read_text(encoding="utf-8")
    for required in (
        "SEC-44",
        "0600",
        "0700",
        "mfa_encryption_key",
        "key_id",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "binance_api_key",
        "binance_api_secret",
        "bootstrap_token",
        "crypidian_admin_web",
        "crypidian_execution_gateway",
        "crypidian_scorer_history",
        "90",
        "365",
    ):
        assert required in doc


# ---------------------------------------------------------------------------
# SEC-45: Gate CI chuỗi cung ứng, quét secret rò rỉ & SBOM/digest
# ---------------------------------------------------------------------------


def test_supply_chain_ci_workflow_is_valid_and_enforces_mandatory_gates() -> None:
    """Workflow GitHub Actions phải có đủ cổng bắt buộc và quyền tối thiểu."""
    workflow_path = ROOT / ".github/workflows/supply-chain-security.yml"
    report = verify_supply_chain_ci_workflow(workflow_path)
    assert report.passed is True
    assert report.violations == ()
    assert "uv_lock_check" in report.verified_gates
    assert "uv_sync_frozen" in report.verified_gates
    assert "uv_audit" in report.verified_gates
    assert "ruff_check" in report.verified_gates
    assert "mypy" in report.verified_gates
    assert "sbom_audit" in report.verified_gates
    assert "secret_scan" in report.verified_gates
    assert "container_hardening" in report.verified_gates
    assert "secret_inventory" in report.verified_gates


def test_supply_chain_ci_verifier_rejects_missing_or_weakened_gates(tmp_path: Path) -> None:
    """Phát hiện ngay nếu workflow bị xóa bước kiểm tra hoặc dùng continue-on-error/write-all."""
    missing_report = verify_supply_chain_ci_workflow(tmp_path / "missing.yml")
    assert missing_report.passed is False

    weakened = tmp_path / "weak.yml"
    weakened.write_text(
        """
name: Supply Chain
on:
  push:
    branches: [dev]
  pull_request:
    branches: [dev]
permissions: write-all
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - run: uv lock --check
      - run: uv sync --frozen
      - run: uv audit --frozen
        continue-on-error: true
      - run: uv run ruff check src tests
      # uv run mypy src/trading_bot
""",
        encoding="utf-8",
    )
    weak_report = verify_supply_chain_ci_workflow(weakened)
    assert weak_report.passed is False
    assert any("write-all" in v for v in weak_report.violations)
    assert any("continue-on-error" in v for v in weak_report.violations)
    assert any("mypy" in v for v in weak_report.violations)
    assert any("sbom" in v.lower() for v in weak_report.violations)


def test_repository_secret_canary_scan_and_sbom_lock_match() -> None:
    """Repo sạch không rò rỉ canary/key; SBOM khớp uv.lock; phát hiện được mẫu rò rỉ."""
    scan_report = scan_repository_for_leaked_secrets(ROOT)
    assert scan_report.passed is True
    assert scan_report.findings == ()

    sbom_match, sbom_diffs = verify_sbom_matches_lockfile(
        ROOT / "docs/implementation/sbom.cdx.json",
        ROOT / "uv.lock",
    )
    assert sbom_match is True
    assert sbom_diffs == ()

    image_report = verify_pinned_container_images(ROOT / "Dockerfile", ROOT / "compose.yaml")
    assert image_report.passed is True
    assert image_report.violations == ()


def test_secret_canary_scan_catches_leaked_canary_and_private_key(tmp_path: Path) -> None:
    """Quét rò rỉ phải bắt được canary bí mật, private key PEM và tệp .secrets."""
    (tmp_path / "src").mkdir()
    canary = "CRYPIDIAN_CANARY_SECRET_9911"
    pem_dummy = "-----BEGIN " + "RSA PRIVATE KEY-----"
    (tmp_path / "src/leaked.py").write_text(
        f'KEY = "{canary}"\nPEM = "{pem_dummy}"\n',
        encoding="utf-8",
    )
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/postgres_password").write_text("oops", encoding="utf-8")

    bad_scan = scan_repository_for_leaked_secrets(tmp_path, extra_canaries=(canary,))
    assert bad_scan.passed is False
    assert len(bad_scan.findings) >= 2


# ---------------------------------------------------------------------------
# SEC-46: Giới hạn tài nguyên container & bind mount :ro
# ---------------------------------------------------------------------------


def test_compose_container_hardening_and_egress_isolation_pass_on_repo() -> None:
    """Toàn bộ service trong compose.yaml phải có giới hạn tài nguyên, :ro bind mount và cách ly."""
    compose_path = ROOT / "compose.yaml"
    expected_services = (
        "postgres",
        "object-store",
        "admin-web",
        "mfa-volume-provisioner",
        "migrator",
        "db-provisioner",
        "qa-seed",
        "trading-runtime",
        "execution-gateway",
        "ingestion-worker",
        "candidate-proof-builder",
        "learning-split-builder",
        "trainer-worker",
        "learning-shadow-observer",
        "learning-evaluator",
        "backtest-worker",
        "telegram-signal-worker",
        "market-signal-worker",
        "report-reader",
    )
    report = verify_compose_container_hardening(
        compose_path,
        required_services=expected_services,
    )
    assert report.passed is True
    assert report.violations == ()
    assert set(report.checked_services) == set(expected_services)

    compose_text = compose_path.read_text(encoding="utf-8")
    assert "./migrations:/app/migrations:ro" in compose_text

    egress_report = verify_compose_egress_isolation(compose_path)
    assert egress_report.passed is True
    assert egress_report.violations == ()


def test_compose_hardening_verifier_rejects_missing_limits_and_rw_bind_mounts(
    tmp_path: Path,
) -> None:
    """Verifier phải fail khi thiếu mem_limit/cpus/pids_limit, thiếu :ro hoặc cấu hình nguy hiểm."""
    bad_compose = tmp_path / "compose.yaml"
    bad_compose.write_text(
        """
services:
  migrator:
    image: crypidian-app:local
    volumes:
      - ./migrations:/app/migrations
    mem_limit: 256m
  worker:
    image: crypidian-app:local
    privileged: true
    network_mode: host
    cpus: 0.5
    mem_limit: 256m
    pids_limit: 32
    volumes:
      - type: bind
        source: ./cfg
        target: /app/cfg
""",
        encoding="utf-8",
    )
    bad_report = verify_compose_container_hardening(bad_compose)
    assert bad_report.passed is False
    joined = "\n".join(bad_report.violations)
    assert "cpus" in joined
    assert "pids_limit" in joined
    assert ":ro" in joined or "read_only" in joined
    assert "privileged" in joined
    assert "network_mode" in joined


def test_docker_compose_config_validates_all_service_limits_and_ro_mounts(
    tmp_path: Path,
) -> None:
    """Chạy `docker compose config --format json` kiểm chứng limits và :ro cho mọi service."""
    assert shutil.which("docker") is not None, "Cần docker CLI để kiểm chứng docker compose config"

    shutil.copy2(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    shutil.copytree(ROOT / "deploy", tmp_path / "deploy")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_local_secrets.py")],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(result.stdout)
    services = parsed["services"]
    for name, service in services.items():
        assert float(service["cpus"]) > 0, f"{name} thiếu cpus"
        assert int(service["mem_limit"]) > 0, f"{name} thiếu mem_limit"
        assert int(service["pids_limit"]) > 0, f"{name} thiếu pids_limit"
        limits = service["deploy"]["resources"]["limits"]
        assert float(limits["cpus"]) > 0
        assert int(limits["memory"]) > 0
        assert int(limits["pids"]) > 0
        for vol in service.get("volumes", []):
            if vol.get("type") == "bind":
                assert vol.get("read_only") is True, f"{name} bind mount {vol} thiếu read_only"

    # Kiểm chứng merge với deploy/compose.tls.yaml cũng hợp lệ
    env = os.environ | {
        "CRYPIDIAN_PUBLIC_HOST": "admin.crypidian.example",
        "TRADING_BOT_ALLOWED_HOSTS": "admin.crypidian.example",
        "TRADING_BOT_TRUSTED_PROXY_IPS": "172.18.0.0/16",
    }
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "deploy/compose.tls.yaml",
            "config",
            "--quiet",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
