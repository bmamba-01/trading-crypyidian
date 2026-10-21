from fastapi import FastAPI
from fastapi.responses import JSONResponse

from trading_bot.config.settings import AppSettings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Tạo ứng dụng mà không kết nối mạng hay tải credential khi import."""
    app_settings = settings or AppSettings()
    app = FastAPI(title="Crypidian Trading Bot", docs_url=None, redoc_url=None)
    app.state.readiness = {
        "database": False,
        "market_data": False,
        "exchange": False,
    }

    @app.get("/health/live", include_in_schema=False)
    def live_health() -> dict[str, str]:
        """Báo process còn hoạt động; không khẳng định các dependency đã sẵn sàng."""
        return {
            "status": "ok",
            "mode": app_settings.trading_mode,
            "role": app_settings.process_role,
        }

    @app.get("/health/ready", include_in_schema=False, response_model=None)
    def ready_health() -> JSONResponse | dict[str, str]:
        """Chỉ cho biết sẵn sàng khi mọi dependency thiết yếu đã được đánh dấu đạt."""
        pending = sorted(name for name, is_ready in app.state.readiness.items() if not is_ready)
        if pending:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "pending": pending},
            )
        return {"status": "ready"}

    return app


app = create_app()
