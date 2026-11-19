from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_bot.app import create_app
from trading_bot.config.settings import AppSettings


def test_admin_wires_block_service_to_admin_factory_without_changing_history_reader() -> None:
    admin_sessions = async_sessionmaker()
    scorer_history_sessions = async_sessionmaker()
    app = create_app(
        AppSettings(_env_file=None, app_env="test", process_role="admin-web"),
        session_factory=admin_sessions,
        scorer_history_session_factory=scorer_history_sessions,
    )

    with TestClient(app):
        block_service = getattr(app.state, "learning_paper_entry_block_service", None)
        assert block_service is not None
        assert block_service._sessions is admin_sessions
        assert (
            app.state.learning_scoring_score_event_read_model._sessions is scorer_history_sessions
        )
        operation = app.openapi()["paths"][
            "/admin/learning/scoring/events/{score_event_id}/paper-block"
        ]["post"]
        assert "200" in operation["responses"]


def test_non_admin_process_does_not_wire_or_register_operator_block_service() -> None:
    app = create_app(
        AppSettings(_env_file=None, app_env="test", process_role="execution-gateway"),
        session_factory=async_sessionmaker(),
    )

    assert getattr(app.state, "learning_paper_entry_block_service", None) is None
    assert (
        "/admin/learning/scoring/events/{score_event_id}/paper-block" not in app.openapi()["paths"]
    )
