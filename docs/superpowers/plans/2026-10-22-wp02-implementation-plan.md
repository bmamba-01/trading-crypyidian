# WP-02 Trading Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for parallel independent tasks; execute dependent tasks sequentially. Each implementation task follows TDD RED → GREEN → REFACTOR and requires a fresh independent review before integration.

**Goal:** Hoàn thiện trading core độc lập với vòng market data → features → strategy candidate → TradePlan/RiskDecision → PAPER execution/ledger → replay/evaluation, được quản lý bằng root WorkspaceSettings versioned qua Admin API.

**Architecture:** Giữ domain thuần Python; adapter/port tách Binance public market data, PostgreSQL, Admin API và PAPER gateway. Root WorkspaceSettings quản lý cấu hình cấp workspace; market/research/feature context dùng chung workspace, còn decision/order/trade gắn mode và các phiên bản đầu vào. Chỉ PAPER được phép thực thi trong phạm vi này.

**Tech Stack:** CPython 3.14, FastAPI, Pydantic, SQLAlchemy 2.1 async, PostgreSQL 18, Alembic, HTTPX2 (`httpx2`, pin trong `uv.lock` sau khi đối chiếu API/PyPI), pytest/pytest-asyncio, Ruff, mypy, Docker Compose.

**Spec:** [WP-02 design](../specs/2026-10-22-wp02-trading-core-design.md)

## Trạng thái WBS tại 2026-10-24

Người dùng đã duyệt WBS ngày 2026-10-22. Trạng thái dưới đây phản ánh code và test hiện tại; các checkbox ở từng bước được đánh dấu chỉ khi đã có bằng chứng.

| WP | Trạng thái | Bằng chứng hiện có | Còn thiếu để nghiệm thu |
| --- | --- | --- | --- |
| WP-02G.1/2 | Hoàn thành phần nền (MVP) | Settings schema/version repository/Admin API; effective loader kiểm tra status/hash/scope; auth/CSRF/RBAC/MFA/publish/RLS probes; runtime áp dụng strategy parameters và pin lineage; PP6 targeted Compose kiểm publish/decision serialization. | UI thuộc WP-04; chưa có thiếu sót trong gate WP-02; không còn thiếu strategy parameter application trong source. |
| WP-02A.1/2 | Hoàn thành phần nền (MVP) | Domain quality, PostgreSQL market repo, Binance public adapter, offline fixture E2E. | Hardening collector/backfill/coverage cho live deployment. |
| WP-02B | Hoàn thành phần nền (MVP) | Formula/quality/regime snapshots, immutable as-of repository, Compose lineage/hash/RLS probe. | Golden reference datasets và production semantic review. |
| WP-02C | Hoàn thành phần nền (MVP) | H01/H03/H06, registry/recipe/chart geometry/no-lookahead; runtime `StrategySelection` lấy parameters từ root settings và pin version. | Các release vẫn là hypothesis, chưa có edge/profitability claim. |
| WP-02D | Hoàn thành phần nền (MVP) | TradePlanner/RiskEngine, persisted decisions/reservations, PostgreSQL concurrency/margin E2E; H03 runtime tạo plan từ candidate thật. | Semantics daily/rolling loss window cần được đặc tả/hoàn thiện. |
| WP-02E | Đạt trong phạm vi PAPER vertical slice | H03 PAPER lifecycle/bootstrap; active order/position ghim execution profile và strategy lineage; publish/decision chia sẻ workspace serialization. PP2 focused unit **17 passed**; PP6 xác minh provider dùng đúng settings/strategy đã publish. | Dispatcher tự chạy, venue reconcile, multi-workspace runtime và lifecycle chưa qua coordinator vẫn ngoài phạm vi. |
| WP-02F | Đạt phần nền MVP trong phạm vi replay hiện tại | Reducer sở hữu account/reservation/exposure progression; RP4 Compose **1 passed / 57.69s**, regression **30 passed**. Replay hash determinism được kiểm tra qua replay lặp không restart worker. | Chưa có mark-to-market; day/rolling risk-window semantics chưa hoàn thiện; funding/spread stress ngoài phạm vi. |
| WP-02 integration | Hoàn thành; independent review đạt | Final full unit **520 passed**; final full E2E **13 passed / 381.82s**; Ruff check/format (**227 files**), mypy 94 files, Alembic single head `b2e7a9d4c631`, migration checks và teardown đạt. PP6 target **1 passed / 109.47s**; review không còn P0/P1/source P2. | Giữ các giới hạn được ghi rõ; không hàm ý bot tự dispatch, Binance execution hay profitability đã được xác nhận. |

**WP-02 được nghiệm thu trong phạm vi PAPER/replay MVP** sau khi PP2/PP6, final unit/E2E, quality/migration gates và independent review đạt. Các giới hạn về mark-to-market, risk windows, autonomous dispatch, Binance execution và profitability vẫn phải được nêu rõ. Không commit trong lần cập nhật tài liệu này.

## Global Constraints

- Python `>=3.14,<3.15`; source identifiers/API tiếng Anh; comment/docstring/tài liệu giải thích tiếng Việt.
- Không có credential Binance, Telegram token, cookie hoặc raw private content trong settings JSON, database event, log, test fixture hoặc model input.
- Chỉ public Binance USDⓈ-M market-data endpoint; không gọi USER_DATA/TRADE endpoint và không gửi lệnh Demo/Live.
- `WorkspaceSettings` là root cấu hình sản phẩm duy nhất; `.env` chỉ chứa bootstrap/service connection/secret references, không có strategy/risk toggles rời rạc.
- Draft không có hiệu lực; publish tạo version/hash bất biến; mỗi decision/plan/order/fill/trade/replay ghim version tương ứng.
- Tất cả query/insert tenant scope phải truyền workspace; RLS và role DB hiện có tiếp tục có hiệu lực.
- Mọi rule có data thiếu, stale, gap, bar chưa đóng, future timestamp, config không hợp lệ hoặc risk vượt cap phải trả NO_TRADE/reject có reason, không dùng default thuận lợi.
- E2E Compose dùng image hash-content reuse hiện có; test run không được tạo một image tag per random project/run.
- Không commit trừ khi toàn bộ unit + E2E, quality gate, memory readback và independent review đều đạt.

## Dependencies and execution order

```text
02G.1 WorkspaceSettings schema/repository ──┬─> 02G.2 Admin API/auth/config publish
                                           └─> market/strategy runtime config binding
02A.1 data contracts/store ─> 02A.2 public adapter/ingestion ─> 02B snapshot/regime
02B snapshot ─> 02C registry/rules ─> 02D planner/reservation ─> 02E PAPER ledger
                                                       └──────────────> 02F replay/evaluation
02A–F + 02G ─> full offline PAPER E2E ─> status/memory/review/commit decision
```

Có thể chạy song song 02G.1, rà/hoàn thiện 02A.1 và strategy tests thuần 02C.1 vì ownership tách nhau. Migration revision sequencing, `entities.py` registrations, settings API integration và full E2E chỉ được một người điều phối ghép; không cho hai nhánh cùng sửa một migration chain đồng thời.

## Review Focus

1. Binance trả precision/rules thay đổi hoặc field thiếu → dùng Decimal, validate `filterType`, lưu observation timestamp và từ chối dùng instrument stale.
2. Nến đang mở, out-of-order, duplicate, missing boundary hoặc backfill chưa complete → input không đủ điều kiện cho decision.
3. Hai admin tạo/publish cạnh tranh hoặc draft chứa risk reference không tồn tại → conflict/field error, không ghi đè hoặc publish một phần.
4. Hai risk request đồng thời, intent UNKNOWN, duplicate retry hoặc snapshot account/config stale → không vượt risk budget; reservation chưa reconcile vẫn chiếm quota.
5. Một OHLC bar chạm cả stop và target, partial fill/cancel hoặc fee/funding ở funding boundary → xử lý conservatively, lưu ambiguity/cost một lần.
6. Feature/strategy/backtest đọc giá trị sau decision time hoặc thay đổi dependency/formula convention → test phát hiện leakage; run không được xem là reproducible.

---

## Task 0 — Baseline và contract freeze

**Files:** đọc `docs/implementation/implementation-memory.md`, `docs/implementation/02-trading-core-paper.md`, `docs/architecture/technical-design.md`, `docs/research/strategies-2026.md`, các domain hiện có và migrations; không sửa production code trong task này.

- [ ] **Step 1: Chạy baseline tests và ghi output.**

Run: `.venv/Scripts/python.exe -m pytest tests/unit -q` và `.venv/Scripts/python.exe -m pytest tests/e2e -q`  
Expected: ghi chính xác pass/fail/skip và thời lượng; lỗi hiện hữu phải được chẩn đoán trước khi thay đổi.

- [ ] **Step 2: Ghi contract inventory.** Liệt kê model/repository, root auth/session/RBAC, UnitOfWork/outbox, migration head, compose image helper, fixtures, test count; đối chiếu từng acceptance của spec với code hiện có.

- [ ] **Step 3: Cập nhật design/implementation plan và backlog trace.** Không bắt đầu Task 1 khi còn gap contract giữa spec và interface của task tiếp.

**Done when:** baseline có bằng chứng; migration head sạch; danh sách task bên dưới khớp spec; WP02A–G được thể hiện riêng trên `project-status.md`.

## Task 1 — WP-02G.1: WorkspaceSettings schema, persistence và version repository — Hoàn thành phần nền

**Files:**
- Create: `src/trading_bot/config/workspace_settings.py`
- Create: `src/trading_bot/adapters/database/repositories/workspace_settings.py`
- Modify: `src/trading_bot/adapters/database/models/entities.py` and `__init__.py`
- Create: Alembic migration after current head (revision ID generated by Alembic)
- Test: `tests/unit/test_workspace_settings.py`, `tests/unit/test_database_models.py`
- Test: `tests/e2e/test_workspace_settings_persistence.py`

**Interfaces:** produce strict Pydantic `WorkspaceSettings`, `WorkspaceSettingsDraft`, `WorkspaceSettingsVersion`, `validate_workspace_settings(payload)`, and repository create/update/list/publish/get-effective methods. Root has sections `runtime`, `market_data`, `features`, `strategy`, `risk`, `paper_execution`; `runtime.mode` is the literal `PAPER`. `paper_execution` includes quote asset and Decimal `starting_equity` for the synthetic paper account. Risk section refers to a `RiskProfileVersion` ID rather than duplicating hard caps. Published records expose immutable version/hash and optimistic-lock revision.

- [ ] **Step 1: Write failing schema/version tests.** Assert required root sections, reject unknown keys/mode `DEMO`/`LIVE`, reject negative age or invalid timeframe, and accept only references to valid version IDs at the validation boundary.
- [ ] **Step 2: Run focused tests and verify expected RED.** Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_workspace_settings.py -q`. Expected: missing module/schema failures.
- [ ] **Step 3: Implement strict Pydantic schema.** Use `extra="forbid"`; decimals/ratios use Decimal constraints where monetary/risk semantics require precision; avoid embedding secrets.
- [ ] **Step 4: Write failing database/repository tests.** Assert workspace RLS scope, unique `(workspace_id, version)`, parent version lineage, stable canonical JSON hash, and inability to mutate a published version.
- [ ] **Step 5: Run expected RED, then add model/repository/migration.** Table: `workspace_settings_version(workspace_id, version, parent_version_id, schema_version, settings_json, settings_hash, status, revision, created_by, approved_by, created_at, published_at, effective_at)`. Use composite workspace FK, JSONB, explicit status/check constraints and RLS/FORCE RLS. A draft may change only through revision compare-and-swap; published row may transition only to immutable published state.
- [ ] **Step 6: Run unit, migration and repository integration tests.** Verify `alembic upgrade head`, `alembic check`, downgrade to prior revision, and re-upgrade on a fresh E2E database.

**Acceptance:** cross-workspace reads return no rows; draft revisions detect stale updates; version/hash are deterministic; publish is atomic; any attempted post-publish mutation is rejected at repository and DB boundary; no secrets appear in stored JSON.

## Task 2 — WP-02G.2: Admin Settings API and publish workflow — Hoàn thành phần nền

**Files:**
- Modify: `src/trading_bot/auth/policy.py`, `src/trading_bot/auth/router.py` only if wiring is needed
- Create: `src/trading_bot/admin/settings_router.py`, `src/trading_bot/admin/settings_service.py`, request/response schema under `src/trading_bot/admin/`
- Modify: `src/trading_bot/app.py`
- Test: `tests/unit/test_admin_settings_service.py`, `tests/unit/test_auth_routes.py`
- Test: `tests/e2e/test_admin_settings_api.py`

**Interfaces:** routes `GET /api/v1/settings/current`, `GET /api/v1/settings/versions`, `POST /api/v1/settings/drafts`, `PATCH /api/v1/settings/drafts/{draft_id}`, `POST /api/v1/settings/drafts/{draft_id}/validate`, `POST /api/v1/settings/drafts/{draft_id}/publish`, `POST /api/v1/settings/versions/{version}/rollback`. Mutations require session + CSRF. Add `SETTINGS_READ` and `SETTINGS_WRITE`; retain risk section changes behind `RISK_PROFILE_WRITE` and recent MFA. Publish may create a new version from old contents but never edit history.

- [ ] **Step 1: Write failing permission/behavior tests.** Cover owner/strategy-admin access, operator/analyst denial, CSRF denial, MFA/risk-reference denial, stale expected parent 409, invalid draft field detail, draft not effective, publish audit/outbox atomicity, and rollback as a new version.
- [ ] **Step 2: Run focused tests and verify expected RED.**
- [ ] **Step 3: Implement permission map and settings application service.** No route may accept workspace ID as authority; derive tenant from the authenticated Principal. Settings mutation creates audit event and outbox event in the same UnitOfWork.
- [ ] **Step 4: Implement routes and response models.** Return redacted config, version/hash/status and field-level validation errors; never echo secret-like fields.
- [ ] **Step 5: Run API unit tests, then PostgreSQL-backed E2E.** The E2E must use real auth/session/CSRF + DB/RLS, including competing draft publish. No browser UI is added in WP-02; WP-04 UI will consume these endpoints.

**Acceptance:** every role/policy test passes; draft cannot affect active config; publish/rollback create auditable immutable versions; stale revision conflicts; risk-affecting mutations require role+MFA; API E2E passes against PostgreSQL.

## Task 3 — WP-02A.1: Market persistence, cursor và quality repository — Hoàn thành phần nền

**Files:**
- Create domain/repository model files under `src/trading_bot/domain/market/` and `src/trading_bot/adapters/database/`
- Modify ORM registration only through owned model module/import; do not mix unrelated auth/outbox changes
- Create migration for instrument, bars, funding/OI, cursor/gap/raw reference and needed feature input lineage
- Test: `tests/unit/test_market_data_domain.py`, `tests/unit/test_market_repository.py`
- Test: `tests/e2e/test_market_data_persistence.py`

**Interfaces:** extend existing `Instrument`, `OHLCVBar`, `MarketSnapshot`, `inspect_bar_series` contracts. Repository methods accept explicit `workspace_id`, symbol/timeframe and `as_of`; ingestion accepts idempotency key/source cursor. Persist Decimal string precision as `NUMERIC`; timestamps are timezone-aware UTC; bars are keyed by workspace/instrument/timeframe/open time/source revision.

- [ ] **Step 1: Add failing unit cases for duplicate idempotency, revised source record lineage, boundary gap, empty range with explicit instrument/timeframe, future `available_at`, incomplete backfill, clock skew and stale instrument metadata.**
- [ ] **Step 2: Verify RED, then implement domain status/reason types without DB coupling.**
- [ ] **Step 3: Add models/repository/migration using RLS/FORCE RLS and workspace composite keys.** Original source/raw reference is immutable; correction appends revision rather than overwrites a used event.
- [ ] **Step 4: Add failing repository integration tests for `as_of`, dedup, gap/cursor, RLS and replay; run RED then implement.**
- [ ] **Step 5: Run Alembic upgrade/check and data repository tests on PostgreSQL.**

**Acceptance:** data versions are immutable and point-in-time; missing boundaries/backfill remain unusable; repeated event is idempotent; stale/out-of-order/unclosed/future data cannot form a usable snapshot; RLS prevents cross-workspace access.

## Task 4 — WP-02A.2: Binance USDⓈ-M public market-data adapter — Hoàn thành phần nền

**Files:**
- Create: `src/trading_bot/ports/market_data.py`
- Create: `src/trading_bot/adapters/binance_usdm/market_data.py`
- Create: JSON fixtures in `tests/fixtures/binance_usdm/`
- Test: `tests/unit/test_binance_market_data_adapter.py`
- Test: `tests/e2e/test_market_ingestion_offline.py`
- Modify dependency files only if async HTTP client is approved by compatibility check (`pyproject.toml`, `uv.lock`, dependency inventory/SBOM)

**Interfaces:** port methods `get_exchange_info()`, `get_klines(symbol, interval, start_time, end_time, limit)`, `get_funding_rates(symbol, start_time, end_time, limit)`, `get_open_interest(symbol)`, and `get_open_interest_history(pair, period, start_time, end_time, limit)`; adapter does not implement signed/private endpoints. Use dependency injection for transport, monotonic clock and rate-limit policy. Parse values as Decimal, preserve raw payload checksum, map venue times to event/available times, and treat current open bar as unclosed. Historical OI availability/retention is explicit and the adapter must report range incomplete when the source window does not cover requested history.

- [ ] **Step 1: Check live Binance documentation and current async HTTP library docs/release; save links/date/version to WP-02 memory and inventory.** Current official index: [Binance USDⓈ-M Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data); representative pages: [Klines](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/market-data/rest-api/Kline-Candlestick-Data), [Exchange Information](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data). Re-check exact funding/OI path/weights before code; catalog may redirect by current docs version.
- [ ] **Step 2: Write failing parser/transport contract tests.** Cover malformed tuple, unknown/changed filter, non-200, 418/429, timeout, Retry-After, server error, request weight, and that generated requests never include signature/API-key header.
- [ ] **Step 3: Verify expected RED; add/update async transport dependency only after official compatibility/license check and lock.**
- [ ] **Step 4: Implement adapter and bounded backoff.** Respect request weight from exchange responses/docs; do not retry non-retryable 4xx; expose degraded/stale state instead of silent empty data.
- [ ] **Step 5: Add offline ingestion E2E with recorded fixtures; prove no network call and no credential access.** Optional live public smoke is manual and not a required test.

**Acceptance:** fixtures parse to canonical immutable models; symbol precision/filter types are validated; retry policy respects quota; no private route/credential or order path exists; external-data failure makes collector degraded and market range incomplete.

## Task 5 — WP-02B: Immutable feature snapshots, formulas và regime labels — Hoàn thành phần nền

**Files:**
- Modify existing `src/trading_bot/domain/market/features.py` only after RED tests
- Create `src/trading_bot/domain/market/feature_snapshot.py`, repository/model migration if needed
- Create: `tests/unit/test_feature_snapshot.py`, extend `tests/unit/test_market_features.py`
- Create: `tests/e2e/test_feature_snapshot_replay.py`
- Modify glossary/research strategy formula docs with pinned definitions and references

**Interfaces:** `FeatureSnapshot` includes snapshot ID, workspace, instrument/timeframe, `as_of`, schema/formula/code hashes, input bar/event IDs, feature map, warm-up/quality/freshness, computed time. `FeatureCalculator.calculate(snapshot, settings) -> FeatureSnapshot`. Keep pure formulas separate from persistence. Implement SMA/EMA/TR/ATR/RSI/ADX-DMI/Bollinger/RV/ER/z-score/VWAP using existing pinned conventions. Regime classification returns `TREND`, `RANGE`, `HIGH_VOLATILITY`, or `UNKNOWN`, confidence/coverage and reason; it does not select trade direction.

- [ ] **Step 1: Add failing tests for formula snapshots, exact input lineage, formula-version hash, warmup/missing/stale gaps, UTC VWAP reset, regime thresholds and unknown/confidence output.**
- [ ] **Step 2: Verify RED; implement snapshot contract and deterministic serializer/hash.**
- [ ] **Step 3: Add migration/repository for immutable snapshots with workspace RLS.**
- [ ] **Step 4: Add failing replay test, then verify that same snapshot inputs/version reproduce exact canonical feature values; changing code/formula version creates distinct schema/hash.**
- [ ] **Step 5: Update glossary EN→VI for any new method/regime term; run golden fixtures against formula definitions from `strategies-2026.md` and authoritative source docs.**

**Acceptance:** missing/stale/unclosed inputs yield invalid quality or NO_TRADE; all derived values reference exact source event IDs and formula version; snapshot is immutable; replay is deterministic; no future input is used.

## Task 6 — WP-02C: Strategy interface, registry, deterministic recipes và baseline hypotheses — Hoàn thành phần nền

**Files:**
- Create: `src/trading_bot/domain/strategy/contracts.py`, `registry.py`, `rules.py`, `recipes.py`
- Extend: `src/trading_bot/domain/strategy/geometry.py`
- Create: `tests/unit/test_strategy_contracts.py`, `test_strategy_hypotheses.py`, `test_strategy_registry.py`
- Create persistence models/repository and migration for `strategy_definition`, `strategy_release`, `strategy_rule` only if required by current schema audit

**Interfaces:** `Strategy.evaluate(snapshot: DecisionSnapshot, settings: WorkspaceSettings) -> Candidate | NoTrade`. Candidate includes direction, entry/stop/target recipe, confirmation/expiry, evidence feature IDs, geometry/version IDs and deterministic reason. `NoTrade` always has code and missing/gate details. Registry resolves only immutable releases referenced by published root settings.

- [ ] **Step 1: Write failing contract/registry tests for deterministic output, unknown release, invalid recipe, missing/stale feature and no lookahead.**
- [ ] **Step 2: Implement pure contract and registry with fixtures; verify RED→GREEN.**
- [ ] **Step 3: Add H01 exact rule tests:** 1h EMA20/80 cross, closed 4h EMA200 direction gate and 1h ADX14≥20; exit stop 2ATR/trailing 3ATR/opposite cross/120-bar limit.
- [ ] **Step 4: Add H03 exact rule tests:** 1h close breaks prior 55-bar HH/LL; current quote volume exceeds prior-20-bar median; no same-bar future fill; exit stop 2ATR, prior-20 channel, trailing 3ATR/120 bars.
- [ ] **Step 5: Add H06 exact rule tests:** 15m ADX14<18 and ER40<0.25; long z20<-2 with RSI14<30, short z20>2 with RSI14>70; macro blackout/HH120-LL120 adverse filter; exit z ±0.2, stop 1.5ATR, target ≤2ATR, 16 bars; no scale-in.
- [ ] **Step 6: Implement each rule minimally and prove tests fail before code; use only as hypotheses, label release experimental/unvalidated.**
- [ ] **Step 7: Persist immutable strategy releases/rules and source/recipe version; add locale label EN→VI and glossary links.**

**Acceptance:** same snapshot/release/settings gives canonical identical Candidate/NoTrade; H01/H03/H06 exact thresholds and exits match research; no pivot/signal before confirmation; every output carries evidence and version IDs; no AI score can promote/release a strategy.

## Task 7 — WP-02D: Full TradePlan, hard RiskEngine và atomic reservation — Hoàn thành phần nền

**Files:**
- Extend: `src/trading_bot/domain/risk/sizing.py`
- Create: `src/trading_bot/domain/risk/planner.py`, `engine.py`, `reservations.py`
- Add DB model/repository/migration for `trade_plan`, `risk_decision`, `risk_reservation`, risk budget lock/projection
- Tests: `tests/unit/test_trade_planner.py`, `tests/unit/test_risk_engine.py`, `tests/unit/test_risk_reservations.py`
- E2E: `tests/e2e/test_risk_reservation_concurrency.py`

**Interfaces:** `TradePlanner.plan(candidate, settings, account_snapshot, open_exposure, cost_model) -> TradePlan | PlanRejected`; `RiskEngine.evaluate(plan, state, settings) -> RiskDecision`; `reserve(decision, idempotency_key) -> RiskReservation`. Decimal only for prices/size/cost/risk amounts. Hard limits include per-trade, aggregate open+pending risk, notional, max leverage, margin available, symbol/concentration, daily/rolling realized loss, max positions and account/config/data freshness. Unknown order outcome retains reservation until reconcile. Reservations are workspace/account-scoped and transactionally serialized by risk scope in PostgreSQL.

- [ ] **Step 1: Write failing property/unit tests for cap math, stop/target side validity, fee/funding/slippage once, step rounding, min notional, all reject reason codes, stale snapshot and boundary equality.**
- [ ] **Step 2: Verify RED; add immutable TradePlan/RiskDecision contracts and plan builder.**
- [ ] **Step 3: Write failing concurrent repository tests.** Two sessions try to reserve above remaining risk; expected aggregate never exceeds limit.
- [ ] **Step 4: Implement transaction lock + reservation insert/idempotency; define expiry/UNKNOWN/release transitions.**
- [ ] **Step 5: Run PostgreSQL concurrency E2E with separate database sessions; verify rollback and retry preserve cap.**

**Acceptance:** no precision/limit overrun; aggregate pending+open risk never exceeds cap under concurrency; UNKNOWN intent keeps reservation; strategy cannot mutate hard profile; every rejection explains rule/version; risk change requires config/profile revalidation.

## Task 8 — WP-02E: PAPER gateway, position monitor và shared ledger — Hoàn thành phần nền (MVP)

**Files:**
- Create: `src/trading_bot/domain/execution/paper.py`, `position_monitor.py`, fill/cost contracts
- Create application use case/worker for decision cycle → risk-approved paper intent → monitor
- Use common `order_intent`, `exchange_order`, `fill`, `position_event`, `trade`, `trade_cost` schema tagged `mode=PAPER`; add migration if current schema lacks required fields.
- Tests: `tests/unit/test_paper_execution.py`, `test_position_monitor.py`, `test_trade_ledger.py`
- E2E: coordinator probe trong `tests/e2e/test_compose_boundary.py` (dùng cùng Compose lifecycle/image để không tạo project/image riêng); closed-trade probe hiện có trong cùng file.

**Interfaces:** initialize a synthetic PAPER account from published `paper_execution.quote_asset` and Decimal `paper_execution.starting_equity`; it must not have secret reference. `PaperExecutionGateway.submit(plan, idempotency_key)`, `apply_market_event(event)`, `cancel_unfilled(intent_id)`, `close_position(recipe)`. Fill model policy is immutable and pinned: entry is next eligible event/bar plus configured latency/spread/slippage stress; fee/funding cost recorded exactly once. OHLC path ambiguous on both stop/target means stop-first or explicit AMBIGUOUS per setting (pin one, default conservative stop-first); no synthesized intra-bar ordering.

- [x] **Step 1: Write failing unit lifecycle tests** for accepted/rejected, partial fill, cancel remainder, stop coverage, reduce-only exit, stop/target ambiguity, trailing monotonicity, time stop, opposing signal, pause entries while protective exits continue, funding boundary and duplicate intent.
- [x] **Step 2: Verify RED; implement in-memory pure transition/fill state machine first.**
- [x] **Step 3: Add failing ledger persistence tests; write shared PAPER-mode order/fill/position/trade/cost via UnitOfWork/outbox.**
- [x] **Step 4: Add failing end-to-end scenario with fixed fixture, published settings, H01/H03/H06 signal, risk reserve, paper fills, exit and closed trade; then integrate runtime.** H03 Compose đi qua `PaperRuntimeApplicationService` được tạo trong app lifespan, retry/conflict DB-backed, entry, restart hydrate, monitor/exit và closed trade/report.
- [x] **Step 5: Assert cost accounting and exact config/strategy/feature/candidate/risk IDs on every resulting record; report net PnL.** E2E đối chiếu lineage, fill/cost ledger và report; profile/settings được pin cho trade đang mở, thay đổi policy xung đột bị fail-closed.

**Acceptance:** offline fixture opens and closes a PAPER position without secret/network; ledger records each transition exactly once; cost total matches component sum and no fee/slippage double count; pause blocks new entries but allows protection; closed trade can be fully replayed.

## Task 9 — WP-02F: Event-driven replay/backtest, leakage gate và report — Hoàn thành phần nền (MVP)

**Files:**
- Create: `src/trading_bot/application/backtest.py`, `src/trading_bot/domain/learning/evaluation.py`, immutable run/report manifests
- Add DB model/repository/migration for backtest run/trial record/report if absent
- Tests: `tests/unit/test_backtest_runner.py`, `test_temporal_split.py`, `test_leakage_checks.py`, `test_evaluation_report.py`
- E2E: `tests/e2e/test_backtest_persistence.py` (Compose project/image lifecycle is shared with the persistence suite).

**Interfaces:** `BacktestRunner.run(range, settings_version, strategy_release, feature_schema, fill_model, seed) -> EvaluationReport`. Event stream is ordered by available/event time under explicit deterministic tie-break; runtime evaluator and backtest call same strategy/risk/fill implementations. Manifest binds dataset hash/range, settings/strategy/formula/fill versions, cost assumptions and trial count.

- [x] **Step 1: Write failing deterministic replay/time split tests; leakage test injects feature with `available_at > decision_time`.** `test_backtest_runner.py`, `test_temporal_split.py` và `test_leakage_checks.py` kiểm tra thứ tự, thời gian available và cửa sổ split.
- [x] **Step 2: Verify RED; implement temporal iterator and no-lookahead guard.** Replay sắp event ổn định theo available/event/id và từ chối look-ahead.
- [x] **Step 3: Add cost/path tests for fee, funding, spread, slippage, rejection/unfilled and ambiguous OHLC.** Cost components và OHLC ambiguity có unit coverage; BASE/FEE/SLIPPAGE/PARAMETER chạy evaluator runtime và persist outcome. Funding/spread stress bị từ chối fail-closed, không tuyên bố đã mô hình hóa.
- [x] **Step 4: Add report metrics and uncertainty output:** trade count, coverage, net PnL, drawdown, expectancy, cost shares, exposure, confidence interval, split windows and caveats; win rate alone is insufficient.
- [x] **Step 5: Add trial record each run, including failed/aborted run and parameter family; no silent repeated tuning.**
- [x] **Step 6: Run fixed-fixture E2E twice; same manifest hash/report totals and ledger outputs must match.** `tests/e2e/test_backtest_persistence.py` chạy hai usable observations theo thứ tự đầu vào khác nhau, so manifest/report/trade hashes, trade count và net PnL. `tests/e2e/test_backtest_outcomes_persistence.py` chạy sensitivity BASE + parameter-neighbor hai lần với thứ tự input/scenario đảo, persist outcome của mỗi lần, tải lại các dòng đã lưu và xác nhận report hash + aggregate outcome hash bằng nhau; cũng kiểm tra retry idempotent, conflict payload, RLS, immutability và migration downgrade/upgrade.

**Acceptance:** runtime and backtest semantics match; any future feature/test leakage is rejected; splits preserve chronology and purge/embargo policy is documented; report includes costs/coverage/uncertainty and does not claim profitability; duplicate run can be identified/replayed from manifest.

## Task 10 — Full WP-02 integration, documentation, review and completion gate — Hoàn thành

**Files:** update `docs/implementation/02-trading-core-paper.md`, `project-status.md`, `docs/implementation/README.md`, `docs/architecture/technical-design.md`, `docs/product/user-stories.md`, `docs/product/use-cases.md`, `docs/product/sequence-diagrams.md`, `docs/research/glossary-vi.md`, `docs/research/strategies-2026.md`, `docs/implementation/implementation-memory.md`, and supplemental [PP2/RP4/PP6 WBS](../../implementation/wp02e-profile-pinning-and-replay-progression-wbs.md); run migrations and Docker E2E boundary only.

- [x] **Step 1: Extend the existing Compose boundary E2E** with an acceptance flow from fixture and current effective root settings to a closed PAPER trade + report; assert each row pins settings, feature, strategy, decision, risk, fill and cost versions. H03 Compose kiểm tra persisted/reloaded feature snapshot, settings/profile, reservation, intent, fills, restart/recovery, closed trade/report và ledger reconciliation.
- [x] **Step 2: Implement only the application wiring needed to pass the E2E.** App lifespan khởi tạo application service; `trading-runtime` bootstrap/hydrate account theo workspace deployment và gate readiness. Không gọi exchange endpoint.
- [x] **Step 3: Run final commands after PP6/provider changes:** full unit **520 passed**; final full E2E **13 passed / 381.82s**. RP4 target **1 passed / 57.69s**, PP2 focused unit **17 passed**, RP4 regression **30 passed**, PP6 target E2E **1 passed / 109.47s** (hai race orders). Ruff check/format (**227 files**), mypy (**94 files**) và Alembic single head `b2e7a9d4c631`; E2E migration checks và teardown đạt.
- [x] **Step 4: Have an independent reviewer check contracts, risk, tenant isolation, config publish, market time, PAPER-only boundary, replay and test evidence. Fix every critical/important finding using RED→GREEN.** Independent review kết luận không còn P0/P1/source P2. Documentation-only P2 đã được đóng khi đồng bộ trạng thái/evidence tại lần này.
- [x] **Step 5: Update plan/status/backlog/story acceptance, glossary links and memory with sources/date, tests, failures/root causes, open risks and current branch; read the new memory entry back and verify each claim.** Memory final đã cập nhật và reread; giữ lại giới hạn hiện hành.
- [x] **Step 6: Run impacted/full suites after PP6; only then mark WP-02 complete.** PP2/PP6, final unit/E2E, review và memory readback đều đạt. Không commit trong lần cập nhật tài liệu này.

**WP-02 completion acceptance:** all 02A–G acceptance rows from the spec pass; `project-status.md` shows WP-02 complete with linked evidence; offline PAPER vertical slice can be replayed and reports independent run/trade outcome; no credentials or venue order path; every strategy remains labelled hypothesis until evaluated; all tests and migration checks pass.

## Post-WP-02 deliverable — Multi-bot architecture brainstorm (not part of WP-02)

Approved design: [Multi-bot architecture](../specs/2026-10-24-multi-bot-architecture-brainstorm.md). Approved detailed WBS: [Multi-Bot Implementation Plan](2026-10-24-multibot-implementation-plan.md). MB-00 contract freeze is recorded in [Multi-Bot architecture contracts](../../architecture/decisions/2026-10-24-multibot-contracts.md); dependent source/schema work follows its acceptance gates.

The reviewable draft compares:

- one workspace control plane and one root configuration with per-bot versioned overlays;
- shared market-data/research/feature/knowledge pipelines with ACL and provenance;
- independent `bot_instance`, account binding/credential lease, strategy/risk/config versions, positions/orders/trades and reports per bot;
- shared signal candidates vs bot-specific risk eligibility/decisions;
- scheduling/resource isolation, idempotency, pause/breaker per bot and workspace-wide kill switch;
- schema keys/indexes/RLS, report filters/aggregation and migration from the WP-02 single-PAPER runtime;
- AI shared model/knowledge vs per-bot decision context, training leakage/tenant boundaries and strategy promotion approvals.

No bot instance schema/runtime code is included before that brainstorm has a user-approved design and its own WBS.
