"""Script diễn tập backup mã hóa PostgreSQL và restore vào DB cô lập có đo RPO/RTO (OPS07C)."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.observability.backup_drill import (
    BackupArtifactInput,
    RestoreDrillPolicy,
    RestoreVerificationError,
    check_disk_space_available,
    create_backup_manifest,
    execute_isolated_restore_drill,
    format_restore_drill_report_json,
    load_backup_manifest,
    serialize_backup_manifest,
    validate_isolated_target,
)


def _run_compose_exec(
    *,
    project_name: str,
    service: str,
    command: list[str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    full_cmd = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "exec",
        "-T",
        service,
        *command,
    ]
    return subprocess.run(full_cmd, input=input_bytes, capture_output=True, check=False)


def run_encrypted_backup(
    *,
    source_project: str,
    postgres_service: str,
    db_user: str,
    db_name: str,
    output_dir: Path,
    backup_id: str,
    operator_id: str,
    passphrase: str,
    object_manifest_path: Path | None = None,
    secret_canaries: tuple[str, ...] = (),
    min_free_bytes: int = 100 * 1024 * 1024,
) -> Path:
    """Dump PostgreSQL từ Compose project, kiểm tra canary, mã hóa AES-256-GCM và tạo manifest."""
    check_disk_space_available(output_dir, min_free_bytes=min_free_bytes)
    output_dir.mkdir(parents=True, exist_ok=True)
    plain_dump = output_dir / f"{backup_id}.pgdump.sql"

    dump_res = _run_compose_exec(
        project_name=source_project,
        service=postgres_service,
        command=["pg_dump", "-U", db_user, "-d", db_name, "--no-owner", "--no-privileges"],
    )
    if dump_res.returncode != 0:
        raise RestoreVerificationError(
            f"pg_dump thất bại trên project '{source_project}': "
            f"{dump_res.stderr.decode('utf-8', errors='replace')}"
        )
    plain_dump.write_bytes(dump_res.stdout)

    artifacts = [BackupArtifactInput(name="postgres_dump", path=plain_dump)]
    if object_manifest_path is not None:
        artifacts.append(
            BackupArtifactInput(name="object_store_manifest", path=object_manifest_path)
        )

    try:
        manifest = create_backup_manifest(
            backup_id=backup_id,
            created_at=datetime.now(UTC),
            operator_id=operator_id,
            artifacts=artifacts,
            secret_canaries=secret_canaries,
            encrypted=True,
            passphrase=passphrase,
            output_dir=output_dir,
        )
    finally:
        # Xóa bản dump plaintext ngay sau khi mã hóa xong để không tồn dư dữ liệu chưa mã hóa.
        plain_dump.unlink(missing_ok=True)

    manifest_path = output_dir / f"{backup_id}.manifest.json"
    manifest_path.write_text(serialize_backup_manifest(manifest), encoding="utf-8")
    return manifest_path


def run_isolated_postgres_restore_drill(
    *,
    manifest_path: Path,
    backup_dir: Path,
    restore_dir: Path,
    target_environment: str,
    target_project: str,
    postgres_service: str,
    db_user: str,
    isolated_db_name: str,
    passphrase: str,
    source_project: str | None = None,
    max_rpo_seconds: int = 900,
    max_rto_seconds: int = 300,
    min_free_bytes: int = 100 * 1024 * 1024,
) -> Path:
    """Giải mã backup, khôi phục vào DB cô lập, đối chiếu bảng và xuất báo cáo RPO/RTO JSON."""
    isolated_target_proj = (
        target_project if source_project and source_project != target_project else None
    )
    validate_isolated_target(
        target_environment,
        source_project=source_project,
        target_project=isolated_target_proj,
    )
    if isolated_db_name.strip().lower() in {"crypidian", "postgres", "production", "prod", "live"}:
        raise RestoreVerificationError(
            f"Từ chối khôi phục vào database chính '{isolated_db_name}'; "
            "chỉ cho phép database cô lập."
        )

    manifest = load_backup_manifest(manifest_path)
    artifact_sources = {rec.name: backup_dir / rec.filename for rec in manifest.artifacts}

    started = time.monotonic()
    drill_report = execute_isolated_restore_drill(
        manifest=manifest,
        artifact_sources=artifact_sources,
        restore_dir=restore_dir,
        target_environment=target_environment,
        incident_at=datetime.now(UTC),
        policy=RestoreDrillPolicy(
            max_rpo_seconds=max_rpo_seconds,
            max_rto_seconds=max_rto_seconds,
        ),
        passphrase=passphrase,
        min_free_bytes=min_free_bytes,
    )

    dump_record = next((a for a in manifest.artifacts if a.name == "postgres_dump"), None)
    if dump_record is None:
        raise RestoreVerificationError("Manifest thiếu artifact 'postgres_dump'.")
    restored_sql = restore_dir / dump_record.filename.removesuffix(".enc")

    # Tạo lại DB cô lập sạch hoàn toàn trước khi nạp dump
    recreate_sql = (
        f"DROP DATABASE IF EXISTS {isolated_db_name};\nCREATE DATABASE {isolated_db_name};\n"
    ).encode()
    prep_res = _run_compose_exec(
        project_name=target_project,
        service=postgres_service,
        command=["psql", "-v", "ON_ERROR_STOP=1", "-U", db_user, "-d", "postgres"],
        input_bytes=recreate_sql,
    )
    if prep_res.returncode != 0:
        raise RestoreVerificationError(
            f"Không thể tạo database cô lập '{isolated_db_name}': "
            f"{prep_res.stderr.decode('utf-8', errors='replace')}"
        )

    restore_res = _run_compose_exec(
        project_name=target_project,
        service=postgres_service,
        command=["psql", "-v", "ON_ERROR_STOP=1", "-U", db_user, "-d", isolated_db_name],
        input_bytes=restored_sql.read_bytes(),
    )
    if restore_res.returncode != 0:
        raise RestoreVerificationError(
            f"psql restore thất bại trên DB cô lập '{isolated_db_name}': "
            f"{restore_res.stderr.decode('utf-8', errors='replace')}"
        )

    verify_query = b"SELECT version_num FROM alembic_version LIMIT 1;"
    verify_res = _run_compose_exec(
        project_name=target_project,
        service=postgres_service,
        command=["psql", "-t", "-A", "-U", db_user, "-d", isolated_db_name],
        input_bytes=verify_query,
    )
    if verify_res.returncode != 0:
        raise RestoreVerificationError(
            f"Kiểm tra alembic_version sau restore thất bại: "
            f"{verify_res.stderr.decode('utf-8', errors='replace')}"
        )
    alembic_rev = verify_res.stdout.decode("utf-8", errors="replace").strip()
    total_rto = round(max(0.0, time.monotonic() - started), 4)
    final_report = replace(
        drill_report,
        rto_seconds=total_rto,
        rto_within_sla=total_rto <= max_rto_seconds,
        db_verification={
            "isolated_database": isolated_db_name,
            "alembic_version": alembic_rev,
        },
        detail_vi=(
            f"Diễn tập backup mã hóa & restore PostgreSQL cô lập '{isolated_db_name}' "
            f"thành công (alembic={alembic_rev}); "
            f"RPO={drill_report.rpo_seconds}s, RTO={total_rto}s."
        ),
    )

    report_path = restore_dir / f"{manifest.backup_id}.restore-report.json"
    report_path.write_text(format_restore_drill_report_json(final_report), encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diễn tập backup mã hóa PostgreSQL và restore cô lập đo RPO/RTO (OPS07C)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bkp = sub.add_parser("backup", help="Tạo backup mã hóa AES-256-GCM và manifest.")
    bkp.add_argument("--source-project", required=True)
    bkp.add_argument("--postgres-service", default="postgres")
    bkp.add_argument("--db-user", default="crypidian_admin")
    bkp.add_argument("--db-name", default="crypidian")
    bkp.add_argument("--output-dir", type=Path, required=True)
    bkp.add_argument("--backup-id", required=True)
    bkp.add_argument("--operator-id", default="ops-drill")
    bkp.add_argument("--passphrase-env", default="CRYPIDIAN_BACKUP_PASSPHRASE")

    rst = sub.add_parser("restore-drill", help="Restore vào DB cô lập và đo RPO/RTO.")
    rst.add_argument("--manifest", type=Path, required=True)
    rst.add_argument("--backup-dir", type=Path, required=True)
    rst.add_argument("--restore-dir", type=Path, required=True)
    rst.add_argument("--target-environment", default="isolated-drill")
    rst.add_argument("--target-project", required=True)
    rst.add_argument("--postgres-service", default="postgres")
    rst.add_argument("--db-user", default="crypidian_admin")
    rst.add_argument("--isolated-db-name", default="crypidian_restore_drill")
    rst.add_argument("--passphrase-env", default="CRYPIDIAN_BACKUP_PASSPHRASE")
    rst.add_argument("--max-rpo-seconds", type=int, default=900)
    rst.add_argument("--max-rto-seconds", type=int, default=300)

    args = parser.parse_args()
    passphrase = os.environ.get(args.passphrase_env, "")
    if not passphrase:
        raise SystemExit(f"Thiếu biến môi trường '{args.passphrase_env}' chứa passphrase.")

    if args.command == "backup":
        manifest_path = run_encrypted_backup(
            source_project=args.source_project,
            postgres_service=args.postgres_service,
            db_user=args.db_user,
            db_name=args.db_name,
            output_dir=args.output_dir,
            backup_id=args.backup_id,
            operator_id=args.operator_id,
            passphrase=passphrase,
        )
        print(f"BACKUP_MANIFEST={manifest_path}")
    elif args.command == "restore-drill":
        report_path = run_isolated_postgres_restore_drill(
            manifest_path=args.manifest,
            backup_dir=args.backup_dir,
            restore_dir=args.restore_dir,
            target_environment=args.target_environment,
            target_project=args.target_project,
            postgres_service=args.postgres_service,
            db_user=args.db_user,
            isolated_db_name=args.isolated_db_name,
            passphrase=passphrase,
            max_rpo_seconds=args.max_rpo_seconds,
            max_rto_seconds=args.max_rto_seconds,
        )
        print(f"RESTORE_REPORT={report_path}")


if __name__ == "__main__":
    main()
