"""Gọi gateway thật bằng artifact chẩn đoán; fixture không chứng minh quyền sản phẩm.

Từ gate TRAINING exact-scope (b7d9f1a3c5e8) không còn đường sản phẩm nào đưa artifact đã
train tới `WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED`: manifest source-item v1–v3 bị chặn
trước khi train, còn manifest replay v4/v5 bị `wp06_scorer_manifest_source_refs` (chỉ nhận
v1–v3) từ chối bằng `WP06_SCORER_RIGHTS_MANIFEST_MISSING`. Probe nhận mã từ chối kỳ vọng
qua `WP06_EXPECTED_DENIAL` và khẳng định không ledger deployment/score event nào đổi.
Phase `ledger` (chạy bằng migrator) kiểm workspace chưa có artifact/deployment/score event.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading_bot.adapters.database.engine import (
    create_database_engine,
    create_session_factory,
    database_url_from_secret,
)
from trading_bot.application.outbox import UnitOfWork

# Pointer DISABLED có thể có sẵn cho workspace; chỉ pointer SHADOW/PAPER_GATE là deployment.
LEDGER_QUERIES = {
    "learning_scorer_artifact": "public.learning_scorer_artifact WHERE workspace_id=:workspace",
    "learning_score_event": "public.learning_score_event WHERE workspace_id=:workspace",
    "learning_scoring_deployment_event": (
        "public.learning_scoring_deployment_event WHERE workspace_id=:workspace"
    ),
    "enabled_deployment_pointer": (
        "public.learning_scoring_deployment_pointer "
        "WHERE workspace_id=:workspace AND mode <> 'DISABLED'"
    ),
}


def _engine():
    return create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )


async def ledger_counts() -> dict[str, int]:
    """Đếm hàng ledger scorer của workspace (migrator, đọc toàn bộ dưới RLS workspace)."""
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    engine = _engine()
    sessions = create_session_factory(engine)
    try:
        async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
            return {
                name: await unit.session.scalar(
                    text(f"SELECT count(*) FROM {query}"), {"workspace": workspace_id}
                )
                for name, query in LEDGER_QUERIES.items()
            }
    finally:
        await engine.dispose()


async def main() -> dict[str, bool]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    expected = os.environ.get("WP06_EXPECTED_DENIAL", "WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED")
    engine = _engine()
    sessions = create_session_factory(engine)
    try:
        async with UnitOfWork(sessions, workspace_id=workspace_id) as unit:
            artifact = (
                await unit.session.execute(
                    text(
                        "SELECT id,state FROM public.learning_scorer_artifact "
                        "WHERE workspace_id=:workspace AND attempt_id=:attempt"
                    ),
                    {"workspace": workspace_id, "attempt": attempt_id},
                )
            ).one()
            # Artifact replay v5 có thể `ready_for_evaluation`; gateway nhận report id ngẫu
            # nhiên (không có report TEST), nên vẫn chỉ là artifact chẩn đoán, chưa eligible.
            assert artifact.state in {"abstain", "ready_for_evaluation"}, artifact.state

            async def snapshot():
                pointer = (
                    await unit.session.execute(
                        text(
                            "SELECT event_id,revision,mode,active_pointer_version "
                            "FROM public.learning_scoring_deployment_pointer "
                            "WHERE workspace_id=:workspace"
                        ),
                        {"workspace": workspace_id},
                    )
                ).all()
                count = await unit.session.scalar(
                    text(
                        "SELECT count(*) FROM public.learning_scoring_deployment_event "
                        "WHERE workspace_id=:workspace"
                    ),
                    {"workspace": workspace_id},
                )
                return pointer, count

            before = await snapshot()
            try:
                async with unit.session.begin_nested():
                    await unit.session.execute(
                        text(
                            "SELECT * FROM public.wp06_verify_scorer_deployment_rights("
                            ":workspace,:artifact,:report)"
                        ),
                        {"workspace": workspace_id, "artifact": artifact.id, "report": uuid4()},
                    )
            except DBAPIError as error:
                if expected not in str(error.orig):
                    raise AssertionError(
                        f"Gateway thật phải chặn bằng {expected} trước lookup report; "
                        "không chấp nhận denial từ gate khác: " + str(error.orig)
                    ) from error
            else:
                raise AssertionError("Gateway không được cấp eligibility cho fixture chẩn đoán")
            assert await snapshot() == before, "Denial không được đổi pointer hoặc event ledger"
        return {
            "real_gateway_paper_rights_denied": True,
            "pointer_and_event_ledger_unchanged": True,
            "diagnostic_artifact_only": True,
            "positive_rights_evidence": False,
        }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    phase = os.environ.get("WP06_GATEWAY_PHASE", "gateway")
    print(json.dumps(asyncio.run(ledger_counts() if phase == "ledger" else main()), sort_keys=True))
