# Runbook Triển khai Reverse Proxy TLS, Trusted Proxy & Giới hạn HTTP (SEC-29, SEC-30)

**Mục tiêu:** Bảo vệ `admin-web` khi triển khai production sau reverse proxy TLS:
- Chặn giả mạo header `Host` bằng `TrustedHostMiddleware` (`HTTP 400`).
- Chỉ tin `X-Forwarded-For` từ IP/CIDR reverse proxy được khai báo (`trusted_proxy_ips` và uvicorn `--forwarded-allow-ips`) để tránh vượt rate limit hoặc làm sai lệch audit IP.
- Giới hạn kết nối đồng thời (`--limit-concurrency`), kích thước request body (`HTTP 413`) và tần suất request mutation / đọc nặng theo phiên + IP (`HTTP 429` + `Retry-After`).

---

## 1. Biến môi trường cấu hình trên `admin-web`

| Biến môi trường | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `TRADING_BOT_ALLOWED_HOSTS` | `""` (bắt buộc ở `production`) | Danh sách tên miền hợp lệ phân tách bằng dấu phẩy, ví dụ `admin.crypidian.example`. Cấm `*` ở `production`. |
| `TRADING_BOT_TRUSTED_PROXY_IPS` | `""` (Compose mặc định `127.0.0.1,::1`) | IP hoặc CIDR của reverse proxy trực tiếp kết nối vào uvicorn (ví dụ subnet mạng `web_ingress` hoặc IP riêng của Caddy/Nginx). Cấm `*` và `/0`. |
| `TRADING_BOT_REQUEST_BODY_MAX_BYTES` | `1048576` (1 MiB) | Kích thước body tối đa cho request HTTP; vượt ngưỡng trả `413`. |
| `TRADING_BOT_RATE_LIMIT_MUTATION_PER_MINUTE` | `60` | Ngưỡng request ghi (`POST/PUT/PATCH/DELETE`) mỗi phút theo từng cặp IP và từng phiên (`/api/v1/telegram/webhook` được miễn vì có secret + limiter riêng). |
| `TRADING_BOT_RATE_LIMIT_HEAVY_READ_PER_MINUTE` | `30` | Ngưỡng request đọc nặng (`GET/HEAD`) mỗi phút theo từng cặp IP và từng phiên. |
| `TRADING_BOT_RATE_LIMIT_HEAVY_READ_PATHS` | `/admin/reports,/admin/run-reports,/admin/api/chart,/admin/backtests,/api/v1/reports` | Danh sách tiền tố path đọc nặng áp dụng ngưỡng riêng. |
| `TRADING_BOT_UVICORN_LIMIT_CONCURRENCY` | `128` | Cờ `--limit-concurrency` cho uvicorn (vượt ngưỡng trả `503`). |

> [!IMPORTANT]
> Khi `TRADING_BOT_APP_ENV=production`, `admin-web` từ chối khởi động nếu thiếu `TRADING_BOT_ALLOWED_HOSTS` hoặc đặt `*`. Không mở cổng `8000` của `admin-web` ra Internet; chỉ cho phép `reverse-proxy` kết nối qua mạng nội bộ `web_ingress`.

---

## 2. Triển khai Reverse Proxy TLS

### Lựa chọn A: Caddy (khuyến nghị, tự động cấp & gia hạn chứng chỉ TLS)

1. Kiểm tra subnet của mạng Docker `web_ingress` (hoặc ghim subnet cố định khi tạo stack) để đặt `TRADING_BOT_TRUSTED_PROXY_IPS` đúng dải IP của container `reverse-proxy`.
2. Cấu hình biến môi trường và khởi động với file override [`deploy/compose.tls.yaml`](../../deploy/compose.tls.yaml) cùng [`deploy/Caddyfile`](../../deploy/Caddyfile):
   ```bash
   export CRYPIDIAN_PUBLIC_HOST="admin.crypidian.example"
   export TRADING_BOT_ALLOWED_HOSTS="admin.crypidian.example"
   export TRADING_BOT_TRUSTED_PROXY_IPS="172.18.0.0/16"
   docker compose -f compose.yaml -f deploy/compose.tls.yaml up -d
   ```
3. Caddy tự động chặn client gửi `X-Forwarded-For` giả, ép kích thước body tối đa `1MB` (`64KB` cho webhook Telegram) và chỉ cho phép dải IP Telegram (`149.154.160.0/20`, `91.108.4.0/22`) gọi `/api/v1/telegram/webhook*`.

### Lựa chọn B: Nginx

1. Sử dụng template tại [`deploy/nginx/crypidian.conf`](../../deploy/nginx/crypidian.conf), thay `admin.crypidian.example` và đường dẫn chứng chỉ TLS.
2. Đặt `limit_req_zone` trong khối `http {}` của `nginx.conf`.
3. Giữ `proxy_set_header X-Forwarded-For $remote_addr;` nếu Nginx là điểm tiếp nhận trực tiếp từ Internet để không nối thêm chuỗi client tự khai báo.

> [!NOTE]
> `BodySizeLimitMiddleware` và `RequestRateLimitMiddleware` hiện lưu bộ đếm trong RAM của một tiến trình `admin-web` (single-instance). Khi mở rộng nhiều replica `admin-web` (MB-07/MB-09), cần bổ sung bộ đếm dùng chung (Redis/DB).

---

## 3. Checklist xác minh sau triển khai

- [ ] **Chặn Host giả (`400`):** `curl -ik https://admin.crypidian.example/health/live -H 'Host: evil.example'` trả HTTP `400`.
- [ ] **Bỏ qua `X-Forwarded-For` từ peer không tin cậy:** Gửi request trực tiếp nội bộ kèm `X-Forwarded-For: 1.2.3.4` từ nguồn ngoài `TRADING_BOT_TRUSTED_PROXY_IPS` không ghi nhận IP `1.2.3.4` vào limiter/audit.
- [ ] **Giới hạn body (`413`):** Gửi payload > `1 MiB` nhận HTTP `413`.
- [ ] **Rate limit (`429`):** Gửi vượt ngưỡng mutation hoặc `/admin/reports` nhận HTTP `429` kèm header `Retry-After`.
- [ ] **TLS & Cookie:** Response qua HTTPS mang đủ `Strict-Transport-Security`, CSP và cookie phiên có tiền tố `__Host-` + cờ `Secure`.

---

## 4. Nguồn tham khảo (kiểm tra 2026-11-18)

- Starlette `TrustedHostMiddleware`: https://starlette.dev/middleware/
- Uvicorn CLI & proxy settings (`--forwarded-allow-ips`, `--limit-concurrency`): https://uvicorn.dev/settings/
- Caddy `reverse_proxy` & `request_body`: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy , https://caddyserver.com/docs/caddyfile/directives/request_body
- Nginx `ngx_http_proxy_module` & `ngx_http_limit_req_module`: https://nginx.org/en/docs/http/ngx_http_proxy_module.html , https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
- OWASP API4:2023 (Unrestricted Resource Consumption): https://owasp.org/API-Security/editions/2023/en/0x11-t10/
