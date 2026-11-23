# Runbook Vận hành Trader Workbench (WP-07E / CRY-377)

**Phạm vi:** Hướng dẫn vận hành chuẩn cho các thành phần tách biệt của Trader Workbench trên Docker Compose:
- **Producer:** `market-signal-worker` (đọc nến `market_bar`, sinh quyết định/ứng viên, ghi outbox)
- **Sender:** `telegram-signal-worker` (gửi tín hiệu & cảnh báo qua Telegram Bot API)
- **Backfill / Replay / Ingestion:** `backtest-worker`, `ingestion-worker`, `scripts/backfill_market_history.py`
- **Admin Web & Metrics:** `admin-web` (`/admin/api/workers/readiness`, `/admin/api/workbench/metrics-alerts`)

> [!CAUTION]
> **Bảo mật & PII:** Không bao giờ in hoặc dán `bot_token`, `api_key`, `api_secret`, `chat_id`, `telegram_user_id`, cookie phiên hoặc chuỗi secret vào log, terminal history hay ticket. Mọi biến bí mật nạp qua `/run/secrets/*` hoặc file env cô lập; xem thêm [secret-inventory-and-rotation.md](./secret-inventory-and-rotation.md).

---

## 1. Tạm dừng, hủy và khởi động lại (Resume / Cancel)

### 1.1. Tạm dừng / khởi động lại worker tách biệt
Sử dụng profile `paper` (hoặc project Compose đang chạy `<PROJECT>`):

```bash
# Kiểm tra trạng thái tất cả worker và readiness
docker compose --profile paper -p <PROJECT> ps
curl -sS -b <COOKIE_FILE> https://<ADMIN_HOST>/admin/api/workers/readiness | jq .

# Tạm dừng từng worker khi bảo trì hoặc điều tra sự cố
docker compose --profile paper -p <PROJECT> stop market-signal-worker
docker compose --profile paper -p <PROJECT> stop telegram-signal-worker
docker compose --profile paper -p <PROJECT> stop backtest-worker

# Khởi động lại (Resume) đúng thứ tự: ingestion -> signal producer -> sender
docker compose --profile paper -p <PROJECT> up -d ingestion-worker
docker compose --profile paper -p <PROJECT> up -d market-signal-worker telegram-signal-worker backtest-worker
```

### 1.2. Hủy hoặc nối lại job backfill / replay
1. Kiểm tra trạng thái job backfill trong `worker_heartbeat` (`component LIKE '%backfill%'`) hoặc hàng chờ `backtest_run`:
   ```bash
   docker compose --profile paper -p <PROJECT> exec -T postgres \
     psql -U crypidian_admin -d crypidian -c \
     "SELECT component, state, reason_code, heartbeat_at, counters->>'manifest_id' AS manifest_id FROM worker_heartbeat ORDER BY heartbeat_at DESC LIMIT 20;"
   ```
2. **Hủy mềm (Cancel):** Dừng `backtest-worker` và đánh dấu `CANCELED` cho job đang treo quá ngưỡng lease trên DB test/dev; **không** sửa bản ghi đã `COMPLETED`.
3. **Nối lại (Resume):** Khởi động lại `backtest-worker` hoặc chạy lại `scripts/backfill_market_history.py` với cùng `--manifest` để tận dụng cơ chế idempotent theo `idempotency_key` / `checksum`.

---

## 2. Xử lý Rate Limit (`429` / `418`)

### 2.1. Binance Futures REST (`429 Too Many Requests` / `418 IP Ban`)
- **Dấu hiệu:** `ingestion-worker` hoặc `backfill` ghi nhận `reason_code` liên quan rate limit, `bar_freshness` báo stale (`MARKET_DATA_STALE`), hoặc HTTP `429`/`418` kèm header `Retry-After`.
- **Xử lý chuẩn:**
  1. **Khi gặp `429`:** Dừng gọi REST thủ công ngay lập tức; giữ nguyên backoff tự động theo `Retry-After`. Kiểm tra số cặp coin/khung nến đang poll trong `workspace_settings_version` để bảo đảm nằm trong hạn mức trọng số của sàn.
  2. **Khi gặp `418` (tạm khóa IP):** Dừng ngay `ingestion-worker` và mọi job `backfill_market_history.py`:
     ```bash
     docker compose --profile paper -p <PROJECT> stop ingestion-worker backtest-worker
     ```
     Chờ hết thời gian `Retry-After` (tối thiểu 2–5 phút hoặc theo thông báo từ sàn) mới bật lại `ingestion-worker`. Tuyệt đối **không** xoay vòng proxy hoặc vượt rào kiểm soát của sàn.

### 2.2. Telegram Bot API (`429 retry_after` & Dead-letter)
- **Dấu hiệu:** Trên Dashboard mục **Chất lượng dữ liệu & độ trễ (WP-07E)** thấy `dead_letter_count > 0` (`TELEGRAM_DELIVERY_DEAD_LETTER`) hoặc bảng **Cảnh báo vận hành** hiện trạng thái `DEAD`.
- **Xử lý chuẩn:**
  1. Kiểm tra chi tiết delivery lỗi (chỉ xem `id`, `status`, `attempts`, `last_error_code`, **không** SELECT `telegram_chat_id`):
     ```bash
     docker compose --profile paper -p <PROJECT> exec -T postgres \
       psql -U crypidian_admin -d crypidian -c \
       "SELECT id, bot_id, status, attempts, last_error_code, available_at FROM telegram_signal_delivery WHERE status IN ('DEAD', 'PENDING') ORDER BY created_at DESC LIMIT 30;"
     ```
  2. Nếu lỗi là `429`: `telegram-signal-worker` tự lùi `available_at` theo `retry_after`; giảm bớt số bot/tần suất thông báo ở cấu hình subscription nếu liên tục chạm ngưỡng.
  3. Nếu `last_error_code` là `BOT_TOKEN_INVALID` hoặc `CHAT_FORBIDDEN`: kiểm tra [telegram-token-rotation-runbook.md](./telegram-token-rotation-runbook.md) hoặc yêu cầu người dùng liên kết lại Telegram thay vì gửi lại mù quáng.

---

## 3. Revision / Correction dữ liệu (`market_bar` & `strategy_release`)

### 3.1. Nguyên tắc bất biến của `market_bar`
- Mọi nến `market_bar` là **bất biến (append-only)**: khi cần đính chính dữ liệu, tạo bản ghi mới với `revision = prev + 1` và `supersedes_id` trỏ tới bản ghi cũ; **tuyệt đối không `UPDATE`/`DELETE` đè lịch sử nến**.
- Kiểm tra các bản ghi có nhiều revision hoặc khoảng trống:
  ```bash
  docker compose --profile paper -p <PROJECT> exec -T postgres \
    psql -U crypidian_admin -d crypidian -c \
    "SELECT symbol, timeframe, open_time, revision, supersedes_id FROM market_bar WHERE revision > 1 ORDER BY created_at DESC LIMIT 20;"
  ```

### 3.2. Kiểm tra & đồng bộ `strategy_release`
- Dùng lệnh kiểm tra chuẩn của `OPS-1` để phát hiện release `PUBLISHED` bị lệch `content_hash` hoặc không còn trong catalog:
  ```bash
  uv run python scripts/seed_signal_test_bots.py check \
    --project <PROJECT> \
    --workspace-id <WORKSPACE_UUID>
  ```
- Nếu phát hiện sai lệch hash trong workspace PAPER dev, chạy lại `apply` + `genesis` theo [ops1-dev-seed-and-reset-runbook.md](../implementation/ops1-dev-seed-and-reset-runbook.md) để tự supersede bản cũ mà giữ nguyên checkpoint genesis.

---

## 4. Quy trình rà soát sự cố (Incident Review)

1. **Thu thập ảnh chụp trạng thái (không cần đọc secret):**
   - Gọi `GET /admin/api/workbench/metrics-alerts` và `GET /admin/api/workers/readiness`.
   - Gọi `GET /api/v1/incidents` và `GET /api/v1/incidents/operational-alerts`.
2. **Kiểm tra log dịch vụ trong khung thời gian sự cố:**
   ```bash
   docker compose --profile paper -p <PROJECT> logs --since 30m \
     market-signal-worker telegram-signal-worker ingestion-worker admin-web
   ```
   - Xác minh mọi log lỗi đã che token/PII (`[REDACTED]`). Nếu phát hiện rò rỉ, chuyển ngay sang quy trình xoay khóa tại [secret-inventory-and-rotation.md](./secret-inventory-and-rotation.md).
3. **Truy vết gốc (`trace_ref`):**
   - Từ `trace_ref` trên cảnh báo WP-07E (`MARKET_DATA_STALE`, `DATASET_RIGHTS_EXPIRED`, `TELEGRAM_DELIVERY_DEAD_LETTER`, ...), truy vấn ngược về `worker_heartbeat.instance_id`, `source_policy_revision.policy_id`, hoặc `telegram_signal_delivery.id`.
4. **Ghi biên bản sau sự cố:** Lưu nguyên nhân gốc, thời gian phát hiện/khắc phục, `trace_ref`, và hành động phòng ngừa vào `docs/implementation/implementation-memory.md` (không ghi dữ liệu nhạy cảm).

---

## 5. Chính sách lưu giữ & Sao lưu (Retention & Backup)

| Bảng / Dữ liệu | Chính sách lưu giữ | Ghi chú vận hành |
| :--- | :--- | :--- |
| `market_bar`, `market_raw_reference` | Lưu dài hạn (append-only) | Dùng `revision` & `supersedes_id`; không xóa đè |
| `audit_event`, `source_policy_revision` | Lưu tối thiểu 365 ngày | Phục vụ truy vết quyền nguồn & kiểm toán |
| `telegram_signal_delivery`, `outbox_event` | Giữ nóng 30–90 ngày; archive bản `SENT`/`EXPIRED` cũ hơn 90 ngày | Giữ lại bản `DEAD` chưa xử lý cho đến khi đóng incident |
| `worker_heartbeat` | 1 dòng mới nhất / `(workspace_id, component, instance_id)` | Tự upsert mỗi chu kỳ; không phình bảng |
| Bản sao lưu DB mã hóa | Theo lịch diễn tập trong `backup-restore-runbook.md` | RPO ≤ 900s, RTO ≤ 300s, mã hóa AES-256-GCM |

Để diễn tập sao lưu và phục hồi cô lập, thực hiện theo [backup-restore-runbook.md](./backup-restore-runbook.md).
