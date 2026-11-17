"""Chạy proof TEST + đánh giá scorer TEST cho một attempt trên stack local (AI-5, CRY-236).

Ba bước, mỗi bước đúng một role DB và container riêng:
1. ``admin-web`` (``crypidian_admin_web``): xuất canonical artifact gốc v1 của attempt
   (evaluator không có quyền đọc bảng artifact gốc). Không ghi gì.
2. ``candidate-proof-builder`` (``crypidian_candidate_builder``): ghi proof cơ hội TEST.
3. ``learning-evaluator`` (``crypidian_learning_evaluator``): đánh giá TEST một lần; artifact
   gốc đi qua stdin và phải có hash trùng ``base_artifact_hash`` scorer đã ghim.

Chạy lại an toàn: proof và report đã lưu được trả lại (không mở lại TEST).

Ví dụ:
    uv run python scripts/ai5_learning_test_report.py --attempt-id A
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(globals().get("__file__", "/app/x/x")).resolve().parents[1]
DEFAULT_WORKSPACE = "c486b52b-912a-446f-aeed-99685c06cad6"
EXPORT_CODE = """
import asyncio, json, os, sys
from pathlib import Path
from uuid import UUID
from sqlalchemy import select
from trading_bot.adapters.database.engine import (
    create_database_engine, create_session_factory, database_url_from_secret)
from trading_bot.adapters.database.models import LearningTrainingArtifactRecord
from trading_bot.adapters.database.repositories.learning_training_artifacts import (
    LearningTrainingArtifactRepository)
from trading_bot.application.outbox import UnitOfWork

async def main():
    workspace, attempt = UUID(sys.argv[1]), UUID(sys.argv[2])
    engine = create_database_engine(database_url_from_secret(
        username=os.environ["TRADING_BOT_DATABASE_USER"],
        password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
        host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian")))
    try:
        async with UnitOfWork(create_session_factory(engine), workspace_id=workspace) as unit:
            row = await unit.session.scalar(select(LearningTrainingArtifactRecord).where(
                LearningTrainingArtifactRecord.workspace_id == workspace,
                LearningTrainingArtifactRecord.attempt_id == attempt))
            if row is None:
                raise SystemExit("Attempt chưa có artifact gốc.")
            LearningTrainingArtifactRepository.validate_record(row)
            sys.stdout.write(row.canonical_artifact)
    finally:
        await engine.dispose()

asyncio.run(main())
"""


def _compose(project: str, *args: str, stdin: str | None = None, timeout: float) -> Any:
    command = [
        "docker", "compose", "-p", project, "-f", "compose.yaml",
        "--profile", "paper", "--profile", "learning", "--profile", "learning-proof",
        "run", "--rm", "--no-deps", "-T", *args,
    ]  # fmt: skip
    return subprocess.run(
        command, cwd=ROOT, input=stdin, capture_output=True, text=True, timeout=timeout
    )


def _last_json(output: str) -> dict[str, Any]:
    for line in reversed(output.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("Không có dòng JSON kết quả.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--project", default="crypidian-review")
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args(argv)
    if "live" in args.project.lower() or "prod" in args.project.lower():
        raise SystemExit("Script chỉ dành cho stack local PAPER.")

    exported = _compose(
        args.project, "admin-web", "python", "-c", EXPORT_CODE, args.workspace_id,
        args.attempt_id, timeout=args.timeout,
    )  # fmt: skip
    if exported.returncode != 0 or not exported.stdout.startswith("{"):
        print(exported.stderr[-2000:], file=sys.stderr)
        raise SystemExit("Không xuất được artifact gốc.")
    base_artifact = exported.stdout

    proof = _compose(
        args.project, "candidate-proof-builder", "python", "-m",
        "trading_bot.cli.learning_scorer_test_proof", "--workspace-id", args.workspace_id,
        "--attempt-id", args.attempt_id, timeout=args.timeout,
    )  # fmt: skip
    if proof.returncode != 0:
        print(proof.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Ghi proof TEST thất bại (mã {proof.returncode}).")
    proof_result = _last_json(proof.stdout)

    evaluated = _compose(
        args.project, "learning-evaluator", "python", "-m",
        "trading_bot.cli.learning_evaluation", "run", "--workspace-id", args.workspace_id,
        "--attempt-id", args.attempt_id, "--base-artifact-stdin",
        stdin=base_artifact, timeout=args.timeout,
    )  # fmt: skip
    if evaluated.returncode != 0:
        print(evaluated.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"Đánh giá TEST thất bại (mã {evaluated.returncode}).")
    print(
        json.dumps(
            {"proof": proof_result, "report": _last_json(evaluated.stdout)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
