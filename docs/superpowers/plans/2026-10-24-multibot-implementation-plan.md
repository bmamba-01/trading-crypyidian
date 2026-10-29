# Multi-Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` for independent work packages after their interfaces are frozen. Dependent tasks run sequentially. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mở rộng workspace hiện tại để quản lý nhiều bot PAPER, mỗi bot độc quyền một account, dùng chung market/research/AI evidence nhưng tách strategy, risk decision, ledger và reports.

**Architecture:** Giữ `WorkspaceSettings` làm cấu hình gốc/hard guardrail; mỗi bot có overlay version hóa và effective config được resolver kiểm tra. Shared signal evidence chưa actionable được fan-out tới strategy riêng của từng bot. Mọi entity giao dịch có `bot_id` cùng workspace/account lineage; reservation và publish root risk cap dùng chung serialization lock workspace→account. Cap bị siết thấp hơn exposure hiện tại tạo trạng thái breach, chặn entry mới và giữ protective monitoring; không tự flatten.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, SQLAlchemy async, PostgreSQL/RLS, Alembic, pytest, Ruff, mypy, Docker Compose. Admin web ưu tiên server-rendered FastAPI/Jinja templates để không thêm một frontend build stack; giao diện Telegram chỉ gọi cùng Admin API/application services. Mọi dependency/version cụ thể phải được đối chiếu tài liệu chính thức tại lúc triển khai.

**Spec:** [Multi-Bot architecture brainstorm](../specs/2026-10-24-multi-bot-architecture-brainstorm.md)

**Trạng thái:** WBS đã được người dùng duyệt ngày 2026-10-24; MB-00 đang thực hiện. Chưa sửa source/schema Multi-Bot.

## Global Constraints

- Source identifiers, class/function/variable/API names viết bằng tiếng Anh; comments, docstrings, hướng dẫn và giải thích viết bằng tiếng Việt.
- Mọi dữ liệu giao dịch/strategy/model phải giữ `workspace_id`, `bot_id` (khi bot-scoped), version/hash, provenance và point-in-time lineage cần thiết để tái dựng quyết định.
- `WorkspaceSettings` là root config duy nhất cho hard caps, shared data/research policies, AI governance, venue/mode và workspace kill switch; bot overlay không thể nới bất kỳ hard limit nào.
- Phiên bản đầu ràng buộc một `BotInstance` với đúng một `TradingAccount`; account là exclusive writer. Không tự adopt/đóng lệnh hoặc vị thế không có bot lineage; phát hiện thì block entry đến khi reconcile.
- Shared signal evidence chỉ là dữ liệu/evidence trung lập; từng bot chạy strategy riêng để tạo `StrategyCandidate` và tự quyết định.
- Workspace aggregate risk cap là tỷ lệ trên tổng equity các account enabled, quy đổi USDT bằng conversion data point-in-time; thiếu/stale equity/rate thì fail-closed. Account cap áp dụng riêng song song.
- Pause/kill switch block entry mới, hủy lệnh chưa khớp và giữ protective monitoring/exits cho vị thế bot đã biết. `Flatten all` là command riêng có quyền cao/recent MFA.
- Không đưa Binance private order execution vào các WP PAPER. Adapter/reconcile contract có thể được thiết kế/test bằng fake; kết nối venue thuộc WP-03.
- Mỗi task implement theo TDD RED → GREEN → REFACTOR; unit và E2E/integration liên quan phải chạy đạt trước commit. Không coi skip/flaky chưa điều tra là đạt.
- Trước mỗi WP phải ghi WBS chi tiết/acceptance đã duyệt; sau mỗi vòng implement cập nhật và đọc lại `docs/implementation/implementation-memory.md`.
- Một content-addressed app image dùng chung cho Compose E2E; không build image riêng từng test. Không cleanup container/volume ngoài đúng project label của test; giữ `crypidian-postgres-1`.

## Review Focus

1. **Race giữa bot trên cùng workspace:** hai bot cùng lúc xin reservation sát workspace cap phải không tạo reservation vượt cap; MB-00 chốt workspace→account lock, MB-04 có PostgreSQL concurrency tests cho reservation/reservation và publish/reservation.
2. **Thiếu/stale FX hoặc account equity:** không được dùng tỷ giá mặc định hoặc equity cũ; Task 5 có fail-closed unit và Compose cases.
3. **Hoạt động account không có bot lineage:** không tự nhận ownership, không tạo lệnh mới, không đóng vị thế lạ; Task 6 test fake reconciler và runtime pause.
4. **Thay root/bot config khi có vị thế mở:** entry sau publish dùng config mới, active position giữ pinned strategy/execution policy; Task 3/6 có restart/publish E2E.
5. **RBAC/report cross-bot hoặc cross-workspace:** bot scope được áp ở API/repository/RLS, workspace aggregate chỉ explicit; Task 2/7/8 có matrix tests.

## File Map

| Thành phần | Trách nhiệm dự kiến |
| --- | --- |
| `src/trading_bot/adapters/database/models/bot_entities.py` | Bot identity, config versions, grants, leases/activity state; không gom model bot vào file lớn khác. |
| `src/trading_bot/adapters/database/models/paper_entities.py`, `risk_entities.py`, `entities.py` | Thêm bot/account composite lineage vào records giao dịch/risk cần thiết. |
| `src/trading_bot/config/bot_settings.py` | Pydantic bot overlay và giới hạn cấu hình cho phép. |
| `src/trading_bot/config/workspace_settings.py` | Thêm versioned workspace aggregate-risk fraction và conversion freshness policy vào root settings. |
| `src/trading_bot/application/bot_config_resolver.py` | Resolve/check root + bot versions, caps, releases và effective time. |
| `src/trading_bot/adapters/database/repositories/bot_instances.py`, `bot_config_versions.py`, `bot_grants.py` | DB access với workspace/bot scope tường minh. |
| `src/trading_bot/admin/bots_router.py`, `bot_schemas.py`, `bot_service.py` | Admin API và permission gates cho bot lifecycle/config. |
| `src/trading_bot/domain/risk/workspace_budget.py`, `application/workspace_risk.py` | Chuẩn hóa equity/risk sang USDT và kiểm tra workspace/account/bot caps. |
| `src/trading_bot/ports/account_activity.py`, `adapters/binance_usdm/account_activity.py` | Contract phát hiện foreign order/position; PAPER fake trước, venue adapter chỉ trong WP-03. |
| `src/trading_bot/domain/market/signal_evidence.py`, `application/bot_dispatcher.py` | Shared neutral evidence và fan-out/idempotency theo bot. |
| `src/trading_bot/application/paper_runtime_bootstrap.py`, `paper_runtime_application.py`, `app.py` | Bot registry/lease, per-bot lifecycle, pause/kill semantics và readiness. |
| `src/trading_bot/admin/bot_pages.py`, `templates/admin/bots/` | Server-rendered Admin Web list/detail/config/report; tiếp tục dùng Python/FastAPI. |
| `src/trading_bot/telegram/bot_commands.py` | Telegram commands gọi cùng service; mọi command ghi rõ bot hoặc reject ambiguous target. |
| `src/trading_bot/application/bot_reporting.py`, `admin/bot_reports_router.py` | Bot-scoped reports và explicit workspace aggregate. |
| `src/trading_bot/domain/learning/knowledge.py`, `application/knowledge_registry.py` | Shared knowledge/model references; không thay thế WP-05 ingestion/WP-06 trainer. |
| `migrations/versions/*_bot_*.py` | Expand/backfill/validate/contract migration, RLS, FK, indexes và downgrade strategy. |
| `tests/unit/test_bot_*.py`, `tests/unit/test_workspace_risk.py`, `tests/e2e/test_multibot_*.py` | Unit contracts, PostgreSQL/RLS/race/migration/runtime/API E2E. |

File names trên là planned boundaries; implementer chỉ đổi tên nếu code graph/AGENTS discovery cho thấy tương đương đã tồn tại, và ghi quyết định vào memory trước khi mở rộng scope.

## Work Package Map and Acceptance Gates

| WP | Mục tiêu | Depends on | Gate |
| --- | --- | --- | --- |
| MB-00 | Contract freeze, migration and RLS design | Approved spec | Chốt entity IDs, root overlay semantics, reservation lock order, conversion freshness and rollout. |
| MB-01 | Bot identity, exclusive account binding, bot grants and migration | MB-00 | Mỗi account có đúng một bot; backfill preserve IDs/counts; RLS/foreign references không cross tenant. |
| MB-02 | Bot config versioning and root+bot resolver/Admin API | MB-01 | Draft/publish/rollback immutable/audited; overlay only tightens; root effective version always recorded. |
| MB-03 | Shared point-in-time evidence and per-bot strategy fan-out | MB-01, MB-02 | One shared market record; separate candidates/decisions; idempotency per bot; no lookahead. |
| MB-04 | Workspace/account risk aggregation and exclusive-account guard | MB-01–03 | USDT normalized aggregate cap and per-account cap atomic; stale equity/FX/foreign activity blocks entry. |
| MB-05 | Multi-bot PAPER runtime, scheduler, pause/kill, recovery/leases | MB-01–04 | Bootstrap all bots safely; one writer/account; one bot failure isolated; active bot positions retain pinned exits. |
| MB-06 | Bot-scoped reports and workspace aggregate | MB-01–05 | Bot reports isolate; aggregate explicit and permission-gated; calculations reconcile to ledger. |
| MB-07 | Admin Web and Telegram controls | MB-01, MB-02, MB-05, MB-06 | Same API/services, RBAC/MFA/CSRF, ambiguous commands rejected, lifecycle actions audited. |
| MB-08 | Shared AI knowledge/model references and per-bot evaluation lineage | MB-03, WP-05/WP-06 contracts | Immutable source/model refs; bot decisions record chosen release; no autonomous config/risk mutation. |
| MB-09 | Cutover, security/load/recovery and final review | MB-01–08 | Migration/restore/rollback, multi-bot E2E, quality gates, independent review, docs/memory/readback. |

WP-03 is a dependency for real Binance account activity detection and private order reconciliation. Before WP-03, MB-04/05 acceptance is PAPER+fake-adapter only and production readiness must remain disabled for venue execution. WP-05/WP-06 own source ingestion/training; MB-08 connects bots to their versioned shared outputs and does not duplicate those pipelines.

## Implementation Tasks

### Task 1 — MB-00: Freeze contracts, config boundary, migration and lock design

**Trạng thái:** Hoàn thành và được review độc lập ngày 2026-10-24; đã commit trên `dev`.

**Files:** Create `docs/architecture/decisions/2026-10-24-multibot-contracts.md`; update this plan only if code discovery changes interfaces.

**Interfaces:** define `BotId`/`WorkspaceId` UUID aliases; immutable `BotConfigOverlay`, `EffectiveBotConfig`, `SharedSignalEvidence`, `BotDecision`, `AccountEquitySnapshot` with explicit equity basis, `RiskConversionObservation`, `WorkspaceRiskBudget`, `AccountActivitySnapshot`, `FencingToken`, and `BotPermission` enum. All time arguments are timezone-aware UTC; monetary/risk/rate values use `Decimal`.

- [x] **Step 1: Read current entities, RLS migrations, settings service, runtime bootstrap, reservations and reports.** Code graph MCP was unavailable, so direct source/model/migration inspection used the documented fallback; recorded actual FK/grant/RLS behavior in the decision document.
- [x] **Step 2: Freeze contract boundaries.** Recorded UUID/UTC/Decimal types, immutable root+overlay semantics, point-in-time evidence/equity, bot permission and migration lineage. RED tests for overlay behavior belong to MB-02 because no production resolver exists in MB-00.
- [x] **Step 3: Freeze transaction order.** Acquire workspace advisory transaction lock before account advisory transaction lock for every mutation that changes open risk/reservations; multi-account calls sort account UUIDs. Never acquire workspace after account.
- [x] **Step 4: Define normalized equity snapshot.** `AccountEquitySnapshot` has an explicit equity basis, workspace/account scope, realized/unrealized values, capture time/source/version/hash/known state; `RiskConversionObservation` has source pair, Decimal rate, availability/as-of, quality and hash.
- [x] **Step 5: Define rollout invariant.** Every existing TradingAccount gets one disabled/active PAPER BotInstance using existing IDs/lineage; no report or ledger row is dropped or reassigned ambiguously.
- [x] **Step 6: Have a fresh reviewer check lock ordering, schema/RLS and migration invariants.** Review found and closed equity double-counting, root-cap publish race and RLS scope gaps; reviewer confirmed no remaining blocker/high-risk issue.

**Acceptance:** contract decision document matches the approved spec, lock order is acyclic and shared by reservation mutations, no root setting duplication, WP-03/WP-05/WP-06 dependencies are explicit, reviewer has no high-impact unanswered question, links and documentation diff checks pass. **Đạt:** unit 520 passed; E2E 13 passed/410.59s; reviewer clean; committed on `dev`.

### Task 2 — MB-01: Bot identity, account exclusivity, grants and schema migration

**WBS chi tiết trước code:** [MB-01 identity/migration WBS](./2026-10-24-mb01-identity-migration-wbs.md). Mode canonical ở TradingAccount; backfill bot mirror trạng thái account hiện hữu, không vô tình disable runtime.

**Files:** Create bot models/repositories and migration; modify `TradingAccount`, paper/risk/decision entities and RLS policies; tests `tests/unit/test_bot_repository.py`, `tests/unit/test_database_models.py`, `tests/e2e/test_multibot_migration.py`.

**Interfaces:** `BotInstanceRepository.create_for_account(workspace_id, account_id, name) -> BotInstance`; `get_enabled_for_runtime(workspace_id) -> Sequence[BotInstance]`; `BotGrantRepository.require(membership_id, bot_id, permission) -> None`.

- [x] **Step 1: Add unit tests for one bot per account, same-workspace composite references, duplicate account binding and revoked/missing bot grants.** Assert cross-workspace IDs fail closed.
- [x] **Step 2: Run unit tests and confirm each new invariant fails for the missing bot identity/constraints.** _(Ghi chú: phiên Codex không lưu output RED ban đầu; các test bổ sung ngày 2026-10-24 — index, grant nhiều role, guard settings, scope cache, resolve-only — đã được chạy RED trước khi sửa.)_
- [x] **Step 3: Add `BotInstance`, `BotGrant`, and bot status/config lineage models.** Database `UNIQUE(workspace_id, trading_account_id)` enforces one bot per account; composite FKs enforce workspace equality.
- [x] **Step 4: Add expand/backfill/contract Alembic migration.** Create one bot per existing account; add nullable `bot_id` to account-scoped decisions/reservations/order/fill/position/trade/report lineage; backfill by `(workspace_id, account_id)`; verify all rows resolve; then enforce NOT NULL/FK/indexes. Keep shared market/research rows workspace-scoped.
- [x] **Step 5: Add bot-scoped grants and RLS/repository filters.** Owner/admin may access all workspace bots; non-owner role requires explicit `bot_id + permission` grant. Runtime sessions set workspace and bot scope explicitly.
- [x] **Step 6: Run PostgreSQL E2E migration from WP-02 schema.** Assert row counts/hashes before and after backfill, account→bot 1:1, report/trade lineage unchanged, cross-tenant and cross-bot reads/writes denied, downgrade/upgrade on isolated DB.
- [x] **Step 7: Run unit, E2E, Alembic check, Ruff, format, mypy and diff checks.** Inspect migration SQL and locks before commit.

**Acceptance:** no account has zero or multiple bot bindings; all existing records map to exactly one bot without changing immutable payloads or PnL; bot and workspace RLS deny unauthorized scope; migrations are reversible before contract finalization and upgrade cleanly.

**Trạng thái 2026-10-24:** đã triển khai và kiểm chứng (unit 534, E2E 14/14 / 452.08s, review độc lập đã xử lý); commit `4871661` trên `dev`. Chi tiết: [MB-01 WBS](./2026-10-24-mb01-identity-migration-wbs.md#kết-quả-triển-khai-2026-10-24).

### Task 3 — MB-02: Versioned bot overlays and effective config Admin API

**Files:** Create `config/bot_settings.py`, `application/bot_config_resolver.py`, bot config repository/service/router/schemas and tests. Reuse Admin auth, CSRF, audit, outbox and root `WorkspaceSettings` contracts.

**Interfaces:** `resolve_effective_bot_config(workspace_settings: PublishedWorkspaceSettings, bot_version: BotConfigVersion, account: TradingAccount, as_of: datetime) -> EffectiveBotConfig`; `BotConfigAdminService.create_draft/publish/rollback(...)`.

- [x] **Step 1: Add unit tests for inheritance, explicit override allowlist, schema/hash validation, root-version pin, effective time and secret-field rejection.**
- [x] **Step 2: Add tests that reject bot overlay raising workspace/account risk caps, changing venue/mode, selecting inactive release or referencing another workspace.** Include boundary equality and Decimal cases.
- [x] **Step 3: Implement immutable `BotConfigVersion` JSON/hash with parent version, revision, author/approver, effective time, audit/outbox event.**
- [x] **Step 4: Implement resolver.** Resolve root published version + bot published version + approved strategy/model releases; return lineage IDs/hashes and validated effective risk subset. Drafts never affect runtime.
- [x] **Step 5: Add Admin API endpoints.** `GET /api/v1/bots`; `POST /api/v1/bots`; `GET /api/v1/bots/{bot_id}`; `POST /api/v1/bots/{bot_id}/config-drafts`; `PUT /api/v1/bots/{bot_id}/config-drafts/{draft_id}`; `POST .../validate`; `POST .../publish`; `POST .../rollback`. Every operation verifies workspace and bot permission; risk change requires recent MFA.
- [x] **Step 6: Add API tests for CSRF, RBAC, stale revision/parent, idempotent retry, audit/outbox and cross-bot/cross-workspace denial.**
- [x] **Step 7: Compose E2E publish a bot overlay while a PAPER position is open.** New entries use new overlay; old position keeps pinned strategy/config and protective exits; root hard caps remain enforced.

**Acceptance:** effective config is deterministic and hash-verifiable; bot publish never mutates root settings; no overlay can relax root cap; publish is immutable, audited and serialized; active positions keep original config lineage.

**Trạng thái 2026-10-25:** hoàn thành và tích hợp vào `dev`; review độc lập đạt. Unit MB-02 670 passed, E2E tập trung đạt; suite tích hợp cuối 793 unit + 18 E2E đạt. Chi tiết và giới hạn còn lại: [MB-02 WBS](./2026-10-24-mb02-bot-config-wbs.md).

### Task 4 — MB-03: Shared evidence and per-bot strategy candidates

**Files:** Create `domain/market/signal_evidence.py`, `application/bot_dispatcher.py`, repositories/outbox consumer and tests `test_signal_evidence.py`, `test_bot_dispatcher.py`, `test_multibot_decision.py`.

**Interfaces:** `SharedSignalEvidence(workspace_id: UUID, evidence_id: UUID, symbol: str, setup_type: str, observed_at: datetime, available_at: datetime, validity_end: datetime, feature_snapshot_id: str, source_refs: tuple[str, ...], model_release_id: UUID | None, evidence_hash: str)`; `async dispatch_evidence(evidence: SharedSignalEvidence, bots: Sequence[BotInstance], as_of: datetime) -> Sequence[BotDecision]`.

- [x] **Step 1: Add domain tests for causality, validity interval, workspace scope, provenance hash and no account/quantity/order fields in shared evidence.**
- [x] **Step 2: Add dispatcher tests with two bots using different strategy releases.** Shared market evidence is persisted once; each bot produces its own candidate/NoTrade and unique decision idempotency; stale/foreign evidence rejects.
- [x] **Step 3: Implement evidence repository and idempotent outbox fan-out.** Deduplicate shared source event once, then enqueue one `(workspace_id, bot_id, evidence_id, bot_config_version_id)` decision job per eligible bot.
- [x] **Step 4: Invoke existing StrategyRegistry/PaperDecisionCycle using bot-specific `StrategySelection`.** Do not make shared detector output directly executable; record per-bot veto/abstain/reason and selection lineage.
- [x] **Step 5: Add temporal tests.** Evidence, feature, market/research inputs and model release must have `available_at <= evaluated_at`; future or revised-later evidence is rejected for historical evaluation.
- [x] **Step 6: Add PostgreSQL E2E.** One shared event feeds two bots; assert one shared evidence row, two bot-scoped decisions, isolated candidate/config/version and retry creates no duplicates.

**Acceptance:** common market/research evidence is not duplicated per bot; each bot runs its own strategy and decision; evidence cannot create a venue order; per-bot replay is deterministic and point-in-time valid.

**Trạng thái 2026-10-25:** hoàn thành và tích hợp vào `dev`; migration được re-chain sau MB-02, có FK ghép config; review độc lập đạt. Suite tích hợp cuối 793 unit + 18 E2E đạt. Các giới hạn đã chuyển tiếp sang MB-04/MB-05 gồm risk gate chưa nhận candidate fan-out, eligibility exclusion chưa persist và một số retry semantics. Chi tiết: [MB-03 WBS](./2026-10-24-mb03-shared-evidence-wbs.md).

### Task 5 — MB-04: Equity conversion and aggregate workspace/account risk

**Trạng thái 2026-10-28: hoàn thành trong phạm vi PAPER, chờ commit tích hợp lên `dev`.** Round review khép cap bot, exposure ledger thực, clock/as_of, SECURITY DEFINER/RLS, equity/partial-exit/cost, reservation replay và publish/reserve race; provenance nguồn seed/trade/fill/cost/mark được tính cùng snapshot SQL và ghi vào decision. Migration tích hợp tuyến tính `f7b9d1e3a5c8 → f7a9c1e3b5d8 → b3d5f7a9c1e4`. Full unit **1570 passed**; MB-04 real-path E2E **1 passed**, config/concurrency E2E **2 passed**; migration subset **17 passed**; Ruff/format/mypy/Compose/Alembic đạt. Full `tests/e2e` **27 passed / 1924.14s**. Failure trước đó được tái hiện bằng event outbox khác loại và sửa bằng `event_types` riêng trong probe; xem [WBS MB-04](./2026-10-25-mb04-workspace-risk-wbs.md) và [implementation memory](../../implementation/implementation-memory.md). Không bật Binance private execution/live.

**Files:** Create `domain/risk/workspace_budget.py`, `application/workspace_risk.py`, account-equity/conversion repositories and tests `test_workspace_risk.py`, `test_risk_conversion.py`, `test_workspace_risk_repository.py`.

**Interfaces:** `mark_to_market_equity(basis: PaperEquityBasis | VenueEquityBasis, open_positions: Sequence[OpenPositionValuation], marks: Mapping[str, MarketMark], as_of: datetime) -> AccountEquitySnapshot`; `normalize_equity_to_usdt(snapshot: AccountEquitySnapshot, conversion: RiskConversionObservation | None, as_of: datetime, max_age: timedelta) -> Decimal`; `evaluate_workspace_budget(workspace_cap_fraction: Decimal, account_caps: Mapping[UUID, Decimal], snapshots: Sequence[AccountEquitySnapshot], open_risk: Mapping[UUID, Decimal], pending_reservations: Mapping[UUID, Decimal], as_of: datetime) -> WorkspaceRiskDecision`. `PaperEquityBasis` capital excludes cumulative realized PnL; `VenueEquityBasis` is a source-verified total balance that already includes realized PnL and must not add it again.

- [x] **Step 1: Write Decimal unit tests for mark-to-market equity and conversion.** Cover PAPER `initial_capital_before_realized_pnl + cumulative_realized_pnl + Σ unrealized_pnl_at_as_of`; cover venue total equity that already includes realized PnL without adding it twice; long/short and adverse/favorable marks, USDT identity/non-USDT conversion, rounding, stale/future/missing mark/rate, unknown equity, negative/non-finite values and mismatched `as_of`. Open position with no usable mark fails closed for new reservations.
- [x] **Step 2: Write budget tests.** `workspace_open_risk = Σ open worst-case stop-risk + Σ active pending reservation`; cap is `workspace_max_open_risk_fraction × workspace equity USDT`; equity includes closed and mark-to-market PnL without target equity double-count. Account cap and tightening/breach behavior are tested.
- [x] **Step 3: Implement immutable normalized equity/conversion snapshots with source/as-of/hash lineage.** MB-04 implementation remains PAPER-only; venue snapshot/live execution belongs to later WP-03. PAPER equity uses point-in-time entry/exit fills, mark and recorded costs. Missing/stale/unsupported inputs fail closed.
- [x] **Step 4: Extend root settings and bot overlay limits.** Validate root/account/bot caps and preserve version/audit semantics; persist root/profile/bot/equity/conversion lineage on risk decisions/reservations.
  - _Ruling R4 (controller phê chuẩn 2026-10-26, sau review độc lập MB-04):_ không bắt buộc `account_max_open_risk_fraction ≤ workspace_max_open_risk_fraction` khi publish vì hai tỷ lệ áp trên hai nền equity khác nhau (account equity vs tổng workspace equity). Runtime kiểm từng scope trên nền equity của chính nó; trần bot hiệu lực = min(bot, account, kẹp theo workspace fraction) nên không bao giờ nới bất kỳ trần nào. Overlay bot vẫn validate chặt khi publish.
- [x] **Step 5: Implement atomic reservation transaction.** Serialize workspace then account; re-read authoritative settings, scopes, exposure and reservations; enforce caps; persist idempotent decision/reservation.
- [x] **Step 6: Add PostgreSQL concurrency tests.** Two-bot/account race and cap tightening prove reservation totals stay within root cap; config publish and reservation use shared locks; existing exposure remains open and blocks new entries.
- [x] **Step 7: Add Compose E2E for equity/risk edge cases and migration.** Verify long/short and partial exit + fee/slippage/funding, stale/future/missing inputs, unsupported quote, RLS/grants, migration downgrade/upgrade and `alembic check`.

**Acceptance:** concurrent new reservations cannot push open + pending worst-case risk above the effective workspace/account/bot caps; concurrent cap tightening is serialized with reservations, and if a lower cap leaves existing exposure over cap, the system blocks new entries while protecting known positions; equity includes realized plus mark-to-market unrealized PnL without double counting at point-in-time; risk is versioned and USDT-normalized; missing/stale mark, conversion, equity or unknown state fails closed; reservations are bot-idempotent and ledger-linked.

### Task 6 — MB-05: PAPER runtime registry, exclusive account guard, pause/kill and recovery

**Files:** Create bot runtime registry/lease repository, `ports/account_activity.py`, PAPER fake activity adapter and tests; modify `paper_runtime_bootstrap.py`, `paper_runtime_application.py`, `app.py`, `domain/execution/paper_state.py`.

**Interfaces:** `AccountActivityPort.inspect(account_id, cursor) -> AccountActivitySnapshot`; `BotRuntimeLease.acquire(workspace_id, bot_id, owner_id, expires_at) -> FencingToken`; `pause_bot(bot_id, reason) -> BotControlResult`; `pause_workspace(reason) -> WorkspaceControlResult`.

- [ ] **Step 1: Add unit tests for per-bot runtime isolation, one writer/account, lease expiry/fencing, stale worker rejection and bot bootstrap failure not disabling sibling bot.**
- [ ] **Step 2: Add activity tests.** Known bot state passes; foreign/manual order/position marks account `EXTERNAL_ACTIVITY_UNRECONCILED`, blocks new entries, emits audit diagnostic, does not adopt or close external state; known bot position still receives protective monitoring/exits.
- [ ] **Step 3: Extend bootstrap to resolve one effective config per bot and construct one PAPER gateway/coordinator per bot/account.** Hydrate before readiness; isolate bot readiness but workspace startup must expose degraded aggregate status.
- [ ] **Step 4: Add lease/fencing at account execution boundary.** Require current fencing token on every state-changing command; stale process cannot submit/cancel/fill/append ledger.
- [ ] **Step 5: Implement bot pause and workspace kill switch.** In one transaction mark entry disabled and cancel unfilled local PAPER orders; continue monitor/protective exits for known open positions. `Flatten all` stays a separate, high-permission/MFA command and is not part of this WP's automatic behavior.
- [ ] **Step 6: Add restart E2E with two bots.** Restart runtime, restore per-bot checkpoints/leases, preserve settings/profile/strategy pins; corrupt one bot state and verify only that bot pauses while sibling state and lineage remain intact.
- [ ] **Step 7: Add foreign activity integration E2E via fake adapter.** Foreign unknown order/position stops that bot's entry; after explicit reconciliation resolution, bot can resume with audited actor/reason. Live exchange implementation remains disabled until WP-03.

**Acceptance:** exactly one active writer/account; foreign state cannot be silently adopted; entry pause/kill semantics match spec; active known positions maintain protective exits; restart preserves isolated bot state; sibling bot failures are isolated.

**Cập nhật MB-05b (2026-10-29):** đã nối persisted `CANDIDATE` decision theo strategy release riêng vào PAPER runtime/MB-04 coordinator, có recovery/idempotency, pause/fencing và E2E hard-cap. Unit 1577 passed; full E2E **36 passed / 3524.36s**; independent review và quality gates đạt. Runtime worker cần `DecisionSnapshotProvider` được inject; app mặc định chưa có adapter market-data → `DecisionSnapshot` và readiness ghi `market_dispatch: not_configured`. Tự động fan-out từ market data phụ thuộc adapter WP-05. Chi tiết: [WBS MB-05](./2026-10-26-mb05-runtime-registry-wbs.md) · [WBS MB-05b](./2026-10-28-mb05b-runtime-fanout-wbs.md).

### Task 7 — MB-06: Bot-scoped reports and explicit workspace aggregation

**Files:** Create `application/bot_reporting.py`, `admin/bot_reports_router.py`, reporting schemas/repository and tests `test_bot_reporting.py`, `test_bot_reports_api.py`, `tests/e2e/test_multibot_reports.py`.

**Interfaces:** `BotReportQuery(workspace_id, bot_id, start, end, mode, report_version)`; `get_bot_report(query, principal) -> BotReport`; `get_workspace_report(query, principal) -> WorkspaceAggregateReport` only when `scope=workspace` is explicit.

- [ ] **Step 1: Add unit tests for trade count/PnL/fees/costs/drawdown lineage, time boundaries and bot filter.**
- [ ] **Step 2: Add authorization tests.** Non-granted bot returns indistinguishable not-found/forbidden policy; implicit bot omission is rejected when query could aggregate; workspace aggregate needs explicit scope and permission.
- [ ] **Step 3: Implement repository queries with workspace/bot filters and deterministic report hash.** Do not recompute or mutate append-only ledger records.
- [ ] **Step 4: Add Compose E2E with two bots/accounts and distinct trades.** Assert each report contains only its bot; explicit workspace report sums both and matches ledger net PnL exactly; RLS prevents cross-workspace visibility.

**Acceptance:** no default report leaks/combines bots; each bot report reconciles to its ledger and carries config/strategy/model lineage; workspace rollup is explicit, authorized, deterministic.

### Task 8 — MB-07: Admin Web and Telegram bot operations

**Files:** Create `admin/bot_pages.py`, `templates/admin/bots/`, `telegram/bot_commands.py`; modify `app.py`, `telegram/` routing and permission policy. Add Jinja dependency only after checking the current locked runtime and official docs.

**Interfaces:** web page handlers use existing `BotAdminService`/`BotReportingService`; Telegram `handle_bot_command(principal, command, bot_id | None) -> BotCommandResult` never embeds direct database writes.

- [ ] **Step 1: Add API/service tests for bot list/detail/create/disable/config lifecycle/status/pause/resume/report using existing auth session, CSRF, RBAC and recent MFA rules.**
- [ ] **Step 2: Add Playwright/browser E2E for create/bind account, edit strategy overlay, validate/publish, view per-bot history and report, pause/resume.** Use only the shared app image/test project; verify accessibility for status and risk state.
- [ ] **Step 3: Add Telegram command tests.** `/bots`, `/bot status <id>`, `/bot pause <id>`, `/bot resume <id>` enforce Telegram binding and bot permissions; ambiguous command with multiple bots rejects with explicit bot IDs; unknown ID never falls back to another bot.
- [ ] **Step 4: Expose workspace-wide pause/kill only to owner/admin and audit all state changes.** High-risk flatten-all is a separate confirmation path with recent MFA in Admin Web; do not add implicit Telegram flatten action.
- [ ] **Step 5: Verify no credential/token appears in rendered HTML, API payload, logs or audit payload.**

**Acceptance:** one shared control plane manages all bots; bot views/actions and reports are scoped; Telegram and UI call same application services; ambiguous/unauthorized actions reject; all operations audited.

### Task 9 — MB-08: Shared knowledge/model artifacts and per-bot evaluation lineage

**Dependencies:** WP-05 source/research contracts and WP-06 model/evaluation/approval contracts. If those WPs are not implemented, build only a typed reference contract and do not duplicate fetch/trainer systems.

**Files:** Create `domain/learning/knowledge.py`, `application/knowledge_registry.py`, knowledge release repository and tests; modify decision/report lineage.

**Interfaces:** `KnowledgeArtifactRef(workspace_id, artifact_id, artifact_hash, source/license_refs, captured_at, available_at, schema_version)`; `ApprovedModelReleaseRef`; bot config can reference an approved immutable release but cannot edit/publish it.

- [ ] **Step 1: Test artifact scope/provenance/license, content hash, availability time, version and immutable release lookup.**
- [ ] **Step 2: Test bots may reference shared approved artifacts but cannot read another workspace artifact or unapproved model release.**
- [ ] **Step 3: Record model/knowledge release IDs/hashes in per-bot decision and report lineage.**
- [ ] **Step 4: Test replay split/cutoff so bot outcomes after decision time cannot leak into shared model context for earlier decision.**
- [ ] **Step 5: Test model/strategy promotion requires approval and per-bot impact report; no scorer may mutate root settings, bot overlays or risk limits.**

**Acceptance:** common knowledge is immutable and provenance-preserving; bot-specific outcome/decision remains isolated; only approved references can be consumed; no autonomous risk/config changes. Ingestion/training remain owned by WP-05/WP-06.

### Task 10 — MB-09: Migration rehearsal, final E2E, operations and independent review

**Files:** update migrations, Compose, `docs/implementation/project-status.md`, `docs/implementation/implementation-memory.md`, this plan and operational documentation.

- [ ] **Step 1: Rehearse expand/backfill/contract migration on a production-shaped DB snapshot.** Record before/after account/bot/trade/reservation/report counts and canonical hashes; rehearse rollback and restore.
- [ ] **Step 2: Add multi-bot Compose vertical slice.** Create two bots/two accounts; publish distinct strategies; ingest one shared market event; produce two bot-specific outcomes; race aggregate reservations; pause one bot while the other continues; kill workspace and confirm protective exits; restart and reconcile reports.
- [ ] **Step 3: Add cross-workspace and bot permission matrix E2E.** Verify owner/admin, granted operator, ungranted operator, runtime bot context and Telegram identity against bot APIs/reports.
- [ ] **Step 4: Add failure/load checks.** stale/missing equity or conversion, foreign venue state, worker lease loss, DB restart, bot exception, shared source outage and scheduler backlog. Confirm only intended bot/workspace scope degrades.
- [ ] **Step 5: Run the complete gates sequentially.** Full unit; all Compose E2E; Alembic check, upgrade/downgrade/upgrade; Ruff check/format; mypy; `git diff --check`; artifact cleanup by exact Compose labels.
- [ ] **Step 6: Request independent review.** Resolve P0/P1/P2 findings, add RED regression tests for each fix, rerun impacted suites and full required gates.
- [ ] **Step 7: Update/read memory.** Record changed behavior, test commands/output, failure root causes, decisions, versions/official URLs, limitations, commit and branch; reread entry and cross-check docs.
- [ ] **Step 8: Commit each accepted WP on `dev` after its unit + E2E gates pass.** No push unless explicitly requested; never commit secrets or local E2E data.

**Final acceptance:** one workspace can operate multiple PAPER bots across exclusive accounts; shared evidence/knowledge is stored once and versioned; each bot has distinct settings/strategy/risk decision/trade/report lineage; concurrent reservations cannot breach effective workspace/account caps, with explicit safe behavior when an administrator tightens a cap below existing exposure; unknown external account state blocks new entries; pause/kill and protective exits obey the approved policy; migration/RLS/RBAC/API/UI/Telegram/recovery/report tests pass; independent review has no unresolved critical/high findings; implementation memory and project status are synchronized.

## Execution Order and Parallelism

1. MB-00 sequentially, then freeze interfaces before parallel code.
2. MB-01 must precede every bot-scoped feature.
3. After MB-01 contracts land, MB-02 and MB-03 may run in parallel on separate files; MB-04 depends on both risk/config and bot scope.
4. MB-05 depends on MB-01–04; it is the first full PAPER multi-bot runtime slice.
5. MB-06 and MB-07 can parallelize after MB-05 service contracts freeze.
6. MB-08 waits for WP-05/WP-06 reference contracts; no duplicate ingestion/trainer implementation.
7. MB-09 runs after all preceding acceptance gates; Compose tests are sequential to avoid duplicate resource/image contention.

Every work package starts with its own accepted WBS/acceptance criteria, uses separate worktree for parallel source edits if supported, and ends with a focused independent review before the next dependent package merges.

## Acceptance Traceability

| Spec requirement | WBS task(s) |
| --- | --- |
| Shared root config + versioned bot overlay; overlay only tightens | MB-00, MB-02 |
| One bot/account, exclusive writer, foreign activity blocks new entries | MB-01, MB-05; real venue detection gated by WP-03 |
| Shared market/research/features/evidence with provenance | MB-03, MB-08; source collectors gated by WP-05 |
| Bot-specific strategy candidate, decision, risk, trades and report | MB-03–06 |
| Aggregate workspace risk normalized to USDT + per-account cap | MB-04 |
| Workspace kill switch, bot pause, protective exits, separate flatten-all | MB-05, MB-07 |
| Admin API/Web/Telegram and role scope | MB-01, MB-02, MB-07 |
| Shared AI knowledge/model releases, no autonomous risk mutation | MB-08; trainer/promotion engine gated by WP-06 |
| Migration/RLS/idempotency/recovery/independent final gate | MB-01, MB-05, MB-09 |

## Term glossary EN→VI

- **Bot instance — thực thể bot:** cấu hình/runtime riêng gắn account, strategy và report trong một workspace.
- **Configuration overlay — lớp cấu hình kế thừa:** phần bot-level chỉ được ghi đè các giá trị allowlist; root hard limits giữ ưu tiên.
- **Shared signal evidence — bằng chứng tín hiệu dùng chung:** market/research/feature fact có provenance/as-of, chưa phải candidate/lệnh của bot.
- **Workspace aggregate risk — rủi ro tổng workspace:** tổng worst-case risk của vị thế mở cộng reservation chờ, so với tỷ lệ tổng equity quy đổi USDT.
- **Risk conversion observation — quan sát quy đổi rủi ro:** tỷ giá point-in-time có nguồn, thời điểm khả dụng, độ mới và hash dùng để chuẩn hóa equity/risk.
- **Exclusive account binding — liên kết account độc quyền:** một bot là writer duy nhất; activity không nhận diện được làm entry bị chặn cho tới reconcile.
- **Fencing token — mã hàng rào quyền sở hữu:** token đơn điệu ngăn worker cũ ghi state sau khi lease đã chuyển.
- **Bot-scoped grant — quyền cấp theo bot:** quyền truy cập riêng cho bot ID; không suy diễn quyền từ việc thuộc cùng workspace.
- **Protective monitoring — giám sát bảo vệ:** stop/target/trailing exits tiếp tục được xử lý dù bot đã pause entry.
