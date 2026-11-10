from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_bot.cli import learning_trainer


def test_cli_reports_only_unexpected_exception_type(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    async def fail_work_once(*_args, **_kwargs) -> int:
        raise RuntimeError("sensitive database detail")

    monkeypatch.setattr(learning_trainer, "AppSettings", lambda: SimpleNamespace())
    monkeypatch.setattr(learning_trainer, "_work_once", fail_work_once)

    result = learning_trainer.main(
        ["work-once", "--workspace-id", str(uuid4()), "--worker-id", "worker-test"]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "RuntimeError" in captured.err
    assert "sensitive database detail" not in captured.err
