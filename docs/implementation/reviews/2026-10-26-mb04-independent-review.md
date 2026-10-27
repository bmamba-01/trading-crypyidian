# Bàn giao: review độc lập MB-04 và nhánh sửa dở `mb04-review-fixes`

**Ngày:** 2026-10-26/27 · **Người nhận:** phiên phụ trách MB-04 (Codex) · **Trạng thái MB-04:** chưa nghiệm thu.

## Nhánh và commit
- `dev` @ `274793e` chứa MB-04 như đã merge (chưa sửa các finding dưới đây).
- Nhánh cục bộ `mb04-review-fixes` (chưa push):
  - `b5efea4` — sửa C1–C3, I1–I6 và minor rẻ; migration `f7a9c1e3b5d8` sau `e1f3a5c7b9d2`; E2E mới `tests/e2e/test_workspace_risk_real_paths.py`. **Đã re-review:** C1, C2, C3 (as_of cũ), I1, I3, I5, I6, minor đạt; I4 theo ruling R4 (đã được controller phê chuẩn, ghi ở plan Task 5 Step 4); còn mở Important 1 (cửa sổ độ mới mark không giới hạn) và Important 2 (as_of tương lai đọc cap version chưa hiệu lực). Kiểm thử báo cáo: unit 1277, full E2E 25/25 / 1422 s.
  - `26ed21e` — **WIP chưa kiểm chứng** cho vòng sửa 2 (Important 1/2, Minor 3 đồng hồ DB, Minor 4 guard loader production; có migration `b3d5f7a9c1e4_workspace_risk_mark_policy.py`). Full E2E bị ngắt do Docker khởi động lại; chưa re-review. Có thể dùng làm điểm xuất phát hoặc bỏ.
- Cả hai commit sẽ cần nối lại chuỗi migration nếu `dev` có migration mới.

## Finding gốc của review độc lập (trên `b85c21b`)
## Unchecked WBS items
- MB-04 WBS line ~119 "Reservation acquire workspace lock rồi account lock; sau lock re-read …": lock ORDER is correct everywhere (coordinator, legacy _lock_account risk_reservations.py:749-751, expiry, root/bot publish settings lock → risk lock → bot lock), but the post-lock re-read is NOT authoritative (C1, C2, C3). Keep unchecked until fixed.
- WBS line ~144 (independent review): closes only after C1–C3 and Important items are fixed test-first and suites re-run.

## Critical
C1. Bot overlay cap silently ignored — src/trading_bot/application/workspace_risk.py:687 returns `effective.risk_limits.account_max_open_risk_fraction` as the bot cap; resolver sets that to the root profile account cap (bot_config_resolver.py:539); overlay is `bot_max_open_risk_fraction` (:540). Scenario: bot publishes bot_max_open_risk_fraction=0.05 under account cap 0.25 → reservations approved up to 25%. Tests missed it because coordinator unit tests inject caps via `test_scope_caps_loader` (tests/unit/test_workspace_risk_coordinator.py:89-93, 663-669) so `_load_authoritative_caps` never runs; no E2E publishes an overlay. Fix: use bot_max_open_risk_fraction; RED test driving `_load_authoritative_caps` with a real resolved overlay (session-level or Compose).

C2. Unknown open risk treated as zero for account and bot caps — workspace_risk.py:702-707 takes worst-case risk from `trade.lineage["worst_case_loss_usdt"|"requested_risk"]` defaulting to ZERO; production trade lineage never has these keys (paper_ledger.py:1001-1016). Once a reservation is TRANSFERRED the position contributes 0 to account/bot exposure → a bot can open positions up to the workspace cap, bypassing account and bot caps (workspace safe only via max(local, aggregate) at workspace_budget.py:544-546). Tests missed it: E2E fixture hand-writes lineage (probes/workspace_risk_concurrency.py:621); unit test builds PositionRisk by hand (test_workspace_risk_coordinator.py:729-736). Fix: derive per-trade risk from the linked reservation (trade.risk_reservation_id → reserved_risk, as the SQL does); missing/invalid fails closed; test where the trade is created by the real ledger.

C3. as_of not bound to wall clock — workspace_risk.py:295, 337-341 (injected clock :202 unused in reserve); e4f6a8c0d2b5_workspace_equity_aggregate.py:53, 59; runtime passes as_of=command.evaluated_at (paper_runtime_application.py:419). (a) a root tightening published at T is ignored by a reservation whose evaluated_at < T even though it runs after the publish commits (load_effective_config(as_of) returns older version); (b) other bots' trades with opened_at > as_of excluded from open risk though their reservations are TRANSFERRED; (c) future as_of drops RESERVED rows with expires_at <= as_of. Compose denial test only sees the new cap because it uses evaluated_at=now+1s (tests/e2e/test_compose_boundary.py:2609). Legacy clock-based candidate-expiry check (risk_reservations.py:244-253) lost on this path. Fix: read caps, exposure and reservations at max(as_of, clock()) (or require as_of within a small skew of clock()), reject expires_at <= clock(); tests for stale as_of after a publish and for a cross-bot trade opened after as_of.

## Important
I1. SECURITY DEFINER aggregates owned by bootstrap superuser — e4f6a8c0d2b5…py:26, c8f1e3a5b7d9…py:32; no ALTER FUNCTION … OWNER TO, owner = migrator crypidian_admin (compose.yaml:116). Follow MB-01 precedent (dedicated NOLOGIN/NOBYPASSRLS owner role + explicit policies, c7a90d3f8e21_bot_identity_scope.py:271-321); SELECT-only grants; E2E asserting proowner and its privileges.
I2. SQL mark freshness uses available_at not observation time — e4f6a8c0d2b5…py:151, 177-180; late/backfilled old bars count as fresh; Python target path uses close_time (account_equity.py:296, 311) → inconsistent; with max_mark_age_seconds=60 and 1h bars the Python path is stale most of each hour. Fix: close_time vs max age and a consistent timeframe policy.
I3. Target account equity ignores partial exits, open-trade costs, uses first entry fill price — account_equity.py:186-216; can overstate equity/headroom (exit 0.5 at 9000, mark back at 10000 → overstated by 500 plus costs); contradicts Plan Step 3; disagrees with SQL. Fix: one shared computation with SQL, or fail closed on disagreement.
I4. account ≤ workspace cap validated only in resolver — bot_config_resolver.py:506 flags profile.account_max_open_risk_fraction > workspace_max_open_risk_fraction, root publish never validates → tightening workspace below profile cap makes every bot with a published overlay unresolvable (PAPER_BOT_CONFIG_NOT_EFFECTIVE/ValueError) instead of recorded CAPACITY_BREACH_AFTER_TIGHTENING. Fix: validate at root/profile publish, or clamp rather than issue.
I5. Breach not recorded at publish (contract §6; admin/settings_service.py:235-236, 298-299 take the risk lock but never read exposure) and no risk-cap publish/reservation race E2E (Review Focus 1; WBS line ~139 marked [x] wrongly — tightening scenario inserts workspace_settings_version by direct SQL sequentially, probes/workspace_risk_concurrency.py:697-718; races in test_compose_boundary.py:2425-2532 exercise strategy-settings lock). Fix: compute and audit breach under the risk lock at publish; Compose barrier E2E publish→reserve and reserve→publish through the admin API.
I6. Idempotent replay can return approved for a dead reservation — workspace_risk.py:253-270 returns approved=True for RELEASED/TRANSFERRED/expired; legacy path rejected these (risk_reservations.py:232-240). Fix: replay only when RESERVED/UNKNOWN and unexpired; otherwise conflict.

## Minor (fix the cheap ones)
- Cap/remaining arithmetic uses ambient Decimal context (workspace_budget.py:549-552, 599-601) → explicit local context, floor caps / ceiling exposure; SQL ::numeric(38,18) sum casts round half-up.
- SQL equity does not flag negative per-account equity as unknown (e4f6…py:157-189) vs domain equity.py:327.
- Old get_workspace_risk_aggregate is dead but still granted; both functions grant EXECUTE to crypidian_admin_web which never calls them (c8f1…py:111-112, e4f6…py:209-211) → drop/revoke.
- get_decision_by_idempotency_key scans every decision in Python (risk_reservations.py:604-619) → index/column.
- WorkspaceBudgetEngine.evaluate(workspace_aggregate=None) silently local-only (workspace_budget.py:335, 544) → make aggregate required.
- test_reread_after_lock_detects_new_cap mutates settings before reserve (test_workspace_risk_coordinator.py:394) → ordering assertion against lock statements.
- Barrier E2E checks DB sum via the function under test (probes/workspace_risk_concurrency.py:468-474) → also query risk_reservation directly.

## Already ruled elsewhere (not part of this fix unless trivially adjacent)
- Coordinator reserve does not re-check single-account hard caps (notional/concentration/max positions/margin) under L3 — MB-05b requirement (WBS MB-05b ruling).

## Finding còn mở sau re-review của `b5efea4`
- **Important 1 (I2/R3):** tolerance = độ dài nến + `max_mark_age` lấy từ dữ liệu (`close_time − open_time`); `timeframe` không ràng buộc; LATERAL mark (migration `f7a9c1e3b5d8` ~:150-157) bỏ qua timeframe/source → nến 1d/1w hoặc nến lỗi `open_time` sớm được coi là mới. Cần allowlist timeframe/source, trần policy cho phần bar length, cùng một quy tắc SQL/Python.
- **Important 2 (latent):** `_load_published`/`_load_authoritative_caps` đọc tại `read_at = max(as_of, now)` → version root/overlay lên lịch tương lai (có thể lỏng hơn) có thể được dùng; repo cho phép `effective_at` tường minh. Cần đọc cap tại `now` sau khóa.
- Minor 3 (đồng bộ đồng hồ admin/runtime — dùng đồng hồ DB), Minor 4 (khôi phục guard không cho production AsyncSession nhận loader inject).
- Deferred: breach phạm vi bot khi publish; trường overlay rủi ro khác có thể làm overlay không resolve sau khi siết root; UNKNOWN reservation chặn cả workspace; index biểu thức cho `idempotency_key`; MB-05b: re-check trần account (notional/concentration/positions/margin) dưới L3 hoặc nạp context trong transaction L1 + E2E.

## Nguyên nhân gốc chung
Test MB-04 ban đầu dùng fake (`test_scope_caps_loader`, lineage trade viết tay, cap inject) nên không đi qua `_load_authoritative_caps`, ledger thật và thứ tự publish/clock thật. Mọi sửa phải có test RED trên đường thật.
