# Runbook Sao lưu Mã hóa & Diễn tập Phục hồi Cô lập (OPS07C)

**Mục tiêu:** Đảm bảo cơ sở dữ liệu PostgreSQL và manifest object-store của Crypidian được sao lưu có mã hóa (AES-256-GCM), không chứa plain secret, kiểm tra toàn vẹn checksum SHA-256, và có thể phục hồi vào môi trường cô lập với **RPO/RTO thực đo** đạt ngưỡng SLA.

- **Ngưỡng SLA mặc định (`RestoreDrillPolicy`):**
  - **RPO tối đa:** `900s` (15 phút)
  - **RTO tối đa:** `300s` (5 phút)
- **Quyền thực hiện:** Chỉ tài khoản thuộc vai trò `Owner` hoặc kỹ sư vận hành được ủy quyền. Không bao giờ chạy lệnh restore đè lên môi trường `production`, `prod`, `live` hoặc database chính `crypidian`.

---

## Checklist xác nhận từng bước

### 1. Trước khi sao lưu (Pre-flight)
- [ ] Xác nhận dung lượng đĩa trống tối thiểu (mặc định ≥ 100 MiB) tại thư mục đích sao lưu.
- [ ] Xác nhận passphrase mã hóa được nạp từ biến môi trường (`CRYPIDIAN_BACKUP_PASSPHRASE`), **không** ghi vào CLI history, log hoặc commit vào git.
- [ ] Xác nhận danh sách `secret_canaries` (API key/token mẫu) để kiểm tra chống rò rỉ plain secret trước khi đóng gói.

### 2. Tạo bản sao lưu mã hóa
- [ ] Chạy `uv run python scripts/backup_restore_drill.py backup` trỏ tới Compose project nguồn:
  ```bash
  export CRYPIDIAN_BACKUP_PASSPHRASE="<passphrase-tu-secret-vault>"
  uv run python scripts/backup_restore_drill.py backup \
    --source-project crypidian-review \
    --output-dir /var/backups/crypidian/drill-01 \
    --backup-id bkp-$(date -u +%Y%m%d-%H%M%S) \
    --operator-id owner@crypidian.io
  ```
- [ ] Xác nhận file `.pgdump.sql` bản rõ đã bị xóa tự động ngay sau khi tạo `.pgdump.sql.enc`.
- [ ] Xác nhận tệp `*.manifest.json` chứa đủ `sha256_hex`, `plaintext_sha256_hex`, `byte_count` và `encrypted: true`.

### 3. Diễn tập phục hồi vào môi trường cô lập
- [ ] Xác nhận `target_environment` là môi trường cô lập (ví dụ `isolated-drill`) và `isolated_db_name` **khác** `crypidian` (ví dụ `crypidian_restore_drill`).
- [ ] Chạy lệnh restore drill:
  ```bash
  uv run python scripts/backup_restore_drill.py restore-drill \
    --manifest /var/backups/crypidian/drill-01/<backup-id>.manifest.json \
    --backup-dir /var/backups/crypidian/drill-01 \
    --restore-dir /tmp/crypidian-restore-drill \
    --target-environment isolated-drill \
    --target-project crypidian-drill-isolated \
    --isolated-db-name crypidian_restore_drill
  ```
- [ ] Xác nhận kiểm tra checksum SHA-256 trước giải mã và sau giải mã đều đạt.
- [ ] Xác nhận `alembic_version` trong DB cô lập khớp phiên bản migration hiện hành.
- [ ] Đối chiếu số lượng bản ghi các bảng cốt lõi (`workspace`, `bot`, `order`, `trade`, `audit_log`, `bot_config_version`) và artifact manifest.
- [ ] Lưu tệp báo cáo `*.restore-report.json` làm bằng chứng RPO/RTO thực đo (`rpo_within_sla: true`, `rto_within_sla: true`).

### 4. Kiểm tra tương thích Migration, Lock & Disk Alert
- [ ] Khi nâng/hạ migration (xem WP-01C): kiểm tra `alembic heads` và chạy test downgrade/upgrade trên DB cô lập trước khi áp dụng lên DB chính.
- [ ] Không chạy `pg_dump` khóa toàn bảng khi có giao dịch nặng kéo dài; ưu tiên snapshot nhất quán của PostgreSQL.
- [ ] Thiết lập cảnh báo đầy đĩa khi dung lượng trống xuống dưới ngưỡng an toàn (hàm `check_disk_space_available` sẽ chủ động chặn nếu thiếu chỗ).

### 5. Quy trình khôi phục & xoay vòng Secret Provider độc lập
- Secret ứng dụng (Binance API key, Telegram bot token, session signing key, MFA encryption key) **không** nằm trong backup PostgreSQL.
- Khi xảy ra sự cố mất môi trường hoặc nghi ngờ lộ khóa:
  1. Kích hoạt `workspace kill` qua Admin Web hoặc API để đóng entry gate ngay lập tức.
  2. Thu hồi (revoke) API key cũ trên Binance và cấp cặp key mới có IP restriction + tắt quyền rút tiền (no-withdrawal).
  3. Xoay vòng passphrase sao lưu và ghi nhận sự kiện vào audit log (không ghi giá trị secret).
  4. Kiểm tra lại health check trước khi mở lại phiên PAPER/Demo.
