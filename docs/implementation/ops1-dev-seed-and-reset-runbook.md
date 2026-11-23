# Runbook Vận hành Dữ liệu Kiểm thử Sạch & Reset Stack Local (OPS-1 / CRY-37)

Tài liệu này hướng dẫn kiểm tra độ sạch `strategy_release` trong workspace local/dev, reset dữ liệu giao dịch PAPER mà **giữ nguyên checkpoint genesis**, và làm lại toàn bộ stack Docker Compose từ đầu bằng cách xóa volume.

> [!CAUTION]
> Các lệnh dưới đây **chỉ dành cho môi trường local/dev PAPER**. Tuyệt đối không chạy trên môi trường `prod` hoặc `live` (các script tự chặn khi tên project chứa `live`/`prod`).

---

## 1. Kiểm tra workspace có release/hash lệch catalog (`check`)

Khi mã chiến lược hoặc `PIPELINE_ENGINE_VERSION` thay đổi, các bản ghi `strategy_release` đã `PUBLISHED` trước đó có thể lệch `content_hash` hoặc không còn tồn tại trong `StrategyRegistry`, gây lỗi `BOT_CONFIG_ROOT_MISMATCH`.

Chạy lệnh kiểm tra trước khi khởi động lại bot:

```bash
uv run python scripts/seed_signal_test_bots.py check --project crypidian-review
```

- **Mã thoát `0` (`"clean": true`)**: Mọi `strategy_release` đang `PUBLISHED` trong workspace khớp 100% với catalog hiện hành.
- **Mã thoát `2` (`"clean": false`)**: Báo cáo JSON liệt kê rõ:
  - `hash_mismatches`: các release có `content_hash` cũ lệch với `expected_hash` hiện hành.
  - `unknown_releases`: các release không còn trong catalog/registry.

### Khắc phục lệch hash an toàn (không mất lịch sử)

```bash
# 1. Đồng bộ toàn bộ release trong workspace (chuyển bản lệch sang SUPERSEDED, publish bản mới)
docker compose --project-name crypidian-review run --rm --no-deps -T migrator \
  python -m trading_bot.cli.strategy_releases sync --workspace-id 11111111-1111-4111-8111-111111111111

# Hoặc chạy lại apply bộ seed 10 bot để vừa sync release vừa xuất bản config bot mới nhất
uv run python scripts/seed_signal_test_bots.py apply --project crypidian-review

# 2. Kiểm tra lại xác nhận sạch
uv run python scripts/seed_signal_test_bots.py check --project crypidian-review
```

---

## 2. Reset ledger PAPER dev mà GIỮ NGUYÊN checkpoint genesis

> [!IMPORTANT]
> **Bài học từ đợt reset ngày 15/11/2026:** Bảng `paper_execution_checkpoint` là sổ cái bất biến (`append-only`). Không bao giờ tạo `paper_account_seed` mới hoặc xóa nhầm checkpoint genesis làm lệch số revision. Khi cần dọn sạch lệnh/vị thế thử nghiệm PAPER nhưng giữ nguyên tài khoản, bot và checkpoint genesis:

1. Tạm dừng worker runtime và signal worker trước khi thao tác:
   ```bash
   docker compose --project-name crypidian-review stop trading-runtime market-signal-worker telegram-signal-worker
   ```
2. Chỉ xóa dữ liệu giao dịch phát sinh sau genesis (theo đúng thứ tự khóa ngoại) trên DB dev trong khi **giữ nguyên** `trading_account`, `paper_account_seed`, và dòng checkpoint genesis đầu tiên (`sequence_number = 1`) của từng account:
   - Không xóa `paper_execution_checkpoint` có `event_key LIKE 'signal-test-genesis:%'`.
   - Sau khi dọn, kiểm tra `check` và khởi động lại worker:
     ```bash
     uv run python scripts/seed_signal_test_bots.py check --project crypidian-review
     docker compose --project-name crypidian-review start trading-runtime market-signal-worker telegram-signal-worker
     ```

---

## 3. Làm lại stack hoàn toàn từ đầu (xóa volume)

Khi workspace bị kẹt bởi `unknown_release` cũ, migration thử nghiệm, hoặc cần môi trường sạch 100%:

```bash
# 1. Dừng và xóa toàn bộ container + volume dữ liệu PostgreSQL / Object Store của stack local
docker compose --project-name crypidian-review --profile paper --profile signal --profile signal-producer down -v

# 2. Khởi tạo lại PostgreSQL và chạy Alembic migrations tới head
docker compose --project-name crypidian-review up -d postgres
docker compose --project-name crypidian-review run --rm migrator alembic upgrade head

# 3. Seed dữ liệu người dùng reviewer local và bộ 10 bot test (kèm genesis checkpoint)
uv run python scripts/seed_local_reviewer.py seed
uv run python scripts/seed_local_reviewer.py apply --project crypidian-review
uv run python scripts/seed_signal_test_bots.py apply --project crypidian-review

# 4. Kiểm tra lại release/hash sạch trước khi bật toàn bộ service
uv run python scripts/seed_signal_test_bots.py check --project crypidian-review

# 5. Khởi động lại toàn bộ stack PAPER + Signal
docker compose --project-name crypidian-review --profile paper --profile signal --profile signal-producer up -d
```
