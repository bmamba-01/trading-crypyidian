"""Định danh và mốc thời gian cố định dùng chung cho mọi lớp của bộ dữ liệu QA.

Mọi ID là UUIDv5 sinh từ namespace cố định (RFC 9562 mục 5.5), nên seed lặp lại luôn ra
cùng khóa chính. ID xác định trước không phải bí mật: an toàn dựa vào RLS/xác thực.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

DATASET_VERSION = "crypidian-qa-v1"
# Đổi chuỗi này sẽ đổi toàn bộ ID và làm dữ liệu đã seed bị mồ côi; chỉ đổi kèm phiên bản mới.
QA_NAMESPACE = uuid5(NAMESPACE_URL, "https://crypidian.invalid/qa-dataset/v1")
# Mốc tạo baseline (created_at, publish settings, xác minh MFA) để dữ liệu tái lập được.
SEEDED_AT = datetime(2026, 10, 1, tzinfo=UTC)
# Thời điểm đóng của nến cuối cùng trong fixture thị trường.
MARKET_END = datetime(2026, 10, 1, tzinfo=UTC)

QA_WORKSPACE_KEY = "qa"
QA_OTHER_WORKSPACE_KEY = "qa_other"


def qa_id(kind: str, key: str) -> UUID:
    """Sinh UUIDv5 ổn định từ loại bản ghi và khóa nghiệp vụ của dataset."""
    if not kind.strip() or not key.strip():
        raise ValueError("kind và key của ID QA không được để trống.")
    return uuid5(QA_NAMESPACE, f"{kind}:{key}")
