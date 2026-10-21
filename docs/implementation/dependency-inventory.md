# Danh mục dependency trực tiếp

**Ngày kiểm tra:** 21/10/2026 · **Lock:** `uv.lock` là nguồn phiên bản/hash chính xác. Tệp này theo dõi dependency trực tiếp; dependency bắc cầu được ghi đầy đủ trong lock file.

[SBOM CycloneDX](./sbom.cdx.json) được sinh từ lock bằng `uv export --preview-features sbom-export --format cyclonedx1.5 --all-groups`. BOM hiện bao gồm dependency runtime và nhóm dev; build backend được ghi rõ trong `pyproject.toml` và danh mục này.

| Phạm vi | Gói | Phiên bản/giới hạn | Giấy phép | Vai trò và nguồn |
| --- | --- | --- | --- | --- |
| Runtime | `fastapi` | 0.156.4 | MIT | HTTP API; [PyPI metadata](https://pypi.org/project/fastapi/), [release notes](https://fastapi.tiangolo.com/release-notes/). |
| Runtime | `pydantic-settings` | 2.16.0 | MIT | Đọc và kiểm tra cấu hình có kiểu; [release](https://github.com/pydantic/pydantic-settings/releases/tag/v2.16.0). |
| Build | `uv_build` | 0.13.4 | MIT | Build package Python; [uv build backend](https://docs.astral.sh/uv/concepts/build-backend/). |
| Test | `pytest` | `>=9.1,<10` | MIT | Unit/E2E test; bản giải quyết cụ thể nằm trong lock. |
| Test | `pytest-asyncio` | `>=1.4,<2` | Apache-2.0 | Hỗ trợ test bất đồng bộ cho repository/worker. |
| Test | `httpx2` | `>=2,<3` | BSD-3-Clause | HTTP client được Starlette TestClient ưu tiên; [FastAPI release notes](https://fastapi.tiangolo.com/release-notes/). |
| Quality | `ruff` | `>=0.14,<1` | MIT | Lint và format Python. |
| Quality | `mypy` | `>=1.18,<2` | MIT | Kiểm tra kiểu tĩnh. |

`uv.lock` chứa phiên bản, nguồn phân phối và hash của cả dependency trực tiếp/bắc cầu trên các marker môi trường. Cập nhật tệp này khi thêm/bỏ gói trực tiếp; kiểm tra lại giấy phép từ metadata phát hành chính thức trước khi đưa dependency mới vào.
