from fastapi.testclient import TestClient

from trading_bot.app import create_app


def test_readiness_is_blocked_until_all_dependencies_are_ready() -> None:
    app = create_app()

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "pending": ["database", "exchange", "market_data"],
    }


def test_readiness_does_not_expose_credentials() -> None:
    app = create_app()
    app.state.readiness = {
        "database": True,
        "market_data": True,
        "exchange": True,
    }

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert "secret" not in response.text.lower()
