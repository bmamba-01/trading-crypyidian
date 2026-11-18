# Runbook Xoay vòng Token Telegram Bot qua BotFather (SEC-39)

**Mục tiêu:** Chuẩn hóa quy trình cấp mới và xoay vòng (rotate) token Telegram Bot API an toàn thông qua `@BotFather` và `SecretProvider`, bảo đảm:
- Token mới được nạp không làm rò rỉ giá trị secret ra log, URL, traceback hay hàng đợi dead-letter (`SEC-35`, `SEC-39`).
- Hệ thống tự làm mới lease token khi hết hạn hoặc khi Bot API trả `HTTP 401`, không phát sinh sai sót cấu hình.
- Môi trường `production` luôn vận hành theo nguyên tắc **fail-closed** (từ chối khởi động nếu thiếu `SecretProvider` hoặc token hợp lệ).

**Quyền thực hiện:** Chỉ tài khoản thuộc vai trò `Owner` (nắm quyền `@BotFather`) hoặc kỹ sư vận hành được ủy quyền trực tiếp quản lý secret vault.

**Tài liệu tham chiếu (đã đối chiếu ngày 18/11/2026):**
- [Telegram Bot features — BotFather (`/mybots`, `/token`, `/revoke`)](https://core.telegram.org/bots/features)
- [Telegram Bots FAQ — Outbound rate limits](https://core.telegram.org/bots/faq)
- [Telegram Bot API — `setWebhook` & `ResponseParameters.retry_after`](https://core.telegram.org/bots/api)

---

## 1. Kiến trúc nạp & xoay vòng token trong Crypidian

1. **Qua `SecretProvider` (ưu tiên ở mọi môi trường, bắt buộc ở `production`):**
   - `AppSettings.telegram_bot_token_reference` (biến môi trường `TRADING_BOT_TELEGRAM_BOT_TOKEN_REFERENCE`) xác định khóa tham chiếu trong `SecretProvider`.
   - `TelegramBotApiSender.from_secret_provider(...)` nhận `SecretLease` kèm `expires_in_seconds` (mặc định `300s` ở adapter file).
   - Khi lease hết hạn hoặc Bot API trả `HTTP 401 Unauthorized` (do token cũ vừa bị `@BotFather` thu hồi), sender tự động gọi lại `SecretProvider.resolve_telegram_bot_token(...)`; nếu nhận được token mới, hệ thống đổi `base_url` nội bộ và thử lại ngay yêu cầu vừa gửi.
2. **Tương thích local dev (`TRADING_BOT_TELEGRAM_BOT_TOKEN_FILE`):**
   - Ở `app_env=local` / `test`, nếu chưa gắn `SecretProvider` ngoài, `app.py` bọc tệp secret (mặc định Docker secret `/run/secrets/telegram_bot_token`) bằng `FileTelegramBotSecretProvider`.
   - Nếu tệp rỗng ở môi trường `local`/`test` (ví dụ chạy E2E với `e2e_telegram_bot_token`), `admin-web` dùng `RecordingTelegramSender` (không gọi mạng thật). Ở `production`, mọi trạng thái thiếu/rỗng/sai định dạng đều **fail-closed** (`RuntimeError`).
3. **Giới hạn outbound & bảo vệ dữ liệu group (`SEC-39`):**
   - `ThrottledTelegramSender` và `TelegramOutboundQueue` giữ nhịp tối đa ≈ `1 tin/giây` mỗi chat riêng, `20 tin/phút` mỗi group, ≈ `30 tin/giây` broadcast tổng thể.
   - HTTP `429` đọc `parameters.retry_after` chính xác và chặn mọi luồng cùng chat trong khoảng chờ; khi vượt ngưỡng ma trận retry (hoặc gặp lỗi vĩnh viễn `400/401/403`), tin được ghi nhận vào `DeadLetterSink` (chỉ lưu digest + mã lỗi, không lưu secret).
   - `GroupSensitivityGuard` chặn hoặc che nội dung vị thế, số dư, PnL, ký quỹ, đòn bẩy trước khi rời tiến trình tới bất kỳ group chat (`chat_id < 0`).

---

## 2. Quy trình xoay vòng token định kỳ qua `@BotFather`

### Bước 1 — Chuẩn bị trước khi xoay (Pre-flight)
- [ ] Xác nhận khung giờ bảo trì hoặc trạng thái ổn định của `admin-web` và `telegram-signal-worker`.
- [ ] **Không** chạy lệnh xoay nếu chưa sẵn sàng cập nhật ngay secret vault / tệp `/run/secrets/telegram_bot_token`, vì ngay khi `@BotFather` thu hồi, token cũ mất hiệu lực tức thì (`401 Unauthorized`).
- [ ] Nếu xoay **kèm** nghi ngờ lộ webhook secret: chuẩn bị sẵn giá trị `telegram_webhook_secret` mới (≥ 32 ký tự `A-Za-z0-9_-`).

### Bước 2 — Thu hồi token cũ và cấp token mới trên Telegram
1. Mở Telegram và tìm đúng tài khoản **`@BotFather`** có dấu tích xanh chính thức.
2. Gửi lệnh **`/revoke`** (hoặc gửi **`/mybots`** → chọn bot → **`API Token`** → **`Revoke current token`**; trên các phiên bản BotFather cũ có thể dùng `/token` để xem và nút xoay token tương ứng).
3. Chọn đúng `@username` của bot Crypidian cần xoay.
4. `@BotFather` ngay lập tức vô hiệu hóa token cũ và trả về chuỗi token mới dạng `<bot_id>:<secret>`.
5. **Tuyệt đối không** dán token mới vào chat nhóm, ticket, terminal history, file `.env` commit lên git hoặc báo cáo Markdown.

### Bước 3 — Cập nhật `SecretProvider` / tệp secret đích
- **Trường hợp dùng Secret Vault / KMS (`TRADING_BOT_TELEGRAM_BOT_TOKEN_REFERENCE`):**
  1. Ghi phiên bản secret mới vào khóa tham chiếu tương ứng trong vault (`telegram_bot_token_reference`).
  2. Chờ tối đa `lease_ttl_seconds` (hoặc kích hoạt một lệnh/healthcheck nhẹ tạo request outbound; khi gặp `401`, sender tự làm mới tức thì không cần chờ hết TTL).
- **Trường hợp dùng Docker Compose / tệp secret local (`/run/secrets/telegram_bot_token`):**
  1. Ghi nguyên tử vào tệp tạm rồi thay thế để tránh tiến trình đọc phải tệp đang ghi dở:
     ```bash
     install -m 0600 /dev/null .secrets/telegram_bot_token.new
     printf '%s' '<DAN_TOKEN_MOI_TAI_DAY>' > .secrets/telegram_bot_token.new
     mv -f .secrets/telegram_bot_token.new .secrets/telegram_bot_token
     ```
  2. Nếu chạy Docker Swarm / Compose mount file secret dạng bind hoặc cần nạp lại biến cấu hình:
     ```bash
     docker compose up -d --force-recreate admin-web
     docker compose --profile signal up -d --force-recreate telegram-signal-worker
     ```

### Bước 4 — Cập nhật Webhook (nếu dùng webhook công khai)
- Nếu xoay đồng thời `webhook_secret`, gọi `setWebhook` lại với `secret_token` mới và cân nhắc truyền `drop_pending_updates=true` (theo `SEC-36`) để xả các update tồn đọng nghi vấn.
- Nếu chạy local qua `scripts/telegram_local_relay.py`, khởi động lại tiến trình relay để nạp token mới từ `.secrets/telegram_bot_token`.

---

## 3. Checklist xác minh sau khi xoay token

- [ ] Kiểm tra `GET /health/live` và `GET /health/ready` của `admin-web` và `telegram-signal-worker` trả `200`.
- [ ] Gửi một tin kiểm tra hoặc lệnh `/bots` từ tài khoản đã liên kết trong chat riêng; xác nhận bot phản hồi bình thường.
- [ ] Kiểm tra log dịch vụ:
  - Phải thấy thông báo an toàn kiểu `Đã xoay vòng token Telegram từ SecretProvider.` (nếu làm mới nóng).
  - **Không** có bất kỳ chuỗi token, URL `api.telegram.org/bot...` hay mô tả body nhạy cảm nào xuất hiện trong log hoặc bản ghi dead-letter (`SEC-35`).
- [ ] Nếu có tin rơi vào dead-letter trong khoảnh khắc chuyển giao, kiểm tra bản ghi dead-letter (`message_id`, `correlation_id`, `error_code`) và phát lại thủ công các tin còn hiệu lực nghiệp vụ nếu cần.

---

## 4. Xử lý sự cố lộ token khẩn cấp (Incident Playbook)

1. **Thu hồi ngay lập tức:** Vào `@BotFather` → `/revoke` cho bot bị lộ (ưu tiên số 1, thực hiện trong vòng < 1 phút để cắt quyền truy cập trái phép).
2. **Nạp token mới** theo Mục 2 Bước 3.
3. **Xoay `webhook_secret` đi kèm:** Đặt `X-Telegram-Bot-Api-Secret-Token` mới qua `setWebhook(..., drop_pending_updates=True)` để vô hiệu mọi webhook giả mạo.
4. **Rà soát audit & dead-letter:**
   - Kiểm tra nhật ký `telegram.command.accepted` / `telegram.command.rejected` trong khoảng thời gian nghi ngờ.
   - Kiểm tra file/store dead-letter xem có tin nào bị `TELEGRAM_HTTP_401` hoặc bị chặn bởi `GroupSensitivityGuard`.
5. **Xác nhận không còn `TELEGRAM_HTTP_401` mới** trong vòng 15 phút vận hành kế tiếp.
