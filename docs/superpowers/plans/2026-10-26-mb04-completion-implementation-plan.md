# MB-04 Completion Implementation Plan

> **For agentic workers:** Thực hiện tuần tự theo TDD RED → GREEN → REFACTOR. Các bước dùng checkbox để theo dõi.

**Goal:** Khép các gap MB04.7–MB04.9 bằng workspace equity aggregate fail-closed, policy/scope authoritative và PAPER runtime gate có khóa/lifecycle nhất quán.

**Architecture:** SQL `SECURITY DEFINER` chỉ trả scalar workspace equity/open/pending/count/unknown dưới `app.workspace_id`. Application coordinator tự nạp root profile, target bot/account và effective overlay sau workspace→account locks; không nhận caps hoặc cross-bot row inputs từ caller. PAPER runtime gọi coordinator trước gateway submit và dùng coordinator cho reservation lifecycle, còn detail target-account đọc theo RLS hiện hành.

**Tech Stack:** Python 3.14, SQLAlchemy async, PostgreSQL 18 / Alembic, pytest + Compose E2E, Ruff, mypy.

**Spec:** `docs/superpowers/plans/2026-10-25-mb04-workspace-risk-wbs.md`, addendum MB04.7–MB04.9; contract `docs/architecture/decisions/2026-10-24-multibot-contracts.md` mục 5–6.

## Global Constraints

- PAPER only; không thêm Binance private execution và không phát lệnh live.
- Database runtime role không có `BYPASSRLS`; không mở row-level cross-bot access.
- Decimal money, UTC-aware `as_of`, source availability ≤ `as_of`; missing/stale/future/unsupported conversion phải deny new entry.
- Advisory lock order duy nhất: workspace risk lock → account locks sort UUID tăng dần.
- Code identifier tiếng Anh; comments/docstrings/docs tiếng Việt; thuật ngữ mới ghi glossary EN→VI có nguồn.
- Cập nhật/reread implementation memory sau vòng; chỉ push khi test/quality evidence mới đã được đối chiếu.

## Review Focus

1. Thiếu seed/fill/entry fill/mark hoặc mark cũ ở một bot khác phải set unknown và block workspace entry.
2. Workspace USDⓈ-M quote asset không hỗ trợ conversion không được giả định rate=1.
3. Bot/account disabled hoặc bot/account ID giả không được tạo target scope hay denominator sai.
4. Draft/expired bot overlay và root/profile ngoài workspace không được áp vào budget.
5. Runtime workspace deny, exception, retry hoặc concurrent fill phải không gọi gateway, không orphan decision/reservation, không vượt cap.
6. Lifecycle transition qua legacy repository không được bỏ qua workspace lock hoặc làm double-count open/pending risk.

---

### Task 1: DB aggregate workspace equity + exposure

**Files:**
- Modify: `src/trading_bot/domain/risk/workspace_budget.py`
- Modify: `src/trading_bot/adapters/database/repositories/risk_reservations.py`
- Add: `migrations/versions/e4f6a8c0d2b5_workspace_equity_aggregate.py` nối Alembic head `d9b2f4a6c8e1`
- Test: `tests/unit/test_workspace_risk.py`, `tests/unit/test_risk_reservation_repository.py`
- E2E: `tests/e2e/probes/workspace_risk_concurrency.py`, `tests/e2e/test_workspace_risk_concurrency_e2e.py`

**Interface:** `WorkspaceRiskAggregate` bổ sung `total_equity_usdt` và `has_unknown_equity`. Repository gọi function với workspace, as_of và max mark age. DB function giữ các aggregate risk hiện có, thêm equity từ enabled PAPER accounts: seed + closed net PnL + unrealized PnL theo weighted entry fills và mark mới nhất khả dụng.

- [x] Viết unit test domain: aggregate unknown-equity/mismatch/zero active bots deny; known aggregate là workspace denominator mà không cộng lại target equity; cross-bot exposure vẫn lấy max scalar để không double-count.
- [x] Chạy test và xác nhận fail vì DTO/engine chưa có total equity/unknown-equity.
- [x] Viết repository/E2E tests: equity 2 accounts (seed + realized + long và short UPNL) có tổng đã tính tay; stale/missing/future mark, thiếu seed/fill, unsupported quote asset => unknown; role runtime chỉ nhận scalar, không thể SELECT cross-bot; function privilege đúng, không BYPASSRLS.
- [x] Chạy test mới để xác nhận RED đúng contract.
- [x] Thêm migration sau head hiện hành, model/DTO/repository parameters, SQL function security (scope GUC, pinned search_path, revoke PUBLIC/grant explicit roles) và engine denominator.
- [x] Chạy focused unit, targeted Compose E2E, `alembic upgrade/check/downgrade/upgrade` và security grant assertions.

### Task 2: Authoritative target scope/policy loader

**Files:**
- Modify: `src/trading_bot/application/workspace_risk.py`
- Modify: `src/trading_bot/domain/risk/workspace_budget.py`
- Use: `src/trading_bot/application/runtime_risk_profile.py`, `src/trading_bot/application/bot_config_resolver.py`, `src/trading_bot/adapters/database/repositories/bot_instances.py`
- Test: `tests/unit/test_workspace_risk_coordinator.py`, `tests/unit/test_workspace_risk.py`
- E2E: `tests/e2e/probes/workspace_risk_concurrency.py`

**Interface:** `reserve(request: ReservationRequest) -> ReservationResult` has no caller cap/other-scope arguments. After workspace→account locks, load effective root at request `as_of`, validate `RiskProfileVersion` in workspace, read enabled PAPER `BotInstance`/`TradingAccount`, resolve effective published bot overlay, derive account cap from root risk profile and bot cap from resolver. Target AccountEquity/FX/positions are loaded only under its bot RLS. Workspace denominator and cross-bot exposure come from Task 1 aggregate.

- [x] Add unit tests that call `reserve(request)` and reject disabled target, wrong bot↔account pair, missing/foreign risk profile, invalid overlay, cap expansion, missing workspace aggregate; prove supplied request cannot override root/account/bot limits.
- [x] Run tests and verify they fail on current signature/caller-provided scopes.
- [x] Add authoritative resolver/repository calls after locks; remove account cap, bot cap and `other_account_scopes` inputs; target `enabled` must come from database and status mismatch denies.
- [x] Assert decision lineage stores exact root settings ID/hash, root risk profile ID, bot config ID/hash, target equity snapshot and conversion IDs.
- [x] Run focused tests, Ruff, mypy and real-role E2E.

### Task 3: PAPER runtime integration and common lifecycle locks

**Files:**
- Modify: `src/trading_bot/application/paper_runtime_application.py`
- Modify: `src/trading_bot/application/paper_decision_cycle.py`
- Modify: `src/trading_bot/application/workspace_risk.py`
- Modify: `src/trading_bot/adapters/database/repositories/risk_reservations.py`
- Modify: `src/trading_bot/adapters/database/repositories/paper_ledger.py` only if lifecycle lock cannot be centralized in risk repository
- Tests: `tests/unit/test_paper_runtime_application.py`, `tests/unit/test_workspace_risk_coordinator.py`, `tests/unit/test_risk_reservation_repository.py`
- E2E: `tests/e2e/probes/workspace_risk_concurrency.py`, `tests/e2e/test_workspace_risk_concurrency_e2e.py`, relevant Compose runtime boundary test.

**Interface:** After the existing per-trade `RiskEngine` approves and records its plan, runtime maps the exact `TradePlan`/`RiskDecision` amounts to `ReservationRequest`, calls `WorkspaceRiskCoordinator`, returns a workspace risk denial without gateway submit, and submits only with coordinator decision/reservation IDs. The coordinator’s decision remains separately linked in evidence from the per-trade RiskEngine decision. Every repository mutation path acquires shared workspace risk lock before account lock.

- [x] Write runtime tests first: root/account/bot workspace denial results in `RISK_REJECTED` and gateway call count 0; approved path passes exact IDs to PAPER submission; downstream failure rolls back both decision and reservation.
- [x] Write lock tests first for legacy reserve, unknown, reconcile, release, expiry and fill transfer; assert workspace lock precedes sorted account lock.
- [x] Run tests and verify expected RED.
- [x] Wire coordinator at runtime boundary; add workspace budget decision to result/evidence; preserve original RiskEngine result separately; avoid direct old reservation creation.
- [x] Route all reservation state transitions through coordinator/shared lock path; preserve idempotency and callback transaction semantics.
- [x] Run focused unit/E2E and check no execution gateway call on denied paths.

### Task 4: Security review, integration regression and close-out

**Files:** WBS, project status, implementation memory, glossary if needed, all touched test/source files.

- [x] Tự review SQL function owner, `search_path`, GUC scope check, PUBLIC execute, role grants, RLS, count/equity leak surface và workspace mismatch; E2E xác minh RLS, function privilege và BYPASSRLS.
- [x] Tự review DB/code/auth/bảo mật/strategy-risk, lineage, precision, freshness, retry/idempotency, concurrency, status/authorization và no-order-on-deny; không có review độc lập trong checkout.
- [x] Đã chạy `tests/unit` 1103 passed; `tests/e2e` 20 passed/1033.63s; Alembic single head + Compose migration upgrade/downgrade/upgrade/check; Ruff check/format, mypy 127 files, Compose config, diff check và rà soát secret.
- [x] Không xảy ra timeout ở full E2E cuối; một lượt trước có stale assertion do snapshot test, đã đồng bộ và chạy lại full suite xanh.
- [x] Kiểm tra thuật ngữ: không phát sinh thuật ngữ mới; nguồn PostgreSQL chính thức và ngày kiểm tra đã có trong WBS.
- [x] Update/read back implementation memory, WBS, main MB plan and project status; confirm all MB04.7–MB04.9 acceptance criteria.
- [x] Commit on `dev`, push to `origin/dev`, then verify remote head equals pushed commit and local workspace contains no unintended changes.
