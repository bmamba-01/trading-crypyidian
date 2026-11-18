"""E2E PostgreSQL thật cho diễn tập Backup mã hóa & Restore cô lập có đo RPO/RTO (OPS07C)."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts.backup_restore_drill import (
    run_encrypted_backup,
    run_isolated_postgres_restore_drill,
)
from scripts.docker_image import build_image_reference, ensure_image_available

from trading_bot.observability.backup_drill import RestoreVerificationError


def test_ops07c_encrypted_backup_and_isolated_postgres_restore_drill(tmp_path: Path) -> None:
    """Chạy backup mã hóa pg_dump từ Postgres thật, restore sang DB cô lập và đo RPO/RTO."""
    root = Path(__file__).parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    image = build_image_reference(root)
    ensure_image_available(root, image)
    project = f"crypidian-bkp-e2e-{uuid.uuid4().hex[:10]}"
    env = os.environ | {"CRYPIDIAN_APP_IMAGE": image}
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

    try:
        subprocess.run(
            [*base, "up", "-d", "--wait", "postgres"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [*base, "run", "--rm", "migrator", "alembic", "upgrade", "head"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        # Seed dữ liệu thật vào workspace và user_account trong DB nguồn
        ws_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        seed_sql = (
            f"INSERT INTO workspace (id, name, slug, mode, status) "
            f"VALUES ('{ws_id}', 'Drill WS', 'drill-ws-{ws_id[:8]}', 'PAPER', 'ACTIVE');\n"
            f"INSERT INTO user_account (id, email_normalized, password_hash, status) "
            f"VALUES ('{user_id}', 'drill-{user_id[:8]}@crypidian.io', 'hash', 'ACTIVE');\n"
        )
        subprocess.run(
            [
                *base,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "crypidian_admin",
                "-d",
                "crypidian",
            ],
            cwd=root,
            env=env,
            input=seed_sql,
            check=True,
            capture_output=True,
            text=True,
        )

        backup_dir = tmp_path / "backups"
        restore_dir = tmp_path / "restore"
        object_manifest = tmp_path / "object_store_manifest.json"
        object_manifest.write_text(
            '{"artifacts": ["model-v1.bin"], "sha256": "deadbeef"}', encoding="utf-8"
        )
        passphrase = "e2e-drill-passphrase-2026"
        backup_id = f"bkp-e2e-{uuid.uuid4().hex[:8]}"

        manifest_path = run_encrypted_backup(
            source_project=project,
            postgres_service="postgres",
            db_user="crypidian_admin",
            db_name="crypidian",
            output_dir=backup_dir,
            backup_id=backup_id,
            operator_id="owner@crypidian.io",
            passphrase=passphrase,
            object_manifest_path=object_manifest,
            secret_canaries=("CANARY_BINANCE_SECRET_999",),
            min_free_bytes=10 * 1024 * 1024,
        )
        assert manifest_path.is_file()
        assert not (backup_dir / f"{backup_id}.pgdump.sql").exists()

        report_path = run_isolated_postgres_restore_drill(
            manifest_path=manifest_path,
            backup_dir=backup_dir,
            restore_dir=restore_dir,
            target_environment="isolated-drill",
            target_project=project,
            postgres_service="postgres",
            db_user="crypidian_admin",
            isolated_db_name="crypidian_restore_drill",
            passphrase=passphrase,
            max_rpo_seconds=900,
            max_rto_seconds=300,
            min_free_bytes=10 * 1024 * 1024,
        )
        assert report_path.is_file()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["verified"] is True
        assert report["rpo_within_sla"] is True
        assert report["rto_within_sla"] is True
        assert report["rto_seconds"] >= 0.0
        assert report["db_verification"]["isolated_database"] == "crypidian_restore_drill"
        assert report["db_verification"]["alembic_version"]

        # Đối chiếu số dòng trong DB cô lập khớp dữ liệu đã seed
        check_res = subprocess.run(
            [
                *base,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-t",
                "-A",
                "-U",
                "crypidian_admin",
                "-d",
                "crypidian_restore_drill",
                "-c",
                f"SELECT count(*) FROM workspace WHERE id = '{ws_id}';",
            ],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert check_res.stdout.strip() == "1"

        # Xác nhận sai passphrase khi restore sẽ bị chặn
        with pytest.raises(RestoreVerificationError, match="Giải mã"):
            run_isolated_postgres_restore_drill(
                manifest_path=manifest_path,
                backup_dir=backup_dir,
                restore_dir=tmp_path / "restore_bad_pass",
                target_environment="isolated-drill",
                target_project=project,
                postgres_service="postgres",
                db_user="crypidian_admin",
                isolated_db_name="crypidian_restore_drill_bad",
                passphrase="wrong-passphrase",
                min_free_bytes=10 * 1024 * 1024,
            )
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
        )
