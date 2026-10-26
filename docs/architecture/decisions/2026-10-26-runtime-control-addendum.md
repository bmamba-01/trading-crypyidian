# ADR addendum: kill switch/pause là control vận hành append-only, không phải trường `WorkspaceSettings`

- **Trạng thái:** đã chấp nhận (controller ruling 25, 27, 30 ngày 2026-10-26); hiện thực ở MB-05.
- **Ngày:** 2026-10-26
- **Bổ sung cho:** [Hợp đồng kiến trúc Multi-Bot](./2026-10-24-multibot-contracts.md) §3 (root config) và §7 (lease/fencing).
- **WBS:** [MB-05](../../superpowers/plans/2026-10-26-mb05-runtime-registry-wbs.md) (ruling 5, 6, 22, 25, 27, 30).

## Bối cảnh

Contract §3 ghi `WorkspaceSettings` là nguồn duy nhất cho "hard cap/chính sách workspace … và workspace kill switch". Publish `WorkspaceSettings` là thao tác có version, MFA, maker-checker và đổi `settings_version_id`, kéo theo kiểm tra drift profile (`PAPER_PROFILE_DRIFT`) và lineage của intent/vị thế. Kill switch và pause bot là hành động **giảm rủi ro**: chúng phải có hiệu lực ngay, ma sát thấp và không được chờ quy trình publish (SEC-22, SEC-47).

## Quyết định

1. **Kill workspace và pause bot là trạng thái vận hành**, lưu ở hai bảng control riêng: `workspace_runtime_control` (scope workspace, `kill_state` `ACTIVE`/`KILLED`) và `bot_runtime_control` (scope bot, `entry_state` `ENABLED`/`PAUSED`). Không có trường kill/pause trong `WorkspaceSettings`; `WorkspaceSettings` tiếp tục là root config duy nhất và không bị nhân đôi giá trị config.
2. **Lịch sử append-only có audit.** Mỗi thay đổi INSERT một hàng revision kế tiếp (revision 1 cho lần đầu); trạng thái hiện hành là hàng có revision lớn nhất; chưa có hàng nghĩa là chưa từng kill/pause. Trigger DB chặn UPDATE/DELETE và ép revision = revision lớn nhất + 1; không role nào có UPDATE/DELETE. Mỗi hàng ghi actor (`changed_by`), mã/lý do, thời điểm theo đồng hồ DB và `correlation_id`, kèm `audit_event` tương ứng. Thao tác lặp đến cùng trạng thái đích là noop (không thêm revision, vẫn audit `*.noop`); resume/release bắt buộc `expected_revision`.
3. **Tách writer.** Admin-web chỉ ghi ý định control (INSERT control); runtime giữ lease mới áp dụng hệ quả (hủy lệnh PAPER chưa khớp, giải phóng reservation) trong transaction có fencing token. Runtime chỉ có `SELECT` trên bảng control; migration MB-05 không cấp cho admin-web quyền ghi lease/state và không cấp mới quyền ghi ledger/checkpoint. **Ngoại lệ còn tồn tại (trước MB-05):** admin-web vẫn kế thừa `SELECT, INSERT` trên `paper_execution_checkpoint` qua group `crypidian_app` (grant gốc migration `a1f4c9d2e6b8`); trigger fence `paper_execution_checkpoint_fence` vẫn áp dụng cho mọi hàng chèn. Việc thu hồi quyền kế thừa này thuộc SEC-01b (xem [WBS SEC-01/02](../../superpowers/plans/2026-10-24-sec01-sec02-db-hardening-wbs.md), ruling R2 và việc còn mở SEC-01b).
4. **Bất biến đọc lại control trong cùng transaction (ruling 27).** Mọi transaction entry hoặc reservation (`evaluate_and_submit`, fill entry-event, reserve/transfer qua MB-04, đường bot decision của MB-05b) phải đọc lại control workspace + bot **trong chính transaction đó**, sau khóa L1 `workspace-settings:{ws}` và trước khi ghi intent/reservation/fill; không dùng giá trị đọc ở transaction trước hoặc cache trong bộ nhớ. Control mutation cũng lấy L1, nên sau khi pause/kill commit không entry mới nào commit được. Intent/reservation ghi `runtime_control_revisions` đã đọc vào evidence. Không đọc được control (lỗi DB/quyền) thì fail-closed `RUNTIME_CONTROL_UNAVAILABLE` cho entry; exits bảo vệ vẫn chạy.
5. **Quyền (ruling 30).** Kill/release workspace chỉ `owner` và `risk_admin` (`Permission.WORKSPACE_KILL = "workspace:kill"`); `operator` chỉ pause bot có grant `bot:runtime:pause` và chỉ resume bot có grant `bot:runtime:resume` (hai grant tách biệt; policy DB `pause_write` chỉ nhận hàng `PAUSED` với grant pause và hàng `ENABLED` với grant resume). Release workspace và resume bot cần MFA gần đây (`RECENT_MFA_LIFETIME`) + lý do + `expected_revision`. Mọi kết quả, kể cả noop và bị từ chối do quyền, đều có audit. RLS `WITH CHECK` ở DB lặp lại trần quyền này như lớp phòng thủ thứ hai; quyết định quyền chính vẫn ở application.
6. **Fencing (bổ sung §7).** Lease theo bot/account là một hàng DB, đồng hồ DB (`clock_timestamp()`) là nguồn thời gian; fencing token tăng đơn điệu khi đổi chủ hoặc lấy lại sau hết hạn. Mọi command thay đổi state PAPER trình token hiện hành: application kiểm `FOR SHARE` hàng lease ngay sau L1; trigger DB trên `paper_execution_checkpoint`, `bot_runtime_state`, `account_activity_incident` từ chối token cũ/hết hạn kể cả khi bỏ qua application. Ngoại lệ chuyển tiếp: checkpoint `fencing_token NULL` chỉ hợp lệ khi bot chưa từng có hàng lease.

## Hệ quả

- Kill/pause không đổi `settings_version_id`, không gây `PAPER_PROFILE_DRIFT`, không ảnh hưởng lineage vị thế đang mở.
- Pause/kill không chạm `trading_account.status` hay `bot_instance.status` (ruling 3); mẫu số risk MB-04 không đổi (ruling 23).
- Flatten-all không thuộc quyết định này (SEC-23, MB-07/WP-03).
- Contract §3 cần một dòng liên kết tới addendum này; contract là tài liệu do controller sở hữu nên controller cập nhật.

## Nguồn

- PostgreSQL 18, [Current Date/Time](https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT) (kiểm tra 2026-10-26): `clock_timestamp()` là giờ thực, `now()` là thời điểm bắt đầu transaction.
- PostgreSQL 18, [SELECT — The Locking Clause](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE) (kiểm tra 2026-10-26): `FOR SHARE` cần quyền UPDATE trên ít nhất một cột; dưới READ COMMITTED khóa chờ rồi lấy phiên bản mới nhất của hàng.
- Martin Kleppmann, [How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) (2016): storage phải kiểm fencing token đơn điệu ở mỗi lệnh ghi.
