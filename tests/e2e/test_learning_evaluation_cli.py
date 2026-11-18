"""E2E CLI đánh giá TEST (AI-5, CRY-236): service learning-evaluator chạy đúng module/role.

Dựng PostgreSQL Compose riêng, migrate head, rồi chạy CLI trong container ``learning-evaluator``
(login ``crypidian_learning_evaluator``, secret Compose). Attempt không tồn tại phải trả lỗi
nghiệp vụ rõ ràng (mã 2), chứng minh: module tồn tại trong image, settings/role khớp, kết nối DB
và RLS đọc ``learning_scorer_artifact`` hoạt động. Hành trình TEST đầy đủ nằm ở probe WP06.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.docker_image import build_image_reference, ensure_image_available

ROOT = Path(__file__).parents[2]


def _compose(
    project_name: str,
    environment: dict[str, str],
    *args: str,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "paper",
            "--profile",
            "learning",
            "--project-name",
            project_name,
            "-f",
            "compose.yaml",
            *args,
        ],
        check=False,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if check:
        assert result.returncode == 0, (args, result.stdout[-5000:], result.stderr[-5000:])
    return result


def test_learning_evaluator_cli_runs_under_evaluator_role() -> None:
    subprocess.run(
        [sys.executable, "scripts/prepare_local_secrets.py"],
        check=True,
        capture_output=True,
        cwd=ROOT,
        text=True,
    )
    project_name = f"crypidian-eval-cli-e2e-{uuid.uuid4().hex[:10]}"
    image_reference = build_image_reference(ROOT)
    environment = os.environ | {"CRYPIDIAN_APP_IMAGE": image_reference}
    ensure_image_available(ROOT, image_reference)
    try:
        _compose(
            project_name,
            environment,
            *("up", "--detach", "--wait", "--wait-timeout", "180", "postgres"),
        )
        _compose(project_name, environment, "run", "--rm", "--no-deps", "db-provisioner")
        _compose(
            project_name,
            environment,
            *("run", "--rm", "--no-deps", "migrator", "alembic", "upgrade", "head"),
        )
        cli = (
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "learning-evaluator",
            "python",
            "-m",
            "trading_bot.cli.learning_evaluation",
        )
        missing = _compose(
            project_name,
            environment,
            *cli,
            "run",
            "--workspace-id",
            str(uuid.uuid4()),
            "--attempt-id",
            str(uuid.uuid4()),
            "--base-artifact-stdin",
            check=False,
            stdin='{"placeholder":true}\n',
        )
        assert missing.returncode == 2, (missing.stdout, missing.stderr)
        assert "Attempt chưa có scorer artifact." in missing.stderr, missing.stderr
        assert "lỗi nội bộ" not in missing.stderr

        empty = _compose(
            project_name,
            environment,
            *cli,
            "run",
            "--workspace-id",
            str(uuid.uuid4()),
            "--attempt-id",
            str(uuid.uuid4()),
            "--base-artifact-stdin",
            check=False,
            stdin="",
        )
        assert empty.returncode == 2
        assert "Stdin không có artifact gốc." in empty.stderr, empty.stderr

        # AI-5: công thức bundle-set hash SQL trùng byte với Python (N=1 = chính hash; N>1 =
        # sha256 JSON compact danh sách đã sắp, không khoảng trắng; thứ tự đầu vào không đổi).
        from trading_bot.adapters.database.replay_training_rights import replay_bundle_set_hash

        hashes = ["f" * 64, "0" * 64, "a" * 64]
        for sample in ([hashes[0]], hashes[:2], list(reversed(hashes))):
            literal = ",".join(f"'{value}'" for value in sample)
            query = "SELECT public.wp06_replay_bundle_set_hash(ARRAY[" + literal + "]::text[])"
            sql = _compose(
                project_name,
                environment,
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                f'psql -U "$POSTGRES_USER" -d crypidian -At -c "{query}"',
            )
            assert sql.stdout.strip() == replay_bundle_set_hash(sample), (sample, sql.stdout)
        assert replay_bundle_set_hash([hashes[0]]) == hashes[0]
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "paper",
                "--profile",
                "learning",
                "--project-name",
                project_name,
                "-f",
                "compose.yaml",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
