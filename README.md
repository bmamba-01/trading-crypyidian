# Crypidian Trading Bot

Bot Binance USDⓈ-M Futures độc lập, được phát triển bằng Python. Hệ thống bắt đầu ở chế độ PAPER; cấu hình hiện chỉ chấp nhận PAPER hoặc DEMO và từ chối LIVE cho tới khi readiness gate được triển khai, kiểm định và phê duyệt.

## Chuẩn bị môi trường

1. Cài CPython 3.14.8 và `uv` 0.13.4 trở lên trong nhánh 0.13.x.
2. Tạo môi trường và cài đúng dependency trong lock file:

   ```powershell
   uv sync --frozen
   ```

3. Sao chép `.env.example` thành `.env` nếu cần thay đổi cấu hình local. Không đưa API key, secret hoặc token thật vào tệp mẫu, Git, log hay tài liệu.

## Kiểm tra

```powershell
uv run pytest
uv run ruff check src tests
uv run mypy src
```

Ứng dụng FastAPI hiện cung cấp `/health/live` để kiểm tra process; endpoint này không kiểm tra database, feed hoặc sàn và không nhận thông tin bí mật. Chỉ số readiness cho các thành phần sẽ được bổ sung cùng process boundary ở Plan 01B.

## Tài liệu

- [Mục lục thiết kế và nghiên cứu](docs/README.md)
- [Kế hoạch triển khai](docs/implementation/README.md)
- [Ghi nhớ các vòng triển khai](docs/implementation/implementation-memory.md)
- [Quy trình AI cho dự án](.agents/skills/futures-bot-implementation/SKILL.md)
