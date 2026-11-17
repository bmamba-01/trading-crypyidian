"""Entrypoint ghi proof cơ hội tập TEST cho một attempt (AI-5, CRY-236).

Bước này trước đây chỉ probe E2E làm được. Chạy bằng login ``crypidian_candidate_builder``
(secret Compose cố định), transaction READ COMMITTED như repository yêu cầu. Proof chỉ chứa
metadata/hash (không payload TEST); ``evaluation_run_id`` mới được sinh một lần và tái dùng
khi chạy lại (proof đã có → trả bản đã lưu, không mở scope mới).

Ví dụ (trong container candidate-proof-builder):
    python -m trading_bot.cli.learning_scorer_test_proof --workspace-id W --attempt-id A
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from uuid import UUID, uuid4

from sqlalchemy import text

from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
    LearningScorerTestOpportunityProofConflict,
    LearningScorerTestOpportunityProofRepository,
)
from trading_bot.cli.candidate_proof import create_builder_pool


def parse_request(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ghi proof cơ hội TEST cho một attempt.")
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--attempt-id", type=UUID, required=True)
    return parser.parse_args(arguments)


async def run(workspace_id: UUID, attempt_id: UUID) -> dict[str, object]:
    engine, sessions = create_builder_pool()
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE")
            )
            await session.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace_id)},
            )
            session.info["workspace_id"] = workspace_id
            repository = LearningScorerTestOpportunityProofRepository(session)
            existing = await repository.get_for_attempt(
                workspace_id=workspace_id, attempt_id=attempt_id
            )
            evaluation_run_id = existing.evaluation_run_id if existing is not None else uuid4()
            result = await repository.build_and_append(
                workspace_id=workspace_id,
                evaluation_run_id=evaluation_run_id,
                attempt_id=attempt_id,
            )
            proof = result.proof
            return {
                "record_id": str(result.record.id),
                "duplicate": result.is_duplicate,
                "evaluation_run_id": str(proof.evaluation_run_id),
                "proof_hash": proof.content_hash,
                "bundle_hash": proof.bundle_hash,
                "expected_opportunity_count": proof.expected_opportunity_count,
                "candidate_count": proof.candidate_count,
                "no_candidate_count": proof.no_candidate_count,
                "source_gap_count": proof.source_gap_count,
                "membership_mismatch_count": proof.membership_mismatch_count,
                "unlabeled_candidate_count": proof.unlabeled_candidate_count,
                "completeness_verified": proof.completeness_verified,
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    request = parse_request(argv)
    try:
        print(
            json.dumps(
                asyncio.run(run(request.workspace_id, request.attempt_id)),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except LearningScorerTestOpportunityProofConflict as error:
        cause = error.__cause__
        detail = f" ({type(cause).__name__}: {str(cause)[:200]})" if cause else ""
        print(f"Proof TEST bị từ chối: {error}{detail}", file=sys.stderr)
        return 1
    except (ValueError, PermissionError, LookupError) as error:
        print(f"Không thể ghi proof TEST: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Không thể ghi proof TEST do lỗi nội bộ ({type(error).__name__}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
