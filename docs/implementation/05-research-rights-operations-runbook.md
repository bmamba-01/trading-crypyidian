# Runbook vận hành quyền dữ liệu nghiên cứu WP-05

**Trạng thái ngày 2026-11-02:** Đây là quy trình thao tác và lưu bằng chứng, chưa phải workflow vận hành đã nối vào ứng dụng. Chưa có publisher/endpoint nào được phê duyệt để fetch; connector, scheduler, HTTP/composition và pipeline research tiếp tục disabled. WP05C hiện chỉ lưu metadata/revision/takedown, không lưu body, excerpt, embedding hay blob.

Thuật ngữ source revision, as-of, data lineage và dataset manifest dùng theo [glossary EN→VI](../research/glossary-vi.md).

## 1. Phạm vi và nguyên tắc quyết định

Runbook áp dụng riêng cho từng publisher, endpoint, phương thức truy cập, loại dữ liệu, workspace và mục đích dùng. Quyền cho một mục đích không suy ra quyền cho mục đích khác. Nếu điều khoản, chủ thể cấp quyền, phạm vi dữ liệu hoặc thời hạn còn mơ hồ, giữ trạng thái `rights_pending` và không gọi endpoint.

Các trạng thái source dùng trong registry:

| Trạng thái | Hành động được phép |
|---|---|
| `discovered` / `rights_pending` | Ghi nhận thông tin tham chiếu và bằng chứng cần kiểm tra; không fetch, lưu body, trích xuất hay dùng làm feature/training. |
| `verified` | Chỉ thực hiện endpoint, phương thức, dữ liệu, mục đích và thời hạn đã được duyệt riêng; vẫn phải qua quota/use gate tại từng operation. |
| `blocked` / `revoked` / `expired` | Dừng fetch và mọi xử lý mới; rà soát dữ liệu/artifact đã tạo theo mục 4. |

## 2. Ma trận mục đích sử dụng

Mỗi mục đích phải được ghi rõ trong policy version và được kiểm tra độc lập. `allowed` dưới đây chỉ có nghĩa là policy có thể cấp quyền nếu bằng chứng của publisher chứng minh đúng phạm vi; đây không phải quyền mặc định.

| Mục đích trong policy | Nghĩa vụ kiểm tra | Mặc định khi chưa có bằng chứng riêng |
|---|---|---|
| `metadata` | Trường metadata được phép, API/method, quota và thời hạn lưu metadata. | Không truy cập nguồn. |
| `display` | Loại nội dung được hiển thị, nhóm người xem, attribution, vùng và giới hạn excerpt/caching. | Không hiển thị nội dung nguồn. |
| `embedding` | Quyền tạo/lưu vector từ đúng loại nội dung, thời hạn vector, cách tìm và xóa các vector phụ thuộc. | Không tạo embedding. |
| `training` | Quyền dùng đúng dữ liệu cho huấn luyện/đánh giá; retention; xử lý correction, revoke và artifact/model dẫn xuất. | Không đưa vào sample, manifest hoặc trainer. |
| `redistribution` | Người nhận, định dạng, trường/nội dung, điều kiện attribution và hạn chế chuyển tiếp. | Không export/chia sẻ dữ liệu nguồn. |

## 3. Thêm hoặc gia hạn một source

1. Ghi `source_id`, publisher/rightsholder, endpoint chính xác, HTTP method, auth scope, loại dữ liệu và workspace. Không dùng danh sách discovery/trader làm bằng chứng cấp quyền.
2. Lưu liên kết điều khoản/tài liệu chính thức, ngày kiểm tra, bản/đoạn liên quan và bản sao tham chiếu được phép lưu. Ghi rõ ai có quyền cấp, ai chịu trách nhiệm recheck, attribution, quota/rate/cost ceiling, retention/deletion deadline và các mục đích được phép.
3. Kiểm tra các giới hạn kỹ thuật trước khi bật: host allowlist, redirect cùng phạm vi đã duyệt, TLS, timeout, giới hạn byte/content type, secret scope, không log token/body, cursor/retry/backoff và quota. Không vượt paywall, login, robots, rate limit hoặc access control.
4. Tạo policy revision ở trạng thái `rights_pending` cho đến khi evidence đủ. Người tạo phải có quyền ghi; khi chuyển thành `verified`, reviewer phải có quyền duyệt, cùng workspace, và khác cả user lẫn membership của người tạo. Không nhận actor/reviewer ID từ request tự khai.
5. Phê duyệt chỉ chính xác các purpose, endpoint, method, dữ liệu, retention và thời hạn đã chứng minh. Đặt lịch recheck trước `valid_until`; nếu terms, endpoint, publisher, auth scope hoặc mục đích đổi thì tạo revision mới và duyệt lại trước lần dùng tiếp theo.
6. Trước mọi lần fetch, kiểm tra policy hiện hành còn `verified`, chưa hết hạn, đúng purpose và quota đã được reserve tại operation boundary. Không dựa vào policy object/cache cũ. Nếu thiếu integration để cưỡng chế các bước này, connector vẫn disabled.

## 4. Hết hạn, thu hồi, takedown hoặc sự cố

Thực hiện theo thứ tự dưới đây; dừng ở bước đầu tiên nếu còn bất định về quyền:

1. Chuyển policy sang `revoked`, `expired` hoặc `blocked` theo nguyên nhân; vô hiệu connector/scheduler/cursor và từ chối quota reservation mới trước lần fetch kế tiếp. Ghi người xử lý, thời điểm, lý do, phạm vi và tham chiếu yêu cầu/takedown; không ghi credential hay nội dung không cần thiết.
2. Đóng băng mọi thao tác tạo mới từ source đó: fetch, parse/extract, feature, embedding, dataset build, training, display và export. Quyền hiện hành phải được kiểm lại kể cả khi truy vấn replay dùng `as_of` trong quá khứ; replay cũ không mở lại quyền đã thu hồi.
3. Tìm artifact phụ thuộc theo source, item, revision và policy: body/blob/excerpt, cache/index/vector, event/feature, sample và dataset manifest, trainer run/model artifact, report, export và bản sao được quản lý. Nếu hệ thống chưa lưu lineage đủ để xác định tập ảnh hưởng, đánh dấu kết quả phụ thuộc là không đủ điều kiện sử dụng và chuyển cho người phụ trách quyền trước khi tiếp tục.
4. Theo điều khoản áp dụng, xóa nội dung phải xóa; nếu được giữ metadata tối thiểu cho audit, giữ tombstone/metadata cần thiết mà không giữ lại body. Xóa hoặc vô hiệu hóa cache/vector; tạo manifest mới loại sample bị ảnh hưởng; đánh dấu report/artifact downstream là cần rà soát, xóa hoặc tái tạo theo yêu cầu cụ thể. Không giả định hash, backup, model hoặc báo cáo tự động được miễn nghĩa vụ xóa.
5. Xác nhận từng nơi lưu trữ/consumer đã xử lý; ghi kết quả, thời điểm và bằng chứng kiểm tra. Chỉ mở lại sau policy revision mới, reviewer độc lập và kiểm tra lại endpoint/quota/retention; không tự bật lại theo retry.

Nếu có takedown khẩn cấp, bước 1 phải được thực hiện trước điều tra lineage. Giữ lại audit tối thiểu cần thiết để chứng minh đã tiếp nhận và xử lý yêu cầu, theo policy lưu trữ áp dụng.

## 5. Lineage, export và dựng lại manifest

- Mỗi dữ liệu hoặc artifact dẫn xuất cần giữ khóa tra ngược tối thiểu tới `source_id`, `item_id`, revision/hash, policy revision, purpose, thời điểm khả dụng và retention. Với item/policy đã lưu, `known_at` là thời điểm transaction observer thấy bản ghi đã commit (mốc bảo thủ), không phải thời điểm chính xác publisher/API công bố hoặc service gate phát hành.
- Trước export, kiểm tra `redistribution` cho đúng người nhận/định dạng/phạm vi, attribution, minimization và thời hạn. Nếu không được phép phân phối lại, chỉ xuất báo cáo/metadata đã được policy cho phép; không suy rằng workspace owner có thể chuyển tiếp dữ liệu nguồn.
- Khi rights/revision thay đổi, không sửa manifest cũ tại chỗ. Tạo manifest version mới, loại các sample không còn đủ điều kiện với reason, ghi nguồn/policy/revision/cutoff và digest mới; ngăn consumer dùng manifest cũ cho công việc mới. Đánh dấu trainer/report/artifact liên quan cần đánh giá lại theo điều khoản.
- Hiện chưa có service tự động truyền takedown sang WP-06 hoặc truy vết/xóa artifact dẫn xuất. Cho tới khi có adapter và E2E chứng minh coverage, quy trình truy vết phải được thực hiện bằng danh sách kiểm kê có người chịu trách nhiệm; training từ research source vẫn disabled.

## 6. Hồ sơ audit và checklist trước khi enable

Lưu trong workspace hạn chế quyền: source/policy revision; evidence URL và ngày kiểm tra; actor/reviewer đã xác thực; endpoint/method/auth scope; purpose; quota; attribution; retention/deletion deadline; lịch recheck; kết quả fetch/use được phép; takedown/revoke case; lineage/exclusion/manifest rebuild; và xác nhận xóa/ẩn. Nhật ký chỉ chứa metadata cần thiết, không chứa API token, secret, raw body hoặc excerpt ngoài quyền.

Chỉ chuyển connector khỏi `disabled` khi tất cả câu trả lời là **có**:

- Evidence chính thức áp dụng cho đúng publisher, endpoint, method, dữ liệu và purpose.
- Reviewer độc lập đã duyệt policy còn hiệu lực; attribution, quota, retention, deletion và người chịu trách nhiệm đã ghi.
- Host/auth/request limits và secret redaction đã được kiểm chứng trong adapter.
- Fetch boundary kiểm rights hiện hành và reserve quota atomically; revoke/expiry chặn request tiếp theo.
- Tombstone/takedown, lineage downstream, export và manifest rebuild có cách xử lý đã kiểm chứng.
- Unit/E2E bao phủ fail-closed, expiry/revoke, downstream eligibility, audit không lộ body/secret và khôi phục đúng policy mới.

**Quyết định hiện tại:** chưa có source nào có evidence đã được xác minh đầy đủ trong repo; vì vậy các mục enable chưa đạt và không được gửi request mạng hoặc lưu nội dung nhà cung cấp.
