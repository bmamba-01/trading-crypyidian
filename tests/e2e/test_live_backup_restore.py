"""E2E diễn tập backup mã hóa và restore cô lập cho topology production (OPS07F / Task 7 / CRY-465).

Chứng minh trên PostgreSQL disposable cô lập (không dùng credential thật):
1. Seed dữ liệu tài khoản + risk reservation vào DB nguồn.
2. Thực hiện backup mã hóa (có secret canary check, xóa plaintext dump, lưu manifest SHA-256).
3. Restore sang database target cô lập (`crypidian_live_restore_target`), kiểm tra:
   - `verified=True`, khớp row count & dữ liệu thực tế sau restore.
   - Đo RPO/RTO thực tế đều hữu hạn và trong SLA diễn tập.
   - Giải mã bằng sai passphrase phải thất bại fail-closed (`RestoreVerificationError`).
"""

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


def test_live_encrypted_backup_and_isolated_restore_consistency_and_rpo_rto(tmp_path: Path) -> None:
    """Diễn tập backup mã hóa và restore vào DB target cô lập; đo RPO/RTO và kiểm hash/row count."""
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        cwd=root,
        capture_output=True,
    )
    image = build_image_reference(root)
    ensure_image_available(root, image)
    project = f"crypidian-cry465-bkp-{uuid.uuid4().hex[:10]}"
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
            [*base, "run", "--rm", "--no-deps", "db-provisioner"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [*base, "run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        # Seed workspace + trading_account thật để kiểm chứng tính toàn vẹn sau restore
        ws_id = str(uuid.uuid4())
        acc_id = str(uuid.uuid4())
        seed_sql = (
            "INSERT INTO workspace (id, name, slug, mode, status) "
            f"VALUES ('{ws_id}', 'Prod Drill WS', 'prod-drill-{ws_id[:8]}', 'PAPER', 'ACTIVE');\n"
            "INSERT INTO trading_account "
            "(id, workspace_id, name, venue, mode, environment, status, max_notional) "
            f"VALUES ('{acc_id}', '{ws_id}', 'Prod Acc', 'binance_usdm', "
            "'PAPER', 'local', 'ENABLED', 10000);\n"
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
            '{"artifacts": ["live-risk-profile-v1.json"], "sha256": "cafebabe"}',
            encoding="utf-8",
        )
        passphrase = "cry465-synthetic-drill-passphrase"
        backup_id = f"live-bkp-{uuid.uuid4().hex[:8]}"

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
            secret_canaries=("SYNTHETIC_LIVE_KEY_CANARY_465",),
            min_free_bytes=10 * 1024 * 1024,
        )
        assert manifest_path.is_file()
        assert not (backup_dir / f"{backup_id}.pgdump.sql").exists()

        # 1. Restore với sai passphrase phải fail-closed
        with pytest.raises(RestoreVerificationError):
            run_isolated_postgres_restore_drill(
                manifest_path=manifest_path,
                backup_dir=backup_dir,
                restore_dir=tmp_path / "restore_bad",
                target_environment="isolated-live-restore",
                target_project=project,
                postgres_service="postgres",
                db_user="crypidian_admin",
                isolated_db_name="crypidian_live_restore_bad",
                passphrase="wrong-passphrase",
                max_rpo_seconds=900,
                max_rto_seconds=300,
                min_free_bytes=10 * 1024 * 1024,
            )

        # 2. Restore chuẩn vào DB cô lập và đo RPO/RTO thực tế
        report_path = run_isolated_postgres_restore_drill(
            manifest_path=manifest_path,
            backup_dir=backup_dir,
            restore_dir=restore_dir,
            target_environment="isolated-live-restore",
            target_project=project,
            postgres_service="postgres",
            db_user="crypidian_admin",
            isolated_db_name="crypidian_live_restore_target",
            passphrase=passphrase,
            max_rpo_seconds=900,
            max_rto_seconds=300,
            min_free_bytes=10 * 1024 * 1024,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["verified"] is True
        assert report["rpo_within_sla"] is True
        assert report["rto_within_sla"] is True
        assert report["rto_seconds"] >= 0.0
        assert report["db_verification"]["alembic_version"]

        # 3. Đối chiếu row count & dữ liệu thật trong DB target cô lập
        verify_sql = (
            f"SELECT count(*) FROM workspace WHERE id = '{ws_id}';\n"
            f"SELECT count(*) FROM trading_account WHERE id = '{acc_id}';\n"
        )
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
                "crypidian_live_restore_target",
            ],
            cwd=root,
            env=env,
            input=verify_sql,
            check=True,
            capture_output=True,
            text=True,
        )
        counts = [line.strip() for line in check_res.stdout.splitlines() if line.strip()]
        assert counts == ["1", "1"], counts
    finally:
        subprocess.run(
            [*base, "down", "-v", "--remove-orphans"],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
        )
