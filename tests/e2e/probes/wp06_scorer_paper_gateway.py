"""Gọi gateway thật bằng artifact chẩn đoán; fixture không chứng minh quyền sản phẩm."""

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


async def main() -> dict[str, bool]:
    workspace_id = UUID(os.environ["WP06_WORKSPACE_ID"])
    attempt_id = UUID(os.environ["WP06_ATTEMPT_ID"])
    engine = create_database_engine(
        database_url_from_secret(
            username=os.environ["TRADING_BOT_DATABASE_USER"],
            password_file=Path(os.environ["TRADING_BOT_DATABASE_PASSWORD_FILE"]),
            host=os.environ.get("TRADING_BOT_DATABASE_HOST", "postgres"),
            database=os.environ.get("TRADING_BOT_DATABASE_NAME", "crypidian"),
        )
    )
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
            assert artifact.state == "abstain", "Fixture chỉ là artifact chẩn đoán, chưa eligible"

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
                if "WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED" not in str(error.orig):
                    raise AssertionError(
                        "Gateway thật phải chặn canonical source thiếu PAPER_DEMO "
                        "trước lookup report; không chấp nhận denial từ gate khác: "
                        + str(error.orig)
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
    print(json.dumps(asyncio.run(main()), sort_keys=True))
