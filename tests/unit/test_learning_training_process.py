from __future__ import annotations

import resource

import pytest

from trading_bot.application.learning_training_process import (
    LearningTrainingProcessFailure,
    LearningTrainingProcessFailureCode,
    _apply_process_limits,
)


def test_process_limits_use_cpu_soft_signal_memory_hard_and_disable_core_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda _resource_id: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_id, limits: calls.append((resource_id, limits)),
    )

    _apply_process_limits(cpu_seconds=7, address_space_bytes=512 * 1024 * 1024)

    assert (resource.RLIMIT_CPU, (7, 8)) in calls
    assert (resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)) in calls
    assert (resource.RLIMIT_CORE, (0, 0)) in calls


def test_process_limits_fail_closed_when_cpu_hard_cap_cannot_allow_signal_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda resource_id: (
            (7, 7)
            if resource_id == resource.RLIMIT_CPU
            else (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        ),
    )

    with pytest.raises(LearningTrainingProcessFailure) as error:
        _apply_process_limits(cpu_seconds=7, address_space_bytes=512 * 1024 * 1024)

    assert error.value.code is LearningTrainingProcessFailureCode.RESOURCE_LIMIT_UNAVAILABLE
