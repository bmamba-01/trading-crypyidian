# Kiểm kê Secret và Runbook Xoay vòng An toàn (SEC-44)

**Mục tiêu:** Chuẩn hóa danh mục toàn bộ bí mật (secret) trong hệ thống Crypidian, quy định quyền tệp POSIX bắt buộc (`0700` cho thư mục `.secrets/`, `0600` cho từng tệp bí mật), định danh `key_id` cho khóa mã hóa MFA để xoay không mất dữ liệu, và quy trình thao tác xoay vòng định kỳ hoặc khẩn cấp.

**Quyền thực hiện:** Chỉ tài khoản `Owner` hoặc kỹ sư vận hành được ủy quyền trực tiếp quản lý Secret Vault / máy chủ triển khai. Tuyệt đối **không** in giá trị secret ra màn hình, ticket, prompt AI, log, hay commit lên Git.

**Tài liệu tham chiếu (đã đối chiếu ngày 19/11/2026):**
- [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) (ASVS 5.0 V13.1.4, V13.3.1, V13.3.4)
- [Docker Compose Secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [Runbook Xoay vòng Token Telegram Bot](./telegram-token-rotation-runbook.md)

---

## 1. Nguyên tắc lưu trữ & phân tách đặc quyền

1. **Cách ly theo tiến trình (`SEC-01`, `SEC-41`, `SEC-44`):**
   - Mỗi dịch vụ dùng một login PostgreSQL và tệp mật khẩu riêng; không dùng chung tài khoản `crypidian_admin` cho ứng dụng.
   - `binance_api_key` và `binance_api_secret` **chỉ** được cấp cho `execution-gateway` (trong chế độ `DEMO`/`LIVE`), tuyệt đối không mount vào `admin-web`, `trading-runtime`, `ingestion-worker` hay `trainer-worker`.
2. **Quyền tệp bắt buộc trên host POSIX:**
   - Thư mục `.secrets/` phải có quyền **`0700`** (`drwx------`).
   - Mọi tệp bí mật bên trong `.secrets/` phải có quyền **`0600`** (`-rw-------`); từ chối mọi quyền đọc/ghi/thực thi cho `group` và `other`.
   - Kịch bản `scripts/prepare_local_secrets.py` tự động tạo thư mục `0700` và ép `chmod 0600` cho mọi tệp mới lẫn tệp đã tồn tại trước đó.
3. **Ngoài môi trường local (`production`):**
   - Không lưu plaintext secret dài hạn trên đĩa host; dùng Secret Vault / Cloud KMS với lease ngắn hạn qua `SecretProvider`.
   - Mọi secret chỉ truyền vào container qua `/run/secrets/<tên>` (chỉ đọc) hoặc `SecretProvider`, không đưa vào biến môi trường (`ENV`) ngoại trừ tệp khởi tạo local `garage.env`.

---

## 2. Danh mục Kiểm kê Secret Hệ thống (`SYSTEM_SECRET_INVENTORY`)

Danh mục mã nguồn chuẩn nằm tại `src/trading_bot/common/secret_inventory.py` (`SYSTEM_SECRET_INVENTORY`).

| Tên secret / tệp | Nhóm | Dịch vụ sử dụng | Lưu trữ Local / Production | Chu kỳ xoay tối đa | Ghi chú bảo mật |
| --- | --- | --- | --- | --- | --- |
| `postgres_password` | Hạ tầng DB | `postgres`, `migrator`, `db-provisioner`, `qa-seed` | `.secrets/postgres_password` (`0600`) / Managed KMS | **90 ngày** | Chỉ dùng cho migrate & provision role |
| `database_admin_web_password` | DB Role | `admin-web` | `.secrets/database_admin_web_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_admin_web` |
| `database_trading_runtime_password` | DB Role | `trading-runtime` | `.secrets/database_trading_runtime_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_trading_runtime` |
| `database_execution_gateway_password` | DB Role | `execution-gateway` | `.secrets/database_execution_gateway_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_execution_gateway` |
| `database_ingestion_worker_password` | DB Role | `ingestion-worker` | `.secrets/database_ingestion_worker_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_ingestion_worker` |
| `database_signal_worker_password` | DB Role | `telegram-signal-worker` | `.secrets/database_signal_worker_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_signal_worker` (`NOINHERIT`) |
| `database_market_signal_worker_password` | DB Role | `market-signal-worker` | `.secrets/database_market_signal_worker_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_market_signal_worker` (`NOINHERIT`) |
| `database_trainer_worker_password` | DB Role | `trainer-worker` | `.secrets/database_trainer_worker_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_trainer_worker` |
| `database_learning_evaluator_password` | DB Role | `learning-evaluator` | `.secrets/database_learning_evaluator_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_learning_evaluator` (`NOINHERIT`) |
| `database_learning_shadow_observer_password` | DB Role | `learning-shadow-observer` | `.secrets/database_learning_shadow_observer_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_learning_shadow_observer` (`NOINHERIT`) |
| `database_replay_writer_password` | DB Role | `backtest-worker` | `.secrets/database_replay_writer_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_replay_writer` (`NOINHERIT`) |
| `database_candidate_builder_password` | DB Role | `candidate-proof-builder`, `learning-split-builder` | `.secrets/database_candidate_builder_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_candidate_builder` (`NOINHERIT`) |
| `database_backtest_worker_password` | DB Role | `backtest-worker` | `.secrets/database_backtest_worker_password` (`0600`) / Vault | **90 ngày** | Role `crypidian_backtest_worker` (`NOINHERIT`) |
| `database_scorer_history_password` | DB Role | `admin-web` | `.secrets/database_scorer_history_password` (`0600`) / Vault | **90 ngày** | Role chỉ đọc `crypidian_scorer_history` (`NOINHERIT`) |
| `mfa_encryption_key` | Mã hóa MFA | `admin-web`, `qa-seed` | `.secrets/mfa_encryption_key` (`0600`) / KMS | **365 ngày** | AES-256-GCM kèm `key_id` & `version` trong ciphertext |
| `telegram_bot_token` | Telegram | `admin-web`, `telegram-signal-worker` | `.secrets/telegram_bot_token` (`0600`) / Vault lease | **365 ngày** | Nạp qua `SecretProvider` (`300s` lease), fail-closed ở prod |
| `telegram_webhook_secret` | Telegram | `admin-web` | `.secrets/telegram_webhook_secret` (`0600`) / Vault | **90 ngày** | Header `X-Telegram-Bot-Api-Secret-Token` (32–256 ký tự) |
| `binance_api_key` | Sàn giao dịch | Chỉ `execution-gateway` | `.secrets/binance_api_key` (`0600`) / Vault lease | **90 ngày** | Ed25519/RSA, IP restriction, tắt rút/chuyển tiền |
| `binance_api_secret` | Sàn giao dịch | Chỉ `execution-gateway` | `.secrets/binance_api_secret` (`0600`) / Vault lease | **90 ngày** | Đọc qua `SecretProvider` gateway, không nạp ở PAPER |
| `bootstrap_token` | Khởi tạo | `admin-web` | `.secrets/bootstrap_token` (`0600`) / Vault một lần | **1 ngày** | Dùng một lần tạo Owner đầu tiên rồi xóa tệp/gỡ mount |
| `garage_rpc_secret`, `garage_s3_access_key`, `garage_s3_secret_key`, `garage.env` | Object Store | `object-store` | `.secrets/*` (`0600`) / IAM Role + Vault | **90 ngày** | Lưu trữ cục bộ S3-compatible |
| Phiên / CSRF / Step-up / `listenKey` | Runtime bộ nhớ & DB | `admin-web`, `execution-gateway` | Hash trong DB / bộ nhớ (`SEC-10`, `SEC-42`) | **Theo phiên / ≤ 60 phút** | Tự xoay khi login/reauth/step-up; `listenKey` keepalive < 60 phút |

---

## 3. Kiểm tra tự động quyền `0600` và tuổi xoay vòng

Chạy lệnh kiểm tra tự động trước khi triển khai hoặc trong lịch bảo trì định kỳ:

```bash
# 1. Chuẩn hóa quyền 0700/0600 cho toàn bộ .secrets/
uv run python scripts/prepare_local_secrets.py

# 2. Kiểm tra danh mục secret, key_id của MFA và quyền POSIX 0600
uv run python -m trading_bot.observability.supply_chain --check-secrets-dir .secrets
```

Để kiểm tra thêm cảnh báo quá hạn xoay vòng theo thời gian sửa đổi tệp (`mtime`), gọi Python helper:

```bash
uv run python -c "
from pathlib import Path
from trading_bot.common.secret_inventory import verify_secret_inventory_and_permissions
report = verify_secret_inventory_and_permissions(Path('.secrets'), check_rotation_age=True)
print('passed=', report.passed, 'mfa_key_id=', report.mfa_key_id)
print('violations=', report.violations)
print('rotation_warnings=', report.rotation_warnings)
"
```

---

## 4. Runbook xoay vòng từng nhóm Secret

### 4.1. Xoay mật khẩu DB Process Role (chu kỳ ≤ 90 ngày)
1. Chọn role cần xoay (ví dụ `database_admin_web_password`).
2. Sinh giá trị ngẫu nhiên mới ≥ 48 byte URL-safe và ghi nguyên tử với quyền **`0600`**:
   ```bash
   install -m 0600 /dev/null .secrets/database_admin_web_password.new
   uv run python -c "import secrets, sys; sys.stdout.write(secrets.token_urlsafe(48))" > .secrets/database_admin_web_password.new
   mv -f .secrets/database_admin_web_password.new .secrets/database_admin_web_password
   chmod 0600 .secrets/database_admin_web_password
   ```
3. Đồng bộ mật khẩu vào PostgreSQL bằng container `db-provisioner` (chạy `ALTER ROLE`):
   ```bash
   docker compose run --rm --no-deps db-provisioner
   ```
4. Khởi tạo lại dịch vụ đích để nạp mật khẩu mới từ `/run/secrets/*`:
   ```bash
   docker compose up -d --no-deps --force-recreate admin-web
   ```
5. Xác nhận `GET /health/ready` trả HTTP `200` và không có lỗi xác thực PostgreSQL trong log.

### 4.2. Xoay khóa mã hóa MFA (`mfa_encryption_key`) không mất dữ liệu (chu kỳ ≤ 365 ngày)
`LocalMfaSecretProvider` gắn `"version": 2` và `"key_id"` vào mỗi ciphertext `.bin` (ví dụ `mfa-v1-<sha256_prefix>`), cho phép giải mã bằng khóa cũ và mã hóa lại sang khóa mới mà không làm hỏng seed TOTP của người dùng.

1. Sao lưu cô lập volume `mfa_secrets` trước khi thao tác (theo [Runbook Sao lưu](./backup-restore-runbook.md)).
2. Sinh khóa AES-256 mới (32 byte Base64URL) vào tệp tạm có quyền `0600`, đồng thời giữ bản sao khóa cũ ở đường dẫn chỉ đọc tạm thời (`mfa_encryption_key.prev`, quyền `0600`):
   ```bash
   cp -p .secrets/mfa_encryption_key .secrets/mfa_encryption_key.prev
   chmod 0600 .secrets/mfa_encryption_key.prev
   install -m 0600 /dev/null .secrets/mfa_encryption_key.new
   uv run python -c "import base64, secrets, sys; sys.stdout.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii'))" > .secrets/mfa_encryption_key.new
   mv -f .secrets/mfa_encryption_key.new .secrets/mfa_encryption_key
   chmod 0600 .secrets/mfa_encryption_key
   ```
3. Khởi tạo `LocalMfaSecretProvider` với `key_file=.secrets/mfa_encryption_key` và `previous_keys={old_key_id: Path('.secrets/mfa_encryption_key.prev')}`, sau đó gọi `rotate_mfa_secret(workspace_id, reference)` cho từng bản ghi MFA đang hoạt động.
4. Xác nhận `inspect_ciphertext_key_id(reference)` của mọi tệp `.bin` đã chuyển sang `active_key_id` mới và giải mã thử khớp giá trị mong đợi.
5. Recreate `admin-web` và xóa `.secrets/mfa_encryption_key.prev` bằng xóa an toàn sau khi xác nhận 100% người dùng đăng nhập TOTP bình thường.

### 4.3. Xoay Telegram Bot Token & Webhook Secret (chu kỳ 365 ngày / 90 ngày)
- Chi tiết quy trình thu hồi `/revoke` trên `@BotFather`, cập nhật `SecretProvider` và xử lý HTTP `401`/`429` xem tại [Runbook Xoay vòng Token Telegram Bot](./telegram-token-rotation-runbook.md).
- Khi xoay `telegram_webhook_secret` (chu kỳ ≤ 90 ngày):
  1. Sinh chuỗi mới `[A-Za-z0-9_-]` độ dài ≥ 32 ký tự vào `.secrets/telegram_webhook_secret` (mode `0600`).
  2. Gọi lại Bot API `setWebhook` với `secret_token` mới (kèm `drop_pending_updates=true` nếu nghi lộ).
  3. Recreate `admin-web` để nạp webhook secret mới.

### 4.4. Xoay cặp Binance API Key / Secret (chu kỳ ≤ 90 ngày hoặc khi đổi IP egress)
1. Trên Binance Futures API Management, tạo key **Ed25519** (hoặc RSA nếu REST yêu cầu), chỉ bật **Futures + Reading**, tắt hoàn toàn withdrawal/transfer/spot/margin, và gắn **IP Access Restriction** đúng IP egress cố định của `execution-gateway`.
2. Đưa cặp key/secret mới vào Secret Vault của `execution-gateway` (trong môi trường `DEMO`/`LIVE`; **không** tạo key thật ở profile `PAPER`).
3. Xác nhận `execution-gateway` vượt qua self-check `GET /sapi/v1/account/apiRestrictions` (`ipRestrict=true`, `enableFutures=true`, các quyền rút/chuyển = `false`).
4. Theo dõi giao dịch/đối soát ổn định rồi thu hồi và xóa key cũ trên Binance.

### 4.5. Vô hiệu hóa Bootstrap Token sau khởi tạo hệ thống
1. Ngay sau khi tạo tài khoản `Owner` đầu tiên thành công:
   - Xóa tệp `.secrets/bootstrap_token` (hoặc gỡ khỏi Secret Vault) trên môi trường triển khai.
   - Ở `production`, gỡ mount secret `bootstrap_token` khỏi dịch vụ `admin-web`.

### 4.6. Xoay Garage RPC Secret & S3 Credentials (chu kỳ ≤ 90 ngày)
1. Tạo `garage_rpc_secret`, `garage_s3_access_key`, `garage_s3_secret_key` mới quyền `0600` và cập nhật `.secrets/garage.env` (`0600`).
2. Recreate `object-store` và xác minh `/garage status` healthy.

---

## 5. Playbook ứng phó sự cố nghi lộ secret khẩn cấp (< 5 phút)

1. **Đóng cổng giao dịch ngay lập tức:** Kích hoạt `workspace kill` qua Admin Web hoặc Telegram để chặn mọi lệnh vào mới.
2. **Cắt nguồn lộ bên ngoài:**
   - Lộ key Binance → **Xóa/Vô hiệu hóa ngay** API key trên Binance trước tiên.
   - Lộ Telegram token → Gửi `/revoke` tới `@BotFather` trong < 1 phút.
3. **Xoay toàn bộ credential bị ảnh hưởng:** Thực hiện mục 4.1 (DB roles), 4.2 (MFA key), 4.3 (Telegram), 4.6 (Garage).
4. **Thu hồi phiên & rà soát kiểm toán:** Thu hồi toàn bộ phiên đăng nhập đang mở, kiểm tra chuỗi băm bất biến `audit_event` (`SEC-33`) và cảnh báo `SecurityAnomalyDetector` (`SEC-34`) để xác định phạm vi tác động.
