# MB-04 WBS — Equity point-in-time và ngân sách rủi ro Multi-Bot

**Trạng thái:** MB-04 hoàn tất trên nhánh `dev` sau full unit/E2E/quality gates ngày 2026-10-26. Nguồn cap/scope authoritative được nạp sau khóa; PAPER runtime đã nối workspace risk gate; migration equity point-in-time và DB security được xác minh. Đã tự review DB/code/auth/bảo mật/cơ chế; không có báo cáo review độc lập trong vòng này.
**Ngày cập nhật:** 2026-10-26
**Gói cha:** [WBS Multi-Bot](./2026-10-24-multibot-implementation-plan.md), Task 5.
**Contract:** [Hợp đồng kiến trúc Multi-Bot](../../architecture/decisions/2026-10-24-multibot-contracts.md), mục 5–6.
**Phụ thuộc:** MB-01, MB-02 và MB-03 phải được tích hợp, review và xác minh trên cùng migration head trước khi bắt đầu code MB-04.
**Liên kết liên quan:** [MB-02 WBS](./2026-10-24-mb02-bot-config-wbs.md), [MB-03 WBS](./2026-10-24-mb03-shared-evidence-wbs.md), [glossary EN→VI](../../research/glossary-vi.md).

## Mục tiêu

Tính vốn account theo dữ liệu đúng thời điểm, quy đổi an toàn về USDT, tính giới hạn rủi ro cho từng bot/account và toàn workspace, rồi kiểm tra/giữ reservation trong cùng transaction với một thứ tự khóa nhất quán. Một reservation mới không được làm tổng rủi ro vượt bất kỳ giới hạn nào đang có hiệu lực. Khi thiếu, sai lệch, quá cũ hoặc không thể xác nhận equity/mark/tỷ giá/exposure thì từ chối entry mới theo fail-closed.

MB-04 chỉ dùng PAPER và adapter giả lập. Dữ liệu equity private của Binance, account activity thật, đặt lệnh, pause/lease runtime và bộ điều phối đầy đủ thuộc WP-03/MB-05. Không bật LIVE/DEMO.

## Phạm vi

- Snapshot vốn account có nguồn, `as_of`, thời điểm khả dụng, phiên bản và hash; đánh dấu rõ `known/unknown`.
- Mark-to-market vị thế PAPER từ ledger account/bot và market mark đã được chấp nhận theo chính sách root.
- Observation quy đổi tài sản sang USDT, ưu tiên cặp trực tiếp được policy cho phép; không giả định 1 USDT cho tài sản khác.
- Root workspace cap; account-level cap; bot overlay chỉ siết cap account.
- Tính tổng worst-case stop-risk của vị thế mở và reservation còn hiệu lực trên toàn workspace.
- Tính quyết định và reservation có lineage root config, bot config, account risk profile, equity/mark/conversion, candidate/decision, bot/account/workspace.
- Tuần tự hóa giao dịch reservation và publish root risk theo workspace → account; duy trì idempotency và trạng thái breach khi cap bị siết dưới exposure.
- API root settings hiện có chỉ được mở rộng qua cùng version/audit/MFA/RBAC flow; không tạo settings store độc lập.

## Ngoài phạm vi

- Không thay đổi strategy, tín hiệu, shared evidence, trainer, tự học hoặc promotion AI.
- Không tự thay root cap, account cap, bot config hoặc risk profile dựa trên kết quả AI.
- Không tự đóng vị thế/flatten khi cap bị hạ hoặc dữ liệu bị stale; vẫn giữ protective monitoring đang có.
- Không tích hợp private account/equity endpoints, Binance mark price riêng, lệnh Demo/Live hoặc foreign activity reconciliation.
- Không tính hiệu quả lợi nhuận từ equity snapshot này; snapshot chỉ phục vụ quyết định và giới hạn rủi ro.

## Rulings ràng buộc implementation

1. **Công thức vốn PAPER:** `equity(as_of) = seed_capital_before_realized + cumulative_net_realized_pnl + Σ unrealized_pnl_at_valid_marks`. Không dùng `realized_equity` đã cộng PnL làm vốn gốc rồi cộng realized lần nữa.
2. **Công thức vốn venue tương lai:** `venue_total_equity_including_realized_and_excluding_unrealized + Σ unrealized_pnl_at_valid_marks`. Chưa triển khai trong MB-04; adapter WP-03 phải xác minh semantics từ tài liệu Binance trước khi dùng.
3. **Mark vị thế:** mark phải cùng hoặc trước `as_of`, không quá tuổi freshness trong root settings, cùng symbol/quote contract, finite và dương. Nếu còn vị thế mở mà thiếu mark phù hợp, snapshot là `unknown` và không có entry mới.
4. **Quy đổi:** chỉ identity khi asset đã là USDT. Với tài sản khác, dùng observation có nguồn/route policy được duyệt, `available_at <= as_of`, không ở tương lai và còn hạn. Không fallback tỉ giá 1.0, không ghép đường quy đổi không được policy cho phép.
5. **Mẫu số workspace:** tổng equity USDT của các account gắn với bot enabled tại cùng `as_of`, theo contract MB-00. Mẫu số không usable nếu bất kỳ account nào bắt buộc phải tính mà snapshot/FX unknown. Rủi ro mở và reservation đang giữ của mọi bot trong workspace vẫn được tính, kể cả bot/account bị disable sau khi mở vị thế.
6. **Giới hạn riêng:** account cap là một trường mới, ngữ nghĩa tách biệt rõ với `max_risk_fraction` hiện hữu (giới hạn rủi ro mỗi giao dịch); không tái sử dụng tên/cột cũ với nghĩa mới. Bot overlay có thể giảm `bot_max_open_risk_fraction`, không thể tăng account hoặc root cap.
7. **Tổng rủi ro:** `open_risk = Σ worst_case_loss_at_protective_stop_for_open_positions + Σ RESERVED_or_UNKNOWN_reservation_risk`. Unknown order/reservation tiếp tục giữ ngân sách đến khi reconcile rõ ràng; không double-count khi reservation chuyển thành open exposure.
8. **Giới hạn:** `workspace_cap = root_workspace_fraction × workspace_equity_usdt`; `account_cap = account_max_open_risk_fraction × account_equity_usdt`; `bot_cap = min(account_cap, bot_overlay_fraction × account_equity_usdt)` khi overlay có mặt. Mỗi entry phải qua cả ba scope và giới hạn notional/margin/per-trade đang có.
9. **RLS cross-bot:** runtime mặc định chỉ đọc bot scope của mình. Nếu cần dữ liệu tổng workspace, triển khai một giao diện aggregate tối thiểu, không trả danh sách account/position/bot khác. Không nới policy permissive workspace-wide cho runtime. Cách triển khai DB (SQL function/projection có quyền tối thiểu hoặc cơ chế tương đương) phải pin `search_path`, giới hạn role/context, có kiểm tra `has_function_privilege`, và E2E bằng role thật; không cấp `BYPASSRLS` cho process runtime.
10. **Khóa:** mọi thao tác thay đổi open-risk/reservation và publish thay đổi workspace risk cap lấy advisory transaction lock theo thứ tự workspace trước account; multi-account khóa account UUID tăng dần. Đọc snapshot trước lock không được là căn cứ cuối để chấp thuận reservation; sau lock phải đọc lại policy, exposure/reservation và revision có hiệu lực.
11. **Cap siết dưới exposure:** cho publish root cap theo flow đã có; biểu thị `CAPACITY_BREACH_AFTER_TIGHTENING`, chặn mọi entry mới ở scope liên quan, giữ protective monitoring/exits và không tự flatten. Chỉ mở lại entry khi dữ liệu mới chứng minh exposure xuống dưới mọi cap.
12. **Định danh:** candidate/decision/reservation phải idempotent theo `(workspace_id, bot_id, account_id, operation, key)`; payload khác với cùng key là conflict. `bot_id` và root settings version ID phải được giữ riêng; config bot không thay thế root version ID trong risk/PAPER ledger lineage.
13. **Tương thích dữ liệu cũ:** migration thêm trường nullable/default hợp lệ, backfill từ account hiện tại với snapshot `unknown` khi không có bằng chứng đầy đủ; không bịa mark, FX, unrealized PnL hoặc lịch sử equity. Entry bị chặn cho tới khi có snapshot dùng được.

## Data contract đề xuất

### `AccountEquitySnapshot`

Immutable; tối thiểu gồm `workspace_id`, `bot_id`, `account_id`, `asset`, `equity_amount`, `equity_basis`, `seed_capital`, `cumulative_realized_pnl`, `unrealized_pnl`, `captured_at`, `as_of`, `available_at`, `source`, `source_version`, `source_hash`, `mark_refs`, `known`, `unknown_reason`, `schema_version`.

### `RiskConversionObservation`

Immutable; tối thiểu gồm `workspace_id`, `base_asset`, `quote_asset=USDT`, `rate`, `as_of`, `available_at`, `source_ref`, `source_version`, `source_hash`, `quality`, `policy_version_id`, `known`, `unknown_reason`.

### `WorkspaceRiskDecision`

Immutable decision payload ghi root settings version/hash, bot config version/hash, account risk profile version, equity snapshot IDs, mark refs, conversion IDs, `as_of`, `workspace_equity_usdt`, per-scope cap/open/pending/requested risk, result/reason, candidate and decision IDs. Mọi tiền/tỉ lệ dùng `Decimal`, thời gian timezone-aware UTC.

### Persisted state

Giữ `risk_decision`/`risk_reservation` làm nguồn ledger đang có; chỉ thêm columns/FK/version/hash cần thiết. Nếu cần bảng snapshot mới, bảng phải có workspace/bot/account scope, unique id/hash, immutability, retention rule được ghi rõ và RLS/grants theo pattern MB-01. Không thêm bảng cache mutable làm nguồn chân lý thứ hai.

## WBS và điều kiện nghiệm thu

### MB04.1 — RED unit contracts cho equity, mark và chuyển đổi

**Tiến độ 2026-10-25:** thêm domain thuần tại `domain/risk/equity.py` và 55 focused unit tests; full unit 848 passed. Hợp đồng giữ UTC/Decimal/frozen lineage và typed reason code. FX DIRECT được duyệt làm tròn xuống 1e-8 USDT; USDT identity giữ nguyên. Full Compose E2E cuối đạt 18 passed / 895.46s sau khi Docker Desktop inspect 500 ở lượt đầu; Ruff/mypy/migration/config và teardown đạt. Chưa nghiệm thu toàn MB-04. Config/repository/budget/reservation chưa nối runtime.

- [x] Viết unit tests trước source cho PAPER equity đúng công thức, không double-count realized; nhiều fills/vị thế long-short; phí đã chốt; mark favorable/adverse; thứ tự mark và `as_of`.
- [x] Kiểm tra `Decimal` chính xác, không nhận float/NaN/Infinity/giá âm, currency mismatch, UTC-naive, thời gian tương lai, mark stale, missing mark, position scope sai, snapshot unknown.
- [x] Kiểm tra USDT identity; non-USDT direct/approved route; stale/missing/future/rejected FX; rounding theo Decimal policy; conversion có provenance/hash.
- [x] Kiểm tra venue basis validator không cộng realized lần hai, nhưng không nối venue adapter trong WP này.

**Nghiệm thu:** RED chứng minh đúng các contract và fail-closed; các trường hợp boundary đều assert reason code cụ thể, không chỉ assert boolean.

### MB04.2 — Unit contracts cho budget/reservation và breach

**Tiến độ 2026-10-25:** thêm module domain `domain/risk/workspace_budget.py` và 35 focused unit tests `tests/unit/test_workspace_risk.py`; full unit đạt 883 passed. Đảm bảo tính toán trần 3 cấp (workspace/account/bot), tổng hợp rủi ro mở + reservation không double-count, loại trừ vốn account disabled nhưng vẫn tính exposure của nó, fail-closed mẫu số không usable, tự động tạo trạng thái `CAPACITY_BREACH_AFTER_TIGHTENING` không tự flatten vị thế, bảo vệ các hành động protective exits/monitoring, và bảo toàn tính idempotent theo `(workspace_id, bot_id, account_id, operation, key)`. Ruff check/format và mypy đạt 100%.

- [x] Kiểm tra workspace/account/bot cap từng scope; giới hạn nhỏ nhất được áp dụng; boundary chính xác bằng cap được phép, vượt cap bị từ chối.
- [x] Kiểm tra open worst-case risk + pending `RESERVED`/`UNKNOWN`; `RELEASED` không còn giữ; transfer sang open-risk không double-count.
- [x] Kiểm tra disabled account/bot theo mẫu số contract và exposure đang mở vẫn được tính.
- [x] Kiểm tra stale/unknown/future/mismatched equity hoặc quote làm `DENY_NEW_ENTRY`; protective-management decision không bị xóa/đổi.
- [x] Kiểm tra cap mới dưới exposure tạo breach có nguyên nhân, chặn entry, phục hồi entry chỉ khi snapshot mới chứng minh dưới cap; không tự flatten.
- [x] Kiểm tra cùng idempotency key/payload cho kết quả cũ, payload khác conflict, key có bot scope khác nhau không xung đột.

**Nghiệm thu:** domain budget thuần/deterministic; không đọc đồng hồ global, DB, API sàn hoặc cấu hình ngầm.

### MB04.3 — Config root/account/bot và migration

- [x] Thêm `workspace_max_open_risk_fraction`, `max_equity_age`, `max_conversion_age`, mark freshness/policy cần thiết vào nested root settings hiện hành; giữ schema/versioning/canonical hash/`extra=forbid`.
- [x] Thêm account `max_open_risk_fraction` độc lập trên risk-profile version; cập nhật create/validate/read API hiện có, migration và tests; giữ `max_risk_fraction` hiện tại cho ngữ nghĩa per-trade.
- [x] Thêm optional `bot_max_open_risk_fraction` vào overlay, canonical serialization và effective resolver; validate `0 < bot <= account <= root`, reject thay đổi ngoài allowlist. **Đính chính 2026-10-26 (review I4, ruling R4 ở MB04.10):** chỉ `bot <= account` và `bot <= root` là bắt buộc (khi validate/publish overlay); cap account (phần vốn account) và cap root (phần vốn workspace) khác mẫu số nên không so trực tiếp; overlay đã publish được kẹp `min(bot, account, root)` khi resolve runtime thay vì mất resolve.
- [x] Khi publish root change tác động risk, lấy workspace advisory lock trước lúc đọc exposure/reservation/equity; giữ audit/MFA/revision/parent checks hiện có.
- [x] Migration head sau nhánh integration đã được chốt; downgrade bảo toàn dữ liệu, từ chối khi snapshot/decision mới không thể tái dựng; không drop dữ liệu ngầm.
- [x] Kiểm JSON/version cũ: defaulting chỉ cho trường settings mới có giá trị an toàn đã được chốt; không hồi sinh/bịa snapshot historical.

**Nghiệm thu:** API/config model từ chối float, cap nới, giá trị ngoài [0,1], stale-policy vô hiệu; version cũ đọc ổn định; migration upgrade/downgrade/upgrade giữ immutable history và chỉ một Alembic head.

### MB04.4 — Equity/conversion repository và root aggregate access

- [x] Dựng PAPER account snapshot từ seed + ledger realized + position state/fills + market mark có `as_of` phù hợp; đối chiếu gross/net, fees và short position sign.
- [x] Lưu mark/FX provenance cùng snapshot/version/hash; không chấp nhận một symbol mark thuộc workspace/account khác.
- [x] Thực hiện tổng hợp workspace equity/open-risk/reservation qua giao diện aggregate tối thiểu như ruling 9; không đưa row-level data bot khác vào process runtime.
- [x] Kiểm database owner, `SECURITY DEFINER`, `search_path`, `row_security`, function execute grants và revocation. Nếu không thể đảm bảo role/context an toàn bằng real-role E2E, fail implementation và dừng để thiết kế lại; không cấp `BYPASSRLS`.
- [x] Admin root validation chỉ dùng workspace-wide roles như MB-01; member operator không được xem aggregate vượt grant.

**Nghiệm thu:** runtime lấy đúng equity bản thân + workspace aggregate tối thiểu; query khác bot bị từ chối; aggregate không trả identity/position/symbol của bot khác; stale/missing input biểu diễn unknown chứ không thành zero.

### MB04.5 — Atomic workspace/account/bot reservation

- [x] Cập nhật risk evaluation để dùng normalized equity/caps, lưu lineage root+bot+account+snapshot/FX refs.
- [x] Reservation acquire workspace lock rồi account lock; sau lock re-read root cap, account profile/status, bot overlay/status, tất cả scope workspace, open worst-case risk, active reservations và equity revision từ nguồn authoritative. **Đóng 2026-10-26 (sửa review C1–C3, xem MB04.10):** cap bot lấy `bot_max_open_risk_fraction` của overlay đã resolve; rủi ro trade OPEN lấy `reserved_risk` của reservation liên kết (thiếu → unknown); mọi căn cứ đọc tại `max(as_of, clock())` lấy sau khóa; bằng chứng qua đường thật `tests/e2e/test_workspace_risk_real_paths.py`.
- [x] Kiểm idempotent replay trước/sau hết hạn; reservation UNKNOWN giữ budget; release/expire/reconcile/transfer phải dùng cùng lock order và không double-count.
- [x] Xử lý root publish race và cap breach theo contract; serialization/audit/outbox trong UoW hiện hành; repository không commit transaction caller.
- [x] Kiểm transaction rollback không để snapshot/budget/decision orphan hoặc runtime state đã publish trước commit.

**Nghiệm thu:** không có đường đua tạo reservation vượt tổng cap; decision/reserve dùng cùng point-in-time lineage đã kiểm; lỗi downstream rollback nguyên tử. 13 unit tests đạt 100% tại `tests/unit/test_workspace_risk_coordinator.py`.

**Gap nghiệm thu lịch sử, đã khép ngày 2026-10-26:** chữ ký `reserve` từng nhận cap/scope từ caller và runtime chưa gọi coordinator. MB04.8–MB04.9 đã chuyển sang policy authoritative sau khóa và nối PAPER runtime vào gate; unit/Compose coverage xác nhận.

### MB04.6 — Compose E2E và quality gates

**Bằng chứng trước đó (2026-10-25):** Bốn scenario Compose đã từng chạy đạt trên PostgreSQL cô lập. Lượt kiểm tra tổng 2026-10-26 tìm thấy hai lỗi regression và một assertion cũ không tương thích MB-04; hai scenario lỗi đã pass sau sửa. Kết quả full suite cuối cùng được ghi ở implementation memory; không dùng số test lịch sử làm kết quả hiện tại.
- Lượt E2E lịch sử trước code freeze: 18 passed, 1 timeout; không tính là nghiệm thu. Kết quả snapshot cuối ngày 2026-10-26: full `tests/e2e` **20 passed / 1007.23s**, không timeout.
- Kịch bản 1: Concurrency barrier kiểm tra Headroom Trap với 2 bot chạy đồng thời xin 2,000 USDT (vượt root cap 3,000 USDT). Đúng 1 bot APPROVED, 1 bot DENY (`WORKSPACE_CAP_EXCEEDED`), DB `sum(reserved_risk) == 2000 <= 3000`.
- Kịch bản 2: Admin Cap Tightening Race & Breach: Bot 1 có vị thế mở stop risk 1,200 USDT. Admin siết Root cap xuống 5% (1,000 USDT). Bot 2 xin 1,000 USDT bị từ chối với `CAPACITY_BREACH_AFTER_TIGHTENING` (1,200 > 1,000). Vị thế Bot 1 giữ nguyên `OPEN` (không flatten).
- Kịch bản 3: DB Roles & Cross-Bot RLS Isolation: Chạy dưới role `crypidian_trading_runtime` (không có `BYPASSRLS`), kiểm tra quyền bảng, RLS chặn SELECT cross-bot (0 rows), và hàm `get_workspace_risk_aggregate(ws_id, as_of)` trả về scalar aggregate an toàn mà không rò rỉ bot_id/symbol.
- Kịch bản 4: Migration Rehearsal: Upgrade head -> chèn custom profile -> downgrade bị chặn bởi `MB04_DOWNGRADE_GUARD_CUSTOM_ACCOUNT_CAPS_EXIST` -> cleanup test data -> downgrade thành công -> upgrade head và `alembic check` sạch.
Quality Gates (bằng chứng lịch sử 2026-10-25): Full 957 unit tests passed, Compose E2E `test_workspace_risk_concurrency_e2e.py` passed (71.75s), Alembic single head (`c8f1e3a5b7d9`), Ruff check/format clean, mypy clean trên 116 files. Không thay thế kết quả kiểm tra 2026-10-26.

- [x] E2E PostgreSQL/Compose tạo ít nhất hai bot và hai PAPER accounts trên cùng workspace; từng account riêng có headroom nhưng cùng nhau vượt workspace cap.
- [x] Dùng concurrency barrier thật: reservation/reservation, cùng account, workspace cap publish→reserve và reserve→publish; xác nhận kết quả bằng DB sau commit. **Đính chính 2026-10-26:** dấu tick trước đó sai — kịch bản siết cap chèn `workspace_settings_version` bằng SQL tuần tự và các race trong `test_compose_boundary.py` chỉ dùng khóa strategy-settings (review I5). Mục này nay mới đạt bằng `mb04_publish_race` trong `tests/e2e/test_workspace_risk_real_paths.py`: `SettingsAdminService.publish_draft` thật (role `crypidian_admin_web`) đua với `evaluate_and_submit` thật (role `crypidian_trading_runtime`), bên giữ khóa dừng giữa transaction tới khi `pg_locks` xác nhận bên kia đang chờ; kiểm cả hai thứ tự, tổng `risk_reservation` đọc thẳng bảng và audit breach.
- [x] Test non-USDT, thiếu/stale/future FX; vị thế có mark stale/missing; venue snapshot không cộng realized hai lần; cap hạ dưới exposure; UNKNOWN reservation và reconcile.
- [x] Chạy bằng DB roles `crypidian_trading_runtime` và `crypidian_admin_web`, verify `has_table_privilege`/`has_function_privilege`, RLS cross-bot và không có `BYPASSRLS`.
- [x] Migration rehearsal trên dữ liệu seeded thật từ head integration: counts, PnL, fee, payload/hash, checkpoint; upgrade→downgrade guard→upgrade; giữ nguyên ảnh Docker dùng chung và teardown theo Compose project label.
- [x] Full unit/quality gates và full E2E một lượt không timeout/failure: unit **1103 passed**; E2E **20 passed / 1007.23s**; Ruff check, format (339 files), mypy (127 source files), Compose config, `git diff --check` và Alembic single head đạt; migration E2E chạy upgrade/downgrade/upgrade + `alembic check`.
- [x] Review độc lập theo spec + quality; mọi finding P0/P1/P2 được sửa test-first và chạy lại suite liên quan. **Đóng 2026-10-26:** review độc lập (dev @ b85c21b) tìm 3 Critical + 6 Important; tất cả và các minor rẻ đã sửa test-first qua đường thật (MB04.10), RED/GREEN, full unit + full E2E chạy lại; chi tiết ở `.superpowers/sdd/2026-10-24-multibot-implementation-plan/mb04-fix-report.md`.
- [x] Cập nhật/reread implementation memory, trạng thái project, MB-04 WBS/main plan và ledger trước commit `dev`; không push trừ khi user yêu cầu.

## WBS bổ sung — Hoàn thiện các gap MB-04 (2026-10-26)

Phần này làm rõ nghiệm thu còn thiếu đã được phát hiện trong vòng review; nằm trong phạm vi MB-04 đã duyệt, vẫn chỉ áp dụng PAPER.

### MB04.7 — Workspace equity aggregate an toàn qua DB

- Tính scalar `total_equity_usdt` cho toàn bộ bot/account PAPER đang ENABLED tại `as_of`: seed + net realized PnL của trade đã đóng + PnL từ exit fill một phần + UPNL phần còn mở theo weighted entry/exit fills và mark khả dụng mới nhất, trừ fee/slippage/funding đã ghi đến `as_of`.
- Hàm trả `has_unknown_equity` nếu thiếu seed, fill/entry, mark; mark tương lai/quá cũ; giá trị âm/non-finite; account quote asset không có direct conversion đã duyệt; hoặc active account count không khớp identity bot/account.
- Mở rộng `get_workspace_risk_aggregate` mà không trả bot/account ID, symbol, balance từng account hay row-level data. Thực thi dưới role chủ sở hữu hàm, khóa `search_path`, kiểm tra `app.workspace_id`, revoke PUBLIC và grant execute đúng runtime/admin.
- Bổ sung E2E bằng `crypidian_trading_runtime` chứng minh tổng equity khớp seed + realized + long/short UPNL, đúng `as_of`, unknown/freshness fail-closed, RLS không rò rỉ và `BYPASSRLS` vẫn false; migration downgrade/upgrade và `alembic check` đạt.

**Nghiệm thu:** aggregate workspace equity được tính độc lập với dữ liệu bot hiện tại, không double-count realized, fail-closed khi một active account chưa định giá được, và chỉ trả scalar tối thiểu.

**Ghi nhận nghiệm thu 2026-10-26:** migration `e4f6a8c0d2b5` nối head `d9b2f4a6c8e1`; scalar equity tính seed + realized + PnL exit một phần + UPNL còn mở − phí/slippage/funding. Fee/slippage từ fill phải khớp `trade_cost`; lệch sổ hoặc quote/cost không hỗ trợ thì unknown. Seed chỉ được dùng nếu `created_at <= as_of`. Compose E2E bao phủ long/short, partial exit/costs, realized, thiếu/future seed, thiếu fill, mark tương lai, quote khác USDT, RLS/grants/BYPASSRLS và migration rehearsal; full suite **20/20** đạt. `alembic check` chạy trong migrator Compose vì host Windows dùng ProactorEventLoop không tương thích async Psycopg.

### MB04.8 — Nạp policy/config và scope authoritative sau khóa

- `WorkspaceRiskCoordinator.reserve` chỉ nhận request plan; bỏ cap và danh sách scope do caller cung cấp.
- Sau khi lấy workspace lock rồi account lock, nạp root settings có hiệu lực và risk profile theo workspace/version; xác minh bot + PAPER account mục tiêu tồn tại, ENABLED và ánh xạ 1:1; resolve bot overlay đã publish từ DB để lấy cap và lineage.
- Chỉ lấy equity/conversion/positions theo bot mục tiêu bằng repository dưới RLS; lấy workspace equity/open/pending/count/unknown qua aggregate scalar. Không đọc rows của bot khác và không chấp nhận snapshot/account scope truyền vào từ caller.
- Kiểm root/account/bot cap, status, profile ID, config version/hash, max age và aggregate scope đồng nhất; thiếu hoặc sai thì deny entry trước persistence reservation.
- Unit tests cho disabled/missing target, foreign bot/account, profile thiếu/sai workspace, overlay draft/không hiệu lực/nới cap, aggregate missing/stale/unknown, active count mismatch và request không thể override cap.

**Nghiệm thu:** toàn bộ căn cứ của quyết định được đọc lại từ nguồn authoritative sau lock; test chứng minh một request giả mạo không thể tăng cap hoặc thêm equity cho workspace.

**Trạng thái 2026-10-26: HOÀN TẤT.** Sau workspace→account locks, `reserve(request)` nạp effective root theo `as_of`, risk profile thuộc workspace, bot overlay đã publish, bot/account mapping/status từ DB; không nhận cap hoặc workspace scope từ caller. Chỉ test fake có thể inject loader riêng; constructor từ chối loader này với `AsyncSession` production. **Đính chính 2026-10-26 (review C1):** chính loader giả này (qua thuộc tính session và nhánh "không phải AsyncSession") đã làm `_load_authoritative_caps` không bao giờ chạy trong test nên che lỗi cap bot; đã gỡ khỏi code production — test chỉ thay qua tham số constructor `caps_loader`/`target_status_loader` tường minh, còn đường thật được kiểm bằng E2E. DTO lưu bot config version/hash cùng root/profile/snapshot/conversion lineage. Unit kiểm tra disabled target, cấu hình theo `as_of`, API không nhận cap và thứ tự khóa bot publish; bot overlay publish/rollback lấy workspace risk lock trước bot lock để serialize với reserve. Full unit/E2E/quality gates đạt.

### MB04.9 — Runtime wiring và khóa vòng đời reservation

- PAPER `evaluate_and_submit` gọi coordinator sau RiskEngine gate, trước mọi gateway submit; quyết định workspace deny được ghi nhận và không gọi gateway.
- Reservation/decision giữ root settings, profile, bot config, snapshot/mark/conversion IDs, request hash và candidate/plan lineage; cùng transaction với intent/outbox.
- Fill, no-fill release, UNKNOWN, reconcile, transfer-to-open và expiry đều lấy cùng workspace risk lock trước account lock; không để đường legacy mutate reservation ngoài thứ tự khóa.
- E2E chạy runtime thật qua request → gate → reservation → PAPER submit, deny do workspace cap không gửi order, cross-bot concurrent request không vượt root cap, và verify commit/rollback/idempotency bằng DB.
- Review thủ công code/SQL/auth/RLS/strategy risk interactions; cập nhật glossary nếu thêm thuật ngữ; full unit/E2E/quality, memory read-back và push `dev`.

**Nghiệm thu:** không có đường PAPER entry nào bỏ qua workspace risk gate; tất cả mutation risk share cùng lock order và reservation lifecycle không double-count.

**Ghi nhận nghiệm thu 2026-10-26:** runtime gọi workspace coordinator trước PAPER submit; workspace deny trả `RISK_REJECTED` và unit test khẳng định gateway call count bằng 0. Workspace decision lưu candidate expiry; kết quả runtime giữ riêng single-account decision ID làm lineage. Mutation lifecycle và expiry batch dùng workspace advisory lock trước account locks. Compose E2E chạy PAPER runtime thật: admin publish root cap rất thấp qua API, runtime đánh giá lại và nhận `CAPACITY_BREACH_AFTER_TIGHTENING`; không tạo order/reservation, gateway snapshot không đổi, decision DENY được lưu. Cùng full E2E suite còn kiểm tra coordinator từ chối khi siết cap và hai bot cạnh tranh không vượt cap.

### MB04.10 — Sửa review độc lập (2026-10-26)

Review độc lập đọc tĩnh dev @ `b85c21b` (`.superpowers/sdd/2026-10-24-multibot-implementation-plan/mb04-review-findings.md`). Mọi bài test sửa lỗi đi qua **đường thật** (resolver/overlay publish thật, trade do ledger thật tạo, đồng hồ/thứ tự publish thật, role DB thật); RED được ghi trên code nền `274793e` trước khi sửa.

- [x] **C1** cap bot dùng `bot_max_open_risk_fraction` của overlay đã resolve (`caps_from_resolved`), không phải cap account. E2E: overlay 0.005 publish qua `BotConfigAdminService` → entry ~100 USDT bị `BOT_CAP_EXCEEDED` (RED: APPROVED).
- [x] **C2** rủi ro trade OPEN = `reserved_risk` của reservation liên kết (`trade.risk_reservation_id`), giống SQL; thiếu/không hợp lệ → `WORKSPACE_EXPOSURE_UNKNOWN`. E2E: entry → fill → trade OPEN (ledger thật), entry thứ hai vượt cap account 150 → `ACCOUNT_CAP_EXCEEDED` (RED: APPROVED).
- [x] **C3** mốc đọc `read_at = max(as_of, clock())` lấy **sau** khóa; candidate hết hạn theo clock bị từ chối; exposure gồm mọi trade OPEN (bỏ lọc `opened_at`); RESERVED giữ budget nếu `expires_at > least(as_of, now())`. E2E: evaluated_at trước publish siết → `WORKSPACE_CAP_EXCEEDED`; trade bot khác mở sau as_of vẫn tính; SQL as_of tương lai vẫn giữ reservation còn hạn (RED cả ba).
- [x] **I1** role `crypidian_risk_aggregator` (NOLOGIN/NOSUPERUSER/NOBYPASSRLS/NOINHERIT, không thành viên, tạo có guard `MB04_RISK_AGGREGATOR_ROLE_UNSAFE`) sở hữu hàm `SECURITY DEFINER`; chỉ `SELECT` 8 bảng cần đọc + policy RLS `FOR SELECT` theo `app.workspace_id`; `search_path` ghim; REVOKE PUBLIC; aggregate definer chỉ cho runtime; hàm cũ `get_workspace_risk_aggregate` bị xóa. E2E kiểm `proowner`, thuộc tính role, ma trận EXECUTE, ACL hàm qua downgrade (least-privilege) và role tạo sẵn không an toàn bị từ chối.
- [x] **I2/I3** một phép tính SQL dùng chung `workspace_account_risk_state` (`SECURITY INVOKER`, dưới RLS của role gọi) cho vốn account mục tiêu, aggregate và đánh giá breach khi publish; mark = nến đóng mới nhất, độ tươi theo `close_time`: `as_of − close_time ≤ (close_time − open_time) + max_mark_age`; PnL vị thế mở không chia (exit notional − entry notional ± mark × phần còn lại − chi phí).
- [x] **I4** bỏ so sánh account ≤ root trong resolver (ruling R4); overlay vẫn bị từ chối khi publish nếu lỏng hơn account/root; resolve runtime kẹp cap bot.
- [x] **I5** publish/rollback root đổi risk: dưới L1 → L2 đang giữ, đọc exposure và ghi `risk.capacity_breach_after_tightening` (audit + outbox, response `capacity`) hoặc `risk.capacity_assessed`; publish vẫn hiệu lực, không auto-flatten. Barrier E2E hai thứ tự qua admin service + runtime thật.
- [x] **I6** replay idempotent chỉ khi reservation còn RESERVED/UNKNOWN và chưa hết hạn; còn lại `ReservationConflict` (E2E RED: RELEASED/hết hạn vẫn trả approved).
- [x] Minor: context Decimal cục bộ (cap/phần còn lại làm tròn xuống, exposure làm tròn lên), kết quả SQL `numeric` không ép scale; `workspace_aggregate` bắt buộc; vốn âm từng account là unknown; assertion thứ tự đọc-sau-khóa; barrier E2E kiểm thêm tổng `risk_reservation` đọc thẳng bảng; tra decision theo idempotency key lọc trong SQL.
- Migration `f7a9c1e3b5d8` nối `e1f3a5c7b9d2`, một head; downgrade trả lại nguyên văn hàm/grant của `e4f6a8c0d2b5`/`c8f1e3a5b7d9` và xóa role nếu không còn phụ thuộc.

**Ruling ghi nhận (chi tiết trong report):** R1 `read_at = max(as_of, clock())` (không từ chối as_of tương lai vì E2E MB-05 dùng thời gian mô phỏng; mốc giữ reservation dùng clock thật); R2 lỗi chính sách trước khi đánh giá (candidate hết hạn, replay reservation chết) là `ReservationConflict` → runtime `WORKSPACE_RISK_POLICY_UNAVAILABLE`, transaction rollback, như đường reserve legacy; R3 độ tươi mark theo `close_time` cộng một khoảng nến; R4 không so cap account với cap root (khác mẫu số; nếu ép account ≤ root thì cap workspace không bao giờ chạm được khi các account cùng phần trăm — trái kịch bản headroom trap); R5 admin-web không gọi aggregate definer mà cộng hàng `workspace_account_risk_state` dưới RLS của chính nó (owner/risk_admin thấy mọi bot), nên EXECUTE definer bị thu hồi khỏi admin-web; breach khi publish đánh giá scope WORKSPACE và ACCOUNT (scope BOT do coordinator chặn khi reserve).

## Cổng nghiệm thu tổng MB-04

MB-04 hoàn tất trong phạm vi PAPER: snapshot vốn point-in-time; PnL closed/open/partial-exit và chi phí không double-count; unknown/stale equity/FX/mark fail-closed; aggregate risk gồm vị thế + reservation; cap/scope authoritative sau khóa và runtime gate; concurrency serialize; RLS/grants và migration/history được kiểm tra. Full unit **1103 passed**, full E2E **20 passed / 1007.23s**, quality gates pass. Tự review DB/code/auth/security hoàn tất; no-submit được chứng minh bằng unit và Compose runtime scenario. Review độc lập không thực hiện và không được ghi nhận như kết quả đạt. **Cập nhật 2026-10-26:** review độc lập đã có (3 Critical + 6 Important) và đã được sửa ở MB04.10; số liệu suite sau sửa nằm trong report sửa lỗi. MB-04 không bật Binance LIVE execution và không tuyên bố strategy có lợi nhuận.

## Nguồn hiện hành và thuật ngữ EN→VI

- PostgreSQL 18, [CREATE FUNCTION / SECURITY DEFINER](https://www.postgresql.org/docs/current/sql-createfunction.html), [Explicit Locking / Advisory Locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS), [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html), kiểm tra **2026-10-26**. E2E phải xác minh quyền function và RLS thật; policy nhiều permissive có phép OR.
- Sửa review (kiểm lại **2026-10-26**): PostgreSQL 18 [CREATE FUNCTION — Writing SECURITY DEFINER Functions Safely](https://www.postgresql.org/docs/current/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY) (đặt `pg_temp` cuối `search_path`, `REVOKE ALL ... FROM PUBLIC` rồi grant tối thiểu trong cùng transaction); [PL/pgSQL — Variable Substitution `#variable_conflict use_column`](https://www.postgresql.org/docs/current/plpgsql-implementation.html#PLPGSQL-VAR-SUBST); [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) (superuser/BYPASSRLS bỏ qua RLS — lý do không để superuser sở hữu hàm definer); Python [decimal — `localcontext`, `ROUND_FLOOR`/`ROUND_CEILING`](https://docs.python.org/3/library/decimal.html).
- **Mark-to-market equity — vốn định giá theo giá thị trường:** vốn gốc + realized PnL + unrealized PnL theo mark cùng thời điểm; [glossary](../../research/glossary-vi.md).
- **Worst-case stop risk — rủi ro lỗ tối đa theo mức dừng:** mức lỗ giả định nếu giá đi tới stop bảo vệ theo công thức/fee đã xác định; [glossary](../../research/glossary-vi.md).
- **Risk conversion observation — quan sát quy đổi rủi ro:** chuyển vốn, exposure và reservation về đơn vị chung USDT theo observation có thời điểm/nguồn; [glossary](../../research/glossary-vi.md).
- **Fail-closed — từ chối khi thiếu điều kiện an toàn:** thiếu scope/dữ liệu/chứng cứ thì không cấp quyền hoặc mở entry; [glossary](../../research/glossary-vi.md).
