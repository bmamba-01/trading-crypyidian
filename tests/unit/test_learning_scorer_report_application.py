from __future__ import annotations

from uuid import uuid4

import pytest

from trading_bot.application import learning_scorer_report_read_model as module


@pytest.mark.asyncio
async def test_report_facade_loads_exact_id_inside_workspace_unit_of_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    report_id = uuid4()
    expected = object()
    traces: list[tuple[str, object, object]] = []

    class Unit:
        def __init__(self, sessions, *, workspace_id):
            assert sessions is session_factory
            assert workspace_id is workspace_id_expected
            self.session = object()

        async def __aenter__(self):
            traces.append(("enter", workspace_id, report_id))
            return self

        async def __aexit__(self, *_args):
            traces.append(("exit", workspace_id, report_id))

    class Repository:
        def __init__(self, session):
            assert session is not None

        async def get_by_id(self, *, workspace_id, report_id):
            assert workspace_id is workspace_id_expected
            assert report_id is report_id_expected
            traces.append(("read", workspace_id, report_id))
            return expected

    session_factory = object()
    workspace_id_expected = workspace_id
    report_id_expected = report_id
    monkeypatch.setattr(module, "UnitOfWork", Unit)
    monkeypatch.setattr(module, "LearningScorerReportReadRepository", Repository)

    result = await module.ScorerReportReadModel(session_factory).load_report_summary(
        workspace_id,
        report_id,
    )

    assert result is expected
    assert [item[0] for item in traces] == ["enter", "read", "exit"]


@pytest.mark.asyncio
async def test_report_facade_rejects_non_uuid_ids_before_opening_session() -> None:
    with pytest.raises(ValueError, match="UUID"):
        await module.ScorerReportReadModel(object()).load_report_summary(uuid4(), "report-id")  # type: ignore[arg-type]
