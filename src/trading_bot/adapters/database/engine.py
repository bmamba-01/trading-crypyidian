from dataclasses import dataclass
from datetime import timedelta
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


@dataclass(frozen=True, slots=True)
class SessionTimeouts:
    """Giới hạn chờ/treo của phiên PostgreSQL (mili giây), đặt khi mở kết nối."""

    lock_timeout_ms: int
    idle_in_transaction_timeout_ms: int
    statement_timeout_ms: int

    def as_options(self) -> str:
        return (
            f"-c lock_timeout={self.lock_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={self.idle_in_transaction_timeout_ms} "
            f"-c statement_timeout={self.statement_timeout_ms}"
        )


def runtime_session_timeouts(lease_ttl: timedelta) -> SessionTimeouts:
    """Timeout runtime suy từ TTL lease (fix round 1 MB-05, I2).

    - `idle_in_transaction_session_timeout` = TTL/3: worker bị treo giữa transaction (giữ L1 +
      `FOR SHARE` hàng lease) bị PostgreSQL cắt phiên sớm hơn hạn lease nên lệnh của bot khác,
      exit bảo vệ, pause/kill và takeover không bị chặn quá một phần ba TTL.
    - `lock_timeout` = TTL/6 (< idle): mọi chờ khóa (advisory L1–L4, hàng lease khi
      acquire/renew) có giới hạn; quá hạn trả `55P03`, supervisor/caller thử lại ở tick sau
      thay vì treo và giữ `_tick_lock`.
    - `statement_timeout` = TTL/2: không câu lệnh nào chạy quá nửa hạn lease.
    """
    ttl_ms = int(lease_ttl.total_seconds() * 1000)
    if ttl_ms < 3000:
        raise ValueError("TTL lease quá ngắn để suy timeout phiên.")
    return SessionTimeouts(
        lock_timeout_ms=ttl_ms // 6,
        idle_in_transaction_timeout_ms=ttl_ms // 3,
        statement_timeout_ms=ttl_ms // 2,
    )


def create_database_engine(
    url: URL, *, session_timeouts: SessionTimeouts | None = None
) -> AsyncEngine:
    """Khởi tạo engine bất đồng bộ với kiểm tra kết nối khi mượn pool."""
    if session_timeouts is None:
        return create_async_engine(url, pool_pre_ping=True)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": session_timeouts.as_options()},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Tạo factory session; transaction do Unit of Work quản lý ở bước tiếp theo."""
    return async_sessionmaker(engine, expire_on_commit=False)
