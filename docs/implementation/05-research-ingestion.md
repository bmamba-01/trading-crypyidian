# Plan 05 — Research ingestion và phân tích thông tin

**Mục tiêu:** thu thập market context/news/trending/ý kiến công khai theo quyền, thời gian point-in-time và provenance; không dùng nội dung bên ngoài như lệnh giao dịch.
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), market snapshot/event schema từ [Plan 02](./02-trading-core-paper.md). WP-05 tạo dữ liệu nghiên cứu có quyền và provenance; WP-06 chỉ được dùng nguồn/feature đã qua kiểm tra quyền, thời điểm khả dụng và điều kiện xóa. WP-05 không sở hữu huấn luyện, đánh giá mô hình hay promotion của WP-06.
**Stories:** US-09…13, US-23.
**Tài liệu:** [research source policy](../research/execution-and-ingestion-2026.md), [macro data](../research/macro-market-data.md), [directory trader/source](../research/trader-source-directory.md), [glossary](../research/glossary-vi.md).

**WBS triển khai:** [WP-05 research ingestion WBS](../superpowers/plans/2026-11-01-wp05-research-wbs.md).

**Foundation hiện có:** `SourceRightsPolicy`, `SourceItemRevision`, `SourceItemHistory` append-only hash chain và `source_revision_access.py` giữ metadata/as-of/purpose/takedown contracts. WP05A có append-only policy/quota persistence, RLS/role split, DB-clock checks, transition guard và downgrade guard (`c6e8a0b2d4f6`); `application/source_registry.py` buộc thao tác qua Principal với quyền write/approve/use riêng. Quyền hiện hành đã revoke/expired/blocked chặn cả historical replay. WP05C đã có repository metadata-only, migrations `c9e1f3a5b7d0` → `d0f2a4c6e8b1` → `d1a3c5e7f9b2`, revision/hash/predecessor append-only, nhóm URL chính xác trong workspace/source, retention deadline và takedown bất biến. `select_accessible` nạp policy hiện hành cùng tombstone/takedown từ DB, dùng DB clock và gọi pure access gate; replay không phục hồi revision bị rút. Chưa có HTTP/composition wiring, verified provider rights, takedown propagation tới artifact dẫn xuất hay connector; quota events là reservation/estimate, chưa mở fetch boundary.

### Quy tắc chung về quyền nguồn và dữ liệu

1. Mặc định source ở trạng thái `discovered` hoặc `rights_pending`; connector phải **disabled** cho tới khi policy của đúng provider/endpoint/phương thức truy cập được duyệt và còn hiệu lực. `verified` chỉ cho phép các mục đích được duyệt rõ ràng, không mặc nhiên cấp quyền lưu body, tạo embedding, huấn luyện hay phân phối lại.
2. Registry version hóa evidence quyền, URL/điều khoản, ngày kiểm tra, người duyệt, endpoint, phương thức/auth scope, quota/rate, attribution, retention, expiry và các mục đích được phép độc lập: metadata, reference/display, embedding, training, redistribution. Không suy ra quyền từ việc nội dung xem được công khai hoặc có RSS/API.
3. Triển khai metadata-first: không ghi body mặc định. Chỉ lưu raw body/excerpt khi policy cho phép đúng nội dung và retention; tách blob khỏi metadata, gắn checksum/URI/TTL và thực thi xóa. Pending/blocked/expired/revoked không được fetch, lưu body, trích xuất, đưa vào evidence, embedding hoặc training.
4. Mọi bản ghi phải giữ thời gian nguồn và thời gian hệ thống biết nội dung riêng: `published_at`, `edited_at`, `observed_at`, `available_at`; sự kiện có `event_at` riêng. Thời gian có timezone, kiểm tra nhân quả/clock skew. Revision/correction/deletion là append-only; replay `as_of` chỉ thấy phiên bản có cả `available_at <= as_of` và thời điểm DB ghi nhận `recorded_at <= as_of`; timestamp nguồn backdate không chứng minh hệ thống từng biết row trước lúc insert.
5. Takedown/thu hồi quyền phải dừng connector ngay, lưu tombstone/audit cần thiết, xóa hoặc hạn chế blob theo điều khoản, và đánh giá toàn bộ artifact dẫn xuất (event, embedding, dataset, model) theo policy. Mỗi artifact downstream phải mang source/policy/revision lineage để WP-06 có thể loại bỏ hoặc dựng lại manifest.
6. Nội dung bên ngoài là dữ liệu không tin cậy, không phải chỉ thị cho hệ thống. Parser/extractor bị giới hạn kích thước, loại nội dung, thời gian và schema; không có tool/secret; lỗi, mơ hồ, stale hoặc thiếu coverage trả `unknown/review/degraded`, không tự tạo giá trị thuận lợi.

**Nguồn chính thức đã kiểm tra 2026-11-01 (điều khoản/API có thể đổi; kiểm tra lại trước khi bật từng connector):**

- X Developer Platform mô tả billing theo credit/resource và hiển thị giá hiện hành; cần kiểm tra console/quota tại thời điểm bật, không hardcode giá trong code: https://developer.x.com/?lang=en
- YouTube `captions.download` yêu cầu quyền edit video và có quota cost 200; video/caption công khai không đồng nghĩa được phép tải caption: https://developers.google.com/youtube/v3/docs/captions/download
- Reddit Data API Terms sửa đổi 2026-07-20; quyền API bị giới hạn/thu hồi, training AI/ML cần sự cho phép rõ ràng của rightsholder, và dữ liệu lưu/các artifact dẫn xuất chịu điều kiện xóa khi chấm dứt quyền: https://redditinc.com/policies/data-api-terms
- Telegram Bot API/FAQ mô tả cập nhật bot qua long polling/webhook, không phải quyền truy cập lịch sử tùy ý của mọi kênh: https://core.telegram.org/bots/api và https://core.telegram.org/bots/faq

## WP-05A — Source registry, quyền truy cập và quota

1. Với mỗi provider ghi source ID, publisher/account, endpoint, phương thức truy cập, authentication, quota, rate policy, terms/robots, retention, attribution và người phụ trách.
2. Chia trạng thái `verified`, `discovered`, `blocked`, `rights_pending`, `revoked`, `expired`; trạng thái ban đầu không được fetch. `verified` chỉ áp dụng cho endpoint, phương thức, thời hạn và các mục đích dùng cụ thể đã duyệt.
3. Chỉ enable connector sau policy pass; mặc định metadata-first, không ghi body hoặc train từ source chưa có quyền riêng cho mục đích đó. Không cào diện rộng, không vượt paywall/đăng nhập/robots/rate/access-control.
4. Thực thi quota/backoff, cursor, retry policy và lịch poll; lưu metadata skip/error để người vận hành thấy nguyên nhân.
5. Version policy thay đổi; hết hạn/thu hồi phải vô hiệu connector và cursor trước lần fetch kế tiếp, sau đó xác định nội dung và artifact/dataset/model cần xóa, đánh dấu ineligible hoặc dựng lại theo retention/license.

**Nghiệm thu:** connector disabled cho tới policy pass; pending/blocked/expired/revoked không fetch, không ghi body, không extract và không vào evidence/training; mỗi mục đích sử dụng có quyền riêng; quota 429 có backoff; mọi item có source/policy provenance và đường takedown.

## WP-05B — Normalize, deduplicate và sửa nội dung

1. Tạo `SourceItem` với canonical URL, hash, publisher, language, `published_at`, `edited_at`, `observed_at`, `available_at`, `expires_at`, source group, policy version và revision.
2. Phân biệt bài gốc, repost, quote, correction và deleted/updated item; tránh đếm bản sao thành nhiều ý kiến.
3. Với nội dung bị sửa/xóa, giữ lineage revision/tombstone, hash và thời điểm hệ thống nhận biết; replay chỉ thấy phiên bản đã khả dụng tại thời điểm đó, còn truy vấn hiện hành tôn trọng takedown.
4. Metadata-first; giữ raw body chỉ khi quyền/retention cho phép. Nếu không, chỉ lưu reference/hash hoặc excerpt tối thiểu được policy cho phép.
5. Tách metadata/event quan trọng khỏi blob; blob có checksum, storage URI và policy retention.

**Lát cắt WP05C metadata-only đã triển khai:** identity `(workspace_id, source_id, item_id)`; URL đã validate được giữ nguyên, nhóm theo URL revision cuối trong cùng workspace/source, không merge item khác source. Revision retry đồng nhất không ghi thêm, payload/predecessor/retention conflict bị từ chối; DB trigger kiểm thứ tự quan sát, predecessor và SHA-256. Ingestion chỉ được append revision; admin chỉ được thêm takedown; cả hai đọc metadata, không có UPDATE/DELETE. Runtime/trainer/gateway chưa được cấp quyền bảng này. Takedown có hiệu lực bền vững ngay với item chưa được ingest; tombstone trở thành hiện hành khi `available_at` đã tới. Retention deadline sớm nhất của item tiếp tục chặn replay, không được kéo dài bằng revision mới. DB trigger force `recorded_at`/`created_at` ở insert, kể cả raw SQL, và giữ thời gian này ngoài hash metadata. Hàng từ schema cũ chỉ được coi là đã biết từ lúc migration backfill, không dùng `created_at` caller cũ để dựng lại lịch sử. Trạng thái tombstone/takedown/retention hiện hành lấy từ toàn bộ history, không lọc theo cutoff. `take_down` refresh row sau flush để timestamp trả lần đầu bằng readback/retry. URL CHECK kiểm authority/host/port và URI chars/escape để raw SQL không bypass validation. Mọi TIMESTAMPTZ revision và takedown có CHECK `isfinite` + miền UTC từ năm 0001 đến hết 9999; NULL chỉ cho cột nullable. Timestamp vô hạn/BCE/năm 10000 không được tạo history Python không thể đọc; migration validate row cũ, fail rõ nếu sai và không sửa timestamp/hash. Không có body/excerpt/embedding/blob hoặc nguồn được fetch; metadata history/group phục vụ kiểm toán, không thay quyền sử dụng. HTTP/worker/WP06 verifier chưa nối.

**Nghiệm thu:** canonical duplicates liên kết cùng nhóm; revision không ghi đè nội dung lịch sử; truy vấn as-of chỉ trả phiên bản đã khả dụng; takedown loại nội dung khỏi truy vấn hiện hành và áp dụng retention lên blob/artifact dẫn xuất.

## WP-05C — News và macro catalyst

1. Ưu tiên nguồn chính thức/được cấp quyền: lịch công bố, cơ quan thống kê, ngân hàng trung ương, sàn, project/company announcement và RSS/API đã duyệt.
2. Extract loại catalyst, entity/ticker, vùng ảnh hưởng, thời hạn, polarity/stance nếu phù hợp, evidence offsets, confidence, model/version và trạng thái human review.
3. Lưu `event_at` riêng với `published_at`, `observed_at`, `available_at`; correction/revision, timezone và policy/source revision có lineage riêng.
4. Không gửi raw article prompt vô hạn; dùng trích đoạn cần thiết, giới hạn token, bảo vệ khỏi prompt injection, schema validate và provenance.
5. Tạo calendar/event risk context cho strategy; event context chỉ làm feature/circuit policy, không ra order trực tiếp.

**Nghiệm thu:** event truy ngược tới source revision/policy/evidence; late event không xuất hiện trong replay trước lúc bot nhận; takedown cập nhật eligibility; extractor lỗi thì đánh `unknown/review`, không bịa giá trị.

## WP-05D — Trending/attention

1. Định nghĩa input được phép: volume/price/OI/funding (exchange), query interest nếu API chính thức khả dụng, public social metrics nếu điều khoản cho phép.
2. Chuẩn hóa theo symbol, nguồn, seasonality, time window và baseline; tạo score kèm source coverage, freshness, missingness và formula version.
3. Deduplicate nội dung theo canonical item/source cluster; hạn chế bot/repost hoặc burst giả nếu có tín hiệu đủ.
4. Với nguồn thiếu hoặc down, đánh dấu `degraded/unknown`; không diễn giải số 0 là không có attention.
5. Tách “đang được chú ý” khỏi “hướng giá” và “chất lượng tín hiệu”; không gửi order từ trending score.

**Nghiệm thu:** score tái lập từ snapshot/version; low coverage bị gắn cờ; source outage không tạo false negative/positive mặc định.

## WP-05E — Trader profile/opinion và reviewer feedback

1. Nạp registry hiện có nhưng giữ nhãn `verified_channel` khác `verified_performance`; không gọi danh sách candidate là top trader có lợi nhuận.
2. Ghi public channel URL, nội dung tham chiếu, stance, asset, horizon, thời điểm dự báo, thời gian công bố/thu thập và evidence.
3. Chuẩn hóa dự báo có thể chấm được: hướng, time horizon, invalidation/target nếu công khai; không suy ngược dự báo từ nội dung sau kết quả.
4. Lưu bất đồng/coverage thay vì trung bình hóa thành consensus giả; mọi performance score phải có methodology và out-of-sample window.
5. Reviewer correction append-only, phân biệt đánh giá chất lượng luận điểm với PnL.

**Nghiệm thu:** mỗi opinion có attribution/link, policy/source revision và timestamp; chưa audited performance luôn hiển thị cảnh báo; reviewer correction không xóa source extraction ban đầu; thu hồi quyền làm opinion và các đánh giá phụ thuộc bị loại/xử lý theo policy.

## WP-05F — Rights, privacy và kiểm soát dữ liệu

Viết source runbook tiếng Việt cho thêm nguồn, kiểm tra terms, quota, retention/deletion, takedown, xuất dữ liệu và tái tạo dataset manifest. Thêm bảng lưu nội dung/mục đích được phép, thời hạn xóa, policy version, bằng chứng và người duyệt. Thu hồi quyền phải dừng fetch trước; sau đó xóa/ẩn dữ liệu và xử lý embedding, dataset, model hoặc báo cáo dẫn xuất theo đúng điều khoản.

**Kiểm tra khi triển khai:** fixtures cho policy gate, connector disabled khi `rights_pending`, dedup/revision/tombstone, timestamp as-of, prompt injection, retention/takedown và source outage; rà soát quyền theo từng provider/endpoint/mục đích trước khi bật connector.

## Quan hệ với WP-06 và MB-08

- WP-06 có thể phát triển scorer/DSL/labeling độc lập, nhưng mọi dataset dùng research context phải pin source/policy/revision, thời điểm khả dụng, mục đích `training` được cấp phép, exclusions và retention trong manifest. Nếu quyền chưa rõ, row bị loại khỏi training; WP-05 không tự tạo label, train, đánh giá hoặc promote model.
- Trước khi WP-06 train trên research data, WP-05 phải cung cấp hợp đồng ổn định cho `SourceItem` revision, as-of query, rights eligibility, takedown/lineage và manifest/rebuild. Thu hồi source phải cho phép WP-06 tìm data/artifact bị ảnh hưởng và loại bỏ hoặc dựng lại theo điều khoản; không mặc định rằng hash hoặc model artifact được miễn nghĩa vụ xóa.
- WP-03 Binance private account/order/reconcile không phải dependency của public/official research ingestion; WP-05 không đọc account credentials hoặc endpoint riêng tư. MB-08 chỉ tiêu thụ immutable, approved reference từ WP-05/WP-06 và không nhân đôi connector/trainer. MB-05 market dispatch vẫn `not_configured` cho đến khi adapter point-in-time và readiness/coverage gate được nối, cấu hình và kiểm chứng.
