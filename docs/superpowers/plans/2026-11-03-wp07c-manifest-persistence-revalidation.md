# WP-07C Manifest persistence và quyền tại ranh giới replay

> **For agentic workers:** Dùng `superpowers:executing-plans` để triển khai từng task và đánh dấu checkbox sau khi có bằng chứng.

**Goal:** Lưu backtest dataset manifest bất biến theo workspace, đọc lại có kiểm toàn vẹn/hash, và chỉ trả manifest `READY` cho replay sau khi policy Source Registry vẫn còn đúng phiên bản/quyền.

**Architecture:** Thêm bảng PostgreSQL riêng cho manifest backtest (không tái sử dụng bảng WP-06 dành cho training sample manifest), RLS workspace, unique workspace/hash, trigger append-only. Canonical JSON là nội dung gốc; cột chỉ mục được đối chiếu khi load. Replay loader dùng cùng Unit of Work để đọc manifest rồi gọi rights gate hiện hành; nếu purpose bị revoke/expire hoặc fingerprint đổi thì từ chối. Không chạy evaluator/backtest hoặc lấy dữ liệu thật trong lát cắt này.

**Tech stack:** Python 3.14, SQLAlchemy/PostgreSQL, Alembic, pytest; không thêm dependency.

**Tài liệu:** [WP-07 WBS](./2026-11-03-wp07-trader-workbench-wbs.md), [manifest builder](./2026-11-03-wp07c-backtest-dataset-manifest.md), [Source Registry gate](./2026-11-03-wp07c-source-registry-backfill-rights.md).

## Ràng buộc

- `READY` là điều kiện cần; thiếu coverage không được load sang replay.
- Revalidate dùng DB clock/current observed registry; thay đổi policy fingerprint chặn replay cho tới khi dataset được đánh giá lại.
- Manifest không chứa toàn bộ bar payload; chỉ lưu range, quyền, hash nội dung và coverage.
- Tenant isolation, idempotency và immutability phải được kiểm tra bằng PostgreSQL E2E.
- Không stage/commit/push, không gọi Binance/Telegram, không tạo lệnh.

## Tasks

- [x] Viết unit RED cho canonical manifest round-trip/tamper, repository idempotency/scope và replay rights same/changed/revoked/incomplete.
- [x] Bổ sung parse canonical JSON có schema/hash validation vào `BacktestDatasetManifest`.
- [x] Thêm model + migration cho bảng append-only workspace-scoped, RLS, admin-web select/insert và immutable trigger.
- [x] Thêm repository append/get-by-hash, duplicate payload check và row-to-domain validation.
- [x] Thêm replay loader: `require_ready()`, dựng rights-check request, revalidate current registry trong transaction, yêu cầu policy fingerprint giữ nguyên.
- [x] Mở rộng Compose E2E: migration/check, insert/readback/idempotent retry, tenant isolation, rights recheck sau policy update/revoke, trigger immutable.
- [x] Chạy focused/full unit, E2E, Ruff/format, mypy và `git diff --check`.
- [x] Cập nhật WBS/status/glossary/memory, đánh dấu plan và đọc lại memory.

**Chưa gồm:** backtest evaluator/fill model, CLI/scheduler, raw response retention, giao diện tiến độ hay dữ liệu Binance thật.
