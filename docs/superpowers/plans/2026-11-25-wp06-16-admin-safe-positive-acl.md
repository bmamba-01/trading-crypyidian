# WP06.16: allowlist dương cho quyền admin-safe

## Mục tiêu

Thay việc chuyển ACL admin theo denylist bằng kiểm kê dương theo object và operation. Migration phải dừng trước khi thay ACL/membership nếu gặp direct grant không có phân loại; khi lỗi, transaction phải rollback toàn bộ. Quyền được phân loại cần có consumer và thao tác cụ thể.

## Phạm vi và ranh giới

- Áp dụng cho `crypidian_admin_web` và `crypidian_admin_web_safe` trong migration tách quyền admin toàn ứng dụng.
- Giữ nguyên RLS, MFA, audit, lifecycle, reader report, quyền hiện hành và khóa `SHADOW`/`PAPER_GATE`.
- Các grant ledger bị chặn khỏi admin-safe phải được ghi rõ trong manifest là `DENY`; không tự chuyển ACL mới chỉ vì relation chưa có tên trong denylist.
- Migration d4 chưa được commit/phát hành trong nhánh này. Đã đọc trạng thái DB dùng chung ở workspace đang chạy: revision `b7d2e4f8c1a3`, không có file/revision d4 hoặc bảng snapshot WP06; thử nghiệm triển khai chỉ dùng PostgreSQL Compose cô lập.

## Kế hoạch

1. **Kiểm kê baseline:** chạy PostgreSQL Compose tại revision ngay trước d4; xuất mọi ACL trực tiếp của admin theo schema/object/column/identity/privilege/grant option, membership, default ACL và ownership. Gắn từng mục với consumer và operation; phân loại raw ledger thành DENY.
2. **RED fail-closed:** thêm Compose E2E tạo object tạm chưa phân loại, cấp direct grant cho admin, rồi yêu cầu migration dừng. Xác nhận membership/ACL/role/snapshot chưa đổi và transaction đã rollback.
3. **Preflight dương:** tạo manifest tường minh cho quyền được chuyển; so sánh toàn bộ catalog trước bất kỳ DDL/ACL/membership mutation nào. Từ chối grant option, object ngoài manifest, privilege ngoài operation đã duyệt, schema lạ và default ACL không phân loại.
4. **Snapshot và rollback:** chỉ sau preflight mới snapshot, áp dụng quyền `ALLOW`, thu hồi direct grant, tách membership. Giữ nguyên SQL restore cho downgrade; E2E kiểm quyền hợp lệ được khôi phục và migration upgrade lại đạt.
5. **Regression:** unit migration contract; PostgreSQL Compose positive/negative grant inventory, atomic rollback, downgrade/upgrade; admin auth/config/control/report và responsive Playwright desktop/mobile bị ảnh hưởng.
6. **Tài liệu:** cập nhật WBS, module, project-status và implementation-memory; đọc lại memory. Không thêm glossary nếu không phát sinh thuật ngữ giao dịch/AI mới.

## Tiêu chí hoàn tất

- Không có grant direct của admin chưa được phân loại.
- Grant mới trên object không có trong manifest làm migration lỗi trước ACL/membership mutation.
- Downgrade khôi phục đầy đủ grant hợp lệ đã snapshot; grant từ chối không được chuyển sang safe role.
- E2E/Playwright liên quan đạt không skip; `SHADOW` và `PAPER_GATE` vẫn khóa.

## Tiến độ hiện tại

- [x] Đọc acceptance CRY-451 và chuyển sang In Progress.
- [x] Kiểm kê baseline PostgreSQL 18.6 tại `f0a2c4e6b8d3`: 295 direct ACL entry (157 relation, 109 column, 29 routine), membership `ADMIN FALSE / INHERIT TRUE / SET TRUE`, không có default ACL/ownership/admin grant ở 9 lớp ACL còn lại.
- [x] Thêm test RED: unit ban đầu thiếu manifest/preflight; Compose E2E ban đầu chấp nhận grant lạ. E2E GREEN xác nhận lỗi trước mutation, transaction rollback giữ membership/grant/revision và không tạo role/snapshot.
- [x] Thêm manifest đóng 295 dòng có schema, định danh object/loại, operation, grantor, consumer, disposition; 274 grant được chuyển, 21 raw-ledger grant chỉ thu hồi. Preflight duyệt cả quan hệ/cột/routine/schema/type/database/large object/language/FDW/server/tablespace/parameter, default ACL, ownership và membership options trước DDL.
- [x] Compose E2E đối chiếu đủ snapshot với manifest, thử downgrade khôi phục quyền hợp lệ và upgrade lại; admin auth/config/control/dashboard/report responsive regressions **19 passed/0 skipped**.
- [x] Unit regression liên quan **955 passed/1 warning**; riêng migration + role E2E **8 passed/1 warning**. Ruff check/format, mypy migration, Alembic sole head `f0a2c4e6b8e0` và diff-check đạt.
- [x] Cập nhật WBS, module, project-status và memory; đã đọc lại entry memory sau khi ghi.

## Bằng chứng cuối

- PostgreSQL Compose: `uv run pytest -q -rs --tb=long tests/e2e/test_db_role_least_privilege.py` → **1 passed/26.53s/0 skipped**; kiểm tra lại cùng unit migration → tổng **8 passed/28.59s/0 skipped**.
- Responsive/admin regressions: 10 selector/file groups, **19 passed/795.22s/0 skipped**; gồm session/navigation, dashboard, report, live monitor, risk, PAPER performance/model card, multibot và report-reader.
- Unit nhóm admin/WP06: **955 passed/37.72s/0 skipped**.
- PostgreSQL 18 tài liệu chính thức kiểm ngày 25/11/2026: [GRANT và danh sách loại object có thể cấp quyền](https://www.postgresql.org/docs/18/sql-grant.html), [catalog `pg_auth_members` và cờ membership](https://www.postgresql.org/docs/current/catalog-pg-auth-members.html).

## Follow-up sau review độc lập — 25/11/2026

Review độc lập sau lần chạy lại unit/E2E **8 passed/1 cảnh báo** tìm thấy các đường catalog chưa nằm trong bằng chứng hiện tại. Giữ ticket In Progress cho tới khi các đường này được đóng và kiểm lại:

### Task 7: Bịt các khoảng trống catalog và bảo toàn snapshot

- [x] **RED:** PostgreSQL Compose phải chứng minh migration fail trước khi đổi role/ACL/snapshot khi gặp direct ACL trên system column (`pg_attribute.attnum < 0`), direct database grant ngoài DB hiện hành, owner trên catalog object ngoài danh sách cũ, default ACL áp dụng lên snapshot, hoặc membership/ACL drift trong cửa sổ preflight.
- [x] **Preflight ổn định:** quét attribute ACL gồm mọi `attnum <> 0`; kiểm `pg_database.datacl` ở toàn cluster; dùng `pg_shdepend` để kiểm ownership tổng quát; giữ manifest positive cho mọi grant được chuyển và từ chối mọi tuple chưa phân loại. Khóa catalog liên quan theo thứ tự cố định trước snapshot để một thay đổi đang chạy phải hoàn tất trước kiểm kê và thay đổi mới không chen giữa preflight/snapshot.
- [x] **Snapshot an toàn:** bảng snapshot không nhận quyền ghi từ PUBLIC/role khác qua default ACL; xác thực quyền sau khi tạo và trước khi commit. Khi downgrade, chỉ thực thi grant SQL nếu toàn bộ snapshot còn khớp manifest đã kiểm; sai/missing/extra row phải dừng rollback an toàn.
- [x] **RED/GREEN:** Compose tạo grant trên system column và database khác, cùng ca snapshot/default ACL và barrier race; xác nhận lỗi/khóa đúng nguyên nhân, transaction rollback, quyền hợp lệ không mất. Downgrade/upgrade lại xác nhận restore chính xác grant đã duyệt.
- [x] Cập nhật inventory/manifest, unit/E2E wrapper, WBS, module, project status và memory; đọc lại memory sau cập nhật. Không thay RLS/MFA/audit/trading modes.

**Ruling:** d4 chưa commit/phát hành và DB Compose của ticket đứng tại revision cha, nên có thể sửa revision chưa phát hành cùng manifest và kiểm đủ vòng upgrade/downgrade. Dữ liệu fixture chỉ chứng minh catalog enforcement; không đại diện entitlement thị trường hoặc quyền production.


**Kiểm kê Task 7.** Manifest chứa thêm membership với grantor và cờ đã duyệt. Tập 295 direct ACL gồm 274 quyền chuyển và 21 quyền chỉ thu hồi. Khi downgrade, d4 kiểm tra đủ chín trường của 296 dòng dưới khóa bảng snapshot rồi chạy các lệnh từ manifest trong image. Bảng snapshot dùng grant SQL duy nhất làm khóa chính.

**Điều kiện vận hành.** Migrator lấy `SHARE ROW EXCLUSIVE` trên 16 catalog theo thứ tự cố định. PostgreSQL 18 có thể nhả khóa catalog của `GRANT` trước cuối transaction. Sau khi lấy đủ khóa, d4 đợi tối đa hai giây cho tập transaction khác đã có XID ở thời điểm đó. Nếu còn writer, d4 dừng để lần chạy sau đọc catalog mới. Một transaction ghi không liên quan, kể cả ở database khác cùng cluster, cũng có thể làm lần chạy dừng. Lệnh lấy khóa catalog tuân theo `lock_timeout` của session. Catalog cấp cluster được giữ khóa tới cuối transaction Alembic.

**Phạm vi bằng chứng.** Compose đối chiếu digest của ACL thực và membership trước/sau vòng upgrade–downgrade, kèm ca âm tính cho grant trên cột hệ thống/database khác, collation owner, default ACL toàn DB/theo schema, ghi cột snapshot, sai/thiếu/thừa dòng và writer đồng thời. Kết quả áp dụng cho fixture PostgreSQL 18.6. [Memory](../../implementation/implementation-memory.md).
