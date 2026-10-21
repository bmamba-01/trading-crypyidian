from fastapi import FastAPI

from trading_bot.config.settings import AppSettings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Tạo ứng dụng mà không kết nối mạng hay tải credential khi import."""
    app_settings = settings or AppSettings()
    app = FastAPI(title="Crypidian Trading Bot", docs_url=None, redoc_url=None)

    @app.get("/health/live", include_in_schema=False)
    def live_health() -> dict[str, str]:
        """Báo process còn hoạt động; không khẳng định các dependency đã sẵn sàng."""
        return {"status": "ok", "mode": app_settings.trading_mode}

    return app
