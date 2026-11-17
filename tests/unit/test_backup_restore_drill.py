"""Unit test cho quy trình Backup, Restore Drill cô lập và đo RPO/RTO (OPS07C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.observability.backup_drill import (
    BackupArtifactInput,
    RestoreDrillPolicy,
    RestoreVerificationError,
    create_backup_manifest,
    execute_isolated_restore_drill,
)


def test_backup_manifest_and_isolated_restore_drill_measures_rpo_rto(tmp_path: Path) -> None:
    """Tạo manifest có SHA-256, chạy restore drill vào thư mục cô lập và đo RPO/RTO đạt ngưỡng."""
    pg_dump = tmp_path / "pg_backup.sql"
    pg_dump.write_text("-- Crypidian ledger snapshot\nSELECT 1;\n", encoding="utf-8")
    obj_manifest = tmp_path / "artifacts_manifest.json"
    obj_manifest.write_text('{"model_sha256": "abc123"}', encoding="utf-8")

    created_at = datetime(2026, 11, 14, 6, 0, tzinfo=UTC)
    incident_at = created_at + timedelta(minutes=8)

    manifest = create_backup_manifest(
        backup_id="bkp-20261114-0600",
        created_at=created_at,
        operator_id="owner@crypidian.io",
        artifacts=[
            BackupArtifactInput(name="postgres_dump", path=pg_dump),
            BackupArtifactInput(name="object_store_manifest", path=obj_manifest),
        ],
        secret_canaries=["CANARY_SECRET_999"],
    )
    assert len(manifest.artifacts) == 2
    assert manifest.artifacts[0].sha256_hex

    restore_target = tmp_path / "isolated_restore"
    report = execute_isolated_restore_drill(
        manifest=manifest,
        artifact_sources={"postgres_dump": pg_dump, "object_store_manifest": obj_manifest},
        restore_dir=restore_target,
        target_environment="isolated-drill",
        incident_at=incident_at,
        policy=RestoreDrillPolicy(max_rpo_seconds=900, max_rto_seconds=300),
    )
    assert report.verified is True
    assert report.rpo_seconds == 480
    assert report.rto_seconds >= 0.0
    assert report.rpo_within_sla is True
    assert report.rto_within_sla is True
    assert (restore_target / "pg_backup.sql").exists()


def test_backup_rejects_plain_secret_and_restore_rejects_corruption_or_prod(tmp_path: Path) -> None:
    """Từ chối backup lộ plain secret; từ chối restore đè production hoặc sai checksum."""
    leaky_file = tmp_path / "leaky.json"
    leaky_file.write_text('{"binance_api_secret": "CANARY_LEAK_001"}', encoding="utf-8")
    with pytest.raises(RestoreVerificationError, match="bí mật"):
        create_backup_manifest(
            backup_id="bkp-leaky",
            created_at=datetime(2026, 11, 14, 6, 0, tzinfo=UTC),
            operator_id="owner@crypidian.io",
            artifacts=[BackupArtifactInput(name="leaky", path=leaky_file)],
            secret_canaries=["CANARY_LEAK_001"],
        )

    clean_file = tmp_path / "clean.sql"
    clean_file.write_text("SELECT 42;", encoding="utf-8")
    manifest = create_backup_manifest(
        backup_id="bkp-clean",
        created_at=datetime(2026, 11, 14, 6, 0, tzinfo=UTC),
        operator_id="owner@crypidian.io",
        artifacts=[BackupArtifactInput(name="postgres_dump", path=clean_file)],
    )

    # Từ chối môi trường production/live
    with pytest.raises(RestoreVerificationError, match="cô lập"):
        execute_isolated_restore_drill(
            manifest=manifest,
            artifact_sources={"postgres_dump": clean_file},
            restore_dir=tmp_path / "prod_dir",
            target_environment="production",
            incident_at=datetime(2026, 11, 14, 6, 5, tzinfo=UTC),
        )

    # Làm hỏng file nguồn sau khi tạo manifest -> phải phát hiện sai checksum
    clean_file.write_text("SELECT 999; -- CORRUPTED", encoding="utf-8")
    with pytest.raises(RestoreVerificationError, match="checksum"):
        execute_isolated_restore_drill(
            manifest=manifest,
            artifact_sources={"postgres_dump": clean_file},
            restore_dir=tmp_path / "isolated_dir",
            target_environment="isolated-drill",
            incident_at=datetime(2026, 11, 14, 6, 5, tzinfo=UTC),
        )
