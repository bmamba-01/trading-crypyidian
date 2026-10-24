# Danh mục dependency trực tiếp

**Ngày kiểm tra:** 22/10/2026 · **Lock:** `uv.lock` là nguồn phiên bản/hash chính xác. Tệp này theo dõi dependency trực tiếp; dependency bắc cầu được ghi đầy đủ trong lock file.

[SBOM CycloneDX](./sbom.cdx.json) được sinh từ lock bằng `uv export --preview-features sbom-export --format cyclonedx1.5 --all-groups`. BOM hiện bao gồm dependency runtime và nhóm dev; build backend được ghi rõ trong `pyproject.toml` và danh mục này.

| Phạm vi | Gói | Phiên bản/giới hạn | Giấy phép | Vai trò và nguồn |
| --- | --- | --- | --- | --- |
| Runtime | `fastapi` | 0.156.4 | MIT | HTTP API; [PyPI metadata](https://pypi.org/project/fastapi/), [release notes](https://fastapi.tiangolo.com/release-notes/). |
| Runtime | `httpx2` | `>=2,<3` (lock: 2.15.1) | BSD-3-Clause | Async HTTP client cho adapter market-data; [tài liệu async](https://github.com/pydantic/httpx2/blob/main/docs/async.md), [timeout](https://github.com/pydantic/httpx2/blob/main/docs/advanced/timeouts.md), [metadata phát hành](https://pypi.org/project/httpx2/2.15.1/). Đã kiểm tra API async và license ngày 22/10/2026; truyền transport để test offline, tắt trust_env và xác thực. |
| Runtime | `sqlalchemy[asyncio]` | 2.1.5 | MIT | ORM và engine bất đồng bộ; extra `asyncio` cài `greenlet` theo thay đổi của SQLAlchemy 2.1; [tài liệu asyncio](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html), [kiểu Numeric/Decimal](https://docs.sqlalchemy.org/en/21/core/type_basics.html). |
| Runtime | `psycopg[binary]` | 3.3.6 | LGPL-3.0-only và PostgreSQL License cho binary package | Driver PostgreSQL bất đồng bộ; [tài liệu asyncio](https://www.psycopg.org/psycopg3/docs/advanced/async.html), [cài đặt](https://www.psycopg.org/psycopg3/docs/basic/install.html). Rà soát điều khoản binary package trước khi phân phối. |
| Runtime | `alembic` | 1.20.0 | MIT | Quản lý migration có revision và rollback; [changelog chính thức](https://alembic.sqlalchemy.org/en/latest/changelog.html). |
| Runtime | `argon2-cffi` | 25.1.0 | MIT | Băm mật khẩu Argon2id; dùng PasswordHasher với profile mặc định, cần benchmark trên tài nguyên triển khai; [API](https://argon2-cffi.readthedocs.io/en/stable/api.html), [tham số](https://argon2-cffi.readthedocs.io/en/stable/parameters.html). |
| Runtime | `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | AES-GCM mã hóa seed TOTP local; [tài liệu AESGCM](https://cryptography.io/en/stable/hazmat/primitives/aead/), [PyPI release](https://pypi.org/project/cryptography/50.0.2/). |
| Runtime | `pydantic-settings` | 2.16.0 | MIT | Đọc và kiểm tra cấu hình có kiểu; [release](https://github.com/pydantic/pydantic-settings/releases/tag/v2.16.0). |
| Runtime | `pyotp` | 2.10.0 | MIT | Tạo URI và xác minh TOTP theo RFC 6238; [tài liệu](https://pyauth.github.io/pyotp/), [PyPI release](https://pypi.org/project/PyOTP/2.10.0/). |
| Runtime | `uvicorn` | 0.54.0 | BSD-3-Clause | ASGI server cho FastAPI; [release notes](https://github.com/Kludex/uvicorn/blob/main/docs/release-notes.md). |
| Build | `uv_build` | 0.13.4 | MIT | Build package Python; [uv build backend](https://docs.astral.sh/uv/concepts/build-backend/). |
| Test | `pytest` | `>=9.1,<10` | MIT | Unit/E2E test; bản giải quyết cụ thể nằm trong lock. |
| Test | `pytest-asyncio` | `>=1.4,<2` | Apache-2.0 | Hỗ trợ test bất đồng bộ cho repository/worker. |
| Quality | `ruff` | `>=0.14,<1` | MIT | Lint và format Python. |
| Quality | `mypy` | `>=1.18,<2` | MIT | Kiểm tra kiểu tĩnh. |

`uv.lock` chứa phiên bản, nguồn phân phối và hash của cả dependency trực tiếp/bắc cầu trên các marker môi trường. Cập nhật tệp này khi thêm/bỏ gói trực tiếp; kiểm tra lại giấy phép từ metadata phát hành chính thức trước khi đưa dependency mới vào.
