# Thẻ ảnh Telegram (TG-3, v1, 15/11/2026)

Thiết kế ba loại thẻ ảnh PNG gửi qua Telegram thay cho tin chữ thuần: **thẻ tín hiệu tham khảo**, **thẻ vị thế PAPER** (mở/đóng) và **thẻ tổng kết nhiều bot**. Phong cách là bản riêng của Crypidian: không dùng logo, màu thương hiệu hay bố cục sao chép từ sàn/ứng dụng nào, không giả dạng sàn.

- View-model (dữ liệu thuần): [`application/telegram_cards.py`](../../src/trading_bot/application/telegram_cards.py)
- Renderer Pillow: [`adapters/telegram/card_renderer.py`](../../src/trading_bot/adapters/telegram/card_renderer.py)
- Gửi ảnh: `TelegramMessageSender.send_photo` ([port](../../src/trading_bot/ports/telegram.py), [Bot API](../../src/trading_bot/adapters/telegram/bot_api_sender.py))
- Ảnh mẫu (DỮ LIỆU MẪU): [`docs/design/telegram-cards/`](telegram-cards/), dựng lại bằng `PYTHONPATH=. uv run python scripts/render_telegram_card_samples.py`

## 1. Khảo sát tham khảo (truy cập 15/11/2026)

| Nguồn | Điều rút ra |
| --- | --- |
| [Cornix — Signal posting format](https://help.cornix.io/en/articles/11659507-signal-posting-format) và [Signal posting](https://help.cornix.io/en/articles/5814956-signal-posting) | Tin tín hiệu phổ biến trên Telegram đi theo **thứ tự khối cố định**: cặp → sàn/loại → hướng → đòn bẩy → vùng vào → các mục tiêu đánh số → một SL. SL phải nằm đúng phía so với vùng vào. Người đọc quen thứ tự này nên thẻ giữ nhịp tương tự (hướng → vùng vào → SL → mục tiêu). |
| [Bybit — Trade Summary Card](https://www.bybit.com/en/help-center/article/How-to-Generate-your-Trade-Summary-Card) (trang tải chậm, chỉ đọc được qua kết quả tìm kiếm) và [Bybit — P&L calculation](https://www.bybit.com/en/help-center/article/FAQ-PL-Calculation) | Thẻ chia sẻ kết quả lấy **một con số PnL lớn** làm trung tâm; "Closed P&L" là PnL sau phí mở/đóng và funding. Crypidian ghi rõ "Lãi/lỗ ròng (sau phí)". |
| [Tealstreet — PnL cards](https://docs.tealstreet.io/docs/trade/pnl-cards) | Thẻ PnL gồm đòn bẩy, phía, giá vào, giá ra, giá hiện tại và PnL (ROE/ROI hoặc tiền); xuất PNG. Thẻ của dự án giữ các trường này, không cần sticker/nền ảnh. |
| [Telegram Bot API — sendPhoto](https://core.telegram.org/bots/api#sendphoto) | Ảnh ≤ 10 MB, rộng + cao ≤ 10000, tỉ lệ cạnh ≤ 20; caption 0–1024 ký tự; tải lên bằng `multipart/form-data`. |
| [Be Vietnam Pro (Google Fonts, OFL 1.1)](https://github.com/google/fonts/tree/main/ofl/bevietnampro) · [SIL OFL](https://openfontlicense.org/) | Font sans-serif thiết kế cho tiếng Việt, đủ dấu chồng (ặ, ổ, ữ…). OFL cho phép đóng gói kèm phần mềm khi giữ nguyên tệp giấy phép; dự án không đổi tên/sửa font. Admin Web đã dùng cùng font (woff2) nên thẻ đồng bộ nhận diện. |

## 2. Mẫu thiết kế áp dụng

1. **Nền tối, phân cấp rõ** (thói quen của app giao dịch): một con số chính lớn nhất trên mỗi thẻ (mã + hướng, PnL ròng, hoặc PnL tổng), phần còn lại nhỏ dần; nhãn xám, giá trị trắng.
2. **Mã màu theo ý nghĩa, không theo trang trí:** xanh = LONG/lãi/đạt, đỏ = SHORT/lỗ/không đạt/SL, xám = chưa xác định; quầng màu đầu thẻ theo hướng/kết quả để nhận ra ngay trong danh sách chat. Luôn kèm dấu (+/−), mũi tên hoặc chữ để không phụ thuộc hoàn toàn vào màu.
3. **Thang giá dọc (price ladder):** sắp theo giá như trục biểu đồ (cao ở trên). Đoạn ray phía SL màu đỏ, phía mục tiêu màu xanh; vùng vào được tô nền; mỗi mốc có chip R và % cách vùng vào. Thẻ vị thế dùng thanh ngang SL — vào — TP tỉ lệ theo giá, thẻ đóng có điểm "Ra".
4. **Checklist gọn:** biểu tượng ✓/✗/— (vẽ bằng nét vì font không có glyph) + nhãn + giá trị thực (tô màu) + "cần ≥ ngưỡng"; thanh tiến độ x/y đạt ở đầu khối.
5. **Cảnh báo PAPER luôn hiện:** dải viền vàng "TÍN HIỆU THAM KHẢO — KHÔNG PHẢI LỆNH" / "KẾT QUẢ PAPER — KHÔNG PHẢI LỆNH THẬT" / "TỔNG KẾT PAPER — KHÔNG PHẢI KẾT QUẢ THẬT", chip PAPER, chân thẻ nhắc không phải khuyến nghị đầu tư; ảnh mẫu thêm chip tím "DỮ LIỆU MẪU". Độ tin cậy ghi "Chưa hiệu chuẩn" (không hiện %).

## 3. Token

| Nhóm | Giá trị |
| --- | --- |
| Nền / mặt khối / mặt phụ / viền | `#0B0F17` / `#131A26` / `#192232` / `#263247` |
| Chữ / phụ / mờ | `#E8EDF5` / `#8A97AD` / `#5C6A82` |
| Nhấn (thương hiệu, vùng vào) | `#7C9CFF` |
| Tăng–lãi–LONG / Giảm–lỗ–SHORT | `#22C38E` / `#F0506E` |
| Cảnh báo PAPER / dữ liệu mẫu | `#F5B841` / `#C77DFF` |
| Font | Be Vietnam Pro Regular/Medium/SemiBold/Bold (`src/trading_bot/assets/fonts/`, kèm `OFL.txt`) |
| Cỡ chữ (px, ảnh rộng 1080) | mã 52 · PnL/giá vào 80 · tiêu đề khối 28 · giá trong thang 32 · nội dung 23–26 · nhãn 19–22 |
| Khoảng cách | lề 48 · đệm khối 32 · khe 16/20/24 · bo góc khối 24, ô 18, chip = nửa chiều cao |

## 4. Quy ước dữ liệu

- Số kiểu Việt Nam: dấu chấm nhóm nghìn, dấu phẩy thập phân, dấu trừ "−". Giá ≥ 1000: 2 số lẻ; 1–1000: tối đa 4; < 1: 5 chữ số có nghĩa (`0,00001234`).
- Thời gian theo `Asia/Ho_Chi_Minh` ("15/11 14:30", ghi "giờ VN"); thiếu tzdata thì dùng UTC+7 cố định.
- Mã tín hiệu/giao dịch rút gọn 8 ký tự trên ảnh; mã đầy đủ nằm trong caption.
- Giới hạn để thẻ không quá dài: 6 mục tiêu, 8 điều kiện (+N khác), 5 điều kiện mất hiệu lực (≤ 2 dòng mỗi điều kiện), 10 bot (+N khác). Mọi chuỗi bị cắt bằng "…" khi tràn.
- Caption là văn bản thuần ≤ 1024 ký tự, không parse mode; tin chữ `build_signal_message` giữ nguyên để làm phương án dự phòng.
- Renderer xác định: cùng dữ liệu → cùng byte PNG (không đọc đồng hồ, layout engine `BASIC`). Ảnh mẫu 120–280 KB.

## 5. Còn mở

- Chưa nối vào worker gửi tin (`workers/telegram_signal_worker.py`) và luồng thông báo giao dịch/tổng kết; khi nối cần fallback về tin chữ nếu render/gửi ảnh lỗi.
- Dòng "mất hiệu lực" lấy nguyên văn từ `build_signal_card` nên giá SL trong câu vẫn theo định dạng máy (`96619.3`).
- Chưa kiểm tra hiển thị trên ứng dụng Telegram thật (ảnh gửi dạng photo bị Telegram nén lại; cần xem độ nét chữ nhỏ trên điện thoại).
