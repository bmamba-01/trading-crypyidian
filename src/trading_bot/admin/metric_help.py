"""Từ điển chú giải chỉ số tiếng Việt: nguồn duy nhất cho tooltip trên Admin Web (UI-1).

Mỗi mục giải thích chỉ số là gì, cách hệ thống NÀY tính nó, cách đọc và giới hạn. Nội dung được
đối chiếu với mã nguồn tính toán (ghi ở chú thích từng nhóm); chỗ nào chưa kiểm chứng được thì
ghi rõ trong `limits_vi` thay vì suy đoán. Module chỉ chứa văn bản trình bày: không tính lại và
không làm tròn số liệu báo cáo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import count

from trading_bot.domain.portfolio.post_mortem import (
    EXCESSIVE_LOSS_R_MULTIPLE,
    FALSE_BREAKOUT_MAX_HOLDING_SECONDS,
    SLIPPAGE_DEVIANT_PLAN_MULTIPLE,
)
from trading_bot.domain.portfolio.reporting import ANALYTICS_DECIMAL_PLACES, REPORT_TIMEZONE


@dataclass(frozen=True, slots=True)
class MetricHelp:
    """Chú giải một chỉ số; `formula_vi` rỗng khi mục không phải là phép tính."""

    title_vi: str
    definition_vi: str
    formula_vi: str
    reading_vi: str
    limits_vi: str
    # Slug tiêu đề mục trong `docs/research/glossary-vi.md` (kiểu anchor của GitHub).
    glossary_anchor: str | None = None


def _vi(value: Decimal) -> str:
    """Số thập phân theo cách viết tiếng Việt (dấu phẩy), bỏ số 0 thừa ở cuối."""
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


_G_PRICE = "a-cấu-trúc-giá-và-biểu-đồ"
_G_FUTURES = "c-futures-dòng-lệnh-và-quản-trị-lệnh"
_G_RISK = "d-rủi-ro-điểm-vàora-và-hiệu-suất"
_G_SECURITY = "m-bảo-mật-adminbot-phân-quyền-và-khóa-sàn"
_G_REPORT = "o-báo-cáo-bot-và-điều-khiển-qua-telegram"
_G_QUANT = "r-kiến-trúc-module-định-lượng-và-thiết-kế-chiến-lược-dạng-khai-báo-quantsmart-synthesis"
_G_POST_MORTEM = (
    "t-hệ-thống-giao-diện-giao-dịch-mổ-xẻ-sau-giao-dịch-và-dữ-liệu-huấn-luyện-ai-"
    "phản-thực-nghiệm-trading-design-system-post-mortem--counterfactual-retraining"
)
_G_WP06 = "v-thuật-ngữ-mới-về-báo-cáo-và-scorer-wp-06-en--vi"
_G_UI = "w-chú-giải-chỉ-số-và-biểu-đồ-trên-admin-web-ui-1"

_SCOPE = "Tính riêng cho từng tài sản định giá (quote asset), trên các giao dịch PAPER đã đóng"
_WINDOW = "trong khoảng báo cáo đang xem (tính theo thời điểm đóng, không gồm mốc kết thúc)"
_ROUNDING = f"làm tròn nửa lên tới {ANALYTICS_DECIMAL_PLACES} chữ số thập phân"
_LEDGER_ONLY = (
    "Số liệu lấy nguyên văn từ sổ cái (ledger) PAPER nội bộ, không phải kết quả khớp lệnh trên sàn."
)
_SLIPPAGE_MULTIPLE = _vi(SLIPPAGE_DEVIANT_PLAN_MULTIPLE)
_EXCESSIVE_LOSS = _vi(EXCESSIVE_LOSS_R_MULTIPLE)
_FALSE_BREAKOUT_MINUTES = FALSE_BREAKOUT_MAX_HOLDING_SECONDS // 60
_QUALITY_RULES = (
    f"Ba quy tắc được kiểm theo thứ tự: trượt giá thực tế lớn hơn {_SLIPPAGE_MULTIPLE} lần mức "
    "kế hoạch dự trù cho cả vào và ra; đóng bằng dừng lỗ, lỗ ròng và giữ lệnh dưới "
    f"{_FALSE_BREAKOUT_MINUTES} phút; lỗ ròng vượt {_EXCESSIVE_LOSS} lần ngân sách rủi ro kế hoạch."
)
_QUALITY_LIMIT = (
    "Các ngưỡng là lựa chọn vận hành nội bộ, chưa được nghiên cứu hay backtest xác nhận. "
    "Nhãn chỉ mô tả việc thực thi một lệnh, không đánh giá chiến lược."
)

# Nguồn đối chiếu: `domain/portfolio/reporting.py` (`_totals`, `realized_drawdown`,
# `_maximum_streaks`, `_daily_performance`, kiểm tra toàn vẹn net = gross - total_cost).
_REPORT_METRICS: dict[str, MetricHelp] = {
    "net_pnl": MetricHelp(
        title_vi="Lãi/lỗ ròng đã thực hiện (Net PnL)",
        definition_vi="Tổng lãi/lỗ sau mọi chi phí của các giao dịch PAPER đã đóng.",
        formula_vi=(
            "Lãi/lỗ ròng = lãi/lỗ gộp − (phí giao dịch + phí tài trợ + chi phí trượt giá); "
            "cộng nguyên văn theo từng giao dịch trong ledger."
        ),
        reading_vi="Dương là lãi, âm là lỗ. " + _SCOPE + " " + _WINDOW + ".",
        limits_vi=(
            "Không gồm vị thế đang mở và không định giá theo thị trường (mark-to-market). "
            + _LEDGER_ONLY
        ),
        glossary_anchor=_G_REPORT,
    ),
    "closed_trade_count": MetricHelp(
        title_vi="Số giao dịch đã đóng",
        definition_vi="Số giao dịch PAPER có thời điểm đóng nằm trong khoảng báo cáo.",
        formula_vi="Đếm các giao dịch đã đóng, gồm cả giao dịch lãi, lỗ và hòa vốn.",
        reading_vi=(
            "Đây là cỡ mẫu của mọi chỉ số còn lại; mẫu càng nhỏ thì tỷ lệ và trung bình càng "
            "kém tin cậy."
        ),
        limits_vi="Không gồm giao dịch đang mở. Số lệnh ít chưa đủ để kết luận về chiến lược.",
        glossary_anchor=_G_REPORT,
    ),
    "win_rate_percent": MetricHelp(
        title_vi="Tỷ lệ giao dịch có lãi (Win Rate)",
        definition_vi="Phần trăm giao dịch đã đóng có lãi/lỗ ròng lớn hơn 0.",
        formula_vi=(
            "Tỷ lệ thắng = số giao dịch có lãi ròng dương ÷ tổng số giao dịch đã đóng × 100; "
            f"giao dịch hòa vốn vẫn nằm trong mẫu số; {_ROUNDING}."
        ),
        reading_vi=(
            "Phải đọc cùng mức lãi và mức lỗ trung bình: tỷ lệ thắng cao vẫn có thể lỗ nếu mỗi "
            "lệnh thua lớn hơn nhiều lệnh thắng."
        ),
        limits_vi=(
            "Lãi hay lỗ xét theo PnL ròng sau chi phí. Không dự báo kết quả lệnh kế tiếp và "
            "dao động mạnh khi số giao dịch còn ít."
        ),
        glossary_anchor=_G_WP06,
    ),
    "profit_factor": MetricHelp(
        title_vi="Hệ số lợi nhuận (Profit Factor)",
        definition_vi=(
            "So sánh tổng tiền lãi của các lệnh thắng với tổng tiền lỗ của các lệnh thua."
        ),
        formula_vi=(
            "Hệ số lợi nhuận = tổng lãi ròng của các giao dịch thắng ÷ trị tuyệt đối tổng lỗ ròng "
            f"của các giao dịch thua; {_ROUNDING}."
        ),
        reading_vi=(
            "Lớn hơn 1 nghĩa là tổng lãi lớn hơn tổng lỗ trong mẫu này; nhỏ hơn 1 nghĩa là ngược "
            "lại."
        ),
        limits_vi=(
            "Không xác định khi không có giao dịch thua (không chia cho 0). Dùng PnL ròng sau "
            "chi phí, nên khác các công cụ tính trên lãi/lỗ gộp. Một vài lệnh lớn có thể chi "
            "phối toàn bộ hệ số."
        ),
        glossary_anchor=_G_WP06,
    ),
    "expectancy_per_trade": MetricHelp(
        title_vi="Kỳ vọng ròng mỗi giao dịch (Expectancy)",
        definition_vi="Lãi/lỗ ròng trung bình của một giao dịch đã đóng trong mẫu.",
        formula_vi=f"Kỳ vọng = tổng lãi/lỗ ròng ÷ số giao dịch đã đóng; {_ROUNDING}.",
        reading_vi="Dương nghĩa là trung bình mỗi lệnh trong mẫu có lãi sau chi phí.",
        limits_vi=(
            "Là trung bình của lịch sử đã chọn, không phải cam kết cho lệnh tương lai. Tính bằng "
            "tiền của tài sản định giá, chưa quy về bội số R."
        ),
        glossary_anchor=_G_RISK,
    ),
    "average_winning_trade": MetricHelp(
        title_vi="Mức lãi ròng trung bình",
        definition_vi="Lãi ròng trung bình của các giao dịch thắng.",
        formula_vi=(
            f"Tổng lãi ròng của giao dịch có PnL ròng dương ÷ số giao dịch đó; {_ROUNDING}."
        ),
        reading_vi=(
            "So với mức lỗ ròng trung bình để biết một lệnh thắng bù được bao nhiêu lệnh thua."
        ),
        limits_vi="Không xác định khi không có giao dịch thắng. Giao dịch hòa vốn không được tính.",
        glossary_anchor=_G_WP06,
    ),
    "average_losing_trade": MetricHelp(
        title_vi="Mức lỗ ròng trung bình",
        definition_vi="Lỗ ròng trung bình của các giao dịch thua.",
        formula_vi=(f"Tổng lỗ ròng của giao dịch có PnL ròng âm ÷ số giao dịch đó; {_ROUNDING}."),
        reading_vi="Giá trị giữ dấu âm. Trị tuyệt đối càng lớn thì mỗi lệnh thua càng tốn kém.",
        limits_vi="Không xác định khi không có giao dịch thua. Giao dịch hòa vốn không được tính.",
        glossary_anchor=_G_WP06,
    ),
    "max_consecutive_wins": MetricHelp(
        title_vi="Chuỗi thắng dài nhất",
        definition_vi="Số giao dịch có lãi ròng liên tiếp nhiều nhất, xếp theo thời điểm đóng.",
        formula_vi="Đếm chuỗi liên tiếp dài nhất; giao dịch thua hoặc hòa vốn ngắt chuỗi.",
        reading_vi="Cho biết lịch sử từng thuận lợi liên tục tới mức nào trong mẫu đã chọn.",
        limits_vi="Chỉ mô tả quá khứ của mẫu; không dự báo chuỗi kế tiếp.",
        glossary_anchor=_G_WP06,
    ),
    "max_consecutive_losses": MetricHelp(
        title_vi="Chuỗi thua dài nhất",
        definition_vi="Số giao dịch lỗ ròng liên tiếp nhiều nhất, xếp theo thời điểm đóng.",
        formula_vi="Đếm chuỗi liên tiếp dài nhất; giao dịch thắng hoặc hòa vốn ngắt chuỗi.",
        reading_vi="Dùng để hình dung giai đoạn thua liên tục mà tài khoản đã phải chịu.",
        limits_vi=(
            "Chỉ mô tả quá khứ của mẫu; chuỗi thua tương lai có thể dài hơn. Không cho biết số "
            "tiền đã mất, hãy xem mức sụt giảm tối đa."
        ),
        glossary_anchor=_G_WP06,
    ),
    "average_holding_seconds": MetricHelp(
        title_vi="Thời gian giữ lệnh trung bình",
        definition_vi="Thời gian trung bình từ lúc mở đến lúc đóng của các giao dịch đã đóng.",
        formula_vi=(
            "Tổng (thời điểm đóng − thời điểm mở) ÷ số giao dịch đã đóng, làm tròn tới giây gần "
            "nhất theo mốc thời gian trong ledger."
        ),
        reading_vi="Đơn vị gốc là giây; một số thẻ đổi sang phút hoặc giờ để dễ đọc.",
        limits_vi=(
            "Không gồm vị thế đang mở. Trung bình có thể bị kéo lệch bởi vài lệnh giữ rất lâu."
        ),
        glossary_anchor=_G_WP06,
    ),
    "gross_pnl": MetricHelp(
        title_vi="Lãi/lỗ gộp đã thực hiện (Gross PnL)",
        definition_vi="Tổng lãi/lỗ của các giao dịch đã đóng trước khi trừ chi phí.",
        formula_vi=(
            "Cộng nguyên văn lãi/lỗ gộp của từng giao dịch trong ledger; lãi/lỗ ròng = lãi/lỗ "
            "gộp − tổng chi phí."
        ),
        reading_vi="Chênh lệch giữa lãi/lỗ gộp và lãi/lỗ ròng chính là tổng chi phí đã trả.",
        limits_vi=(
            "Báo cáo không tính lại lãi/lỗ gộp của từng giao dịch mà dùng giá trị ledger đã ghi. "
            + _LEDGER_ONLY
        ),
        glossary_anchor=_G_REPORT,
    ),
    "fee_total": MetricHelp(
        title_vi="Phí giao dịch",
        definition_vi="Tổng phí giao dịch ledger ghi cho các giao dịch đã đóng.",
        formula_vi="Cộng nguyên văn khoản phí của từng giao dịch; là một phần của tổng chi phí.",
        reading_vi="Số dương là chi phí và làm giảm lãi/lỗ ròng.",
        limits_vi=(
            "Phí do mô hình PAPER nội bộ tính theo hồ sơ thực thi, không phải phí sàn thực thu. "
            "Chú giải này không nêu biểu phí vì biểu phí thay đổi theo cấu hình."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "funding_total": MetricHelp(
        title_vi="Phí tài trợ (Funding)",
        definition_vi=(
            "Tổng khoản thanh toán funding ledger ghi cho các giao dịch đã đóng. Funding là khoản "
            "thanh toán định kỳ giữa bên mua và bên bán của hợp đồng tương lai vĩnh cửu."
        ),
        formula_vi=(
            "Cộng nguyên văn khoản funding của từng giao dịch; là một phần của tổng chi phí."
        ),
        reading_vi="Trong báo cáo này số dương là chi phí và làm giảm lãi/lỗ ròng.",
        limits_vi=(
            "Phụ thuộc việc mô hình PAPER có ghi funding cho giao dịch hay không; chú giải này "
            "chưa kiểm chứng cách mô hình PAPER tính từng kỳ funding."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "slippage_total": MetricHelp(
        title_vi="Chi phí trượt giá (Slippage)",
        definition_vi=(
            "Tổng chi phí trượt giá ledger ghi cho các giao dịch đã đóng. Trượt giá là chênh lệch "
            "giữa giá dự kiến và giá khớp."
        ),
        formula_vi=(
            "Cộng nguyên văn khoản trượt giá của từng giao dịch; là một phần của tổng chi phí."
        ),
        reading_vi="Số dương là chi phí và làm giảm lãi/lỗ ròng.",
        limits_vi=(
            "Là trượt giá do mô hình PAPER mô phỏng, không phải trượt giá đo trên sổ lệnh của "
            "sàn. Chú giải này chưa kiểm chứng công thức mô phỏng của từng lần khớp."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "max_drawdown": MetricHelp(
        title_vi="Mức sụt giảm tối đa đã thực hiện (Max Drawdown)",
        definition_vi=(
            "Mức giảm lớn nhất từ đỉnh xuống đáy của lãi/lỗ ròng tích lũy đã thực hiện trong "
            "khoảng báo cáo."
        ),
        formula_vi=(
            "Cộng dồn lãi/lỗ ròng theo thứ tự đóng lệnh, bắt đầu từ 0; mức sụt giảm tối đa là "
            "giá trị lớn nhất của (đỉnh tích lũy trước đó − giá trị tích lũy hiện tại)."
        ),
        reading_vi=(
            "Là số tiền của tài sản định giá, không phải phần trăm vốn. Bằng 0 khi lãi/lỗ tích lũy "
            "chưa từng giảm so với đỉnh."
        ),
        limits_vi=(
            "Chỉ tính trên PnL ròng đã thực hiện; không định giá theo thị trường (mark-to-market) "
            "nên không thấy mức lỗ tạm thời khi vị thế còn mở. Phụ thuộc khoảng báo cáo đã chọn."
        ),
        glossary_anchor=_G_REPORT,
    ),
}

# Nguồn đối chiếu: `admin/web_router.py` (ngữ cảnh dashboard, `_get_bot_trading_data`,
# `_recent_trade_view`, `_dashboard_performance_chart`) và `templates/admin/base.html`.
_DASHBOARD_AND_BOT: dict[str, MetricHelp] = {
    "bot_count": MetricHelp(
        title_vi="Tổng số bot",
        definition_vi="Số bot đã cấu hình trong danh sách bot của không gian làm việc.",
        formula_vi="Đếm mọi bot trong danh sách, không phân biệt đang bật hay đã tắt.",
        reading_vi="So với số bot sẵn sàng để biết còn bao nhiêu bot chưa vận hành được.",
        limits_vi=(
            "Không cho biết bot có đang giao dịch hay không; xem trạng thái từng bot bên dưới."
        ),
    ),
    "ready_bot_count": MetricHelp(
        title_vi="Bot sẵn sàng",
        definition_vi="Số bot có mức sẵn sàng vận hành là Sẵn sàng (READY).",
        formula_vi=(
            "Đếm các bot mà dịch vụ vận hành báo mức sẵn sàng READY tại lần cập nhật gần nhất."
        ),
        reading_vi="Bot không sẵn sàng (suy giảm, cần khôi phục, chưa xác định) không được tính.",
        limits_vi=(
            "Sẵn sàng không có nghĩa là đang được phép mở vị thế: công tắc dừng khẩn cấp hoặc "
            "trạng thái tạm dừng vẫn có thể chặn lệnh mới."
        ),
    ),
    "workspace_kill_state": MetricHelp(
        title_vi="Trạng thái khẩn cấp (công tắc dừng khẩn cấp)",
        definition_vi=(
            "Trạng thái công tắc dừng khẩn cấp (kill switch) của toàn không gian làm việc: Đang "
            "hoạt động hoặc Đã dừng khẩn cấp."
        ),
        formula_vi="",
        reading_vi=(
            "Khi đã dừng khẩn cấp, hệ thống chặn mở vị thế mới trên mọi bot cho tới khi người có "
            "quyền bỏ dừng. Lần thay đổi là số phiên bản của trạng thái này."
        ),
        limits_vi=(
            "Công tắc chỉ chặn mở vị thế mới; việc đóng các vị thế đang mở là thao tác riêng "
            "(Đóng toàn bộ vị thế)."
        ),
        glossary_anchor=_G_SECURITY,
    ),
    "operating_mode": MetricHelp(
        title_vi="Chế độ vận hành",
        definition_vi=(
            "Môi trường mà lệnh được xử lý. PAPER là mô phỏng nội bộ: giao dịch chỉ được ghi vào "
            "cơ sở dữ liệu của hệ thống."
        ),
        formula_vi="",
        reading_vi="Ở chế độ PAPER, hệ thống không gọi API và không đặt lệnh lên Binance.",
        limits_vi=(
            "Kết quả PAPER dùng giá khớp và chi phí mô phỏng nên có thể khác kết quả khi giao "
            "dịch thật."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "open_positions_count": MetricHelp(
        title_vi="Vị thế đang mở",
        definition_vi="Số giao dịch PAPER của bot đang ở trạng thái mở trong ledger khi tải trang.",
        formula_vi="Đếm các giao dịch đang mở trong báo cáo 30 ngày gần nhất của bot.",
        reading_vi="Bằng 0 nghĩa là bot không giữ vị thế PAPER nào tại thời điểm tải trang.",
        limits_vi=(
            "PAPER chưa định giá theo thị trường nên không có giá tham chiếu hay lãi/lỗ chưa "
            "chốt cho vị thế mở."
        ),
    ),
    "position_symbol_side": MetricHelp(
        title_vi="Cặp / Hướng",
        definition_vi=(
            "Cặp giao dịch và hướng vị thế: LONG (mua lên) kỳ vọng giá tăng, SHORT (bán xuống) "
            "kỳ vọng giá giảm."
        ),
        formula_vi="",
        reading_vi="Đọc từ trường mã cặp và hướng của giao dịch trong ledger.",
        limits_vi="Hướng chỉ mô tả vị thế đã mở, không phải khuyến nghị giao dịch.",
        glossary_anchor=_G_FUTURES,
    ),
    "position_opened_at": MetricHelp(
        title_vi="Mở lúc",
        definition_vi="Thời điểm ledger ghi giao dịch được mở.",
        formula_vi="",
        reading_vi="Hiển thị theo múi giờ Asia/Ho_Chi_Minh.",
        limits_vi=(
            "Nếu nguồn thiếu múi giờ, trang ghi rõ chưa xác định thời gian thay vì tự suy đoán."
        ),
    ),
    "quote_asset": MetricHelp(
        title_vi="Tài sản định giá",
        definition_vi=(
            "Đồng tiền dùng để tính lãi/lỗ và chi phí của giao dịch, ví dụ USDT trong cặp BTCUSDT."
        ),
        formula_vi="",
        reading_vi="Số liệu tiền tệ luôn được tách riêng theo từng tài sản định giá.",
        limits_vi="Hệ thống không quy đổi hay cộng lẫn các tài sản định giá khác nhau.",
    ),
    "trade_id": MetricHelp(
        title_vi="Mã giao dịch",
        definition_vi="Mã định danh duy nhất của giao dịch trong ledger PAPER.",
        formula_vi="",
        reading_vi="Dùng mã này để tra cứu giao dịch trong báo cáo và nhật ký.",
        limits_vi="Đây là mã nội bộ của hệ thống, không phải mã lệnh trên sàn.",
    ),
    "trade_closed_at": MetricHelp(
        title_vi="Thời gian đóng",
        definition_vi="Thời điểm ledger ghi giao dịch được đóng hoàn toàn.",
        formula_vi="",
        reading_vi=(
            "Hiển thị theo múi giờ Asia/Ho_Chi_Minh. Khoảng báo cáo và lịch lãi/lỗ đều xét theo "
            "thời điểm này."
        ),
        limits_vi=(
            "Nếu nguồn thiếu múi giờ, trang ghi rõ chưa xác định thời gian thay vì tự suy đoán."
        ),
    ),
    "trade_quantity": MetricHelp(
        title_vi="Khối lượng",
        definition_vi="Tổng khối lượng của các lần khớp vào lệnh (fill) ledger ghi cho giao dịch.",
        formula_vi="Cộng khối lượng của các lần khớp vào lệnh có đủ khối lượng và giá.",
        reading_vi="Đơn vị là tài sản cơ sở của cặp giao dịch, ví dụ BTC với BTCUSDT.",
        limits_vi="Hiện Chưa có dữ liệu khi ledger không lưu lần khớp vào lệnh nào đủ số liệu.",
    ),
    "trade_entry_price": MetricHelp(
        title_vi="Giá vào",
        definition_vi="Giá vào lệnh bình quân của giao dịch.",
        formula_vi=(
            "Giá bình quân gia quyền theo khối lượng = tổng (khối lượng × giá) ÷ tổng khối lượng "
            "của các lần khớp vào lệnh."
        ),
        reading_vi="Đơn vị là tài sản định giá trên một đơn vị tài sản cơ sở.",
        limits_vi=(
            "Là giá khớp do mô hình PAPER ghi, không phải giá khớp trên sàn. Hiện Chưa có dữ "
            "liệu khi thiếu lần khớp."
        ),
    ),
    "trade_exit_price": MetricHelp(
        title_vi="Giá ra",
        definition_vi="Giá đóng lệnh bình quân của giao dịch.",
        formula_vi=(
            "Giá bình quân gia quyền theo khối lượng = tổng (khối lượng × giá) ÷ tổng khối lượng "
            "của các lần khớp đóng lệnh."
        ),
        reading_vi="So với giá vào và hướng lệnh để hiểu lãi/lỗ gộp trước chi phí.",
        limits_vi=(
            "Là giá khớp do mô hình PAPER ghi, không phải giá khớp trên sàn. Hiện Chưa có dữ "
            "liệu khi thiếu lần khớp."
        ),
    ),
    "equity_curve": MetricHelp(
        title_vi="Đường vốn theo PnL ròng đã thực hiện",
        definition_vi=(
            "Mỗi đường là lãi/lỗ ròng tích lũy của một bot trong 30 ngày, tính trên các giao dịch "
            "USDT đã đóng."
        ),
        formula_vi=(
            "Bắt đầu từ 0; tại mỗi thời điểm đóng lệnh cộng thêm lãi/lỗ ròng của giao dịch đó. "
            "Thời điểm vào lệnh chỉ đánh dấu mốc, không làm đổi giá trị."
        ),
        reading_vi="Đường đi lên nghĩa là lãi ròng đã thực hiện tăng; mỗi bot một màu riêng.",
        limits_vi=(
            "Bắt đầu từ 0 chứ không từ vốn ban đầu, nên không phải số dư tài khoản. Không gồm "
            "lãi/lỗ chưa chốt của vị thế mở và không gồm tài sản định giá khác USDT."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "equity_markers": MetricHelp(
        title_vi="Mốc vào và đóng lệnh trên đường vốn",
        definition_vi=(
            "Mũi tên đánh dấu thời điểm vào lệnh và thời điểm đóng lệnh của từng giao dịch trên "
            "đường vốn."
        ),
        formula_vi=(
            "Mốc vào: mũi tên lên cho LONG, xuống cho SHORT. Mốc đóng: mũi tên ngược chiều lệnh, "
            "kèm mã lý do rút gọn (ví dụ SL, TP, TIME, ĐẢO; ĐÓNG cho lý do khác) và lãi/lỗ ròng."
        ),
        reading_vi=(
            "Mốc đóng màu xanh khi lệnh lãi ròng, đỏ khi lỗ ròng, xám khi hòa vốn. Rê chuột hoặc "
            "chạm vào mốc để đọc chiến thuật và lý do."
        ),
        limits_vi=(
            "Chỉ gồm giao dịch USDT đã đóng trong 30 ngày; danh sách chi tiết tối đa 50 giao dịch."
        ),
        glossary_anchor=_G_UI,
    ),
}

# Nguồn đối chiếu: `domain/portfolio/post_mortem.py` (`evaluate_trade_post_mortem`) và các
# trường của `EntryReasoningBreakdown`/`ExitReasoningBreakdown`.
_REPORT_PAGE: dict[str, MetricHelp] = {
    "report_window_start": MetricHelp(
        title_vi="Mốc bắt đầu khoảng báo cáo",
        definition_vi="Thời điểm đầu của khoảng báo cáo; giao dịch đóng đúng mốc này được tính.",
        formula_vi="",
        reading_vi="Nhập theo định dạng ISO-8601 kèm múi giờ, ví dụ 2026-11-01T00:00:00+07:00.",
        limits_vi="Giao dịch được chọn theo thời điểm đóng, không theo thời điểm mở.",
    ),
    "report_window_end": MetricHelp(
        title_vi="Mốc kết thúc khoảng báo cáo",
        definition_vi=(
            "Thời điểm cuối của khoảng báo cáo; giao dịch đóng đúng mốc này không được tính."
        ),
        formula_vi="",
        reading_vi="Nhập theo định dạng ISO-8601 kèm múi giờ, ví dụ 2026-11-10T23:59:59+07:00.",
        limits_vi="Khoảng báo cáo là nửa mở: gồm mốc bắt đầu, không gồm mốc kết thúc.",
    ),
    "total_cost": MetricHelp(
        title_vi="Tổng chi phí thực thi",
        definition_vi="Tổng mọi chi phí ledger ghi cho giao dịch.",
        formula_vi="Tổng chi phí = phí giao dịch + phí tài trợ (funding) + chi phí trượt giá.",
        reading_vi=(
            "Lãi/lỗ ròng = lãi/lỗ gộp − tổng chi phí. Chi phí càng lớn thì lãi ròng càng nhỏ."
        ),
        limits_vi=(
            "Báo cáo dừng với lỗi toàn vẹn nếu tổng này không khớp ba khoản thành phần. Chi phí "
            "do mô hình PAPER mô phỏng, không phải chi phí sàn thực thu."
        ),
        glossary_anchor=_G_REPORT,
    ),
    "holding_period": MetricHelp(
        title_vi="Thời gian nắm giữ",
        definition_vi="Khoảng thời gian từ lúc ledger ghi mở giao dịch đến lúc ghi đóng.",
        formula_vi="",
        reading_vi="Hai mốc hiển thị theo múi giờ Asia/Ho_Chi_Minh.",
        limits_vi="Chỉ có ở giao dịch đã đóng.",
        glossary_anchor=_G_WP06,
    ),
    "entry_price_quantity": MetricHelp(
        title_vi="Giá vào và khối lượng",
        definition_vi=(
            "Giá vào bình quân và tổng khối lượng của các lần khớp vào lệnh trong ledger."
        ),
        formula_vi=(
            "Giá vào = tổng (khối lượng × giá) ÷ tổng khối lượng của các lần khớp vào lệnh; khối "
            "lượng = tổng khối lượng các lần khớp đó."
        ),
        reading_vi=(
            "Hiển thị dạng giá × khối lượng; tích của hai số là giá trị danh nghĩa vào lệnh."
        ),
        limits_vi=(
            "Khi ledger không lưu khối lượng, chỉ hiện giá của lần khớp vào đầu tiên. Thiếu cả "
            "hai thì ghi Ledger chưa lưu."
        ),
    ),
    "reference_price_signal_time": MetricHelp(
        title_vi="Giá tham chiếu và thời điểm tín hiệu",
        definition_vi=(
            "Giá tham chiếu và thời điểm mà nguồn sinh tín hiệu ghi lại khi đề xuất vào lệnh."
        ),
        formula_vi="",
        reading_vi=(
            "So giá tham chiếu với giá vào thực tế để thấy độ lệch giữa tín hiệu và lần khớp."
        ),
        limits_vi="Chỉ có khi lý do vào lệnh trong ledger lưu hai trường này; không tự suy diễn.",
    ),
    "leverage": MetricHelp(
        title_vi="Đòn bẩy và rủi ro tài khoản",
        definition_vi=(
            "Đòn bẩy là tỷ lệ giữa giá trị danh nghĩa của vị thế và ký quỹ. Rủi ro tài khoản là "
            "phần trăm tài khoản mà nguồn tín hiệu ghi là chấp nhận mất cho lệnh này."
        ),
        formula_vi=(
            "Đòn bẩy lấy từ kế hoạch đã được cổng rủi ro duyệt; nếu không có thì lấy từ lý do "
            "vào lệnh. Hệ thống không tự tính lại."
        ),
        reading_vi="Đòn bẩy cao hơn làm lãi và lỗ trên ký quỹ đều lớn hơn.",
        limits_vi=(
            "Đòn bẩy chỉ nhận số nguyên dương; thiếu nguồn thì ghi Ledger chưa lưu. Đòn bẩy "
            "không làm thay đổi ngân sách rủi ro của lệnh."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "layer_l4_report": MetricHelp(
        title_vi="L4 Cổng bối cảnh (Context Gate)",
        definition_vi=(
            "Kết quả của tầng L4 khi vào lệnh: bối cảnh thị trường có đạt điều kiện để giao dịch "
            "hay không, kèm chế độ thị trường mà nguồn tín hiệu ghi."
        ),
        formula_vi="",
        reading_vi="ĐẠT nghĩa là cổng cho phép xét tín hiệu; KHÔNG ĐẠT nghĩa là cổng đã chặn.",
        limits_vi=(
            "Chỉ có khi nguồn sinh tín hiệu thật sự ghi chế độ thị trường vào lý do vào lệnh; "
            "ledger PAPER hiện thường không lưu nên hiện Ledger chưa lưu."
        ),
        glossary_anchor=_G_QUANT,
    ),
    "layer_l5_report": MetricHelp(
        title_vi="L5 Điểm kích hoạt (Trigger Score)",
        definition_vi=(
            "Điểm của tầng L5 khi vào lệnh: điểm nến rút chân và điểm phá vỡ do nguồn tín hiệu "
            "ghi, thang 0 đến 100."
        ),
        formula_vi="",
        reading_vi="Điểm cao hơn nghĩa là mẫu hình rõ hơn theo cách chấm của nguồn tín hiệu.",
        limits_vi=(
            "Trang chỉ hiển thị lại điểm đã ghi, không tự chấm; chú giải này chưa kiểm chứng "
            "công thức chấm điểm của từng chiến lược. Thiếu nguồn thì hiện Ledger chưa lưu."
        ),
        glossary_anchor=_G_QUANT,
    ),
    "risk_reward_ratio": MetricHelp(
        title_vi="L6 Tỷ lệ R:R và kế hoạch vùng giá",
        definition_vi=(
            "Tỷ lệ lợi nhuận trên rủi ro (R:R) của kế hoạch cùng giá dừng lỗ (SL) và giá chốt lời "
            "(TP) đã lên trước khi vào lệnh."
        ),
        formula_vi=(
            "R:R = |giá chốt lời − giá vào| ÷ |giá vào − giá dừng lỗ| theo kế hoạch đã duyệt; hiển "
            "thị dạng 1 : R:R."
        ),
        reading_vi=(
            "R:R bằng 2 nghĩa là mức lãi mục tiêu gấp đôi mức lỗ chấp nhận, tính theo khoảng giá."
        ),
        limits_vi=(
            "Tính trên giá kế hoạch, chưa trừ phí và trượt giá. Không có kế hoạch đã duyệt thì "
            "dùng giá trị nguồn tín hiệu ghi, hoặc hiện Ledger chưa lưu."
        ),
        glossary_anchor=_G_RISK,
    ),
    "stop_loss": MetricHelp(
        title_vi="Dừng lỗ (SL)",
        definition_vi="Mức giá kế hoạch mà tại đó vị thế được đóng để giới hạn khoản lỗ.",
        formula_vi="Lấy từ giá dừng của kế hoạch đã duyệt; nếu không có thì từ lý do vào lệnh.",
        reading_vi="Với LONG, dừng lỗ nằm dưới giá vào; với SHORT, nằm trên giá vào.",
        limits_vi=(
            "Giá đóng thực tế có thể xấu hơn mức dừng lỗ do trượt giá. Thiếu nguồn thì không "
            "hiển thị."
        ),
        glossary_anchor=_G_RISK,
    ),
    "take_profit": MetricHelp(
        title_vi="Chốt lời (TP)",
        definition_vi="Mức giá kế hoạch mà tại đó vị thế được đóng để hiện thực hóa lợi nhuận.",
        formula_vi="Lấy từ giá mục tiêu của kế hoạch đã duyệt; nếu không có thì từ lý do vào lệnh.",
        reading_vi="Với LONG, chốt lời nằm trên giá vào; với SHORT, nằm dưới giá vào.",
        limits_vi="Một số kế hoạch không có mức chốt lời cố định; khi đó trang ghi rõ điều này.",
        glossary_anchor=_G_RISK,
    ),
    "planned_risk_budget": MetricHelp(
        title_vi="Ngân sách rủi ro kế hoạch",
        definition_vi=(
            "Số tiền tối đa kế hoạch chấp nhận mất cho lệnh này, đã được cổng rủi ro duyệt trước "
            "khi vào lệnh."
        ),
        formula_vi=(
            "Lấy từ ngân sách rủi ro của kế hoạch đã duyệt; hệ thống không tự đặt giá trị mặc định."
        ),
        reading_vi="Đây là 1R của lệnh: kết quả lãi/lỗ được quy về bội số của số tiền này.",
        limits_vi=(
            "Thiếu kế hoạch đã duyệt thì hiện Ledger chưa lưu và quy tắc lỗ vượt kế hoạch không "
            "kiểm được."
        ),
        glossary_anchor=_G_RISK,
    ),
    "r_multiple": MetricHelp(
        title_vi="Bội số R",
        definition_vi="Kết quả của lệnh quy đổi theo ngân sách rủi ro ban đầu (R).",
        formula_vi="Bội số R = lãi/lỗ ròng ÷ ngân sách rủi ro kế hoạch.",
        reading_vi=(
            "+2R nghĩa là lãi ròng gấp đôi mức rủi ro đã chấp nhận; −1R nghĩa là lỗ đúng bằng "
            "ngân sách rủi ro."
        ),
        limits_vi=(
            "Không tính được khi thiếu ngân sách rủi ro kế hoạch. Lỗ vượt −"
            f"{_EXCESSIVE_LOSS}R bị gắn nhãn sai lệch."
        ),
        glossary_anchor=_G_RISK,
    ),
    "trade_slippage": MetricHelp(
        title_vi="Trượt giá thực tế (Slippage)",
        definition_vi=(
            "Chi phí trượt giá ledger ghi cho giao dịch, kèm tỷ lệ theo điểm cơ bản (bps) so với "
            "giá trị danh nghĩa vào lệnh."
        ),
        formula_vi=(
            "Trượt giá (bps) = chi phí trượt giá ÷ (tổng khối lượng × giá của các lần khớp vào "
            "lệnh) × 10 000. Một điểm cơ bản bằng 0,01%."
        ),
        reading_vi=(
            "Mức kế hoạch dự trù = 2 × chi phí trượt giá vào lệnh trong kế hoạch (cho cả vào và "
            f"ra). Thực tế lớn hơn {_SLIPPAGE_MULTIPLE} lần mức dự trù bị gắn nhãn sai lệch."
        ),
        limits_vi=(
            "Là trượt giá do mô hình PAPER mô phỏng, không đo trên sổ lệnh của sàn. Không tính "
            "được bps khi thiếu lần khớp vào lệnh."
        ),
        glossary_anchor=_G_UI,
    ),
    "trade_fees_funding": MetricHelp(
        title_vi="Phí giao dịch và phí tài trợ (Funding)",
        definition_vi=(
            "Phí giao dịch và khoản funding ledger ghi cho riêng giao dịch này. Funding là khoản "
            "thanh toán định kỳ giữa bên mua và bên bán của hợp đồng tương lai vĩnh cửu."
        ),
        formula_vi="Hai khoản này cộng với chi phí trượt giá thành tổng chi phí thực thi.",
        reading_vi="Số dương là chi phí và làm giảm lãi/lỗ ròng.",
        limits_vi=(
            "Do mô hình PAPER mô phỏng theo hồ sơ thực thi; chú giải này chưa kiểm chứng cách "
            "tính từng kỳ funding."
        ),
        glossary_anchor=_G_FUTURES,
    ),
    "trade_cost_slippage": MetricHelp(
        title_vi="Chi phí / trượt giá",
        definition_vi=(
            "Tổng chi phí của giao dịch và riêng phần trượt giá trong tổng đó, theo tài sản định "
            "giá."
        ),
        formula_vi="Tổng chi phí = phí giao dịch + phí tài trợ + chi phí trượt giá.",
        reading_vi="Số trong ngoặc (Trượt) là phần trượt giá, đã nằm trong tổng chi phí.",
        limits_vi="Chi phí do mô hình PAPER mô phỏng, không phải chi phí sàn thực thu.",
        glossary_anchor=_G_REPORT,
    ),
    "exit_reason": MetricHelp(
        title_vi="Lý do đóng",
        definition_vi=(
            "Nguyên nhân vị thế được đóng mà ledger ghi, ví dụ chốt lời, dừng lỗ, đóng theo thời "
            "gian, tín hiệu ngược chiều hoặc đóng theo thao tác quản trị."
        ),
        formula_vi="",
        reading_vi=(
            "Dừng lỗ (SL): giá chạm mức giới hạn lỗ. Chốt lời (TP): giá chạm mục tiêu. Đóng theo "
            "thời gian: hết thời gian giữ lệnh cho phép."
        ),
        limits_vi=(
            "Chỉ có khi sự kiện đóng vị thế lưu lý do; mã chưa được dịch sẽ hiển thị nguyên văn."
        ),
        glossary_anchor=_G_RISK,
    ),
    "execution_quality": MetricHelp(
        title_vi="Đánh giá thực thi",
        definition_vi=(
            "Nhãn hậu kiểm tự động cho một giao dịch đã đóng: Tối ưu, Chấp nhận, Sai lệch hoặc "
            "Chưa đủ dữ liệu."
        ),
        formula_vi=_QUALITY_RULES,
        reading_vi=(
            "Không vi phạm quy tắc nào kiểm được: lỗ ròng thì Chấp nhận, còn lại là Tối ưu. Quy "
            "tắc thiếu dữ liệu được liệt kê ở mục Chưa kiểm được."
        ),
        limits_vi=_QUALITY_LIMIT,
        glossary_anchor=_G_POST_MORTEM,
    ),
    "quality_optimal": MetricHelp(
        title_vi="Tối ưu",
        definition_vi=(
            "Giao dịch không lỗ ròng và không vi phạm quy tắc sai lệch nào trong số các quy tắc "
            "kiểm được."
        ),
        formula_vi=_QUALITY_RULES,
        reading_vi=(
            "Tối ưu nói về việc thực thi đúng kế hoạch, không khẳng định đây là lệnh tốt "
            "nhất có thể."
        ),
        limits_vi=(
            "Quy tắc thiếu dữ liệu đầu vào bị bỏ qua, nên nhãn này có thể dựa trên ít hơn ba quy "
            "tắc. " + _QUALITY_LIMIT
        ),
        glossary_anchor=_G_POST_MORTEM,
    ),
    "quality_acceptable": MetricHelp(
        title_vi="Chấp nhận",
        definition_vi=(
            "Giao dịch lỗ ròng nhưng không vi phạm quy tắc sai lệch nào trong số các quy tắc kiểm "
            "được."
        ),
        formula_vi=_QUALITY_RULES,
        reading_vi="Khoản lỗ nằm trong phạm vi kế hoạch theo những gì kiểm được.",
        limits_vi=(
            "Quy tắc thiếu dữ liệu đầu vào bị bỏ qua, nên nhãn này có thể dựa trên ít hơn ba quy "
            "tắc. " + _QUALITY_LIMIT
        ),
        glossary_anchor=_G_POST_MORTEM,
    ),
    "quality_deviant": MetricHelp(
        title_vi="Sai lệch",
        definition_vi="Giao dịch vi phạm ít nhất một quy tắc sai lệch; lý do cụ thể được ghi kèm.",
        formula_vi=_QUALITY_RULES,
        reading_vi=(
            "Đây là ca cần người rà soát xem lại; có thể đưa vào dữ liệu rà soát cho huấn luyện AI."
        ),
        limits_vi=_QUALITY_LIMIT,
        glossary_anchor=_G_POST_MORTEM,
    ),
    "quality_unverified": MetricHelp(
        title_vi="Chưa đủ dữ liệu",
        definition_vi=(
            "Không kết luận được chất lượng thực thi vì thiếu lãi/lỗ ròng hoặc không quy tắc nào "
            "có đủ dữ liệu để kiểm."
        ),
        formula_vi=_QUALITY_RULES,
        reading_vi="Cần người rà soát xem xét; nhãn này không có nghĩa là lệnh tốt hay xấu.",
        limits_vi=(
            "Thường gặp khi ledger không lưu kế hoạch đã duyệt, lý do đóng hoặc ngân sách rủi ro "
            "của lệnh."
        ),
        glossary_anchor=_G_POST_MORTEM,
    ),
    "daily_pnl_calendar": MetricHelp(
        title_vi="Lịch lãi/lỗ theo ngày",
        definition_vi="Tổng lãi/lỗ ròng đã thực hiện của các giao dịch đóng trong từng ngày.",
        formula_vi=(
            f"Gom giao dịch theo ngày đóng tính theo múi giờ {REPORT_TIMEZONE}, tách riêng từng "
            "tài sản định giá, rồi cộng lãi/lỗ ròng."
        ),
        reading_vi="Mỗi thẻ là một ngày có giao dịch đã đóng, kèm số lệnh lãi, lỗ và hòa vốn.",
        limits_vi=(
            "Ngày không có giao dịch đã đóng không được hiển thị và không được coi là ngày hòa "
            "vốn. Không gồm lãi/lỗ chưa chốt."
        ),
        glossary_anchor=_G_WP06,
    ),
    "daily_trade_outcomes": MetricHelp(
        title_vi="Có lãi / thua lỗ / hòa vốn",
        definition_vi="Số giao dịch đóng trong ngày chia theo dấu của lãi/lỗ ròng.",
        formula_vi="Có lãi: PnL ròng lớn hơn 0; thua lỗ: nhỏ hơn 0; hòa vốn: đúng bằng 0.",
        reading_vi="Tổng ba số bằng số giao dịch đã đóng của ngày đó.",
        limits_vi="Xét sau chi phí, nên một lệnh lãi gộp vẫn có thể được đếm là thua lỗ.",
        glossary_anchor=_G_WP06,
    ),
}

# Nguồn đối chiếu: `static/trade-chart.js`, `adapters/database/repositories/bot_reports.py`
# (`load_trade_bars`: nến đã đóng, khả dụng tại thời điểm báo cáo) và thư viện Lightweight
# Charts 5.2.1 (nến tăng khi giá mở ≤ giá đóng).
_CHART: dict[str, MetricHelp] = {
    "candle_ohlc": MetricHelp(
        title_vi="Nến Mở / Cao / Thấp / Đóng",
        definition_vi=(
            "Mỗi nến tóm tắt giá trong một khung thời gian: giá mở, giá cao nhất, giá thấp nhất "
            "và giá đóng."
        ),
        formula_vi=(
            "Thân nến nối giá mở và giá đóng; râu nến chạm giá cao nhất và thấp nhất. % thay đổi "
            "= (giá đóng − giá mở) ÷ giá mở × 100."
        ),
        reading_vi=(
            "Nến xanh khi giá đóng không thấp hơn giá mở, nến đỏ khi giá đóng thấp hơn giá mở. "
            "Rê chuột hoặc chạm vào nến để đọc bốn mức giá."
        ),
        limits_vi=(
            "Nến không cho biết thứ tự giá diễn ra bên trong khung thời gian, nên không suy ra "
            "được giá chạm dừng lỗ hay chốt lời trước."
        ),
        glossary_anchor=_G_PRICE,
    ),
    "candle_volume": MetricHelp(
        title_vi="Khối lượng",
        definition_vi="Khối lượng giao dịch của nến do kho nến nội bộ lưu.",
        formula_vi="",
        reading_vi="Khối lượng lớn cho biết khung thời gian đó có nhiều giao dịch hơn.",
        limits_vi=(
            "Hiển thị đúng giá trị kho nến lưu; chú giải này chưa kiểm chứng đơn vị của nguồn "
            "(thường là tài sản cơ sở). Chỉ hiện khi nến có trường khối lượng."
        ),
        glossary_anchor=_G_PRICE,
    ),
    "chart_timeframe": MetricHelp(
        title_vi="Khung thời gian",
        definition_vi=(
            "Độ dài của mỗi nến trên biểu đồ, ví dụ 5m là 5 phút, 1h là 1 giờ, 1d là 1 ngày."
        ),
        formula_vi="",
        reading_vi=(
            "Khung nhỏ cho thấy chi tiết quanh lúc vào và đóng lệnh; khung lớn cho thấy bối cảnh "
            "rộng hơn."
        ),
        limits_vi=(
            "Mốc vào và đóng lệnh được gắn vào nến gần thời điểm khớp nhất, nên ở khung lớn vị "
            "trí mốc kém chính xác hơn."
        ),
        glossary_anchor=_G_PRICE,
    ),
    "chart_entry_marker": MetricHelp(
        title_vi="Mũi tên vào lệnh",
        definition_vi="Đánh dấu nến gần nhất với thời điểm một lần khớp vào lệnh của giao dịch.",
        formula_vi=(
            "LONG: mũi tên xanh hướng lên, đặt dưới nến. SHORT: mũi tên đỏ hướng xuống, đặt trên "
            "nến. Nhãn ghi hướng và giá khớp."
        ),
        reading_vi=(
            "Rê chuột hoặc chạm vào nến có mốc để đọc thời điểm, giá, chiến thuật và tín hiệu."
        ),
        limits_vi=(
            "Mốc đặt theo nến gần nhất chứ không đúng từng giây; giá khớp là giá mô hình PAPER ghi."
        ),
        glossary_anchor=_G_UI,
    ),
    "chart_exit_marker": MetricHelp(
        title_vi="Mốc đóng lệnh",
        definition_vi="Đánh dấu nến gần nhất với thời điểm một lần khớp đóng hoặc giảm vị thế.",
        formula_vi=(
            "Mũi tên ngược chiều lệnh. Nhãn gồm mã lý do rút gọn (ví dụ SL: dừng lỗ, TP: chốt lời, "
            "TIME: đóng theo thời gian, ĐẢO: tín hiệu ngược chiều; ĐÓNG cho lý do khác), giá khớp "
            "và lãi/lỗ ròng."
        ),
        reading_vi=(
            "Màu xanh khi giao dịch lãi ròng, đỏ khi lỗ ròng, xám khi hòa vốn; mốc giảm vị thế "
            "một phần có màu xanh dương."
        ),
        limits_vi="Mốc đặt theo nến gần nhất chứ không đúng từng giây.",
        glossary_anchor=_G_UI,
    ),
    "chart_stop_line": MetricHelp(
        title_vi="Đường dừng lỗ (SL)",
        definition_vi="Đường ngang nét đứt màu đỏ tại giá dừng lỗ của kế hoạch.",
        formula_vi="",
        reading_vi="Giá chạm đường này là điều kiện đóng lệnh để giới hạn lỗ theo kế hoạch.",
        limits_vi="Chỉ vẽ khi giao dịch có giá dừng lỗ từ kế hoạch đã duyệt hoặc lý do vào lệnh.",
        glossary_anchor=_G_RISK,
    ),
    "chart_target_line": MetricHelp(
        title_vi="Đường chốt lời (TP)",
        definition_vi="Đường ngang nét đứt màu xanh tại giá chốt lời của kế hoạch.",
        formula_vi="",
        reading_vi=(
            "Giá chạm đường này là điều kiện đóng lệnh để hiện thực hóa lợi nhuận theo kế hoạch."
        ),
        limits_vi=(
            "Chỉ vẽ khi giao dịch có giá chốt lời; một số kế hoạch không có mục tiêu cố định nên "
            "không có đường này."
        ),
        glossary_anchor=_G_RISK,
    ),
    "chart_entry_line": MetricHelp(
        title_vi="Đường giá vào bình quân",
        definition_vi="Đường ngang nét chấm tại giá vào bình quân gia quyền của giao dịch.",
        formula_vi="",
        reading_vi=(
            "So vị trí đường này với đường dừng lỗ và chốt lời để thấy khoảng rủi ro và mục tiêu."
        ),
        limits_vi="Chỉ vẽ khi hậu kiểm tính được giá vào từ các lần khớp trong ledger.",
        glossary_anchor=_G_UI,
    ),
    "chart_candle_source": MetricHelp(
        title_vi="Nguồn nến nội bộ",
        definition_vi=(
            "Nến lấy từ kho nến nội bộ của hệ thống: chỉ gồm nến đã đóng và đã khả dụng tại thời "
            "điểm dựng báo cáo."
        ),
        formula_vi="",
        reading_vi=(
            "Biểu đồ bao quanh khoảng thời gian của giao dịch, có thêm một số nến trước và sau."
        ),
        limits_vi=(
            "Không phải dữ liệu trực tiếp từ sàn; nếu kho nến thiếu dữ liệu cho giai đoạn này thì "
            "biểu đồ không được vẽ."
        ),
        glossary_anchor=_G_PRICE,
    ),
}

# Nguồn đối chiếu: `domain/strategy_card/models.py` (`CompositionLayer`) và mục R của glossary.
_LAYERS: dict[str, MetricHelp] = {
    "layer_l0_data_hygiene": MetricHelp(
        title_vi="Tầng L0 — Độ sạch dữ liệu (Data Hygiene)",
        definition_vi="Tầng kiểm tra dữ liệu nến đầu vào trước khi tính toán.",
        formula_vi="",
        reading_vi="Nến không hợp lệ làm dừng quy trình ngay tại tầng này.",
        limits_vi="Chỉ kiểm tra tính hợp lệ của dữ liệu, không đánh giá cơ hội giao dịch.",
        glossary_anchor=_G_QUANT,
    ),
    "layer_l1_candle_anatomy": MetricHelp(
        title_vi="Tầng L1 — Giải phẫu nến (Candle Anatomy)",
        definition_vi="Tầng đo hình dạng của từng nến: tỷ lệ râu, tỷ lệ thân và vị trí giá đóng.",
        formula_vi="",
        reading_vi="Kết quả là các đại lượng hình học làm đầu vào cho những tầng sau.",
        limits_vi="Chỉ mô tả một nến, chưa xét xu hướng hay bối cảnh thị trường.",
        glossary_anchor=_G_QUANT,
    ),
    "layer_l2_indicators": MetricHelp(
        title_vi="Tầng L2 — Chỉ báo kỹ thuật (Indicators)",
        definition_vi=(
            "Tầng tính các chỉ báo kỹ thuật từ chuỗi nến, ví dụ đường trung bình hay độ biến động."
        ),
        formula_vi="",
        reading_vi="Mỗi nút là một chỉ báo mà Strategy Card khai báo.",
        limits_vi="Chỉ báo dựa trên dữ liệu quá khứ nên luôn có độ trễ.",
        glossary_anchor=_G_QUANT,
    ),
    "layer_l3_algorithms": MetricHelp(
        title_vi="Tầng L3 — Thuật toán điểm số (Algorithms)",
        definition_vi="Tầng chuẩn hóa và kết hợp kết quả các tầng trước thành điểm số trung gian.",
        formula_vi="",
        reading_vi="Số nút bằng 0 nghĩa là chiến lược không dùng tầng này.",
        limits_vi="Điểm số chỉ có nghĩa trong phạm vi chiến lược đã khai báo nó.",
        glossary_anchor=_G_QUANT,
    ),
    "layer_l4_context_gate": MetricHelp(
        title_vi="Tầng L4 — Cổng bối cảnh thị trường (Context Gate)",
        definition_vi="Tầng quyết định bối cảnh thị trường có phù hợp để xét tín hiệu hay không.",
        formula_vi="",
        reading_vi="Nếu cổng chặn (BLOCK), quy trình ngắt mạch: tầng L5 và L6 không được chạy.",
        limits_vi="Cổng chỉ lọc bối cảnh, không tự tạo tín hiệu vào lệnh.",
        glossary_anchor=_G_QUANT,
    ),
    "layer_l5_trigger": MetricHelp(
        title_vi="Tầng L5 — Bộ kích hoạt chiến lược (Strategy Trigger)",
        definition_vi=(
            "Tầng phát tín hiệu vào lệnh LONG hoặc SHORT khi điều kiện của chiến lược được thỏa."
        ),
        formula_vi="",
        reading_vi="Chỉ được đánh giá sau khi cổng bối cảnh L4 cho qua (PASS).",
        limits_vi=(
            "Tín hiệu chưa phải là lệnh: còn phải qua lập kế hoạch và cổng rủi ro trước khi vào "
            "PAPER."
        ),
        glossary_anchor=_G_QUANT,
    ),
    "layer_l6_zones": MetricHelp(
        title_vi="Tầng L6 — Vùng thực thi (Zones)",
        definition_vi=(
            "Tầng xác định vùng giá của lệnh: điểm vào, dừng lỗ ban đầu và mục tiêu chốt lời."
        ),
        formula_vi="",
        reading_vi="Kết quả được đóng gói thành ứng viên giao dịch cho bộ lập kế hoạch.",
        limits_vi="Vùng giá là đề xuất của chiến lược; kế hoạch cuối cùng do cổng rủi ro duyệt.",
        glossary_anchor=_G_QUANT,
    ),
}

# Nguồn đối chiếu: `domain/learning/paper_performance.py` (coverage, brier_regression,
# log_loss_regression) và mục V của glossary.
_PAPER_PERFORMANCE: dict[str, MetricHelp] = {
    "paper_covered_outcomes": MetricHelp(
        title_vi="Outcome đóng",
        definition_vi=(
            "Số ý định PAPER trong cohort đã được quy kết với đúng một giao dịch đã đóng."
        ),
        formula_vi="",
        reading_vi="Đây là cỡ mẫu dùng để tính điểm Brier và log-loss quan sát.",
        limits_vi=(
            "Chỉ gồm quyết định đã vào lệnh, nên mẫu có thiên lệch chọn mẫu (selection bias)."
        ),
        glossary_anchor=_G_WP06,
    ),
    "paper_eligible_decisions": MetricHelp(
        title_vi="Intent trưởng thành",
        definition_vi="Số ý định PAPER trong cohort đã đủ thời gian quan sát để được xét kết quả.",
        formula_vi="",
        reading_vi="Là mẫu số của độ phủ.",
        limits_vi="Ý định chưa đủ thời gian quan sát không được tính.",
        glossary_anchor=_G_WP06,
    ),
    "paper_coverage": MetricHelp(
        title_vi="Độ phủ",
        definition_vi="Tỷ lệ ý định trưởng thành đã có kết quả giao dịch đóng được quy kết.",
        formula_vi=(
            "Độ phủ = số outcome đóng ÷ số intent trưởng thành; bằng 0 khi chưa có intent nào."
        ),
        reading_vi="Độ phủ thấp hơn ngưỡng policy thì cohort chưa đủ cơ sở để kết luận.",
        limits_vi=(
            "Độ phủ cao không chứng minh mô hình dự báo tốt; nó chỉ cho biết mẫu đầy đủ tới đâu."
        ),
        glossary_anchor=_G_WP06,
    ),
    "paper_brier": MetricHelp(
        title_vi="Điểm Brier quan sát / TEST",
        definition_vi=(
            "Sai số bình phương của xác suất dự báo so với kết quả thực tế: giá trị quan sát trên "
            "PAPER và giá trị nền của tập TEST."
        ),
        formula_vi="",
        reading_vi="Càng thấp càng tốt. So giá trị quan sát với giá trị TEST để thấy mức suy giảm.",
        limits_vi=(
            "Nhãn là dấu PnL của giao dịch PAPER đã đóng; chỉ số không đo lợi nhuận. Hiện — khi "
            "cohort chưa có outcome."
        ),
        glossary_anchor=_G_WP06,
    ),
    "paper_brier_delta": MetricHelp(
        title_vi="Δ Brier",
        definition_vi="Chênh lệch điểm Brier giữa PAPER và tập TEST.",
        formula_vi="Δ Brier = Brier quan sát − Brier TEST.",
        reading_vi="Dương nghĩa là dự báo trên PAPER kém hơn trên TEST.",
        limits_vi=(
            "Chỉ bị coi là suy giảm khi cả Δ Brier và Δ log-loss cùng vượt ngưỡng policy; mẫu "
            "nhỏ cho chênh lệch nhiễu."
        ),
        glossary_anchor=_G_WP06,
    ),
    "paper_log_loss": MetricHelp(
        title_vi="Log-loss quan sát / TEST",
        definition_vi=(
            "Mức phạt logarit cho xác suất gán cho kết quả thực tế: giá trị quan sát trên PAPER "
            "và giá trị nền của tập TEST."
        ),
        formula_vi="",
        reading_vi=(
            "Càng thấp càng tốt; phạt rất nặng khi mô hình gán xác suất thấp cho kết quả đã xảy ra."
        ),
        limits_vi=(
            "Nhãn là dấu PnL của giao dịch PAPER đã đóng; chỉ số không đo lợi nhuận. Hiện — khi "
            "cohort chưa có outcome."
        ),
        glossary_anchor=_G_WP06,
    ),
    "paper_log_loss_delta": MetricHelp(
        title_vi="Δ Log-loss",
        definition_vi="Chênh lệch log-loss giữa PAPER và tập TEST.",
        formula_vi="Δ log-loss = log-loss quan sát − log-loss TEST.",
        reading_vi="Dương nghĩa là dự báo trên PAPER kém hơn trên TEST.",
        limits_vi=(
            "Chỉ bị coi là suy giảm khi cả Δ Brier và Δ log-loss cùng vượt ngưỡng policy; mẫu "
            "nhỏ cho chênh lệch nhiễu."
        ),
        glossary_anchor=_G_WP06,
    ),
}

HELP: dict[str, MetricHelp] = {
    **_REPORT_METRICS,
    **_DASHBOARD_AND_BOT,
    **_REPORT_PAGE,
    **_CHART,
    **_LAYERS,
    **_PAPER_PERFORMANCE,
}

_DOM_ID_COUNTER = count(1)


def metric_help(key: str) -> MetricHelp:
    """Tra chú giải theo khóa; khóa lạ là lỗi lập trình nên ném `KeyError`."""
    return HELP[key]


def metric_help_or_none(key: object) -> MetricHelp | None:
    """Bản dùng trong template: khóa lạ trả `None` để trang vẫn hiển thị nhãn, không vỡ."""
    return HELP.get(str(key))


def metric_help_title(key: object) -> str:
    """Tên hiển thị của chỉ số; chuỗi rỗng khi khóa không có trong từ điển."""
    entry = HELP.get(str(key))
    return entry.title_vi if entry is not None else ""


def metric_help_dom_id() -> str:
    """Cấp id DOM không trùng trong một tiến trình để nối nút với tooltip qua `aria-describedby`."""
    return f"metric-help-{next(_DOM_ID_COUNTER)}"
