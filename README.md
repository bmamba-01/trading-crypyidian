# Crypidian Trading Bot

Bot Binance USDⓈ-M Futures độc lập, được phát triển bằng Python. Hệ thống bắt đầu ở chế độ PAPER; cấu hình hiện chỉ chấp nhận PAPER hoặc DEMO và từ chối LIVE cho tới khi readiness gate được triển khai, kiểm định và phê duyệt.

## Chuẩn bị môi trường

1. Cài CPython 3.14.8 và `uv` 0.13.4 trở lên trong nhánh 0.13.x.
2. Tạo môi trường và cài đúng dependency trong lock file:

   ```powershell
   uv sync --frozen
   ```

3. Sao chép `.env.example` thành `.env` nếu cần thay đổi cấu hình local. Không đưa API key, secret hoặc token thật vào tệp mẫu, Git, log hay tài liệu.

## Chạy môi trường local PAPER

Docker Compose V2 dùng profile `paper`; mọi process hiện đều cấu hình `PAPER`. Tạo secret hạ tầng ngẫu nhiên và các tệp Binance để trống trước khi khởi động:

```powershell
uv run python scripts/prepare_local_secrets.py
docker compose --profile paper up -d --build
docker compose --profile paper ps
docker compose --profile paper logs -f admin-web
```

`prepare_local_secrets.py` không tạo hoặc gọi Binance API key. Tệp `.secrets/` bị loại khỏi Git và build context; chỉ `execution-gateway` được mount hai tệp Binance. Script tạo env-file riêng cho Garage local từ access key ngẫu nhiên, chỉ gắn vào `object-store`. Cấu hình Garage local cho phép đọc secret file có mode rộng do Docker Desktop trên Windows trình bày file mount là `0777`; ACL của file nguồn vẫn được quản lý trên host. Môi trường triển khai khác phải dùng secret provider và quyền file chặt, không tái sử dụng cấu hình local này. Chỉ điền credential Demo sau khi adapter Demo được triển khai; chưa có lệnh giao dịch trong các process bootstrap hiện tại.

```powershell
docker compose --profile paper down
```

Lệnh `down` giữ named volume; `down --volumes` xóa database và object store local. Backup database trước khi xóa hoặc nâng cấp:

```powershell
docker compose exec -T postgres pg_dump -U crypidian_admin -d crypidian > .\crypidian-backup.sql
```

Process `admin-web`, `trading-runtime`, `execution-gateway`, `ingestion-worker` và `trainer-worker` hiện dùng chung app bootstrap để kiểm tra ranh giới process. Chưa có business loop, kết nối Binance, ingestion hay training. `/health/live` chỉ kiểm tra process; `/health/ready` trả `503` cho tới khi database, market data và exchange probe được cài đặt và báo sẵn sàng. Compose chỉ chờ health của PostgreSQL/object store trước khi khởi động các service phụ thuộc.

`db-provisioner` đọc secret file local để tạo hoặc xoay mật khẩu login riêng cho từng process mỗi lần khởi động; `migrator` chạy `alembic upgrade head` trước `admin-web`. Cấu trúc ban đầu có workspace, user/membership/role/session/MFA/Telegram, trading account, secret reference, config/risk versions, approval, watchlist và strategy release. Từng app role là `NOSUPERUSER` và `NOBYPASSRLS`; `user_account` chỉ được cấp cho admin web. Tài khoản admin PostgreSQL chỉ dùng cho provisioner, migrator và backup local. Rollback schema local có thể chạy `docker compose --profile paper run --rm migrator alembic downgrade base`; lệnh này xóa các bảng WP-01C và dữ liệu trong đó.

Các bảng chứa `workspace_id` được bảo vệ bằng PostgreSQL Row-Level Security (RLS, chính sách lọc hàng theo workspace). Repository còn truyền workspace rõ ràng; RLS không thay thế kiểm tra ở ứng dụng. Thay đổi schema mới cần Alembic revision riêng, có kế hoạch rollout expand → deploy → backfill → contract.

Network `web_ingress` cho phép máy host truy cập cổng admin đã bind vào loopback; `backend` vẫn là network nội bộ. `exchange_egress` chỉ dành cho gateway trong topology Compose local; network bridge không phải allowlist domain/IP. Khi triển khai, dùng firewall/egress policy để giới hạn host Binance và web ingress; không chuyển cấu hình local này sang LIVE.

Object store local là Garage, một dịch vụ S3-compatible (tương thích API lưu trữ S3) phát hành theo AGPL-3.0. Cấu hình replication 1 chỉ dùng cho môi trường local một node; cần rà soát giấy phép và topology trước khi triển khai phân phối.

## Kiểm tra

```powershell
uv run pytest
uv run ruff check src tests
uv run mypy src
```

Ứng dụng FastAPI hiện cung cấp `/health/live` và `/health/ready`. Endpoint readiness báo dependency chưa sẵn sàng cho tới khi các probe thật được tích hợp; không endpoint nào trả secret.

## Tài liệu

- [Mục lục thiết kế và nghiên cứu](docs/README.md)
- [Kế hoạch triển khai](docs/implementation/README.md)
- [Ghi nhớ các vòng triển khai](docs/implementation/implementation-memory.md)
- [Image và digest Docker](docs/implementation/container-images.md)
- [Quy trình AI cho dự án](.agents/skills/futures-bot-implementation/SKILL.md)
