from pathlib import Path

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def database_url_from_secret(
    *, username: str, password_file: Path, host: str, database: str
) -> URL:
    """Tạo URL kết nối từ secret file, không ghép mật khẩu vào chuỗi URL."""
    password = password_file.read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError("Tệp mật khẩu database rỗng")
    return URL.create(
        "postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        database=database,
    )


def create_database_engine(url: URL) -> AsyncEngine:
    """Khởi tạo engine bất đồng bộ với kiểm tra kết nối khi mượn pool."""
    return create_async_engine(url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Tạo factory session; transaction do Unit of Work quản lý ở bước tiếp theo."""
    return async_sessionmaker(engine, expire_on_commit=False)
