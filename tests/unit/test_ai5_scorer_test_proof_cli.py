"""AI-5 (CRY-236): CLI ghi proof TEST chạy bằng login candidate builder."""

from __future__ import annotations

from uuid import uuid4

import pytest

from trading_bot.adapters.database.repositories.learning_scorer_test_opportunity_proofs import (
    LearningScorerTestOpportunityProofConflict,
)
from trading_bot.cli import learning_scorer_test_proof as cli


def test_parse_request_requires_workspace_and_attempt() -> None:
    workspace, attempt = uuid4(), uuid4()
    request = cli.parse_request(["--workspace-id", str(workspace), "--attempt-id", str(attempt)])
    assert (request.workspace_id, request.attempt_id) == (workspace, attempt)
    with pytest.raises(SystemExit):
        cli.parse_request(["--workspace-id", str(workspace)])


def test_conflict_reports_cause_and_exit_code(monkeypatch, capsys) -> None:
    async def fail(*_args):
        try:
            raise ValueError("TEST outcomes không thuộc cùng một BASE trial/run.")
        except ValueError as cause:
            raise LearningScorerTestOpportunityProofConflict("Không dựng được inventory") from cause

    monkeypatch.setattr(cli, "run", fail)
    code = cli.main(["--workspace-id", str(uuid4()), "--attempt-id", str(uuid4())])
    err = capsys.readouterr().err
    assert code == 1
    assert "ValueError" in err
    assert "BASE trial" in err


def test_internal_error_hides_detail(monkeypatch, capsys) -> None:
    async def boom(*_args):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli, "run", boom)
    code = cli.main(["--workspace-id", str(uuid4()), "--attempt-id", str(uuid4())])
    err = capsys.readouterr().err
    assert code == 2
    assert "secret detail" not in err
