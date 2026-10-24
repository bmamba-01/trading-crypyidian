# Bộ dữ liệu QA và tài khoản kiểm thử (PAPER)

Tài liệu này mô tả bộ dữ liệu QA tất định (deterministic QA dataset) dùng để kiểm thử end-to-end dự án Crypidian multi-bot ở chế độ PAPER: nội dung, cách gieo dữ liệu (seed) vào PostgreSQL local/Compose, nơi lưu thông tin đăng nhập, chốt an toàn và cách các work package (WP) sau mở rộng.

> Chỉ dùng cho môi trường local/test. Dataset không gọi Binance, không chứa API key sàn và không tạo lệnh thật. Thông tin đăng nhập **không bao giờ** được ghi trong tài liệu, mã nguồn hay log — chỉ có đường dẫn tệp.

## 1. Thành phần

| Thành phần | Vai trò |
| --- | --- |
| `tests/fixtures/qa_dataset/` | Builder thuần (không I/O, không đồng hồ, không ngẫu nhiên). Gọi `build_qa_dataset()` luôn trả cùng một `QaDataset`. |
| `tests/fixtures/qa_dataset/identity.py` | Namespace UUIDv5, hàm `qa_id(kind, key)`, mốc thời gian cố định `SEEDED_AT`/`MARKET_END`, phiên bản `crypidian-qa-v1`. |
| `tests/fixtures/qa_dataset/access.py` | Lớp danh tính và quyền: workspace, thành viên, PAPER account, grant theo bot. |
| `tests/fixtures/qa_dataset/root_settings.py` | Lớp cấu hình gốc: risk profile, `WorkspaceSettings` v1 đã publish, seed vốn PAPER. |
| `tests/fixtures/qa_dataset/market.py` | Lớp dữ liệu thị trường tổng hợp: instrument và nến BTCUSDT/ETHUSDT. |
| `scripts/seed_qa_data.py` | CLI: lệnh `seed` (máy dev, điều phối Compose, quản lý tệp credentials) và `apply` (trong container). |
| `scripts/qa_seed_layers.py` | Ghi dataset vào PostgreSQL theo từng lớp trong một transaction (`SEED_LAYERS`). |
| Service Compose `qa-seed` (profile `qa`) | Container chạy `apply` với login owner local, volume MFA và khóa mã hóa MFA. `docker compose up` mặc định không chạy service này. |
| `tests/unit/test_qa_dataset.py` | Bất biến dataset, chốt an toàn, credentials, thứ tự lớp. |
| `tests/e2e/test_qa_seed.py`, `tests/e2e/probes/qa_seed.py` | Compose E2E: seed hai lần, đăng nhập API thật, ma trận RLS/grant, runtime bot scope, git ignore. |

## 2. Nội dung dataset

Mọi ID là UUIDv5 sinh từ `QA_NAMESPACE` và khóa nghiệp vụ (ví dụ `qa_id("trading_account", "A")`), nên seed lặp lại luôn ra cùng khóa chính trên mọi máy. ID tất định **không phải bí mật**; an toàn dựa vào xác thực và RLS. Riêng `bot_instance.id` do trigger DB sinh ngẫu nhiên khi insert account (đúng hợp đồng MB-01); seed tra bot theo account và in ra trong tóm tắt.

### 2.1 Workspace

| Khóa | Tên | Slug (dùng khi đăng nhập) | Mode | Trạng thái |
| --- | --- | --- | --- | --- |
| `qa` | Crypidian QA | `crypidian-qa` | PAPER | ACTIVE |
| `qa_other` | Crypidian QA Other | `crypidian-qa-other` | PAPER | ACTIVE |

### 2.2 Thành viên và role

Role lưu chữ thường trong DB. Mọi thành viên có MFA TOTP đã xác minh (login của dự án luôn yêu cầu mật khẩu + TOTP).

| Khóa | Email | Workspace | Role | Membership | Grant theo bot |
| --- | --- | --- | --- | --- | --- |
| `qa.owner` | `qa.owner@example.test` | `qa` | `owner` | ACTIVE | — (role workspace-wide) |
| `qa.strategy_admin` | `qa.strategy-admin@example.test` | `qa` | `strategy_admin` | ACTIVE | — (role workspace-wide) |
| `qa.risk_admin` | `qa.risk-admin@example.test` | `qa` | `risk_admin` | ACTIVE | — (role workspace-wide) |
| `qa.operator` | `qa.operator@example.test` | `qa` | `operator` | ACTIVE | Bot A: `bot:read`, `bot:runtime:pause`, `bot:runtime:resume` |
| `qa.analyst_reviewer` | `qa.analyst-reviewer@example.test` | `qa` | `analyst_reviewer` | ACTIVE | Bot A: `bot:read`, `bot:report:read` |
| `qa.suspended` | `qa.suspended@example.test` | `qa` | `risk_admin` | **SUSPENDED** | Bot A: `bot:read` |
| `qa_other.owner` | `qa-other.owner@example.test` | `qa_other` | `owner` | ACTIVE | — |

`qa.suspended` cố ý giữ role workspace-wide và một grant còn hiệu lực: mọi phép thử từ chối (login 401, RLS không thấy bot) chỉ có thể đến từ trạng thái membership. Email dùng TLD `.test` dành riêng cho kiểm thử theo RFC 2606. Grant do `qa.owner` cấp và nằm trong trần quyền của role (`role_allows_bot_permission`).

### 2.3 PAPER account, bot và seed vốn

| Account | Workspace | Tên (= tên bot) | Trạng thái | `max_notional` | Seed vốn |
| --- | --- | --- | --- | --- | --- |
| A | `qa` | `qa-paper-a` | ENABLED | 10000 | 10000 USDT |
| B | `qa` | `qa-paper-b` | ENABLED | 10000 | 10000 USDT |
| C | `qa` | `qa-paper-c` | DISABLED | 5000 | 10000 USDT |
| OTHER | `qa_other` | `qa-other-paper` | ENABLED | 5000 | 5000 USDT |

Tất cả account là `PAPER`/`binance_usdm`/`local`. Bot được trigger `trading_account_provision_bot` tạo khi insert account (seed không insert `bot_instance`). Seed vốn dùng khóa `paper-seed:<account_id>` và vốn bằng `paper_execution.starting_equity` của settings, đúng quy ước `PaperAccountSeedRepository`, nên runtime không coi là xung đột.

### 2.4 Ma trận quyền thấy bot mong đợi (RLS dưới `crypidian_admin_web`)

| Thành viên (scope workspace) | Bot thấy được |
| --- | --- |
| owner / strategy_admin / risk_admin @ `qa` | A, B, C |
| operator @ `qa` | A |
| analyst_reviewer @ `qa` | A |
| suspended @ `qa` | không bot nào |
| thành viên QA bất kỳ @ `qa_other` | không bot nào |
| `qa_other.owner` @ `qa_other` | OTHER |

Ở tầng ứng dụng (`BotGrantRepository.require`): operator được pause/resume bot A nhưng không pause bot B, không `bot:config:write`, không `bot:report:read` (thiếu grant dù role cho phép); analyst được `bot:report:read` bot A; risk_admin pause được mọi bot nhưng không publish config; strategy_admin publish config được mọi bot. Runtime (`crypidian_trading_runtime`) với scope bot A chỉ đọc được hàng của bot A.

### 2.5 Cấu hình gốc

- Mỗi workspace có risk profile `QA conservative` v1 (`max_risk_fraction` 0.01, hard caps giống seed E2E H03) và `WorkspaceSettings` v1 trạng thái PUBLISHED, `effective_at = 2026-10-01T00:00:00Z`.
- `qa`: symbols `BTCUSDT`, `ETHUSDT`; timeframe `1h`/`4h`; strategy mặc định `h03-v1` (entry 1h, xác nhận 4h); vốn khởi tạo 10000 USDT; phí `paper-fees-v1`; fill `next_bar_with_adverse_costs`.
- `qa_other`: symbol `BTCUSDT`; vốn khởi tạo 5000 USDT; còn lại như trên.
- Hash settings tính bằng đúng `settings_hash(canonical_settings(...))` của repository.

### 2.6 Dữ liệu thị trường tổng hợp (chỉ workspace `qa`)

- Instrument nguồn `BINANCE_USDM` (quy cách mô phỏng): BTCUSDT tick 0.1, bước khối lượng 0.001, notional tối thiểu 5; ETHUSDT tick 0.01.
- Nến nguồn `CRYPIDIAN_QA_FIXTURE`, đều đã đóng: mỗi symbol 240 nến 1h và 220 nến 4h (tổng 920), kết thúc lúc `2026-10-01T00:00:00Z`. Đủ cho các strategy built-in: H03 cần 56 nến 1h, H06 cần 121 nến 1h, H01 cần EMA200 trên 4h.
- Giá sinh tất định từ sóng tam giác số nguyên (Decimal, không làm tròn). Nến 4h là tổng hợp OHLCV đúng của chuỗi 1h nền, nên 60 nến 4h cuối khớp 240 nến 1h đã lưu.
- BTCUSDT kết thúc bằng một nến phá kênh Donchian 55 nến với khối lượng lớn hơn median 20 nến (ứng viên H03); ETHUSDT dao động trong biên độ (không có tín hiệu phá kênh).
- Mốc thời gian là quá khứ cố định: runtime live sẽ coi dữ liệu là cũ và fail-closed. Fixture phục vụ replay/backtest, kiểm thử RLS/scope và màn hình đọc dữ liệu.

## 3. Seed và reset

### 3.1 Local với Compose project mặc định

Chạy từ thư mục gốc repository, dùng Python của `.venv`:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_local_secrets.py
docker compose build admin-web          # hoặc dùng image đã có, đặt CRYPIDIAN_APP_IMAGE
docker compose --profile paper up -d --wait
.\.venv\Scripts\python.exe scripts\seed_qa_data.py seed --local-only
```

`seed` tự khởi động dependency của `qa-seed` (postgres, db-provisioner, migrator, mfa-volume-provisioner) nếu chưa chạy; `--no-deps` bỏ bước này khi stack đã chạy. Tùy chọn khác: `--project-name <tên>` (mặc định `crypidian`), `--credentials-file <tệp .json trong .secrets/>`, `--json` (in tóm tắt máy đọc được, không có bí mật), `--compose-file`.

Kết quả in workspace ID, bot ID theo account, số hàng QA từng bảng và danh sách thay đổi của lần chạy. Lần chạy lặp lại khi dữ liệu đã đúng baseline in "không có (đã đúng baseline)".

### 3.2 Ngữ nghĩa idempotent và reset về baseline

- Hàng được tạo theo ID tất định; chạy lại không tạo hàng trùng.
- Hàng đã có phải khớp identity của dataset (slug, email, mode, account, hash settings...). Nếu bị dữ liệu khác chiếm, seed dừng và không ghi đè.
- Trạng thái mà QA hay thay đổi khi thử thủ công được đưa về baseline: trạng thái user/membership/account, `max_notional`, tên account, tập role của thành viên QA, grant đang hiệu lực (grant lạ của thành viên QA bị thu hồi bằng `revoked_at`; grant bị thu hồi được cấp lại bằng hàng mới vì lịch sử grant bất biến), mật khẩu và TOTP theo tệp credentials.
- Bảng bất biến (settings đã publish, seed vốn PAPER, nến, lịch sử grant) không bị sửa hay xóa. Settings v2 do QA publish qua Admin API vẫn giữ và có hiệu lực.
- Xoay mật khẩu/TOTP: xóa hoặc sửa mục trong tệp credentials rồi chạy lại `seed`; mục thiếu/hỏng được sinh mới và DB được cập nhật theo.
- Reset hoàn toàn (xóa mọi dữ liệu local của project): `docker compose --profile paper --profile qa down --volumes`, rồi `up` và `seed` lại. Lệnh này xóa volume PostgreSQL/MFA của project, chỉ dùng cho môi trường local.

### 3.3 Compose E2E

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m pytest tests\e2e\test_qa_seed.py -q -p no:cacheprovider -rA
```

Test tạo Compose project ngẫu nhiên `crypidian-qa-e2e-<hex>`, migrate head (`alembic check`), seed hai lần (số hàng và digest nội dung không đổi, lần hai không có thay đổi), đăng nhập từng tài khoản qua Admin API thật, kiểm tra ma trận RLS/grant dưới login `crypidian_admin_web`, bot scope dưới `crypidian_trading_runtime`, và tệp credentials bị git ignore. Test dùng tệp credentials riêng `.secrets/qa_accounts-<project>.json` và xóa nó khi kết thúc.

## 4. Thông tin đăng nhập

- Đường dẫn mặc định: `.secrets/qa_accounts.json` (thư mục `.secrets/` đã có trong `.gitignore`). Seed từ chối mọi đường dẫn ngoài `.secrets/` và tệp bị git theo dõi/không bị ignore.
- Mật khẩu sinh lúc seed bằng `secrets.token_urlsafe(24)`; TOTP seed sinh bằng `TotpManager.generate_secret()`. DB chỉ giữ hash Argon2id (`PasswordManager`) và reference tới TOTP seed đã mã hóa AES-GCM (`LocalMfaSecretProvider`, volume `mfa_secrets`).
- Tệp ghi nguyên tử, quyền `0600` trên POSIX; Windows dựa vào ACL hồ sơ người dùng.
- Mỗi mục theo khóa thành viên gồm: `workspace_slug`, `email`, `roles`, `membership_status`, `password`, `totp_secret`. Không dán nội dung tệp vào issue, chat, log hay tài liệu.
- Mật khẩu/TOTP đi từ máy dev vào container `qa-seed` qua stdin, không qua tham số dòng lệnh hay biến môi trường; output của container được che lại nếu có giá trị bí mật.

## 5. Chốt an toàn (seed guard)

1. Bắt buộc cờ `--local-only` ở cả `seed` và `apply`; thiếu cờ thì dừng trước khi gọi Docker hay ghi tệp.
2. `seed` chỉ chạy với Docker engine trên chính máy: `DOCKER_HOST` hoặc endpoint của Docker context phải là `unix://`, `npipe://` hoặc `tcp://` loopback; `ssh://` và TCP từ xa bị từ chối.
3. `apply` yêu cầu `TRADING_BOT_APP_ENV` là `local`/`test`, `TRADING_BOT_TRADING_MODE=PAPER`, database host thuộc `postgres`, `localhost`, loopback, `*.localhost` hoặc `*.test`.
4. Chốt dữ liệu sau khi kết nối: schema phải có đủ bảng (đã migrate), role phải là owner local (superuser/BYPASSRLS như migrator), database không có workspace/account nào khác PAPER, và slug/email/tên account/settings v1 của dataset không bị bản ghi khác chiếm.
5. Mọi bước ghi chạy trong một transaction có advisory lock; lỗi giữa chừng rollback DB và xóa TOTP seed vừa mã hóa.

## 6. Kiểm thử thủ công

### 6.1 Admin API

1. Seed như mục 3.1 và mở tệp credentials trên máy của bạn để lấy email, mật khẩu, TOTP seed.
2. Sinh mã TOTP 6 số từ TOTP seed bằng ứng dụng authenticator hoặc `pyotp` (`pyotp.TOTP(<totp_secret>).now()`). Mỗi mã chỉ dùng một lần; đăng nhập lại cùng tài khoản cần chờ chu kỳ 30 giây kế tiếp.
3. Gọi `POST http://127.0.0.1:8000/api/v1/auth/login` với JSON `{"workspace_slug": "crypidian-qa", "email": "<email>", "password": "<mật khẩu từ tệp>", "code": "<mã TOTP>"}`. Phản hồi có `roles` và `csrf_token`; cookie `crypidian_session` dùng cho các request tiếp theo, request ghi cần header `X-CSRF-Token`.
4. Các tình huống nên thử: đăng nhập từng role; `qa.suspended` phải nhận 401; `qa.owner` với slug `crypidian-qa-other` phải nhận 401; thao tác cần MFA gần đây (ví dụ publish settings) dùng `POST /api/v1/auth/reauthenticate`.

### 6.2 Telegram và Admin Web (sau MB-07)

Dataset chưa có `telegram_binding`. Khi MB-07 thêm lệnh Telegram, thêm lớp seed binding cho `qa.operator` (chỉ bot A) và `qa.owner`, dùng `telegram_user_id` giả lập; các lệnh `/bot pause <id>` phải chấp nhận bot A và từ chối bot B cho operator, giống ma trận ở mục 2.4.

## 7. Mở rộng cho các WP sau

Hợp đồng lớp (layer contract):

1. Thêm module builder thuần mới trong `tests/fixtures/qa_dataset/` (ví dụ `bot_configs.py`), dùng `qa_id(kind, key)` cho mọi ID và mốc thời gian cố định; không đọc đồng hồ, không sinh ngẫu nhiên, không chứa bí mật.
2. Thêm trường mới trên `QaDataset` và gọi builder trong `build_qa_dataset()`. Không đổi khóa/ID của lớp hiện có; đổi `QA_NAMESPACE` sẽ làm dữ liệu đã seed bị mồ côi.
3. Thêm coroutine ghi lớp trong `scripts/qa_seed_layers.py` và chèn vào `SEED_LAYERS` đúng vị trí phụ thuộc; cập nhật bất biến thứ tự lớp trong `tests/unit/test_qa_dataset.py`. Lớp phải idempotent: tạo theo ID tất định, kiểm identity, chỉ đưa field mutable về baseline, không sửa bảng bất biến. Nếu lớp cần bảng mới, thêm tên bảng vào `REQUIRED_TABLES` và mô hình vào `_COUNTED_MODELS`.
4. Thêm bất biến unit và assertion E2E (số hàng mong đợi trong `EXPECTED_COUNTS`, quyền/scope liên quan) rồi cập nhật tài liệu này.

Gợi ý theo WP:

- **MB-02 (bot config versions):** module `bot_configs.py` tạo `BotConfigVersion` v1 đã publish cho bot A và B (overlay chỉ siết cap, ví dụ cap thấp hơn cho B), bot C giữ cấu hình gốc; lớp `bot_configs` đứng sau `bots`. ID dạng `qa_id("bot_config_version", "A:1")`. Bot ID lấy từ `SeedContext.bot_ids`.
- **MB-03 (shared evidence):** module `evidence.py` tạo evidence dùng chung từ nến cuối của BTCUSDT (ứng viên phá kênh) với `available_at` từ fixture; lớp `shared_evidence` đứng sau `market`. Evidence không chứa account/quantity.
- **MB-04 (risk tổng hợp):** settings v1 bất biến; nếu cần cap workspace mới, thêm settings v2 như phiên bản mới (parent v1) thay vì sửa v1.
- **MB-05 (runtime nhiều bot):** không seed lease/checkpoint runtime; test tự tạo. Account C (DISABLED) dùng để kiểm bot không được khởi chạy.
- **MB-06 (báo cáo):** lớp `paper_ledger` ghi các trade tất định cho bot A và B qua repository ledger, kèm PnL mong đợi dạng hằng số để đối chiếu báo cáo từng bot và tổng workspace.
- **MB-07 (Admin Web/Telegram):** xem mục 6.2; dùng chính các tài khoản ở mục 2.2 cho kiểm thử trình duyệt.

## 8. Giới hạn đã biết

- Seed ghi bằng login owner local (superuser của image PostgreSQL), giống migrator và các seed E2E hiện có, để ghi nhiều scope trong một transaction (bot_grant chỉ cho `crypidian_admin_web` có owner session, nến thuộc ingestion, seed vốn thuộc runtime). RLS được kiểm riêng bằng login process thật trong E2E.
- Policy `bot_grant_read` cho thành viên đọc grant của chính mình mà không xét trạng thái membership; thành viên bị đình chỉ vẫn không có phiên đăng nhập vì `login`/`authenticate` yêu cầu ACTIVE.
- Dữ liệu thị trường chỉ có ở workspace `qa`; mốc thời gian là quá khứ nên không dùng để chạy runtime live.
- Reset hoàn toàn cần xóa volume vì nhiều bảng là append-only/bất biến.

## 9. Thuật ngữ

Thuật ngữ mới (bộ dữ liệu QA, seed dữ liệu, UUIDv5, chốt an toàn seed, tài khoản kiểm thử, profile Compose) được đề xuất cho `docs/research/glossary-vi.md`; xem thêm [TOTP](../research/glossary-vi.md), [RLS](../research/glossary-vi.md) và [Idempotency key](../research/glossary-vi.md).
