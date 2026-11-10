"""Nhãn và định dạng tiếng Việt; không đổi mã máy hoặc tính lại số liệu báo cáo."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MARKET_UP_COLOR = "#0ECB81"
MARKET_DOWN_COLOR = "#F6465D"
MARKET_FLAT_COLOR = "#8B98A5"
PNL_METRIC_KEYS = frozenset(
    {
        "net_pnl",
        "expectancy_per_trade",
        "average_winning_trade",
        "average_losing_trade",
        "gross_pnl",
    }
)

LABELS = {
    "ACTIVE": "Đang hoạt động",
    "ENABLED": "Đã bật",
    "DISABLED": "Đã tắt",
    "READY": "Sẵn sàng",
    "STARTING": "Đang khởi động",
    "STANDBY": "Đang chờ tiếp quản",
    "RECOVERY_REQUIRED": "Cần khôi phục",
    "VERIFIED": "Đã xác minh",
    "UNVERIFIED": "Chưa xác minh",
    "OPEN": "Đang mở",
    "NEW": "Lệnh mới",
    "PARTIALLY_FILLED": "Đã khớp một phần",
    "FILLED": "Đã khớp toàn bộ",
    "CLOSED": "Đã đóng",
    "REJECTED": "Bị từ chối",
    "FLATTEN_MARK_UNAVAILABLE": "Chưa có giá đánh dấu; hãy tải lại bản xem trước",
    "FLATTEN_MARK_STALE": "Giá đánh dấu đã cũ; hãy tải lại bản xem trước",
    "NOT_READY": "Chưa sẵn sàng",
    "UNKNOWN": "Chưa xác định",
    "PAUSED": "Tạm dừng",
    "KILLED": "Đã chặn mở vị thế",
    "BLOCKED": "Bị chặn",
    "DEGRADED": "Suy giảm",
    "STOPPED": "Đã dừng",
    "PAPER": "Mô phỏng",
    "DEMO": "Thử nghiệm",
    "LIVE": "Giao dịch thực",
    "DRAFT": "Bản nháp",
    "PUBLISHED": "Đã công bố",
    "PENDING": "Đang chờ",
    "RUNNING": "Đang xử lý",
    "COMPLETED": "Đã hoàn tất",
    "PARTIAL": "Hoàn tất một phần",
    "FAILED": "Thất bại",
    "CANCELLED": "Đã hủy",
    "CANCELED": "Đã hủy",
    "EXPIRED": "Đã hết hạn",
    "POSITION": "Vị thế",
    "ENTRY": "Vào vị thế",
    "EXIT": "Đóng vị thế",
    "LONG": "Mua lên",
    "SHORT": "Bán xuống",
    "STOP_LOSS": "Dừng lỗ",
    "TAKE_PROFIT": "Chốt lời",
    "TIME_STOP": "Đóng theo thời gian",
    "OPPOSING_SIGNAL": "Tín hiệu ngược chiều",
    "FLATTEN_OPERATION": "Đóng theo thao tác quản trị",
    "ORDER": "Lệnh",
    "OPEN_ORDER": "Lệnh mở",
    "CLOSE_POSITION": "Đóng vị thế",
    "CANCEL_ORDER": "Hủy lệnh",
    "SKIPPED": "Đã bỏ qua",
    "QUEUED": "Đã xếp hàng",
    "PROCESSING": "Đang xử lý",
    "VALID": "Hợp lệ",
    "INVALID": "Không hợp lệ",
    "bot": "Một bot",
    "workspace": "Toàn không gian làm việc",
    "—": "Chưa xác định",
}
METRICS = (
    ("net_pnl", "Lãi/lỗ ròng đã thực hiện", True),
    ("closed_trade_count", "Số giao dịch đã đóng", False),
    ("win_rate_percent", "Tỷ lệ giao dịch có lãi (%)", False),
    ("profit_factor", "Hệ số lợi nhuận", False),
    ("expectancy_per_trade", "Kỳ vọng ròng mỗi giao dịch", True),
    ("average_winning_trade", "Mức lãi ròng trung bình", True),
    ("average_losing_trade", "Mức lỗ ròng trung bình", True),
    ("max_consecutive_wins", "Chuỗi thắng dài nhất", False),
    ("max_consecutive_losses", "Chuỗi thua dài nhất", False),
    ("average_holding_seconds", "Thời gian giữ lệnh trung bình (giây)", False),
    ("gross_pnl", "Lãi/lỗ gộp đã thực hiện", True),
    ("fee_total", "Phí giao dịch", True),
    ("funding_total", "Phí tài trợ", True),
    ("slippage_total", "Chi phí trượt giá", True),
    ("max_drawdown", "Mức sụt giảm tối đa đã thực hiện", True),
)


def label_vi(value: object) -> str:
    """Mã chưa nhận biết không được trình bày như trạng thái đã xác minh."""
    return LABELS.get(str(value), "Chưa xác định")


def local_time(value: object) -> str:
    """Chỉ đổi múi giờ hiển thị khi nguồn có múi giờ; không đoán thời gian thiếu offset."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "Chưa xác định thời gian"
        return parsed.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime(
            "%d/%m/%Y %H:%M:%S (Asia/Ho_Chi_Minh)"
        )
    except ValueError, TypeError:
        return "Chưa xác định thời gian"


def number_vi(value: object) -> str:
    """Giữ nguyên độ chính xác thập phân của MB-06, không làm tròn hoặc quy đổi tài sản."""
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "Chưa có dữ liệu"
        return format(number, "f").replace(".", ",")
    except InvalidOperation, ValueError:
        return "Chưa có dữ liệu"


def pnl_tone(value: object) -> str:
    """Phân loại dấu của PnL mà không làm tròn hoặc ép giá trị lỗi thành hòa vốn."""
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "unknown"
        if number > 0:
            return "positive"
        if number < 0:
            return "negative"
        return "flat"
    except InvalidOperation, TypeError, ValueError:
        return "unknown"
