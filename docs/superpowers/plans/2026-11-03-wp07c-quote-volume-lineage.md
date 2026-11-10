# WP-07C Quote volume lineage cho chiến thuật và replay

> **For agentic workers:** Dùng test-first, migration E2E và giữ hash schema v2 tương thích.

**Goal:** Lưu quote asset volume nguyên Decimal từ USDⓈ-M kline đến `MarketBar`/replay để H03 so volume đúng; dữ liệu cũ/nguồn thiếu giữ `NULL` để strategy abstain.

**Nguồn đã xác minh 2026-11-03:** [Binance USDⓈ-M REST API, Kline/Candlestick Data](https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#kline-candlestick-data) mô tả GET `/fapi/v1/klines`, tuple 12 trường, vị trí `[7]` là `Quote asset volume`. Endpoint technical contract không cấp quyền lưu/trưng bày; Source Registry gate hiện có tiếp tục áp dụng.

**Architecture:** thêm `quote_volume: Decimal | None` vào `OHLCVBar`/`MarketBar`, parse REST và archive field 7, include vào checksum dữ liệu mới và trả qua repository. Migration nullable, check null hoặc không âm; downgrade từ chối nếu đã có giá trị quote volume. `PostgresMarketDecisionSnapshotProvider` truyền tuple quote volume khi mọi bar có trường; nếu có bar cũ/thiếu thì cả series không có quote volume và H03 giữ nguyên hành vi fail-closed. Backtest dataset manifest nâng hash schema lên v3 để ghim trường mới; parser vẫn đọc schema v2 cũ và replay hash v2 giữ thuật toán legacy, nhưng v2 không được replay nếu DB trả quote volume mới chưa ghim.

## Files chính

- Modify: `domain/market/models.py`, market DB model/repository, Binance REST parser, Vision archive parser/checksum, backfill checksum, replay manifest/loader/bundle, live market decision snapshot composition.
- Migration: tạo revision kế tiếp `b1d3f5a7c9e2`, thêm nullable quote column/check và downgrade guard.
- Tests: domain, REST parser, archive parser, market repository/model metadata, manifest v2/v3 round-trip/hash, replay legacy gate, signal snapshot quote volume, Compose PostgreSQL migration/readback/downgrade guard.

## Tasks

- [x] Viết RED cho Decimal/None/negative domain behavior, REST/archive field 7 parsing, repository persistence/readback, v2 compatibility, v3 hash, H03 series quote-volume input và downgrade guard.
- [x] Thêm nullable domain/model/repository field, migration/RLS-compatible check, downgrade guard.
- [x] Nối parser REST/archive, REST paginator và raw-content checksum; cập nhật fixture fields.
- [x] Version manifest bar hash/canonical payload; đọc v2 nguyên trạng, tạo v3 mặc định, v2 replay chỉ khi mọi quote volume còn NULL.
- [x] Cấp quote volumes cho live DecisionSnapshot chỉ khi đầy đủ; không tự quy đổi base×close.
- [x] Chạy focused/full unit, Compose E2E, Ruff/format, mypy, Alembic head và `git diff --check`.
- [x] Cập nhật WBS/status/glossary/memory, đọc lại memory.

**Chưa gồm:** dựng lịch sử feature/DecisionSnapshot từ replay bundle, macro event data cho H06, PAPER context/evaluator orchestration, CLI/scheduler, raw payload retention và tải dữ liệu thật.
