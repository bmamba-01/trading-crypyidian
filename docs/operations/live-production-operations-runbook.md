# Runbook Vận hành Production, Cảnh báo & Diễn tập (OPS07F / Task 9)

**Phạm vi:** Tài liệu hướng dẫn vận hành chuẩn bằng tiếng Việt dành cho Operator, Risk Admin và Owner khi vận hành, giữ an toàn, chuyển cấp và khôi phục hệ thống Crypidian trên môi trường production (Binance USDⓈ-M Futures).
**Trạng thái Live:** **LIVE VẪN ĐANG KHÓA.** Tài liệu này không tự cấp quyền mở Live; mọi diễn tập hiện hành chỉ chạy trên môi trường disposable cô lập với synthetic account/transport.

---

## 0. Nguyên tắc bất biến & Cảnh báo Blocker hạ tầng

1. **Không bao giờ chia sẻ secret qua chat/ticket/log:** API key, secret key, passphrase backup, MFA key và DB password chỉ nạp từ secret mount/vault được ủy quyền.
2. **Tách bạch `Pause / Entry Kill` và `Flatten`:**
   - `Pause` / `Entry Kill` chỉ **đóng cổng mở vị thế mới (`NEW_ENTRY`)**; tuyệt đối **không xóa các protective/Algo order (Stop-Loss / Take-Profit) đã xác nhận** của vị thế đang mở.
   - `Flatten` là hành động đóng vị thế riêng biệt, đòi hỏi phê duyệt độc lập (maker-checker) và xác minh sau thực thi.
3. **Fail-closed mặc định:** Mọi trạng thái không chắc chắn (`UNKNOWN`, stale snapshot, stream gap, lỗi DB/endpoint, thiếu protection, sai hash profile) đều **đóng entry gate** và **giữ nguyên `risk_reservation`** (không chuyển về 0/flat).
4. **Blocker cần Owner chốt trước khi áp dụng cho production thật (không tự phát minh):**
   - `[BLOCKER-ONCALL]` Danh sách trực Primary / Secondary On-Call và thông tin liên hệ chính thức: **Chưa do Owner cung cấp.**
   - `[BLOCKER-ALERT-DEST]` Kênh nhận cảnh báo production chính thức (PagerDuty / Opsgenie / Telegram Ops group ID / Webhook URL): **Chưa do Owner cung cấp.**
   - `[BLOCKER-TOPOLOGY]` Host production, IP egress tĩnh của `execution-gateway`, Secret Manager đích và SLA RPO/RTO production chính thức (đồng bộ với `CRY-465`): **Chưa do Owner cung cấp.**

---

## RB-01. Triển khai, Version Pin, Migration & Rollback

- **Mức độ / Trigger:** Planned Change (SEV-4) — phát hành bản dựng mới hoặc rollback khẩn cấp sau lỗi triển khai.
- **Người chịu trách nhiệm:** Operator triển khai (Maker) + Owner/Risk Admin phê duyệt bản dựng (Checker).
- **Tiền điều kiện:**
  - Image đã có digest cố định (`@sha256:...`), qua đầy đủ CI unit + E2E (`CRY-466`).
  - Đã tạo bản sao lưu mã hóa mới nhất theo **RB-09** trước khi chạy migration.
- **Lệnh / nút được phép:**
  - Kiểm tra render cấu hình (không khởi động container):
    ```bash
    docker compose -f compose.yaml -f deploy/compose.tls.yaml -f deploy/compose.production.yaml config
    ```
  - Kiểm tra trạng thái migration trước/sau:
    ```bash
    docker compose --profile production-locked -f compose.yaml -f deploy/compose.tls.yaml -f deploy/compose.production.yaml \
      run --rm --no-deps migrator alembic current
    ```
- **Các bước cụ thể:**
  1. Đưa workspace/bot về trạng thái `PAUSED` (đóng entry mới, giữ nguyên protective orders).
  2. Chạy backup mã hóa trước khi migrate (RB-09) và lưu `backup_id` + `manifest.sha256`.
  3. Render và rà soát `docker compose ... config` để xác nhận đúng image digest và `TRADING_BOT_LIVE_DISPATCH_LOCKED=true` (trừ khi có dossier G6 đã ký riêng).
  4. Chạy `migrator alembic upgrade head`, xác minh 1 sole head duy nhất.
  5. Khởi động dịch vụ theo thứ tự: `postgres` → `migrator` → `execution-gateway` → `trading-runtime` → `admin-web` → `reverse-proxy`.
  6. Chạy preflight chỉ đọc (**RB-02**) trước khi xem xét resume.
- **Expected Safe State:** Mọi container chạy non-root `10001:10001`, `read_only: true`, `/health/ready` trả `200`, entry gate ở trạng thái đóng cho tới khi preflight pass.
- **Tiêu chí dừng / Escalate:** Migration lỗi, container crash-loop > 2 lần, hoặc preflight trả `RECOVERY_REQUIRED` → dừng rollout ngay, giữ entry đóng, chuyển cấp Owner.
- **Rollback / Recovery:**
  1. Giữ entry đóng (`PAUSED`).
  2. Ghim lại `CRYPIDIAN_PRODUCTION_IMAGE` về digest bản ổn định trước đó.
  3. Không tự ý chạy `alembic downgrade` trên DB đã có ghi dữ liệu giao dịch mới nếu chưa có phê duyệt Owner; chỉ restore từ bản backup trước release vào DB cô lập để kiểm tra trước.
- **Thông tin cần lưu:** Image digest cũ/mới, `alembic current`, `backup_id`, thời điểm bắt đầu/kết thúc, kết quả preflight `evidence_hash`.
- **Điều cấm:** Cấm deploy tag `:latest` hoặc `:local`; cấm bật Live bằng cờ env đơn lẻ; cấm bỏ qua bước backup trước migration.

---

## RB-02. Preflight Chỉ đọc, Kiểm tra Clock/NTP & Xác nhận Egress

- **Mức độ / Trigger:** Trước mỗi lần mở entry gate, sau restart, sau reconnect hoặc khi có cảnh báo lệch đồng hồ (`PREFLIGHT_CLOCK_DRIFT_EXCESSIVE`).
- **Người chịu trách nhiệm:** Operator / Runtime System.
- **Tiền điều kiện:** `execution-gateway` đang chạy và chỉ có quyền đọc (`GET`) trong bước preflight.
- **Lệnh / nút được phép:**
  - Chạy bộ kiểm thử preflight cô lập:
    ```bash
    uv run pytest -q --tb=short tests/unit/test_live_readiness_reconciliation.py tests/e2e/test_live_readonly_preflight_e2e.py
    ```
  - Kiểm tra ranh giới egress/secret từ overlay Compose:
    ```bash
    uv run pytest -q --tb=short tests/e2e/test_live_production_compose.py
    ```
  - Kiểm tra NTP/Clock trên host: `chronyc tracking` hoặc `timedatectl status` (chênh lệch phải `< 1000ms`, timezone container luôn `TZ=UTC`).
- **Các bước cụ thể:**
  1. Xác nhận `TZ=UTC` và clock drift giữa host và `GET /fapi/v1/time` nhỏ hơn `1000ms`.
  2. Xác nhận chỉ duy nhất `execution-gateway` nằm trên mạng `exchange_egress_prod` và mount secret Binance.
  3. Chạy `LiveReadOnlyPreflightService.run_preflight()` để thu thập snapshot chỉ đọc (`account`, `balance`, `positionRisk`, `openOrders`, `algo_orders`) và đối chiếu với sổ cái cục bộ.
  4. Kiểm tra `verdict.passed == True` và lưu `verdict.evidence_hash`.
- **Expected Safe State:** Nếu bất kỳ mục nào lỗi/lệch, `bot_runtime_state.readiness = 'RECOVERY_REQUIRED'`, entry đóng, `risk_reservation` được giữ nguyên.
- **Tiêu chí dừng / Escalate:** Drift `> 1000ms`, sai `account_id`/`environment`, phát hiện service ngoài gateway có egress sàn → chặn ngay và báo Owner + Security.
- **Rollback / Recovery:** Đồng bộ lại NTP (`chronyc makestep` theo chính sách host), sửa sai lệch cấu hình rồi chạy lại preflight từ đầu.
- **Thông tin cần lưu:** `evidence_hash`, `reason_code`, độ lệch đồng hồ (ms), thời gian kiểm tra UTC.
- **Điều cấm:** Tuyệt đối cấm gọi `POST/PUT/DELETE /fapi/v1/order` trong bước preflight; cấm bỏ qua cảnh báo clock drift.

---

## RB-03. Xoay (Rotate) & Thu hồi (Revoke) Credential

- **Mức độ / Trigger:** Định kỳ theo lịch bảo mật hoặc khẩn cấp (SEV-1) khi nghi lộ API key, phát hiện quyền `WITHDRAW`/`TRANSFER` (`PREFLIGHT_API_WITHDRAWAL_ENABLED`), hoặc thay đổi nhân sự.
- **Người chịu trách nhiệm:** **Owner** (thao tác trực tiếp trên sàn Binance) + Operator (cập nhật secret store/container).
- **Tiền điều kiện:** Không gửi bất kỳ raw key/secret nào qua chat, email hay ticket.
- **Lệnh / nút được phép:**
  - Trên Admin Web: nhấn **Pause / Kill Workspace** để đóng entry mới.
  - Trên Binance Portal (**chỉ Owner thực hiện thủ công**): tạo/xóa API Key, giới hạn IP allowlist theo IP egress tĩnh, tắt hoàn toàn `Enable Withdrawals` và `Internal Transfer`.
- **Các bước cụ thể:**
  1. Đóng entry gate (`PAUSED` / `KILLED`); xác nhận protective orders đang mở vẫn được giữ nguyên.
  2. **[Owner thủ công trên Binance]:** Thu hồi key bị nghi lộ ngay lập tức; tạo Ed25519/API key mới chỉ bật quyền `Enable Reading` + `Enable Futures`, gắn IP egress tĩnh, **tắt** `Withdrawals`/`Transfer`.
  3. Nạp key mới vào secret vault / thư mục `CRYPIDIAN_LIVE_SECRET_DIR` (chỉ `execution-gateway` đọc được).
  4. Khởi động lại duy nhất `execution-gateway` để nạp secret mới.
  5. Chạy preflight chỉ đọc (**RB-02**) xác nhận `api_permissions` hợp lệ (có `READ`, không có `WITHDRAW`/`TRANSFER`).
- **Expected Safe State:** Key cũ vô hiệu; key mới chỉ đọc được trong `execution-gateway`; không có log/exception nào lộ chuỗi key/signature.
- **Tiêu chí dừng / Escalate:** Nếu phát hiện truy cập lạ hoặc lệnh lạ trên sàn khi key lộ → chuyển ngay sang **RB-06 (Foreign Activity)** và giữ hệ thống đóng hoàn toàn.
- **Rollback / Recovery:** Không bao giờ dùng lại key đã thu hồi; nếu key mới lỗi cấu hình IP/quyền, tạo key mới khác và lặp lại bước 2–5.
- **Thông tin cần lưu:** Thời điểm thu hồi key cũ, mã định danh key mới (chỉ 4 ký tự cuối/alias, KHÔNG lưu raw key), kết quả preflight sau xoay.
- **Điều cấm:** Cấm lưu key trong Git, `.env`, Compose YAML, log hoặc ticket; cấm bật quyền rút tiền.

---

## RB-04. Sự cố API Outage / Rate Limit `429` / IP Ban `418`

- **Mức độ / Trigger:** SEV-2 (`429` kéo dài hoặc `503` liên tiếp) / SEV-1 (`418` IP ban).
- **Người chịu trách nhiệm:** Operator trực + Risk Admin.
- **Tiền điều kiện:** Hệ thống đã tự động backoff và chuyển `PREFLIGHT_ENDPOINT_FAILURE` / `DEGRADED`.
- **Lệnh / nút được phép:** Đọc `GET /health/ready`, đọc log đã redact của `execution-gateway`, kích hoạt `PAUSE` workspace nếu chưa tự đóng.
- **Các bước cụ thể:**
  1. Đóng entry gate (`PAUSE`) để cắt toàn bộ traffic tạo lệnh mới.
  2. Kiểm tra header `Retry-After` và `X-MBX-USED-WEIGHT-1M` trong log `execution-gateway`.
  3. Tạm dừng các tiến trình polling không thiết yếu; **không** chạy vòng lặp retry dồn dập khi đã bị `429`/`418`.
  4. Nếu là `418`, chờ hết thời gian ban ghi trong `Retry-After` (có thể từ 2 phút đến 3 ngày tùy mức vi phạm) và báo Owner kiểm tra vị thế mở thủ công trên Binance.
  5. Sau khi sàn ổn định và hết cửa sổ phạt, chạy **RB-02 (Preflight)** đối soát toàn bộ lệnh/vị thế trước khi mở lại.
- **Expected Safe State:** Entry đóng; không gửi request spam; mọi lệnh đang `SUBMITTING` gặp `503` đã chuyển sang `UNKNOWN` và giữ nguyên `risk_reservation`.
- **Tiêu chí dừng / Escalate:** Bị `418` khi đang có vị thế mở hoặc `503` kéo dài > 5 phút → escalate ngay cho Owner để giám sát trực tiếp trên Binance.
- **Rollback / Recovery:** Chỉ mở lại sau khi hết thời gian `Retry-After`, kiểm tra trọng số weight trở về mức an toàn và preflight trả `PREFLIGHT_PASSED`.
- **Thông tin cần lưu:** Thời điểm bắt đầu/kết thúc, mã HTTP (`429`/`418`/`503`), giá trị `Retry-After`, danh sách intent chuyển `UNKNOWN`.
- **Điều cấm:** Cấm tắt backoff để retry liên tục; cấm giả định lệnh gặp `503` là "không khớp" mà xóa reservation.

---

## RB-05. Sự cố User-Stream Hết hạn, Stream Gap & Dữ liệu Stale

- **Mức độ / Trigger:** SEV-2 — cảnh báo `PREFLIGHT_STREAM_GAP_DETECTED`, `PREFLIGHT_SNAPSHOT_STALE`, `LIVE_RISK_MARK_STALE`, hoặc `listenKey` hết hạn/mất kết nối.
- **Người chịu trách nhiệm:** Operator trực.
- **Tiền điều kiện:** Hệ thống đã fail-closed đóng entry gate khi phát hiện lỗ hổng sequence hoặc giá mark quá hạn (`> 10s`).
- **Lệnh / nút được phép:** Kiểm tra `/health/ready`, tái tạo kết nối stream qua restart `execution-gateway` / `ingestion-worker`, chạy preflight đối soát REST.
- **Các bước cụ thể:**
  1. Xác nhận entry gate đã đóng (`RECOVERY_REQUIRED` hoặc `PAUSED`).
  2. Kiểm tra trạng thái WebSocket và độ tươi của giá mark (`mark_observed_at`).
  3. Khi stream kết nối lại, **không** tin cậy stream đơn lẻ: chạy preflight REST chỉ đọc (**RB-02**) để lấy snapshot `positions`, `openOrders`, `algo_orders` và `userTrades` bù khoảng trống.
  4. Xác nhận `filled_quantity` đơn điệu (không tụt giảm do event đến muộn/trùng).
- **Expected Safe State:** Entry đóng trong suốt thời gian mất đồng bộ; không có quyết định rủi ro nào dùng giá mark cũ hoặc ước lượng thay thế.
- **Tiêu chí dừng / Escalate:** Nếu REST và stream vẫn không đồng thuận (`PREFLIGHT_EXPOSURE_MISMATCH`) sau khi reconnect → chuyển sang **RB-06** và báo Risk Admin/Owner.
- **Rollback / Recovery:** Chỉ mở lại khi stream ổn định, giá mark tươi (`<= 10s`), và preflight REST khớp 100% sổ cái.
- **Thông tin cần lưu:** `last_processed_stream_seq`, sequence bị nhảy, thời gian mất đồng bộ, `evidence_hash` sau đối soát.
- **Điều cấm:** Cấm bỏ qua sequence gap; cấm điền giá mark giả/cũ để vượt qua cổng rủi ro.

---

## RB-06. Order `UNKNOWN`, Partial Fill, Foreign Activity & Reconcile Mismatch

- **Mức độ / Trigger:** SEV-1 — `PREFLIGHT_UNKNOWN_STATE_HELD`, `PREFLIGHT_FOREIGN_POSITION`, `PREFLIGHT_FOREIGN_ORDER`, `PREFLIGHT_EXPOSURE_MISMATCH`.
- **Người chịu trách nhiệm:** Risk Admin + Owner (bắt buộc cho resolve foreign activity / thao tác sàn).
- **Tiền điều kiện:** Entry gate đang đóng (`RECOVERY_REQUIRED`), toàn bộ `risk_reservation` (`RESERVED` & `UNKNOWN`) đang được giữ nguyên trong PostgreSQL.
- **Lệnh / nút được phép:**
  - Đọc bảng `risk_reservation`, `bot_runtime_state`, `account_activity_incident` trên Admin Web / DB chỉ đọc.
  - Với `UNKNOWN`: chạy đối soát từ bằng chứng sàn xác thực (`VenueOrderReconciliationService`).
  - Với `FOREIGN_POSITION` / `FOREIGN_ORDER`: **Owner xử lý thủ công trên Binance** (đóng/hủy lệnh ngoại lai), sau đó ghi resolution có lý do & MFA trên Admin Web.
- **Các bước cụ thể:**
  1. Giữ nguyên entry gate đóng; **không** tự động adopt, hủy hay sửa vị thế/lệnh ngoại lai bằng bot.
  2. Kiểm tra chi tiết incident (symbol, side, quantity, `client_order_id`, `evidence_hash`).
  3. Với lệnh `UNKNOWN`: đối chiếu `client_order_id` qua `GET /fapi/v1/openOrders` và `GET /fapi/v1/userTrades`. Chỉ chuyển trạng thái reservation khi có bằng chứng xác thực (`ORDER_ABSENT` hoặc `OPEN_RISK_RECORDED`).
  4. Với hoạt động ngoại lai: Owner kiểm tra nguyên nhân (nhập tay hay lộ key → nếu nghi lộ key chạy ngay **RB-03**), xử lý xong trên sàn rồi thực hiện resolve incident trên Admin Web.
  5. Chạy lại preflight chỉ đọc (**RB-02**) xác nhận sạch 100%.
- **Expected Safe State:** Không mất reservation; vị thế mở đã biết vẫn giữ nguyên protective order; không có lệnh mới nào được gửi cho tới khi incident đóng và preflight sạch.
- **Tiêu chí dừng / Escalate:** Nghi ngờ lộ API key hoặc không giải thích được nguồn gốc vị thế ngoại lai → nâng lên SEV-1, chạy ngay **RB-03** thu hồi key.
- **Rollback / Recovery:** Chỉ thực hiện **RB-10 (Resume)** sau khi mọi `UNKNOWN` đã giải quyết, mọi incident foreign đã `RESOLVED` và preflight trả `PREFLIGHT_PASSED`.
- **Thông tin cần lưu:** ID incident, `evidence_hash`, bằng chứng đối chiếu sàn, actor & lý do resolve.
- **Điều cấm:** Cấm xóa tay bản ghi `risk_reservation` trong DB; cấm gán `UNKNOWN` thành `0`/flat; cấm để bot tự động hủy lệnh không do bot tạo.

---

## RB-07. Thiếu hoặc Bị từ chối Protective / Algo Order

- **Mức độ / Trigger:** SEV-1 — `PREFLIGHT_PROTECTION_MISSING`, hoặc Algo/Stop-Loss order bị sàn từ chối/hủy ngoài ý muốn khi đang có vị thế mở.
- **Người chịu trách nhiệm:** Operator trực + Owner (can thiệp sàn ngay nếu bot không đặt lại được bảo vệ).
- **Tiền điều kiện:** Đang tồn tại vị thế mở trên sàn nhưng thiếu lệnh cắt lỗ tương ứng.
- **Lệnh / nút được phép:** Đóng entry mới ngay (`PAUSE`/`KILL`), kiểm tra `algo_orders` trên sàn; Owner đặt stop-loss thủ công hoặc thực hiện quy trình **Flatten (RB-10)** có phê duyệt.
- **Các bước cụ thể:**
  1. Giữ entry gate đóng ngay lập tức (`PREFLIGHT_PROTECTION_MISSING`).
  2. Kiểm tra lý do lệnh bảo vệ bị từ chối (ví dụ: vượt bộ lọc giá/khối lượng, giá thị trường đã vượt mức stop, hoặc lỗi sàn).
  3. Nếu đang có vị thế hở không bảo vệ: thông báo ngay cho **Owner** để đặt Stop-Loss bảo vệ thủ công trên Binance hoặc phê duyệt lệnh **Flatten** đóng vị thế.
  4. Sau khi vị thế đã được bảo vệ hoặc đóng hoàn toàn, chạy lại preflight (**RB-02**).
- **Expected Safe State:** Entry đóng; vị thế không bị bỏ trống kéo dài; mọi protective order hợp lệ của các symbol khác vẫn được giữ nguyên.
- **Tiêu chí dừng / Escalate:** Vị thế mở không có stop-loss quá 60 giây → escalate SEV-1 trực tiếp cho Owner.
- **Rollback / Recovery:** Chỉ xem xét resume sau khi sửa nguyên nhân reject protective order và xác nhận preflight sạch.
- **Thông tin cần lưu:** Symbol, khối lượng vị thế, mã lỗi reject của sàn, hành động bảo vệ đã thực hiện, thời gian phát hiện và khắc phục.
- **Điều cấm:** Cấm mở thêm vị thế mới khi có bất kỳ vị thế nào thiếu bảo vệ; cấm xóa các lệnh stop-loss đang hợp lệ của bot.

---

## RB-08. Sự cố DB hoặc Control-Plane Outage

- **Mức độ / Trigger:** SEV-1 — PostgreSQL mất kết nối, `/health/ready` trả `503`, hoặc `admin-web` / control plane không phản hồi.
- **Người chịu trách nhiệm:** Operator trực + Owner.
- **Tiền điều kiện:** Runtime hoạt động theo nguyên tắc fail-closed: mất DB/lease thì không thể ghi nhận quyết định rủi ro và phải ngừng mở vị thế mới.
- **Lệnh / nút được phép:** Kiểm tra container health (`docker compose ps`), log PostgreSQL, khôi phục dịch vụ DB hoặc chuyển sang **RB-09 (Restore)**.
- **Các bước cụ thể:**
  1. Xác nhận `trading-runtime` đã dừng cấp entry mới (không giữ được `bot_runtime_lease` hoặc không thể `FOR UPDATE` trên `trading_account`).
  2. Kiểm tra tài nguyên host (disk full, OOM, kết nối PostgreSQL).
  3. **Owner** kiểm tra trực tiếp trên Binance để bảo đảm các vị thế đang mở vẫn còn protective orders.
  4. Khôi phục PostgreSQL; nếu hỏng dữ liệu, thực hiện **RB-09** vào target cô lập để kiểm tra tính toàn vẹn trước khi cắt chuyển.
  5. Khi DB và control plane hoạt động lại, chạy preflight chỉ đọc (**RB-02**) để đối soát trạng thái trước/sau gián đoạn.
- **Expected Safe State:** Không có lệnh mới nào được gửi khi DB mất kết nối; mọi transaction lỗi đã rollback sạch, không làm mất `UNKNOWN` reservation đã commit trước đó.
- **Tiêu chí dừng / Escalate:** DB không phục hồi sau 5 phút hoặc nghi hỏng dữ liệu → escalate SEV-1 cho Owner và kích hoạt quy trình restore **RB-09**.
- **Rollback / Recovery:** Khôi phục DB + chạy `alembic current` + chạy preflight **RB-02** xác nhận toàn bộ trạng thái khớp sàn.
- **Thông tin cần lưu:** Thời điểm mất kết nối, nguyên nhân sự cố, kết quả kiểm tra tính toàn vẹn DB, `evidence_hash` preflight sau phục hồi.
- **Điều cấm:** Cấm chạy runtime ở chế độ "bỏ qua DB"; cấm tự ý chạy `docker compose down -v` trên volume production.

---

## RB-09. Backup Mã hóa & Restore Cô lập (Đối chiếu CRY-269 & CRY-465)

- **Mức độ / Trigger:** Định kỳ theo lịch backup hoặc khi cần diễn tập/phục hồi thảm họa (SEV-1).
- **Người chịu trách nhiệm:** Owner hoặc kỹ sư vận hành được ủy quyền.
- **Tiền điều kiện:**
  - Tham chiếu chi tiết lệnh tại [backup-restore-runbook.md](./backup-restore-runbook.md).
  - **Lưu ý tính áp dụng bằng chứng cũ (`CRY-269`):** Bằng chứng diễn tập `CRY-269` và `CRY-465` (`tests/e2e/test_live_backup_restore.py`) được đo trên **PostgreSQL Compose disposable local**, **KHÔNG** thay thế bằng chứng trên host/storage production thật. Sau khi Owner chốt topology production (`[BLOCKER-TOPOLOGY]`), bắt buộc chạy lại diễn tập trên hạ tầng thật và gắn evidence mới.
- **Lệnh / nút được phép:**
  - Diễn tập cô lập trên môi trường test:
    ```bash
    uv run pytest -q --tb=short tests/e2e/test_live_backup_restore.py
    ```
  - Backup mã hóa & restore drill: `uv run python scripts/backup_restore_drill.py backup ...` và `restore-drill ...` (chi tiết trong [backup-restore-runbook.md](./backup-restore-runbook.md)).
- **Các bước cụ thể:**
  1. Chạy backup mã hóa AES-256-GCM với passphrase từ secret vault (`CRYPIDIAN_BACKUP_PASSPHRASE`), xác minh file `.pgdump.sql` bản rõ đã bị xóa và `secret_canaries` không rò rỉ.
  2. Restore vào **database cô lập** (tên khác `crypidian`), xác minh `verified=True`, `alembic_version`, SHA-256 trước/sau giải mã và đối chiếu row count.
  3. Ghi nhận RPO/RTO thực đo (giây) vào biên bản diễn tập.
- **Expected Safe State:** Bản sao lưu được mã hóa toàn vẹn; dữ liệu DB chính không bị chạm khi diễn tập restore.
- **Tiêu chí dừng / Escalate:** Checksum sai, giải mã lỗi, hoặc RTO vượt SLA đã duyệt → dừng, báo Owner và tạo bản backup sạch mới ngay.
- **Rollback / Recovery:** Luôn thực hiện restore vào DB cô lập và đối soát trước khi cắt chuyển.
- **Thông tin cần lưu:** `backup_id`, `manifest.sha256`, `rpo_seconds`, `rto_seconds`, `alembic_version`, row count đối chiếu.
- **Điều cấm:** Cấm chạy `restore-drill` trực tiếp vào DB `crypidian` đang chạy; cấm lưu passphrase vào shell history hoặc Git.

---

## RB-10. Phân tách `Pause / Entry Kill` vs `Flatten` và Quy trình `Resume`

- **Mức độ / Trigger:** Mọi tình huống cần dừng khẩn cấp hoặc khôi phục giao dịch sau sự cố.
- **Người chịu trách nhiệm:**
  - `Pause` / `Entry Kill`: Operator / Risk Admin / Owner (hiệu lực ngay lập tức, 1 người thực hiện để cắt rủi ro tức thời).
  - `Flatten`: Hành động đóng vị thế riêng biệt — cần **Owner + Approver độc lập (maker-checker)** và xác minh vị thế về 0 sau thực thi.
  - `Resume`: Yêu cầu **Operator/Owner + Risk Approver độc lập** (maker-checker sau kill/sự cố rủi ro).
- **Tiền điều kiện:**
  - Hiểu rõ: `Pause` / `Entry Kill` **CHỈ chặn `NEW_ENTRY`**, tuyệt đối **KHÔNG hủy lệnh Stop-Loss/Take-Profit bảo vệ** đang có trên sàn.
  - Để `Resume`, bắt buộc: (1) nguyên nhân gốc đã được đóng, (2) mọi `UNKNOWN` & `account_activity_incident` đã `RESOLVED`, (3) preflight chỉ đọc (**RB-02**) trả `PREFLIGHT_PASSED`, (4) profile rủi ro còn hạn và đủ chữ ký maker-checker.
- **Lệnh / nút được phép:** Nút **Pause Bot** / **Kill Workspace**, nút **Flatten** (luồng riêng có step-up MFA + lý do), nút **Resume** trên Admin Web (kèm MFA + phê duyệt).
- **Các bước cụ thể:**
  1. **Khi cần dừng khẩn cấp:** Kích hoạt `Pause` hoặc `Kill Workspace`. Xác minh `entry_allowed == False` và xác minh các protective orders của vị thế mở vẫn còn nguyên.
  2. **Chỉ khi cần đóng vị thế (`Flatten`):** Thực hiện quy trình phê duyệt riêng (maker-checker + step-up MFA), theo dõi lệnh giảm vị thế (reduce-only) cho tới khi xác nhận vị thế trên sàn về `0` qua preflight.
  3. **Khi `Resume`:** Xác nhận toàn bộ 4 tiền điều kiện ở trên, ghi lý do khôi phục và chữ ký Approver độc lập, sau đó mở lại cổng entry.
- **Expected Safe State:** `Pause/Kill` giữ nguyên lớp bảo vệ; `Flatten` không bao giờ tự chạy ngầm khi bấm `Kill`; `Resume` bị từ chối nếu còn bất kỳ điều kiện nào chưa sạch.
- **Tiêu chí dừng / Escalate:** Nếu nút `Pause/Kill` không phản hồi → Owner thu hồi API key ngay theo **RB-03** để cắt hoàn toàn đường ra sàn.
- **Rollback / Recovery:** Nếu phát hiện bất thường ngay sau `Resume`, lập tức bấm `Pause/Kill` quay lại trạng thái đóng entry.
- **Thông tin cần lưu:** Actor thực hiện, Approver duyệt (nếu `Flatten`/`Resume`), timestamp UTC, lý do, `evidence_hash` của preflight sạch.
- **Điều cấm:** Cấm gộp `Flatten` vào chung hành động `Kill`; cấm `Resume` khi chưa reconcile sạch hoặc tự duyệt (maker == checker).

---

## RB-11. Alert Routing, On-Call & Escalation

- **Mức độ / Trigger:** Mọi cảnh báo vận hành phát qua `operational_alert_outbox` (`CRY-274`) hoặc health monitor.
- **Người chịu trách nhiệm:**
  - **Primary On-Call:** `[BLOCKER-ONCALL — Chờ Owner chỉ định chính thức, không tự phát minh]`
  - **Secondary On-Call / Escalation:** `[BLOCKER-ONCALL — Chờ Owner chỉ định chính thức]`
  - **Alert Destinations (Primary / Fallback):** `[BLOCKER-ALERT-DEST — Chờ Owner cấu hình endpoint/kênh chính thức]`
- **Phân mức độ & SLA phản hồi mục tiêu (đề xuất chờ Owner ký duyệt):**
  - **SEV-1 (Khẩn cấp — lộ key, `418`, `FOREIGN_POSITION`, `PROTECTION_MISSING`, DB outage):** Đóng entry tức thời; escalate ngay lập tức tới Primary + Owner; nếu không phản hồi sau 5 phút chuyển Secondary.
  - **SEV-2 (Nghiêm trọng — `429`, stream gap, `UNKNOWN`, stale mark, outbox dead-letter):** Đóng entry tức thời; phản hồi trong 15 phút.
  - **SEV-3 (Cảnh báo — tiệm cận cap lỗ/exposure, worker degraded):** Kiểm tra và xử lý trong ca trực.
- **Dự phòng khi kênh Telegram/Chat lỗi:**
  - Cảnh báo được lưu bền trong `operational_alert_outbox` (retry exponential backoff + dead-letter) và luôn hiển thị trên Admin Web (`/admin/incidents/operational-alerts`) — đã kiểm chứng qua `CRY-274`.
- **Điều cấm:** Cấm gửi raw secret, API key, số dư nhạy cảm hoặc PII vào nội dung alert; cấm tắt cảnh báo mà không ghi lý do và thời hạn.

---

## Phụ lục A. Biên bản Diễn tập Disposable (Synthetic — 2026-11-25)

Toàn bộ các ca diễn tập dưới đây được thực thi tự động bằng [test_live_operations_runbook_drill_e2e.py](../../tests/e2e/test_live_operations_runbook_drill_e2e.py) trên môi trường **disposable cô lập**, với `autouse` outbound socket guard cấm mọi truy cập `*.binance.com`, sử dụng synthetic account/transport và tuyệt đối không gửi/hủy lệnh thật.

| Mã Drill | Kịch bản diễn tập | Trigger | Actor | Safe State kỳ vọng & thực tế | Evidence / Kiểm chứng |
| --- | --- | --- | --- | --- | --- |
| **DR-01** | Pause/Kill không xóa protective order & tách biệt Flatten (RB-10) | `paused_or_killed=True` | `operator-drill` | Entry mới bị chặn (`LIVE_RISK_PAUSED_OR_KILLED`); `algo_orders` / `active_protection_symbols` giữ nguyên 100%; `flatten` không bị kích hoạt | `test_drill_pause_kill_preserves_protection_and_resume_requires_clean_reconcile` |
| **DR-02** | Order `UNKNOWN` & Stream Gap (RB-05, RB-06) | `unknown_intent_ids` / `stream_seq` nhảy 100→105 | `runtime-drill` | `PREFLIGHT_UNKNOWN_STATE_HELD` / `PREFLIGHT_STREAM_GAP_DETECTED`, `hold_reservations=True`, sinh SHA-256 `evidence_hash` | `test_drill_unknown_stream_gap_and_key_revoke_fail_closed` |
| **DR-03** | Key bật quyền `WITHDRAW` / thu hồi quyền `READ` (RB-03) | `WITHDRAW` trong `api_permissions` / thiếu `READ` | `owner-drill` | Đóng entry ngay (`PREFLIGHT_API_WITHDRAWAL_ENABLED` / `PREFLIGHT_API_PERMISSIONS_INSUFFICIENT`), không lộ raw key | `test_drill_unknown_stream_gap_and_key_revoke_fail_closed` |
| **DR-04** | Resume chỉ khi reconcile sạch + maker-checker hợp lệ (RB-10) | Tự duyệt hoặc còn lỗi preflight | `operator-drill` + `risk-approver-drill` | Bị từ chối nếu tự duyệt (`LIVE_RISK_MAKER_CHECKER_VIOLATION`) hoặc preflight lỗi; chỉ pass khi preflight sạch + 2 actor khác nhau | `test_drill_pause_kill_preserves_protection_and_resume_requires_clean_reconcile` |
| **DR-05** | PostgreSQL Compose: Alert Escalation + Isolated Backup/Restore (RB-09, RB-11) | Sự cố ghi `bot_runtime_state` + backup mã hóa & restore cô lập | `owner-drill` | Denial lưu bền trong PostgreSQL, backup mã hóa xóa plaintext, wrong passphrase fail-closed, restore DB cô lập khớp row count & đo RTO thực tế | `test_drill_postgres_denial_and_isolated_backup_restore` |
