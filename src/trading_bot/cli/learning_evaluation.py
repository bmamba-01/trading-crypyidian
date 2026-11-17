"""Entrypoint đánh giá scorer trên tập TEST một lần cho một attempt (AI-5, CRY-236).

Chạy dưới login DB ``crypidian_learning_evaluator`` (process_role ``learning-evaluator``, PAPER).
Scorer artifact và proof TEST nạp từ DB theo attempt; policy lấy từ biến môi trường
``TRADING_BOT_SCORER_TEST_POLICY_*`` (hash policy ghim vào receipt). Role evaluator không được
đọc bảng artifact gốc v1, nên canonical artifact gốc được truyền qua stdin và **phải** có hash
trùng ``base_artifact_hash`` mà scorer đã ghim trong DB: bytes sai một ký tự cũng bị từ chối.

Payload TEST không bao giờ đi qua argv/stdin: gateway DB mở đúng một lần trong phase hai.
Chạy lại cùng attempt trả report đã lưu (``is_duplicate``) và không mở lại TEST.

Ví dụ (trong container learning-evaluator):
    python -m trading_bot.cli.learning_evaluation run --workspace-id W --attempt-id A \
        --base-artifact-stdin < base_artifact.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.adapters.database.models import LearningScorerArtifactRecord
from trading_bot.adapters.database.repositories.learning_scorer_artifacts import (
    LearningScorerArtifactRepository,
)
from trading_bot.adapters.database.repositories.learning_scorer_runtime_targets import (
    learning_cpu_artifact_from_canonical_json,
    learning_scorer_artifact_from_canonical_json,
)
from trading_bot.adapters.database.repositories.learning_scorer_test_access import (
    LearningScorerTestAccessRepository,
    LearningScorerTestEvaluationReportRepository,
)
from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
    LearningScorerTestOpportunityProofRepository,
)
from trading_bot.application.learning_evaluation_inputs import (
    evaluation_scope_from_proof,
    opportunity_proof_from_canonical,
)
from trading_bot.application.learning_scorer_test_service import (
    ScorerTestEvaluationAccessConflict,
    ScorerTestEvaluationReportConflict,
    ScorerTestEvaluationRequest,
    ScorerTestEvaluationResult,
    ScorerTestEvaluationService,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.config.settings import AppSettings

EVALUATOR_USER = "crypidian_learning_evaluator"
EVALUATOR_PASSWORD_FILE = Path("/run/secrets/database_learning_evaluator_password")
MAX_BASE_ARTIFACT_BYTES = 8 * 1024 * 1024
SUMMARY_FIELDS = (
    "status",
    "evaluation_run_id",
    "attempt_id",
    "scorer_artifact_hash",
    "base_artifact_hash",
    "policy_version",
    "policy_hash",
    "evaluated_at",
    "test_sample_count",
    "coverage",
    "abstain_count",
    "log_loss",
    "brier_score",
    "prior_baseline_log_loss",
    "expected_calibration_error",
    "candidate_count",
    "scored_candidate_count",
    "approved_candidate_count",
    "vetoed_candidate_count",
    "false_veto_rate",
    "candidate_retention",
    "report_hash",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Đánh giá scorer trên TEST một lần.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Đánh giá TEST cho đúng một attempt đã có proof.")
    run.add_argument("--workspace-id", required=True, type=UUID)
    run.add_argument("--attempt-id", required=True, type=UUID)
    run.add_argument(
        "--base-artifact-stdin",
        action="store_true",
        required=True,
        help="Đọc canonical artifact gốc v1 từ stdin (kiểm hash với scorer đã lưu).",
    )
    return parser


def create_evaluator_pool(
    settings: AppSettings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Pool bằng login/secret evaluator cố định; không nhận user hoặc file từ argv."""
    if settings.process_role != "learning-evaluator":
        raise PermissionError("CLI đánh giá chỉ chạy trong process_role=learning-evaluator.")
    if settings.trading_mode != "PAPER":
        raise PermissionError("CLI đánh giá chỉ chạy trong PAPER.")
    if settings.database_user != EVALUATOR_USER:
        raise PermissionError("CLI đánh giá cần login database evaluator riêng.")
    if settings.database_password_file != EVALUATOR_PASSWORD_FILE:
        raise PermissionError("CLI đánh giá cần secret evaluator cố định do Compose mount.")
    engine = create_database_engine(
        database_url_from_secret(
            username=EVALUATOR_USER,
            password_file=EVALUATOR_PASSWORD_FILE,
            host=settings.database_host,
            database=settings.database_name,
        )
    )
    return engine, create_session_factory(engine)


def read_base_artifact(stream: IO[str]) -> str:
    """Đọc canonical artifact gốc; bỏ đúng một newline cuối do shell/heredoc thêm vào."""
    raw = stream.read(MAX_BASE_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_BASE_ARTIFACT_BYTES:
        raise ValueError("Artifact gốc vượt giới hạn kích thước.")
    if raw.endswith("\n"):
        raw = raw[:-1]
    if not raw:
        raise ValueError("Stdin không có artifact gốc.")
    return raw


async def build_request(
    sessions: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    attempt_id: UUID,
    base_canonical: str,
    settings: AppSettings,
    evaluated_at: datetime,
) -> ScorerTestEvaluationRequest:
    """Nạp scorer + proof theo attempt, khớp artifact gốc với hash scorer đã ghim."""
    async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
        scorer_row = await unit.session.scalar(
            select(LearningScorerArtifactRecord).where(
                LearningScorerArtifactRecord.workspace_id == workspace_id,
                LearningScorerArtifactRecord.attempt_id == attempt_id,
            )
        )
        if scorer_row is None:
            raise LookupError("Attempt chưa có scorer artifact.")
        LearningScorerArtifactRepository.validate_record(scorer_row)
        scorer = learning_scorer_artifact_from_canonical_json(
            scorer_row.canonical_artifact, expected_hash=scorer_row.artifact_hash
        )
        proof_row = await LearningScorerTestOpportunityProofRepository(
            unit.session
        ).get_for_attempt(workspace_id=workspace_id, attempt_id=attempt_id)
        if proof_row is None:
            raise LookupError("Attempt chưa có proof TEST; chạy candidate builder trước.")
        proof = opportunity_proof_from_canonical(
            record_id=proof_row.id,
            canonical_proof=proof_row.canonical_proof,
            expected_hash=proof_row.proof_hash,
        )
    if scorer.state != "ready_for_evaluation":
        raise ValueError(f"Scorer ở trạng thái {scorer.state}; không đánh giá TEST.")
    base = learning_cpu_artifact_from_canonical_json(
        base_canonical, expected_hash=scorer.base_artifact_hash
    )
    return ScorerTestEvaluationRequest(
        scope=evaluation_scope_from_proof(proof),
        scorer_artifact=scorer,
        base_artifact=base,
        policy=settings.scorer_test_evaluation_policy(),
        evaluated_at=evaluated_at,
    )


def result_summary(result: ScorerTestEvaluationResult) -> dict[str, Any]:
    """Tóm tắt aggregate (không có sample/feature/label) để in ra stdout."""
    report = json.loads(result.report.canonical_json)
    summary = {name: report.get(name) for name in SUMMARY_FIELDS}
    summary["report_hash"] = result.report.report_hash
    summary["record_id"] = str(result.record_id)
    summary["is_duplicate"] = result.is_duplicate
    summary["filtered_portfolio"] = report.get("filtered_portfolio")
    summary["unfiltered_portfolio"] = report.get("unfiltered_portfolio")
    return summary


async def _run(
    settings: AppSettings, workspace_id: UUID, attempt_id: UUID, base_canonical: str
) -> int:
    engine, sessions = create_evaluator_pool(settings)
    try:
        reports = LearningScorerTestEvaluationReportRepository(sessions)
        request = await build_request(
            sessions,
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            base_canonical=base_canonical,
            settings=settings,
            evaluated_at=datetime.now(UTC),
        )
        # Chạy lại: giữ evaluated_at của report đã lưu để fingerprint trùng → trả duplicate.
        existing = await reports.get_by_run(
            workspace_id=workspace_id, evaluation_run_id=request.scope.evaluation_run_id
        )
        if existing is not None:
            request = replace(request, evaluated_at=existing.report.evaluated_at)
        service = ScorerTestEvaluationService(LearningScorerTestAccessRepository(sessions), reports)
        result = await service.evaluate(request)
        print(json.dumps(result_summary(result), ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None, *, stdin: IO[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        settings = AppSettings()
        if arguments.command == "run":
            base_canonical = read_base_artifact(stdin or sys.stdin)
            return asyncio.run(
                _run(settings, arguments.workspace_id, arguments.attempt_id, base_canonical)
            )
        raise ValueError("CLI command không được hỗ trợ.")
    except (ScorerTestEvaluationAccessConflict, ScorerTestEvaluationReportConflict) as error:
        print(f"Đánh giá TEST bị từ chối: {error}", file=sys.stderr)
        return 1
    except (ValueError, PermissionError, LookupError) as error:
        print(f"Không thể đánh giá TEST: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"Không thể đánh giá TEST do lỗi nội bộ ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
