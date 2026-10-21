from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings


def test_health_endpoint_reports_safe_paper_status() -> None:
    client = TestClient(create_app(AppSettings(trading_mode="PAPER")))

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "PAPER"}
