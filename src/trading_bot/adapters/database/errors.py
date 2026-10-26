"""Phân loại lỗi DB tạm thời (fix round 2 MB-05).

Runtime đặt `lock_timeout`/`statement_timeout` theo TTL lease (fix round 1, I2) nên chờ khóa có
giới hạn. Hai SQLSTATE dưới đây nghĩa là "chưa làm được lúc này" chứ không phải dữ liệu hỏng:
caller phải thử lại (STANDBY + backoff), không ghi RECOVERY_REQUIRED hay chặn workspace vĩnh viễn.

- `55P03` lock_not_available: hết `lock_timeout` khi chờ khóa (advisory L1–L4, hàng lease).
- `57014` query_canceled: hết `statement_timeout`.

Nguồn: PostgreSQL Appendix A "Error Codes" và "Client Connection Defaults"
(https://www.postgresql.org/docs/current/errcodes-appendix.html,
https://www.postgresql.org/docs/current/runtime-config-client.html), kiểm tra 2026-10-26.
"""

from __future__ import annotations

TRANSIENT_SQLSTATES = frozenset({"55P03", "57014"})


def database_sqlstate(error: BaseException) -> str | None:
    """SQLSTATE của lỗi DBAPI (psycopg `sqlstate`), tìm qua `orig` và chuỗi `__cause__`."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (getattr(current, "orig", None), current):
            sqlstate = getattr(candidate, "sqlstate", None)
            if isinstance(sqlstate, str):
                return sqlstate
        current = current.__cause__ or current.__context__
    return None


def is_transient_database_error(error: BaseException) -> bool:
    """True cho lock/statement timeout: thử lại sau, không phải lỗi recovery."""
    return database_sqlstate(error) in TRANSIENT_SQLSTATES
