# Hợp đồng kiến trúc Multi-Bot

- **Trạng thái:** MB-00 đề xuất đã hoàn tất; hợp đồng được dùng làm đầu vào MB-01–MB-08.
- **Ngày:** 2026-10-24
- **Spec:** [Brainstorm kiến trúc Multi-Bot](../../superpowers/specs/2026-10-24-multi-bot-architecture-brainstorm.md)
- **WBS:** [Kế hoạch triển khai Multi-Bot](../../superpowers/plans/2026-10-24-multibot-implementation-plan.md)
- **Phạm vi:** đóng băng domain contract, phân ranh root/bot config, scope dữ liệu, serialization reservation, equity/conversion point-in-time và quy tắc rollout. Không tạo schema/runtime Multi-Bot trong MB-00.

## 1. Căn cứ hiện trạng

Đã đối chiếu source/model/migration hiện có ngày 2026-10-24:

- `WorkspaceSettings` là Pydantic model bất biến, `extra="forbid"`; runtime hiện chỉ cho phép PAPER. `PublishedWorkspaceSettings` mang workspace ID, version ID, hash và `effective_at`.
- `TradingAccount` là entity có `workspace_id`; bảng account-scoped PAPER/risk dùng composite key/FK theo workspace và account. Chưa có `bot_id`.
- `RiskReservationRepository.reserve()` kiểm tra idempotency, rồi khóa hàng `TradingAccount` bằng `SELECT ... FOR UPDATE` trước khi tính active totals. Khóa account đơn lẻ không tuần tự hóa ngân sách aggregate workspace.
- Migration nền áp RLS theo `app.workspace_id`; hiện chưa có bot-scoped policy/grant.
- Settings publish/runtime có serialization theo workspace. Kinh nghiệm WP-02 ghi nhận runtime DB role từng bị từ chối `SELECT FOR UPDATE` trên workspace row; advisory transaction lock được dùng ở boundary publish để tránh mở rộng quyền `UPDATE`.
- PAPER hiện chưa có Binance private execution/reconciliation. Account activity contract ở MB-00 là abstraction; adapter thật vẫn thuộc WP-03.

## 2. Định danh và kiểu dữ liệu

- `WorkspaceId`, `BotId`, `AccountId`, `MembershipId`, `EvidenceId`, `DecisionId`, `ReservationId` là alias kiểu của `UUID`. ORM/API serialize thành UUID/string chuẩn; không tạo ID giả hoặc ID phụ thay cho khóa hiện có.
- Mọi thời điểm domain là `datetime` có timezone, chuẩn hóa UTC. Từ chối datetime naïve.
- Tiền, tỷ lệ, giá, PnL, risk và tỷ giá dùng `Decimal`; không chuyển qua `float` ở domain, DB decision hoặc so sánh cap.
- Contract immutable sau khi dựng; JSON/hash serialization chuẩn hóa ổn định và có schema version.

## 3. Root config và bot overlay

`WorkspaceSettings` tiếp tục là nguồn gốc duy nhất cho hard cap/chính sách workspace, allowed venue/mode, market/research policy, AI governance và workspace kill switch. Không tạo một root config thứ hai cho multibot.

`BotConfigOverlay` chỉ chứa allowlist có thể override theo bot:

- strategy release/parameters đã được publish/approve;
- universe/timeframe là tập con của root policy;
- optional bot risk fraction thấp hơn hoặc bằng account/workspace cap;
- tùy chọn thông báo/lifecycle không thay đổi execution semantics.

Overlay không chứa credential/secret, venue, runtime mode, root cap, workspace policy hoặc tùy chọn thực thi ngoài allowlist. Thay đổi trường không được phép phải bị reject, không được bỏ qua âm thầm.

`EffectiveBotConfig` là kết quả resolve bất biến, mang workspace settings version ID/hash, bot config version ID/hash, account ID, strategy/model release refs, `effective_at` và thời điểm resolve. Draft không có hiệu lực runtime. Quyết định/lệnh/vị thế đang mở giữ lineage đã pin; publish chỉ ảnh hưởng quyết định mới.

## 4. Shared evidence và quyết định theo bot

`SharedSignalEvidence` là fact/setup trung lập, scope workspace, point-in-time và immutable; tối thiểu có `workspace_id`, `evidence_id`, symbol/setup type, `observed_at`, `available_at`, `validity_end`, feature/source/model refs và evidence hash. Nó không có bot/account ID, strategy decision, quantity, order hay quyền thực thi.

Mỗi bot nhận evidence qua dispatcher idempotent và tự chạy strategy/config riêng. `BotDecision` có `workspace_id`, `bot_id`, `account_id`, evidence ID/hash, candidate hoặc abstain/veto reason, config/strategy/model lineage, `evaluated_at`, status và idempotency key. Evidence chỉ được sử dụng nếu `available_at <= evaluated_at < validity_end`; dữ liệu tương lai hoặc revision chưa khả dụng bị từ chối.

## 5. Equity, quy đổi và workspace budget

`AccountEquitySnapshot` mang workspace/account scope, asset, tổng equity, equity basis, realized PnL, unrealized PnL, `captured_at`, nguồn, source version/hash, mark refs và trạng thái known. Snapshot phải khai báo rõ một trong hai cách dựng, không trộn chúng:

```text
PAPER: equity(as_of) = initial_capital_before_realized_pnl + cumulative_realized_pnl + sum(unrealized_pnl_at_valid_marks)
Venue: equity(as_of) = venue_total_equity_including_realized_and_excluding_unrealized + sum(unrealized_pnl_at_valid_marks)
```

`initial_capital_before_realized_pnl` không được là balance đã cộng realized PnL. Với venue, adapter phải ánh xạ field theo semantics chính thức đang dùng và không cộng realized lần hai. Snapshot phải cùng một `as_of` logic; mỗi vị thế mở phải có mark hợp lệ, không tương lai, không cũ hơn freshness policy. Thiếu mark khi còn vị thế mở làm snapshot không usable cho entry mới; realized-only equity không được giả lập thành tổng equity.

`RiskConversionObservation` chứa source asset pair, Decimal rate, `as_of`, `available_at`, source/version/hash và quality. Identity conversion chỉ được dùng khi asset đã là USDT. Non-USDT conversion chỉ hợp lệ nếu observation được policy cho phép, `available_at <= as_of`, không tương lai và còn trong freshness window. Không có implicit 1.0, fallback cũ hoặc đường quy đổi tự ghép không provenance.

`WorkspaceRiskBudget` được tính tại một `as_of` duy nhất:

```text
workspace_equity_usdt = sum(equity_usdt của account thuộc bot enabled)
workspace_cap_usdt = workspace_max_open_risk_fraction × workspace_equity_usdt
workspace_open_risk = sum(worst_case_open_stop_risk_usdt) + sum(active_pending_reservation_risk_usdt)
```

Đồng thời kiểm tra từng account và bot theo cap tương ứng; bot có thể siết nhưng không nới cap. Open risk/reservation/equity được chuẩn hóa USDT bằng observation point-in-time có lineage. Stale/unknown/missing/non-finite/không đồng bộ/mismatched scope hoặc snapshot future làm quyết định `DENY_NEW_ENTRY` (fail-closed). Không xóa protective monitoring/exits cho vị thế đã biết. Không reservation mới nào được làm vượt cap đang hiệu lực; nếu admin siết cap thấp hơn risk đã mở/đang reserve, publish vẫn có thể có hiệu lực nhưng hệ thống ghi trạng thái `CAPACITY_BREACH_AFTER_TIGHTENING`, chặn toàn bộ entry mới cho tới khi exposure xuống dưới cap và tiếp tục protective monitoring. Không tự động flatten.

## 6. Reservation serialization và idempotency

Mọi transaction có thể làm thay đổi tổng open risk/pending reservation hoặc publish thay đổi root risk cap phải lấy khóa theo thứ tự toàn cục:

1. **Workspace policy/risk scope** — cùng một transaction-scoped PostgreSQL advisory lock cho publish root risk policy và mutation risk/reservation, định danh bởi workspace UUID và namespace cố định.
2. **Account risk scope** — advisory lock cùng cơ chế định danh, namespace account, cho account UUID.
3. Đọc lại config version/effective time, position/open risk, reservation còn hiệu lực, snapshot và conversion; validate; ghi publication hoặc reservation/state/ledger và commit một lần trong Unit of Work.

Root risk publisher và reservation dùng cùng workspace lock để tạo total order. Reservation lấy version/cap có hiệu lực sau khi lấy lock; publish lấy workspace lock trước khi đọc tất cả account exposure/reservations, sau đó có thể publish mà không lấy account locks vì workspace lock đã chặn mọi mutation risk trong workspace. Nếu cap mới thấp hơn exposure, publish ghi nhận trạng thái breach như trên, không tạo thêm reservation. Đây là serialization của thay đổi root risk cap; các thay đổi không ảnh hưởng risk không buộc phải khóa risk scope.

Dùng hash ổn định 64-bit từ namespace + UUID cho khóa advisory; va chạm hash chỉ gây tuần tự hóa thừa, không được làm mất kiểm tra dữ liệu scope. Không dùng lock có thứ tự phụ thuộc bot, symbol hay thứ tự input. Mọi operation nhiều account phải khóa workspace một lần rồi account IDs theo thứ tự UUID tăng dần. Không được giữ account lock rồi mới xin workspace lock.

Áp dụng contract workspace→account cho create, expire, release, transfer, reconcile và bất kỳ mutation nào ảnh hưởng active totals. Advisory xact lock được giải phóng khi transaction kết thúc; repository không tự commit. Các thao tác đọc không mutate có thể không khóa nhưng phải đọc snapshot nhất quán và không được quyết định entry dựa trên kết quả đọc đó nếu thiếu transaction reserve atomic.

Idempotency key được scope tối thiểu `(workspace_id, bot_id, account_id, operation, key)`. Cùng key + cùng canonical payload trả kết quả đã ghi; cùng key + payload khác trả conflict. Candidate/decision ID không thay thế idempotency reservation. Unique/FK và workspace/bot/account lineage vẫn bắt buộc dù đã khóa.

## 7. Account exclusivity, grants, RLS và leases

- Một `BotInstance` gắn đúng một account; v1 unique `(workspace_id, trading_account_id)`. Foreign/manual activity không có bot lineage khóa entry và yêu cầu reconcile; không tự adopt, sửa hoặc đóng foreign state.
- `BotPermission` là enum ổn định, tối thiểu phân quyền đọc, config draft/publish, vận hành pause/resume và xem report. Owner/admin có quyền workspace-level theo policy; role khác phải có grant bot-scoped đang hiệu lực.
- Mọi repository/admin API truy vấn bot phải truyền `workspace_id` và `bot_id` tường minh; bot ID không phải bằng chứng ủy quyền. Application authorization kiểm tra principal/grant theo từng hành động. Database RLS là lớp scope bổ sung: tenant + bot row scope; không thay thế permission granularity trong application.
- DB transaction context dùng `app.workspace_id`, `app.bot_id` và (với user request) `app.membership_id`, được context wrapper thiết lập transaction-locally sau khi xác thực principal. Policy yêu cầu workspace khớp và một trong: (a) runtime context chỉ khớp đúng `bot_id`; (b) user bot-scoped context khớp bot đã được application authorization cấp; (c) workspace-admin context được xác lập sau khi xác thực role owner/admin. Context/policy không tự quyết định quyền config-publish/pause/report; service/API phải kiểm tra `BotPermission` theo hành động trước DB mutation/query. Không tin `bot_id` hoặc admin scope từ request. MB-01 phải test owner/admin multi-bot, operator có grant, operator không grant, runtime đúng bot và thiếu scope; API tests chứng minh action grants.
- `FencingToken` là số nguyên đơn điệu tăng theo lease generation trong scope bot/account; state-changing runtime command phải mang token hiện hành. Worker cũ/stale bị reject trước khi ghi ledger, reservation hay order state.

## 8. Rollout và lineage migration

Expand trước, backfill, validate, rồi mới enforce constraint:

1. Tạo đúng một bot cho mỗi existing account PAPER, giữ nguyên workspace/account IDs và mirror trạng thái ENABLED/DISABLED của account để không đổi hành vi runtime. Mode chỉ lưu tại TradingAccount; account mode/status chưa hỗ trợ làm migration dừng fail-closed.
2. Backfill bot IDs trên account-scoped records bằng `(workspace_id, account_id)`; shared market/research rows giữ workspace-only.
3. Trước khi NOT NULL/FK/unique được enforce, đối chiếu count/hash, tìm orphan/duplicate, và chứng minh mỗi record resolve đúng một bot.
4. Không reassign/xóa immutable trade/fill/decision payload; lịch sử giữ cùng IDs, payload và PnL. Nếu row không map duy nhất, migration dừng fail-closed và cần sửa mapping có audit.
5. Downgrade chỉ cho phép trước khi contract migration xóa khả năng chứa nhiều bot; rollback không được drop dữ liệu hoặc âm thầm gộp ledger.

## 9. Ruling MB-00 về WBS/test boundary

WBS bản đầu đặt test `test_bot_settings.py` trong MB-00 nhưng test đó kiểm tra hành vi overlay/resolver chưa tồn tại và thuộc MB-02. MB-00 là gói contract/documentation, không thêm implementation để làm test giả lập. Chuyển các hành vi overlay này sang MB-02 RED-first; MB-00 xác minh contract bằng review, link/diff checks và cập nhật ledger. Không thay đổi acceptance sản phẩm đã duyệt.

## 10. Rủi ro và điểm phải giữ mở

- Multi-account advisory workspace lock làm tuần tự hóa mọi reservation trong cùng workspace; đổi lại đảm bảo cap tổng đơn giản/đúng. Chỉ tối ưu shard/lock sau khi có benchmark và chứng minh lại invariant.
- Root risk-cap publish và reservation cùng workspace cũng bị tuần tự hóa; giảm cap dưới exposure hiện tại tạo trạng thái breach có kiểm soát, chặn entry mới, không force-close vị thế.
- PostgreSQL advisory lock dùng namespace/UUID hashing; MB-04 phải kiểm tra helper/SQL thực tế, quyền runtime, cùng transaction và isolation behavior bằng concurrency E2E.
- Bot-scoped RLS với quyền owner/admin cần được triển khai/test cẩn thận; application grant là bắt buộc dù policy được bật.
- `WorkspaceRiskBudget` phụ thuộc mark/equity observations đồng bộ. Nếu luồng PAPER chưa tính mark-to-market, bot mới không được mở entry khi có vị thế mà không có mark hợp lệ.
- Binance account activity, private equity và execution chỉ có thể được xác nhận sau WP-03; không tuyên bố live-ready trước đó.

## Thuật ngữ EN→VI

- **Advisory transaction lock — khóa gợi ý theo transaction:** khóa logic PostgreSQL do ứng dụng định danh, tự giải phóng khi transaction kết thúc; [PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS).
- **Fencing token — mã hàng rào quyền sở hữu:** số thế hệ lease giúp chặn worker cũ ghi dữ liệu sau khi mất quyền.
- **Mark-to-market — định giá theo thị trường hiện tại:** tính giá trị vị thế mở theo mark hợp lệ tại thời điểm snapshot, thay vì chỉ cộng PnL đã chốt.
- **Fail-closed — từ chối khi thiếu điều kiện an toàn:** dữ liệu/permission/lineage không đủ thì chặn entry hoặc mutation.
- **Point-in-time — dữ liệu đúng thời điểm:** dữ liệu phải khả dụng tại thời điểm quyết định và không chứa thông tin tương lai.
