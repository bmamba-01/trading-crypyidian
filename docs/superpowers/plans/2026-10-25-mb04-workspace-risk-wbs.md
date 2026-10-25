# MB-04 WBS — Equity point-in-time và ngân sách rủi ro Multi-Bot

**Trạng thái:** WBS được người dùng duyệt ngày 2026-10-25; MB04.1 domain đã đạt các gate; MB04.2–6 chưa bắt đầu.
**Ngày:** 2026-10-25
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

- [ ] Kiểm tra workspace/account/bot cap từng scope; giới hạn nhỏ nhất được áp dụng; boundary chính xác bằng cap được phép, vượt cap bị từ chối.
- [ ] Kiểm tra open worst-case risk + pending `RESERVED`/`UNKNOWN`; `RELEASED` không còn giữ; transfer sang open-risk không double-count.
- [ ] Kiểm tra disabled account/bot theo mẫu số contract và exposure đang mở vẫn được tính.
- [ ] Kiểm tra stale/unknown/future/mismatched equity hoặc quote làm `DENY_NEW_ENTRY`; protective-management decision không bị xóa/đổi.
- [ ] Kiểm tra cap mới dưới exposure tạo breach có nguyên nhân, chặn entry, phục hồi entry chỉ khi snapshot mới chứng minh dưới cap; không tự flatten.
- [ ] Kiểm tra cùng idempotency key/payload cho kết quả cũ, payload khác conflict, key có bot scope khác nhau không xung đột.

**Nghiệm thu:** domain budget thuần/deterministic; không đọc đồng hồ global, DB, API sàn hoặc cấu hình ngầm.

### MB04.3 — Config root/account/bot và migration

- [ ] Thêm `workspace_max_open_risk_fraction`, `max_equity_age`, `max_conversion_age`, mark freshness/policy cần thiết vào nested root settings hiện hành; giữ schema/versioning/canonical hash/`extra=forbid`.
- [ ] Thêm account `max_open_risk_fraction` độc lập trên risk-profile version; cập nhật create/validate/read API hiện có, migration và tests; giữ `max_risk_fraction` hiện tại cho ngữ nghĩa per-trade.
- [ ] Thêm optional `bot_max_open_risk_fraction` vào overlay, canonical serialization và effective resolver; validate `0 < bot <= account <= root`, reject thay đổi ngoài allowlist.
- [ ] Khi publish root change tác động risk, lấy workspace advisory lock trước lúc đọc exposure/reservation/equity; giữ audit/MFA/revision/parent checks hiện có.
- [ ] Migration head sau nhánh integration đã được chốt; downgrade bảo toàn dữ liệu, từ chối khi snapshot/decision mới không thể tái dựng; không drop dữ liệu ngầm.
- [ ] Kiểm JSON/version cũ: defaulting chỉ cho trường settings mới có giá trị an toàn đã được chốt; không hồi sinh/bịa snapshot historical.

**Nghiệm thu:** API/config model từ chối float, cap nới, giá trị ngoài [0,1], stale-policy vô hiệu; version cũ đọc ổn định; migration upgrade/downgrade/upgrade giữ immutable history và chỉ một Alembic head.

### MB04.4 — Equity/conversion repository và root aggregate access

- [ ] Dựng PAPER account snapshot từ seed + ledger realized + position state/fills + market mark có `as_of` phù hợp; đối chiếu gross/net, fees và short position sign.
- [ ] Lưu mark/FX provenance cùng snapshot/version/hash; không chấp nhận một symbol mark thuộc workspace/account khác.
- [ ] Thực hiện tổng hợp workspace equity/open-risk/reservation qua giao diện aggregate tối thiểu như ruling 9; không đưa row-level data bot khác vào process runtime.
- [ ] Kiểm database owner, `SECURITY DEFINER`, `search_path`, `row_security`, function execute grants và revocation. Nếu không thể đảm bảo role/context an toàn bằng real-role E2E, fail implementation và dừng để thiết kế lại; không cấp `BYPASSRLS`.
- [ ] Admin root validation chỉ dùng workspace-wide roles như MB-01; member operator không được xem aggregate vượt grant.

**Nghiệm thu:** runtime lấy đúng equity bản thân + workspace aggregate tối thiểu; query khác bot bị từ chối; aggregate không trả identity/position/symbol của bot khác; stale/missing input biểu diễn unknown chứ không thành zero.

### MB04.5 — Atomic workspace/account/bot reservation

- [ ] Cập nhật risk evaluation để dùng normalized equity/caps, lưu lineage root+bot+account+snapshot/FX refs.
- [ ] Reservation acquire workspace lock rồi account lock; sau lock re-read root cap có hiệu lực, account cap, bot cap, open worst-case risk, active reservations, exchange/account state và equity revision.
- [ ] Kiểm idempotent replay trước/sau hết hạn; reservation UNKNOWN giữ budget; release/expire/reconcile/transfer phải dùng cùng lock order và không double-count.
- [ ] Xử lý root publish race và cap breach theo contract; serialization/audit/outbox trong UoW hiện hành; repository không commit transaction caller.
- [ ] Kiểm transaction rollback không để snapshot/budget/decision orphan hoặc runtime state đã publish trước commit.

**Nghiệm thu:** không có đường đua tạo reservation vượt tổng cap; decision/reseve dùng cùng point-in-time lineage đã kiểm; lỗi downstream rollback nguyên tử.

### MB04.6 — Compose E2E và quality gates

- [ ] E2E PostgreSQL/Compose tạo ít nhất hai bot và hai PAPER accounts trên cùng workspace; từng account riêng có headroom nhưng cùng nhau vượt workspace cap.
- [ ] Dùng concurrency barrier thật: reservation/reservation, cùng account, workspace cap publish→reserve và reserve→publish; xác nhận kết quả bằng DB sau commit.
- [ ] Test non-USDT, thiếu/stale/future FX; vị thế có mark stale/missing; venue snapshot không cộng realized hai lần; cap hạ dưới exposure; UNKNOWN reservation và reconcile.
- [ ] Chạy bằng DB roles `crypidian_trading_runtime` và `crypidian_admin_web`, verify `has_table_privilege`/`has_function_privilege`, RLS cross-bot và không có `BYPASSRLS`.
- [ ] Migration rehearsal trên dữ liệu seeded thật từ head integration: counts, PnL, fee, payload/hash, checkpoint; upgrade→downgrade guard→upgrade; giữ nguyên ảnh Docker dùng chung và teardown theo Compose project label.
- [ ] Chạy full unit, full E2E tuần tự, Alembic `heads`/`check`, Ruff check/format, mypy, Compose config và `git diff --check`.
- [ ] Review độc lập theo spec + quality; mọi finding P0/P1/P2 được sửa test-first và chạy lại suite liên quan.
- [ ] Cập nhật/reread implementation memory, trạng thái project, MB-04 WBS/main plan và ledger trước commit `dev`; không push trừ khi user yêu cầu.

## Cổng nghiệm thu tổng MB-04

MB-04 hoàn tất khi snapshot vốn là point-in-time và không double-count; mọi equity/FX/mark unknown hoặc stale fail-closed; aggregate risk gồm vị thế + reservation đang giữ và kiểm tra workspace/account/bot cap đồng thời; concurrency E2E chứng minh reservation và root cap publish được serialize theo workspace→account; cap tightening không flatten và không cho mở entry mới; RLS/grants giữ nguyên cách ly bot và workspace; migration/history chính xác; toàn bộ unit/E2E/quality pass; review độc lập không có finding nghiêm trọng chưa xử lý; memory/status/WBS đồng nhất.

## Nguồn hiện hành và thuật ngữ EN→VI

- PostgreSQL 18, [CREATE FUNCTION / SECURITY DEFINER](https://www.postgresql.org/docs/current/sql-createfunction.html), [Explicit Locking / Advisory Locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS), [Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html), kiểm tra **2026-10-25**. E2E phải xác minh quyền function và RLS thật; policy nhiều permissive có phép OR.
- **Mark-to-market equity — vốn định giá theo giá thị trường:** vốn gốc + realized PnL + unrealized PnL theo mark cùng thời điểm; [glossary](../../research/glossary-vi.md).
- **Worst-case stop risk — rủi ro lỗ tối đa theo mức dừng:** mức lỗ giả định nếu giá đi tới stop bảo vệ theo công thức/fee đã xác định; [glossary](../../research/glossary-vi.md).
- **Risk conversion observation — quan sát quy đổi rủi ro:** chuyển vốn, exposure và reservation về đơn vị chung USDT theo observation có thời điểm/nguồn; [glossary](../../research/glossary-vi.md).
- **Fail-closed — từ chối khi thiếu điều kiện an toàn:** thiếu scope/dữ liệu/chứng cứ thì không cấp quyền hoặc mở entry; [glossary](../../research/glossary-vi.md).
