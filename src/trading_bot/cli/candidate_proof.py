"""Entrypoint một lần cho principal tin cậy ghi proof; không nhận arbitrary payload."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.application.candidate_universe_proof import CandidateUniverseProofService


class ProofUniverse(StrEnum):
    """Chỉ hai thuật toán proof cố định, không nhận query tự do."""

    CLOSED_TRADES = "closed-trades-v1"
    DECISION_OPPORTUNITIES = "decision-opportunities-v1"


PASSWORD_FILE = Path("/run/secrets/database_candidate_builder_password")


def create_builder_pool() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Pool writer riêng; thiếu/rỗng credential thì dừng, không fallback trainer/admin."""
    engine = create_database_engine(
        database_url_from_secret(
            username="crypidian_candidate_builder",
            password_file=PASSWORD_FILE,
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
    return engine, create_session_factory(engine)


def parse_request(arguments: list[str] | None = None) -> argparse.Namespace:
    """Chỉ chấp nhận hai định danh scope; không có tham số credential/proof/counts."""
    parser = argparse.ArgumentParser(
        description="Dựng proof mọi trade đóng của một BASE trial đã lưu."
    )
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--trial-record-id", type=UUID, required=True)
    parser.add_argument(
        "--universe",
        type=ProofUniverse,
        choices=tuple(ProofUniverse),
        default=ProofUniverse.CLOSED_TRADES,
    )
    return parser.parse_args(arguments)


async def run(
    workspace_id: UUID, trial_record_id: UUID, universe: ProofUniverse = ProofUniverse.CLOSED_TRADES
) -> None:
    """Chỉ in metadata đã xác minh, đóng engine kể cả khi gate/transaction thất bại."""
    engine, sessions = create_builder_pool()
    try:
        if not isinstance(universe, ProofUniverse):
            raise ValueError("Universe phải thuộc enum cố định.")
        service = CandidateUniverseProofService(sessions)
        builder = (
            service.build_decision_opportunity
            if universe is ProofUniverse.DECISION_OPPORTUNITIES
            else service.build
        )
        result = await builder(workspace_id=workspace_id, trial_record_id=trial_record_id)
        print(
            json.dumps(
                {
                    "record_id": str(result.record_id),
                    "duplicate": result.duplicate,
                    "proof": json.loads(result.proof.canonical_json),
                },
                ensure_ascii=False,
            )
        )
    finally:
        await engine.dispose()


def main() -> None:
    """Không có scope UUID rõ ràng thì không mở pool hoặc chạy job."""
    request = parse_request()
    asyncio.run(run(request.workspace_id, request.trial_record_id, request.universe))


if __name__ == "__main__":
    main()
