"""Kiểu PostgreSQL bổ sung dùng cho barrier transaction top-level."""

from typing import Any

from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType


class Xid8(UserDefinedType[str]):
    """Giữ nguyên xid8; không rút gọn xuống xid 32 bit hoặc thời điểm commit."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "xid8"


ischema_names["xid8"] = Xid8
