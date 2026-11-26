# Runbook Vận hành Thực thi Binance USDⓈ-M (WP-03D / Task 7 — CRY-295)

**Phạm vi:** Tài liệu thao tác tiếng Việt cho tầng thực thi Binance USDⓈ-M (Demo và nền tảng sẵn sàng Production; **LIVE vẫn đang khóa**). Liên kết trực tiếp với:
- [venue_reconciliation.py](../../src/trading_bot/domain/execution/venue_reconciliation.py) (`VenueAuthoritativeSnapshot`, `VenueReconcileOutcome`, `reconcile_venue_startup`, `reconcile_venue_reconnect`)
- [venue_readiness_gate.py](../../src/trading_bot/application/venue_readiness_gate.py) (`VenueStartupReadinessGate`)
- [account_activity_incidents.py](../../src/trading_bot/adapters/database/repositories/account_activity_incidents.py) (`AccountActivityIncidentRepository`)
- Runbook tổng thể production: [live-production-operations-runbook.md](./live-production-operations-runbook.md)

---

## 0. Nguyên tắc an toàn bất biến

1. **Cổng khởi động đóng mặc định:** `VenueStartupReadinessGate.is_open` luôn bắt đầu bằng `False` (`STARTUP_RECONCILIATION_REQUIRED`). Runtime chỉ được phép mở `NEW_ENTRY` khi `reconcile_venue_startup` trả `MATCHED`, `risk_policy_approved=True` và `open_incidents_count == 0`.
2. **Không tự adopt / hủy / flatten hoạt động ngoại lai:** Mọi lệnh hoặc vị thế trên sàn không thuộc lineage của bot (`FOREIGN_ACTIVITY` / `EXTERNAL_ACTIVITY_UNRECONCILED`) chỉ ghi nhận incident bền `OPEN` và đóng entry mới — bot **tuyệt đối không tự adopt, không tự hủy lệnh ngoại lai và không tự flatten**.
3. **Cap breach / Pause / Entry Kill không tự flatten:** Chạm giới hạn lỗ/exposure hoặc kích hoạt `Pause`/`Kill` chỉ chặn mở vị thế mới (`NEW_ENTRY`) và **giữ nguyên các lệnh bảo vệ (Stop-Loss / Algo) đã xác nhận**. `Flatten` luôn là hành động riêng có phê duyệt và xác minh.
4. **Không chứa secret:** Không bao giờ ghi API key, secret key, `listenKey`, chữ ký HMAC/Ed25519 hoặc thông tin tài khoản cá nhân vào runbook, ticket, log hay Git.

---

## 1. Bảng ánh xạ Reason Code Incident ↔ Cổng Entry

| Reason Code / Outcome | Trạng thái cổng Entry | Trạng thái Reservation | Kịch bản xử lý |
| --- | --- | --- | --- |
| `STARTUP_RECONCILIATION_REQUIRED` | **ĐÓNG** | Giữ nguyên | Mục 2 (Restart) |
| `VENUE_SNAPSHOT_MISSING` / `STALE` / `INCOMPLETE` / `FUTURE` | **ĐÓNG** | Giữ nguyên | Mục 2 & Mục 4 |
| `STREAM_GAP_UNCLOSABLE` / `FILL_HISTORY_GAP_UNCOVERED` | **ĐÓNG** | Giữ nguyên (`UNKNOWN`) | Mục 5 (Stream gap) |
| `EXTERNAL_ACTIVITY_UNRECONCILED` (`FOREIGN_ACTIVITY`) | **ĐÓNG** | Giữ nguyên | Mục 7 (Foreign activity) |
| `VENUE_PROTECTION_MISSING` (`PROTECTION_MISSING`) | **ĐÓNG** | Giữ nguyên | Mục 6 (Lệnh bảo vệ) |
| `VENUE_POSITION_QUANTITY_MISMATCH` (`MISMATCH`) | **ĐÓNG** | Giữ nguyên | Mục 7 (Xung đột đối soát) |
| `UNKNOWN_INTENT_CONFIRMED_ABSENT` (`ORDER_ABSENT`) | Chờ sạch toàn bộ | Được phép giải phóng nguyên tử | Mục 2 & Mục 4 |
| `UNKNOWN_INTENT_FOUND_ON_VENUE` (`OPEN_EXPOSURE_RECORDED`) | Chờ sạch toàn bộ | Chuyển sang open exposure | Mục 2 & Mục 4 |
| `VENUE_READY` (`MATCHED`) | **MỞ** (khi đủ risk policy & 0 incident) | Hoạt động bình thường | — |

---

## 2. Kịch bản 1 — Restart khi đang có lệnh / vị thế mở

- **Trigger:** Container `trading-runtime` hoặc `execution-gateway` khởi động lại trong lúc tài khoản đang có vị thế mở, lệnh đang chờ hoặc intent ở trạng thái `UNKNOWN`.
- **Thao tác từng bước:**
  1. Xác nhận `VenueStartupReadinessGate` đang ở trạng thái **ĐÓNG** (`STARTUP_RECONCILIATION_REQUIRED`).
  2. Thu thập `VenueAuthoritativeSnapshot` đầy đủ (chỉ dùng endpoint `GET` read-only: balances, positions, open normal orders, open algo orders, recent fills).
  3. Chạy `reconcile_venue_startup(...)` để:
     - Dedupe `recent_fills` theo `trade_id` (`deduped_fills()`) nhằm chống ghi trùng khớp lệnh sau restart.
     - Đối chiếu từng intent `UNKNOWN`:
       - Nếu thấy trên `open_orders`/`algo_orders`/`recent_fills` → `OPEN_EXPOSURE_RECORDED` (chuyển reservation sang exposure thực tế).
       - Nếu lịch sử REST phủ tới `created_at` của intent và không thấy trên sàn → `ORDER_ABSENT` (được phép giải phóng reservation nguyên tử).
       - Nếu lịch sử REST **không phủ tới** `created_at` → `FILL_HISTORY_GAP_UNCOVERED` (giữ nguyên `UNKNOWN` và reservation, không tự hủy).
  4. Kiểm tra `open_incidents_count == 0` và `risk_policy_approved == True` trước khi mở cổng.
- **Lệnh kiểm chứng cô lập:**
  ```bash
  uv run pytest -q --tb=short tests/unit/test_venue_reconciliation.py tests/unit/test_venue_readiness.py tests/e2e/test_wp03d_venue_reconciliation_e2e.py
  ```

---

## 3. Kịch bản 2 — Xoay (Rotate) & Thu hồi (Revoke) API Key Demo

- **Trigger:** Định kỳ xoay key, key sắp hết hạn, hoặc nghi ngờ rò rỉ API key Demo.
- **Quyền hạn:** **Owner** thao tác trực tiếp trên cổng Binance Demo (`demo-fapi.binance.com` / `testnet.binancefuture.com`); Operator cập nhật secret mount cho duy nhất `execution-gateway`.
- **Thao tác từng bước:**
  1. Chuyển bot/workspace sang `PAUSED` để đóng `NEW_ENTRY` (không xóa protective orders đang mở).
  2. **[Owner thủ công trên Binance Demo]:** Thu hồi key cũ; tạo key mới (ưu tiên Ed25519) chỉ bật quyền `READ` + `FUTURES`, tuyệt đối tắt `WITHDRAW` và `TRANSFER`.
  3. Cập nhật file secret tại thư mục bí mật của `execution-gateway` (chỉ container `execution-gateway` được mount; `admin-web`, `trading-runtime`, `migrator` không có quyền đọc).
  4. Khởi động lại duy nhất `execution-gateway` và chạy preflight chỉ đọc xác nhận:
     - Host khớp allowlist Demo (`demo-fapi.binance.com`, `demo-fstream.binance.com`), không bao giờ rơi về production `fapi.binance.com`.
     - Quyền API hợp lệ (có `READ`, không có `WITHDRAW`/`TRANSFER`).
  5. Ghi log audit gồm thời điểm xoay và alias 4 ký tự cuối; **không** lưu raw key.

---

## 4. Kịch bản 3 — API Outage, HTTP `429`, `418` và phân loại HTTP `503`

### 4.1. HTTP `429 Too Many Requests` (Vượt giới hạn Rate Limit)
- **Hành vi an toàn:**
  1. Đóng `NEW_ENTRY` ngay nếu lỗi lặp lại.
  2. Đọc header `Retry-After` và `X-MBX-USED-WEIGHT-1M` (ngưỡng mặc định 2400 weight/phút); thực hiện exponential backoff có jitter, **không** retry dồn dập.
  3. Tạm giảm các truy vấn polling phụ cho tới khi trọng số giảm về ngưỡng an toàn.

### 4.2. HTTP `418 I'm a teapot` (IP Ban tự động)
- **Hành vi an toàn:**
  1. Dừng lập tức mọi lời gọi REST từ `execution-gateway` trong suốt thời gian `Retry-After` (có thể kéo dài từ 2 phút đến 3 ngày).
  2. Giữ cổng entry đóng (`RECOVERY_REQUIRED`); báo ngay cho **Owner** để giám sát thủ công các vị thế mở và stop-loss trực tiếp trên sàn.
  3. Sau khi hết thời hạn ban, bắt buộc chạy `reconcile_venue_startup` (Mục 2) trước khi mở lại.

### 4.3. Phân loại từng loại HTTP `503` (Bắt buộc xử lý khác nhau)
- **Loại A — HTTP `503` kèm thông điệp `"Unknown error"` (hoặc network timeout sau khi gửi request):**
  - Trạng thái thực thi trên sàn **không xác định** (lệnh có thể đã khớp hoặc chưa tới sàn).
  - **Cấm retry ngay lập tức.** Chuyển intent sang `VenueOrderState.UNKNOWN`, giữ nguyên `risk_reservation` (`reservation_action = "RETAIN"`).
  - Bắt buộc query đối soát (`GET /fapi/v1/openOrders`, `GET /fapi/v1/userTrades` theo `client_order_id`) để xác minh `ORDER_ABSENT` hoặc `OPEN_EXPOSURE_RECORDED` trước khi có bất kỳ hành động tiếp theo nào.
- **Loại B — HTTP `503` kèm thông điệp `"Service Unavailable"` hoặc `"Internal error"`:**
  - Hệ thống sàn tạm thời quá tải hoặc chưa tiếp nhận; chuyển sang trạng thái chờ retry có backoff (`retry_not_before` ở tương lai) và giới hạn deadline.
  - Nếu lỗi xảy ra trên request gửi lệnh mới và còn nghi ngờ, vẫn ưu tiên query `client_order_id` trước khi gửi lại với cùng `idempotency_key` để chống tạo lệnh trùng.

---

## 5. Kịch bản 4 — Listen Key hết hạn, Stream Gap & Reconnect

- **Đặc thù Binance USDⓈ-M User Data Stream:**
  - `listenKey` có thời hạn 60 phút (cần gia hạn định kỳ trước khi hết hạn); kết nối WebSocket có giới hạn tối đa 24 giờ và có thể ngắt bất ngờ.
  - Event `ACCOUNT_UPDATE` chỉ chứa phần thay đổi (delta), **không** phải snapshot đầy đủ.
  - **Giới hạn lịch sử REST:** Lịch sử lệnh/khớp trên REST bị giới hạn theo cửa sổ sàn (3 ngày / 7 ngày / 90 ngày tùy loại endpoint).
- **Thao tác khi mất stream hoặc nhảy sequence:**
  1. Đóng cổng entry ngay khi phát hiện mất kết nối, `listenKey` hết hạn hoặc nhảy sequence (`PREFLIGHT_STREAM_GAP_DETECTED`).
  2. Khởi tạo lại `listenKey` / WebSocket, đồng thời lấy `VenueAuthoritativeSnapshot` + lịch sử khớp gần đây (`recent_fills`).
  3. Gọi `reconcile_venue_reconnect(..., last_stream_event_at=...)`:
     - Nếu `history_coverage_from > last_stream_event_at` (hoặc `None`) → trả `STREAM_GAP_UNCLOSABLE` (`STALE_INCOMPLETE`): **giữ cổng entry đóng**, không suy đoán trạng thái trong khoảng trống, chuyển Owner đối soát thủ công trên sàn.
     - Nếu khép được gap và `reconcile_venue_startup` trả `MATCHED` → mới được phép mở lại cổng readiness.

---

## 6. Kịch bản 5 — Lệnh bảo vệ (Stop / Algo Service Order) thiếu hoặc bị từ chối

- **Trigger:** `VENUE_PROTECTION_MISSING` / `PREFLIGHT_PROTECTION_MISSING`, hoặc Algo/Stop-Loss order bị sàn từ chối (`REJECTED`) / hủy ngoài ý muốn khi vẫn còn vị thế mở.
- **Thao tác từng bước:**
  1. Đóng ngay `NEW_ENTRY` (`VenueStartupReadinessGate.is_open = False`).
  2. Kiểm tra lý do sàn từ chối (ví dụ: sai `tickSize`/`stepSize`, giá thị trường đã vượt mức kích hoạt stop, hoặc giới hạn lệnh Algo).
  3. **Không xóa** các lệnh bảo vệ đang hợp lệ của các vị thế khác và **không tự động flatten** khi chưa có phê duyệt.
  4. Cảnh báo ngay cho **Owner**:
     - Hoặc đặt lệnh Stop-Loss bảo vệ thủ công trực tiếp trên sàn cho vị thế đang hở;
     - Hoặc thực hiện luồng **Flatten** riêng biệt (có phê duyệt maker-checker + step-up MFA) để đóng vị thế an toàn.
  5. Sau khi vị thế đã có bảo vệ hợp lệ (hoặc đã về `0`), chạy lại `reconcile_venue_startup` để xác nhận sạch.

---

## 7. Kịch bản 6 — Xung đột đối soát (`MISMATCH`), Foreign Activity & Xử lý thủ công

- **Trigger:** `EXTERNAL_ACTIVITY_UNRECONCILED` (`FOREIGN_ACTIVITY`) hoặc `VENUE_POSITION_QUANTITY_MISMATCH` (`MISMATCH`).
- **Quy tắc bất biến:**
  - Hệ thống ghi nhận `AccountActivityIncidentRecord(status='OPEN', source='VENUE')` bền trong PostgreSQL (kèm `snapshot_hash`, `fingerprint`, `fencing_token`) và đóng cổng entry.
  - Bot **KHÔNG** tự adopt vị thế/lệnh ngoại lai vào sổ cái, **KHÔNG** tự hủy lệnh ngoại lai và **KHÔNG** tự flatten.
- **Quy trình xử lý thủ công & khôi phục:**
  1. **Owner** kiểm tra trực tiếp trên sàn nguồn gốc lệnh/vị thế ngoại lai:
     - Nếu nghi ngờ truy cập trái phép / lộ key → thực hiện ngay **Mục 3 (Thu hồi API Key)**.
     - Nếu là thao tác tay của Owner → đóng vị thế hoặc hủy lệnh ngoại lai trên sàn.
  2. Trên Admin Web (vai trò `owner` hoặc `risk_admin`, yêu cầu MFA + lý do rõ ràng), chuyển incident từ `OPEN` sang `RESOLVED` với mã hợp lệ:
     - `EXTERNAL_STATE_REMOVED_BY_OPERATOR` (đã xử lý xong trên sàn), hoặc
     - `FALSE_POSITIVE_CONFIRMED` (đã xác minh đối chiếu sổ cái).
  3. Chạy lại `reconcile_venue_startup` xác nhận `MATCHED` và `open_incidents_count == 0`.
  4. Sau khi mọi điều kiện sạch, thực hiện mở lại `VenueStartupReadinessGate` (`VENUE_READY`).
