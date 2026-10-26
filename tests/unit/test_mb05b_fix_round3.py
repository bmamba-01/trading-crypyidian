"""MB-05 Part B fix round 3: ghim một đường `.reserve` qua coordinator; ánh xạ lỗi coordinator."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_paper_runtime_application import _application_with_fakes

from trading_bot.application.paper_runtime_application import PaperRuntimeApplicationError
from trading_bot.domain.risk.reservations import ReservationConflict
from trading_bot.domain.risk.workspace_budget import BudgetConflictError

RUNTIME = (
    Path(__file__).parents[2]
    / "src"
    / "trading_bot"
    / "application"
    / "paper_runtime_application.py"
)
COORDINATOR_FACTORY = "_workspace_risk_coordinator_factory"


def assert_single_coordinator_reserve(source: str) -> None:
    """Đúng một lời gọi `.reserve(` và receiver là `self._workspace_risk_coordinator_factory(...)`.

    `ALLOWED_CALLERS` của test fencing chỉ so tên method; kiểm này chặn một
    `risk_repository.reserve(...)` (đường reserve thứ hai bỏ qua MB-04) lọt vào runtime.
    """
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reserve"
    ]
    assert len(calls) == 1, [ast.unparse(call) for call in calls]
    receiver = calls[0].func.value  # type: ignore[attr-defined]
    assert isinstance(receiver, ast.Call), ast.unparse(receiver)
    factory = receiver.func
    assert isinstance(factory, ast.Attribute), ast.unparse(receiver)
    assert factory.attr == COORDINATOR_FACTORY, ast.unparse(receiver)
    assert isinstance(factory.value, ast.Name), ast.unparse(receiver)
    assert factory.value.id == "self", ast.unparse(receiver)


def test_runtime_has_exactly_one_reserve_call_through_coordinator_factory() -> None:
    assert_single_coordinator_reserve(RUNTIME.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "extra",
    [
        "\nasync def _bypass(risk_repository, decision):\n"
        "    return await risk_repository.reserve(decision, idempotency_key='k')\n",
        "\nasync def _bypass(self, session, request):\n"
        "    return await self._risk_repository_factory(session).reserve(request)\n",
    ],
)
def test_second_reserve_call_variant_is_rejected(extra: str) -> None:
    source = RUNTIME.read_text(encoding="utf-8") + extra
    with pytest.raises(AssertionError):
        assert_single_coordinator_reserve(source)


def test_reserve_through_other_receiver_is_rejected() -> None:
    source = (
        "async def f(self, s, r):\n    return await self._risk_repository_factory(s).reserve(r)\n"
    )
    with pytest.raises(AssertionError):
        assert_single_coordinator_reserve(source)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Root workspace settings không hiệu lực tại as_of."),
        ReservationConflict("synthetic reservation conflict"),
    ],
)
def test_coordinator_policy_errors_map_to_reason_code_and_roll_back(error: Exception) -> None:
    class _Failing:
        def __init__(self, _session: object) -> None:
            pass

        async def reserve(self, request: Any, **_: Any) -> Any:
            raise error

    service, command, _callbacks, _keys, counters, *_ = _application_with_fakes(
        fail_submission=False, workspace_risk_coordinator_factory=_Failing
    )
    from tests.unit.test_paper_runtime_application import _FakeUnitOfWork

    with pytest.raises(PaperRuntimeApplicationError) as rejected:
        asyncio.run(service.evaluate_and_submit(command))

    assert rejected.value.reason_code == "WORKSPACE_RISK_POLICY_UNAVAILABLE"
    assert counters.submissions == 0
    assert _FakeUnitOfWork.instances[-1].rolled_back is True


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (BudgetConflictError("idempotency"), "PAPER_IDEMPOTENCY_CONFLICT"),
    ],
)
def test_other_coordinator_errors_keep_their_mapping(error: Exception, code: str) -> None:
    class _Failing:
        def __init__(self, _session: object) -> None:
            pass

        async def reserve(self, request: Any, **_: Any) -> Any:
            raise error

    service, command, *_ = _application_with_fakes(
        fail_submission=False, workspace_risk_coordinator_factory=_Failing
    )
    with pytest.raises(PaperRuntimeApplicationError) as rejected:
        asyncio.run(service.evaluate_and_submit(command))
    assert rejected.value.reason_code == code


def test_programming_errors_from_coordinator_are_not_masked() -> None:
    class _Broken:
        def __init__(self, _session: object) -> None:
            pass

        async def reserve(self, request: Any, **_: Any) -> Any:
            raise TypeError("bug")

    service, command, *_ = _application_with_fakes(
        fail_submission=False, workspace_risk_coordinator_factory=_Broken
    )
    with pytest.raises(TypeError):
        asyncio.run(service.evaluate_and_submit(command))
