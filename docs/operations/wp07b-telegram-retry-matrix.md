# Ma trận Retry / Dead-Letter cho Telegram Bot API (WP-07B / CRY-348)

**Ngày kiểm tra tài liệu chính thức:** `2026-11-24`  
**Nguồn tham chiếu Telegram Bot API:**
- Making requests & lỗi chung: https://core.telegram.org/bots/api#making-requests
- `ResponseParameters.retry_after`: https://core.telegram.org/bots/api#responseparameters
- Giới hạn tần suất gửi tin (`1/s` private, `20/m` group, `30/s` broadcast): https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this

---

## 1. Bảng Policy theo nhóm mã lỗi

| Nhóm lỗi Bot API | Ví dụ mã lỗi / exception | `TelegramFailureClass` | Hành động (`RetryOutcomeAction`) | Số lần thử tối đa | Backoff / Delay | Khả năng gửi trùng |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **HTTP `429` Too Many Requests** | `TELEGRAM_HTTP_429` kèm `parameters.retry_after` | `RATE_LIMITED` | `retry` tới khi hết `max_attempts` → `dead_letter` (nếu `retry_after > max_retry_after_seconds` → `dead_letter` ngay) | `5` | Dùng đúng `max(1, retry_after)` giây | Không (server từ chối nhận) |
| **HTTP `403` Forbidden** | Bot bị user chặn, chưa vào group, bị kick hoặc thiếu quyền `Post Messages` | `PERMANENT_CLIENT` | `dead_letter` ngay ở lần 1 (`retryable=False`) | `1` | `0s` | Không |
| **HTTP `400` Bad Request** | `chat not found`, `reply_to_message_id` không tồn tại, lỗi `parse_mode` / nội dung rỗng / >4096 ký tự, `ValueError` | `PERMANENT_CLIENT` | `dead_letter` ngay ở lần 1 (`retryable=False`) | `1` | `0s` | Không |
| **HTTP `401` Unauthorized** | Token bot không hợp lệ hoặc đã thu hồi | `AUTH_ERROR` | `dead_letter` ngay sau khi refresh secret không thành công (`retryable=False`) | `1` | `0s` | Không |
| **Nội dung nhạy cảm vào group** | `TelegramSensitivityBlockedError` | `SENSITIVE_CONTENT_BLOCKED` | `dead_letter` (hoặc `drop` nếu rule đặt `drop_without_dead_letter=True`) | `1` | `0s` | Không (bị chặn trước khi gọi HTTP) |
| **HTTP `5xx` Server Error** | `500`, `502`, `503`, `504` từ Telegram Bot API | `TRANSIENT_SERVER` | `retry` hữu hạn với exponential backoff → `dead_letter` | `4` | `1s, 2s, 4s` (trần `30s`) | Thấp |
| **Timeout / Lỗi mạng mơ hồ** | `TimeoutError`, `OSError`, `TELEGRAM_NETWORK_ERROR`, `TELEGRAM_INVALID_RESPONSE` | `NETWORK_ERROR` | `retry` hữu hạn với exponential backoff → `dead_letter` | `4` | `1s, 2s, 4s` (trần `30s`) | **Có (`ambiguous_duplicate_risk=True`)** |
| **Lỗi chưa phân loại khác** | Exception nội bộ bất ngờ | `UNKNOWN` | `retry` hữu hạn → `dead_letter` | `2` | `2s` (trần `30s`) | Tùy ngữ cảnh |

---

## 2. Lưu ý về khả năng gửi trùng khi Timeout mơ hồ

Telegram Bot API `sendMessage` / `sendPhoto` **không hỗ trợ `idempotency_key`**. Khi client gửi request HTTP và gặp:
- `TimeoutError` lúc chờ phản hồi (`ReadTimeout`), hoặc
- `TELEGRAM_INVALID_RESPONSE` / đứt kết nối sau khi TCP đã đẩy body lên server Telegram,

server Telegram **có thể đã xử lý và gửi tin nhắn tới chat người dùng rồi** trước khi kết nối bị ngắt. Do đó:
1. `TelegramRetryMatrix.decide(...)` gắn cờ `ambiguous_duplicate_risk = True` cho toàn bộ nhóm `NETWORK_ERROR`.
2. `TelegramSignalDeliveryWorker` ghi log cảnh báo (chỉ gồm `delivery_id` và `error_code`, không chứa `chat_id` hay `token`) để vận hành phân biệt retry do timeout mơ hồ với lỗi 429/5xx.
3. Mỗi `delivery_id` vẫn bị chặn tối đa `4` lần thử để không spam lặp vô hạn.
