# WP05A — Trang quản trị nguồn và policy quyền

**Ngày:** 04/11/2026 <br>
**Trạng thái:** Thiết kế cho lát offline; triển khai theo ủy quyền tổng thể trước đó của người dùng về cập nhật WBS và tiếp tục implementation. <br>
**Liên quan:** [WP-05 deep gap analysis](../plans/2026-11-04-wp05-deep-gap-analysis.md), [WBS WP-05](../plans/2026-11-01-wp05-research-wbs.md), [Plan 05](../../implementation/05-research-ingestion.md).

## Ý định

Nối policy registry hiện có vào admin để operator có thể đăng ký source candidate, xem lịch sử/quota, gửi policy revision chờ duyệt và để một reviewer độc lập duyệt hoặc từ chối. Hiện domain, PostgreSQL append-only history, role boundary, database clock, quota reservation và post-commit observation đã tồn tại, nhưng chưa có route quản trị hoặc giao diện.

Trang và API chỉ quản lý metadata/policy offline. Giao diện phải dùng tiếng Việt, responsive trên mobile và desktop, đồng nhất Bootstrap 5.3 cùng font Be Vietnam Pro đang dùng. Trạng thái `VERIFIED` là xác nhận của actor có quyền dựa trên evidence reference họ nhập; hệ thống không tự xác minh quyền pháp lý.

## Mục tiêu

- Liệt kê source theo workspace đã xác thực; xem trạng thái hiện hành, revision history, actor/reviewer, evidence reference, thời hạn và usage so với quota hiện tại.
- Cho owner có `RESEARCH_SOURCE_WRITE` tạo candidate `DISCOVERED`, gửi revision `RIGHTS_PENDING`, block hoặc revoke policy theo transition hợp lệ.
- Cho reviewer có `RESEARCH_SOURCE_APPROVE` duyệt revision pending bằng một revision mới `VERIFIED`; reviewer phải khác submitter theo `user_id` và `membership_id`. Approval giữ submitter trong `actor_id` của revision được duyệt và ghi reviewer vào `approved_by`.
- Lấy workspace, actor và reviewer hoàn toàn từ session đã xác thực và history đã lưu; không nhận identity scope từ request.
- Dùng repository và `PostCommitSourceObserver` hiện có để ghi append-only policy trước, sau đó finalize observation trong transaction mới. Nếu observation không hoàn tất, thao tác báo lỗi; policy vẫn không được xem là usable vì access path fail-closed.
- Tách policy admin port khỏi quota reservation port: admin-web dùng role policy/observation hiện có, không cần và không được mượn ingestion worker factory để ghi quota.
- Mutation nhận idempotency UUID ổn định. Với revision sau candidate, key có phạm vi workspace: retry cùng key phải nhận lại revision đã append và thử khôi phục observation còn thiếu; payload khác cùng key trả conflict kể cả khi request nhắm source khác. Candidate đầu tiên giữ identity source ID + canonical payload theo quy tắc riêng bên dưới.
- Candidate đầu tiên giữ policy ID do `SourceRegistryEntry.discover()` tạo; source ID + canonical payload là identity idempotent của thao tác tạo. Với revision tiếp theo, policy ID chứa idempotency UUID và SHA-256 của payload chuẩn hóa để cùng key nhưng payload đổi luôn conflict mà không cần thêm cột.
- Ghi reason, actor, reviewer, sequence và database timestamps trong policy history hiện có; không sửa revision cũ.
- API và HTML đều kiểm RBAC phía server; mutation HTML kiểm CSRF, mutation API yêu cầu CSRF token; mutation yêu cầu MFA gần đây theo cửa sổ 15 phút hiện có.

## Không thuộc phạm vi

- Không fetch, crawl, schedule, resume cursor hoặc gọi provider; không chọn/duyệt nguồn S1.
- Không lưu credential, raw body, excerpt, embedding hoặc artifact; không thay đổi retention/deletion behavior.
- Không đánh dấu `VERIFIED` chỉ vì URL/public endpoint tồn tại. Evidence reference không phải bằng chứng pháp lý được hệ thống xác minh.
- Không tạo quyền source read cho trading/trainer workers và không cấp quyền source-dependent learning.
- Không thay đổi quota ledger semantics hoặc mutation quota limits ngoài policy revision.

## Các cách tiếp cận đã cân nhắc

1. **CLI-only:** ít thay đổi app nhưng không phục vụ operator qua admin mobile, và thiếu workflow/history hiển thị.
2. **API-only:** cung cấp integration point nhưng operator vẫn cần một client riêng để review history và quota.
3. **Authenticated API + trang admin responsive — chọn theo ràng buộc admin/mobile đã nêu:** API dùng cho client; trang server-rendered và form CSRF cung cấp luồng operator ngay trong admin hiện tại. Cả hai gọi cùng application service và cùng policy contracts.

Lựa chọn là quyết định triển khai trong phạm vi ủy quyền trước đó của người dùng; không tạo connector hoặc nguồn ngoài.

## Ranh giới và luồng

```text
session Principal
       │
       ├── GET /api/v1/research-sources[/<source_id>]
       ├── POST source / policy revision / approve / block / revoke
       └── GET /admin/research-sources (responsive Vietnamese UI)
                   │
           SourceRegistryService
           ├── quyền WRITE: owner tạo candidate / submit / block / revoke
           ├── quyền APPROVE: reviewer duyệt pending; phải khác submitter
           └── transaction: đọc current → validate transition → append
                                  │
                         PostCommitSourceObserver
                         ├── admin-web session ghi revision
                         ├── observer transaction ghi receipt sau commit
                         └── quota usage đọc metadata ledger, không gọi source
```

### Policy workflow

1. Owner tạo source candidate state `discovered`; app tạo revision đầu tiên qua `SourceRegistryEntry.discover()` và append/observe.
2. Owner gửi revision mới state `rights_pending`, nhập evidence reference, purpose đề xuất, quota và lý do. Candidate không có quyền sử dụng. Request có idempotency key; policy ID của revision được tạo ổn định từ key và payload chuẩn hóa để retry sau append/observation lỗi không sinh revision khác.
3. Reviewer khác owner đọc history/evidence reference và có thể approve. App xác nhận policy đang là revision mới nhất, vẫn `rights_pending`, reviewer có permission và không trùng submitter theo cả user lẫn membership. Revision mới lưu actor `user_id:membership_id`; với history cũ chỉ có user ID, adapter tra membership trong workspace và từ chối nếu không thể xác định. Append revision `verified` với purpose, `reviewed_at`, expiry, actor từ revision pending và `approved_by` từ reviewer Principal.
4. Owner có thể block/revoke bằng revision mới kèm reason; access gate hiện hành tiếp tục fail-closed theo policy/expiry/observation semantics.

Tất cả phép đổi trạng thái được kiểm lại trong application/domain/repository; UI chỉ ẩn nút không thay thế authorization. Concurrent update tạo conflict và cần tải lại history.

### HTTP và trình bày

- `GET /api/v1/research-sources`: source list trong workspace hiện thực; chỉ principal có WRITE hoặc APPROVE.
- `GET /api/v1/research-sources/{source_id}`: toàn bộ revision history và quota usage/current budget window.
- `POST /api/v1/research-sources`: tạo candidate discovered; yêu cầu header `Idempotency-Key` là UUID; cùng source ID retry cùng payload trả kết quả đã lưu, payload khác conflict.
- `POST /api/v1/research-sources/{source_id}/revisions`: gửi `rights_pending`, `blocked` hoặc `revoked`; schema không nhận `verified`; bắt buộc `Idempotency-Key` UUID ổn định.
- `POST /api/v1/research-sources/{source_id}/revisions/{policy_id}/approve`: reviewer duyệt pending revision; bắt buộc idempotency key ổn định. Request không nhận actor, approver hoặc workspace IDs.
- `GET /admin/research-sources`: list, chi tiết/history, quota usage và forms theo permission. Form hiển thị rõ label “Evidence tham chiếu do người quản trị cung cấp — chưa được hệ thống xác minh pháp lý”.
- Response admin không cache; không render ORM object/secret. Các field auth scope chỉ hiển thị dưới dạng khai báo scope, không cho phép nhập giá trị credential.

## Bảo mật và dữ liệu

- Workspace chỉ lấy từ `AuthIdentity.principal`; mọi repository query gắn RLS/workspace GUC.
- Write: `Permission.RESEARCH_SOURCE_WRITE`; approve: `Permission.RESEARCH_SOURCE_APPROVE`; read: một trong hai permission. Role hiện tại giữ nguyên: owner có tất cả, analyst reviewer có quyền approve nhưng không write.
- Dual control kiểm user và membership độc lập. Reviewer không thể tự approve revision do chính họ tạo.
- Mutation yêu cầu CSRF đúng flow HTML/API và MFA gần đây theo `RECENT_MFA_LIFETIME`.
- Application admin dùng `SourcePolicyAdminService` riêng với policy read/write port; không dựng `SourceRegistryService` có quota reserve trong admin-web. `PostCommitSourceObserver` được gọi qua append-only protocol; quota reservation vẫn chỉ thuộc service/role ingestion hiện có.
- Mỗi append dùng workspace/key advisory lock khi policy ID mang revision idempotency key, sau đó dùng source advisory lock và sequence liên tiếp; source history bất biến. Observation receipt chạy sau commit theo `PostCommitSourceObserver`; thiếu receipt làm policy history inaccessible/fail-closed. Policy ID revision từ idempotency key khiến retry gọi lại đúng revision để adapter hoàn tất receipt còn thiếu.
- Detail/history và quota snapshot được đọc cùng transaction dưới cùng source advisory lock; view không ghép policy sequence cũ với usage/budget của revision mới.
- Quota hiển thị aggregate của ledger trong cửa sổ hiện hành bằng database clock và budget revision hiện hành; không reserve quota từ admin page.
- API errors không lộ SQL, raw credential hay workspace tồn tại khác. Ghi `Cache-Control: no-store`.

## Kiểm thử chấp nhận

- Unit: build candidate, state-transition validation, actor/reviewer provenance, self-approval rejection, recent MFA, deterministic serialization/quota summary, stale pending revision conflict.
- PostgreSQL Compose: real login roles; workspace isolation; append-only history và post-commit observation; owner submit → separate analyst approve; same-user approval denied; unauthenticated/role/CSRF denial; quota usage window; rollback hoặc observer failure không làm policy usable; upgrade/check/downgrade guards nếu schema đổi.
- Playwright: owner/reviewer workflow trên desktop 1280×800 và mobile 390×844; kiểm tiếng Việt, thao tác bàn phím, không tràn ngang, quyền hiển thị đúng; chụp screenshot sau khi mở source detail/history trên cả hai viewport.
- Không chạy provider/network hoặc gửi lệnh/Telegram.

## Quyết định kỹ thuật để builder hoàn tất

- Tận dụng schema policy/receipt/quota đang có; chỉ thêm migration nếu test chứng minh có trường/index thiếu.
- Thêm application methods/read ports cần thiết, `SourcePolicyAdminService` riêng và app-state wiring trên admin-web; quota-writing `SourceRegistryService` tiếp tục cần factory ingestion tường minh và không được mở trong admin-web.
- Tách API router và HTML template theo conventions repo; cùng service, không sao chép policy logic vào route.
- Giữ metadata-only; page copy là tiếng Việt và nêu trạng thái legal-evidence chính xác.
