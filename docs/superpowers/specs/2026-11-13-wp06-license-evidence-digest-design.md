# WP06/WP05 — Digest và phạm vi quyền bằng chứng license

## Ý định

Cho phép operator ghi nhận bằng chứng quyền sử dụng nguồn dữ liệu đủ chặt để WP06 kiểm tra phạm vi. Backend nhận bytes của tài liệu, tự tính SHA-256, bỏ bytes sau request và chỉ lưu digest cùng phạm vi quyền có kiểu, metadata upload, actor và tham chiếu tới kho tài liệu bảo mật bên ngoài.

## Phạm vi được duyệt

- Nhận tài liệu qua endpoint quản trị đã xác thực; đọc body theo luồng, giới hạn cứng 10 MiB, từ chối body rỗng và content type không nằm trong allowlist. Băm incremental SHA-256. Không gọi `Request.body()`, không tải URL, không ghi bytes vào PostgreSQL, Garage, log hay artifact. Bản ghi chỉ được tạo sau khi stream hoàn tất và đạt giới hạn.
- `evidence_ref` là tham chiếu do operator cung cấp tới kho tài liệu bên ngoài; backend không resolve hoặc fetch tham chiếu này.
- Tạo bảng evidence append-only riêng, scoped theo workspace/source, lưu `evidence_id`, `evidence_ref`, SHA-256, byte count, media type, `scope_version`, canonical typed scope, scope hash, người ghi nhận và thời điểm DB.
- Scope chuẩn hóa gồm provider, product, venue, contract, tập symbol, interval, khoảng dữ liệu UTC có biên đầu/cuối, các purpose được cấp, loại output, attribution, retention và giới hạn quyền với output/derived data. Phạm vi không rõ, trùng, rỗng, sai thời gian hoặc không hợp lệ sẽ bị từ chối.
- Revision policy VERIFIED mới phải tham chiếu bằng chứng theo cùng workspace/source. Revision cũ vẫn đọc được nhưng không được xem là bằng chứng hiện hành có digest/scope. Không backfill digest giả.
- Rights fingerprint/revision receipt dùng đúng digest và scope hash; Python và PostgreSQL phải cùng contract canonical/hash. WP06 TRAINING và PAPER/SHADOW fail-closed khi evidence thiếu, hết hiệu lực, source mismatch hoặc scope không bao phủ yêu cầu. Các purpose khác tiếp tục theo policy hiện hành.
- Admin tiếng Việt responsive hiển thị digest rút gọn/copy đầy đủ, scope đã chuẩn hóa và trạng thái thiếu evidence. Form upload yêu cầu scope, external reference, file, CSRF, Idempotency-Key và MFA gần đây; phê duyệt policy tiếp tục là hành động riêng với reviewer khác người nộp.

## Không thuộc phạm vi

- Backend không xác minh tính xác thực pháp lý của tài liệu, chữ ký, chữ ký số hay diễn giải điều khoản. Digest chỉ xác nhận bytes được gửi lên không đổi; operator và reviewer vẫn chịu trách nhiệm xác minh nội dung và quyền.
- Không thay đổi/quét URL của external archive; không tự chuyển source sang VERIFIED; không tạo quyền QA, quyền public-access mặc định hoặc giấy phép.
- Không lưu hoặc tái dựng bytes, không nới ACL scorer, ingestion, trainer, admin hoặc market signal.

## Các cách đã cân nhắc và quyết định

1. Operator tự gõ hash: không chứng minh backend đã nhận đúng bytes và dễ nhập nhầm.
2. Lưu tài liệu trong object store: bị loại theo lựa chọn “chỉ lưu digest/scope” vì giữ thêm bản hợp đồng và kéo theo retention/access policy.
3. **Được chọn:** upload bytes qua `Request.stream()`, băm theo chunk, bỏ bytes, lưu metadata và typed scope. Endpoint không cần multipart/parser/dependency mới; nội dung không được ghi xuống database hay object store.

## Ràng buộc và tiêu chí chấp nhận

- Scope v1 là contract `wp06-license-scope-v1`: `source_id`; `provider/product/venue/contract` dạng token ASCII chữ hoa; `symbols` là tập token uppercase không wildcard; `intervals` là tập timeframe canonical (`1m`, `15m`, `1h`, `1d`, ...); `data_start` inclusive và `data_end` exclusive, UTC; `allowed_purposes` là enum hiện có; `allowed_outputs` thuộc allowlist (`derived_features`, `trained_model`, `aggregate_report`, `paper_inference`); `max_retention_days` là số nguyên dương hoặc `null` (không có giới hạn tối đa trong license); `attribution` chuỗi bắt buộc; `derived_use` là tập enum (`feature_extraction`, `model_training`, `paper_inference`, `aggregate_reporting`). Set input có giá trị trùng bị từ chối; thứ tự được chuẩn hóa bằng sort, không tự khử trùng lặp. Không chấp nhận wildcard.
- Canonical bytes của scope là UTF-8 từ `json.dumps(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))`; thời gian luôn UTC RFC 3339 với hậu tố `Z`, độ chính xác microsecond cố định. `scope_hash = SHA256(canonical_bytes)`. Bảng lưu cả canonical text lẫn JSONB để DB có thể xác minh digest trực tiếp trên bytes chuẩn; không hash `jsonb::text`.
- Một policy revision VERIFIED mới ghim đúng một `evidence_id`. Một evidence record có thể được dùng lại cho revision cùng workspace/source nếu `evidence_ref`, digest và scope giữ nguyên; policy purposes không được vượt `scope.allowed_purposes`. `evidence_ref` mới trỏ kho archive bảo mật bên ngoài; giá trị cũ vẫn đọc nguyên trạng và không được xem là xác minh.
- `SourcePolicyRevision` bổ sung identity fields `provider/product/venue/contract` nullable để đọc lịch sử cũ. Khi source đi từ `rights_pending` sang verified mới, bốn field bắt buộc và phải khớp chính xác với scope; sau khi identity đã có giá trị thì mọi revision kế tiếp giữ nguyên. Muốn đổi provider/product/venue/contract phải tạo source ID mới.
- `SourceUseScope` có cùng identity fields, `symbols`, `intervals`, `data_start`, `data_end`, một `purpose`, tập `outputs`, tập `derived_use` và `retention_days` hiệu lực. Consumer dựng scope nội bộ từ SourcePolicyRevision + manifest symbols/date range + feature/timeframe schema + purpose/output của operation; không nhận JSON scope từ caller. `retention_days=null` biểu thị giữ không thời hạn. Khi consumer chưa có dữ liệu để dựng một field, quyền không đủ điều kiện.
- Quy tắc scope: source ID và provider/product/venue/contract phải khớp chính xác; symbol/interval dùng của manifest phải là tập con; khoảng dùng phải nằm trong `[data_start, data_end)`; purpose/output/derived-use phải là tập con. Nếu retention hiệu lực của use là `null` (không thời hạn), license cũng phải cho `max_retention_days=null`; nếu use có số ngày hữu hạn, license phải cho `null` hoặc giá trị không nhỏ hơn số ngày dùng. Do hệ thống hiện giữ lineage/model artifact lâu dài, chỉ scope không giới hạn retention mới có thể qua gate hiện tại; license có giới hạn ngày bị từ chối cho tới khi có retention/delete lifecycle tương ứng. Nếu consumer chưa cung cấp đủ chiều dữ liệu để so sánh, từ chối eligibility (không suy diễn/wildcard) cho WP06 TRAINING/PAPER.
- Với policy mới, `SourcePolicyRevision.attribution` phải bằng đúng scope attribution. `retention_ref` vẫn là tham chiếu tới quy trình lưu giữ/xóa nội bộ; nó không thay thế `max_retention_days`. Rights hash giữ cả hai để reviewer có thể truy vết điều khoản và cách vận hành.
- Luồng admin có hai bước để không đưa scope qua header/query: tạo `rights_pending` revision mang typed scope + `evidence_ref`, sau đó `POST /{source_id}/revisions/{policy_id}/license-evidence` gửi body bytes thô. Route đọc scope/ref từ pending revision; `Content-Type` allowlist là `application/pdf`, `text/plain`, `text/html` hoặc `application/vnd.openxmlformats-officedocument.wordprocessingml.document`. Không lưu tên file. Cùng `Idempotency-Key` + cùng source/policy/scope/digest trả evidence cũ; cùng key khác digest hoặc payload trả conflict; cạnh tranh được bảo vệ bằng unique constraint.
- Upload tối đa 10 MiB; `Content-Length` nếu có chỉ là kiểm tra sớm, số bytes thực đọc vẫn là rào chắn. Body rỗng, content-type ngoài allowlist, disconnect, policy không còn pending, actor không có quyền/MFA/CSRF hoặc scope/ref không khớp đều không tạo evidence. Evidence upload và approval là hành động riêng; reviewer vẫn phải là người khác uploader của policy.
- Exact-scope check không xác nhận tính hợp pháp; provider/product/venue/contract và giới hạn retention/attribution vẫn là nội dung do uploader khai báo và reviewer đối chiếu với hợp đồng gốc. Mọi dimension chưa có dữ liệu authoritative trong một consumer làm consumer đó fail-closed.
- Python 3.14; FastAPI 0.156.4 và Starlette 1.9.0 hiện đã khóa trong dự án. Không thêm dependency.
- Giới hạn body được kiểm tra theo số bytes thực đọc, kể cả khi thiếu/sai `Content-Length`; request vượt 10 MiB dừng đọc và không ghi evidence. Client disconnect hoặc lỗi DB không để lại evidence.
- Evidence và policy mới có RLS FORCE, FK ghép workspace/source, append-only trigger và role grants tối thiểu. Không cấp admin quyền SELECT vào file hay kho ngoài.
- Kiểm thử unit cho canonical scope/hash, validation, chunk/boundary/empty/disconnect và domain transition; PostgreSQL Compose cho RLS, hash/fingerprint parity, append-only, workspace isolation, legacy policy denial, receipt/revocation và resolver TRAINING+PAPER. Playwright kiểm form tiếng Việt, responsive desktop/mobile, CSRF/MFA/idempotency, preview scope và không lưu file.

## Nguồn kiểm tra

- FastAPI Request reference, kiểm tra 2026-11-13: https://fastapi.tiangolo.com/reference/request/ — `Request.stream()` duyệt các chunk mà không giữ toàn bộ body trong bộ nhớ; `Request.body()` gom toàn bộ nội dung.
- Starlette Requests, kiểm tra 2026-11-13: https://www.starlette.io/requests/ — stream API và giới hạn request body; upload multipart có thể spool ra đĩa tạm, vì vậy thiết kế này không dùng multipart.
