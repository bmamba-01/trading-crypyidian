from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings

WORKSPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.mark.asyncio
async def test_market_signal_process_wires_its_own_worker_and_readiness() -> None:
    class Worker:
        last_tick_succeeded = False

        async def run(self, stopping: asyncio.Event) -> None:
            self.last_tick_succeeded = True
            await stopping.wait()

    worker = Worker()
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return worker

    app = create_app(
        AppSettings(
            app_env="test",
            process_role="market-signal-worker",
            market_signal_workspace_id=WORKSPACE,
        ),
        session_factory=object(),  # type: ignore[arg-type]
        database_probe=lambda: _ready(),
        market_signal_worker_factory=factory,
    )

    with TestClient(app) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}

    assert {getattr(route, "path", None) for route in app.routes} == {
        "/health/live",
        "/health/ready",
    }
    assert len(factory_calls) == 1
    assert factory_calls[0]["workspace_id"] == WORKSPACE
    assert "sender" not in factory_calls[0]


async def _ready() -> bool:
    return True
