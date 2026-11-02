# Plan 05 — Research ingestion và phân tích thông tin

**Mục tiêu:** thu thập market context/news/trending/ý kiến công khai theo quyền, thời gian point-in-time và provenance; không dùng nội dung bên ngoài như lệnh giao dịch.
**Phụ thuộc:** [Plan 01](./01-foundation-database.md), market snapshot/event schema từ [Plan 02](./02-trading-core-paper.md). WP-05 tạo dữ liệu nghiên cứu có quyền và provenance; WP-06 chỉ được dùng nguồn/feature đã qua kiểm tra quyền, thời điểm khả dụng và điều kiện xóa. WP-05 không sở hữu huấn luyện, đánh giá mô hình hay promotion của WP-06.
**Stories:** US-09…13, US-23.
**Tài liệu:** [research source policy](../research/execution-and-ingestion-2026.md), [macro data](../research/macro-market-data.md), [directory trader/source](../research/trader-source-directory.md), [glossary](../research/glossary-vi.md).

**WBS triển khai:** [WP-05 research ingestion WBS](../superpowers/plans/2026-11-01-wp05-research-wbs.md).

**Foundation hiện có:** `SourceRightsPolicy`, `SourceItemRevision`, `SourceItemHistory` append-only hash chain và `source_revision_access.py` giữ metadata/as-of/purpose/takedown contracts. WP05A có append-only policy/quota persistence, RLS/role split, DB-clock checks, transition guard và downgrade guard (`c6e8a0b2d4f6`); `application/source_registry.py` buộc policy write/approve/use qua Principal. WP05C đã có metadata-only `SourceItemRepository`, `source_item_revision`/`source_item_takedown`, observation receipts, retention deadline, append-only hash/predecessor guard, RLS/role split và DB-clock access selection (`c9e1f3a5b7d0` → `e0b2d4f6a8c1`). `select_accessible` kiểm policy history/receipt, expiry, purpose, tombstone, takedown và retention trong workspace; nó hiện chỉ trả revision entry, chưa trả rights decision/policy lineage cho WP-06. Receipt `known_at` là thời điểm observer thấy source đã commit, một upper bound bảo thủ; không phải exact commit hoặc thời điểm gate công bố. Chưa có policy/item outbox, expiry worker, source connector, WP06 verifier/reverse index hay downstream invalidation. Item revision hiện chưa ghim policy sequence; manifest WP-06 v1 giữ refs và hashes trong hai mảng opaque độc lập. Không có content/blob store.

### Quy tắc chung về quyền nguồn và dữ liệu

1. Mặc định source ở trạng thái `discovered` hoặc `rights_pending`; connector phải **disabled** cho tới khi policy của đúng provider/endpoint/phương thức truy cập được duyệt và còn hiệu lực. `verified` chỉ cho phép các mục đích được duyệt rõ ràng, không mặc nhiên cấp quyền lưu body, tạo embedding, huấn luyện hay phân phối lại.
2. Registry version hóa evidence quyền, URL/điều khoản, ngày kiểm tra, người duyệt, endpoint, phương thức/auth scope, quota/rate, attribution, retention, expiry và các mục đích được phép độc lập: metadata, reference/display, embedding, training, redistribution. Không suy ra quyền từ việc nội dung xem được công khai hoặc có RSS/API.
3. Triển khai metadata-first: không ghi body mặc định. Chỉ lưu raw body/excerpt khi policy cho phép đúng nội dung và retention; tách blob khỏi metadata, gắn checksum/URI/TTL và thực thi xóa. Pending/blocked/expired/revoked không được fetch, lưu body, trích xuất, đưa vào evidence, embedding hoặc training.
4. Mọi bản ghi phải giữ thời gian nguồn và thời gian hệ thống biết nội dung riêng: `published_at`, `edited_at`, `observed_at`, `available_at`; sự kiện có `event_at` riêng. Thời gian có timezone, kiểm tra nhân quả/clock skew. Revision/correction/deletion là append-only; replay `as_of` chỉ thấy phiên bản có cả `available_at <= as_of` và receipt quan sát sau commit có `known_at < as_of`. `created_at`/`reviewed_at`/`recorded_at` là audit/provisional, không phải bằng chứng commit hoặc lịch sử gate đã publish.
5. Expiry phải bị chặn đồng bộ bằng DB clock tại mỗi access/fetch/use boundary; expiry worker chỉ fan out sự kiện, không phải lớp duy nhất bảo vệ quyền. Policy revoke/block/purpose change và item takedown phải phát sự kiện bền vững cùng transaction ghi nguồn. Hiện `select_accessible` chặn source read, nhưng chưa có connector/cursor để dừng và chưa có downstream consumer.
6. WP-06 manifest giữ nguyên vì immutable. Invalidation phải là trạng thái/event append-only riêng và consumer phải recheck rights trước build/use; không sửa canonical manifest bằng tay. Chỉ tuyên bố downstream removal khi có lineage index, storage/retention contract và deletion operation đã nghiệm thu.
7. Nội dung bên ngoài là dữ liệu không tin cậy, không phải chỉ thị cho hệ thống. Parser/extractor bị giới hạn kích thước, loại nội dung, thời gian và schema; không có tool/secret; lỗi, mơ hồ, stale hoặc thiếu coverage trả `unknown/review/degraded`, không tự tạo giá trị thuận lợi.

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

**Ranh giới thực thi hiện có:** `SourceItemRepository.select_accessible` đọc DB clock, policy observation receipts, item observation receipts, tombstone, takedown và retention rồi trả revision khi gate cho phép. Policy hết hạn/revoke chặn truy vấn đồng bộ, nhưng không có expiry scheduler, event fan-out hoặc connector/cursor để dừng. Policy/item writes hiện không enqueue rights-change event.

## WP-05B — Normalize, deduplicate và sửa nội dung

1. Tạo `SourceItem` với canonical URL, hash, publisher, language, `published_at`, `edited_at`, `observed_at`, `available_at`, `expires_at`, source group, policy version và revision.
2. Phân biệt bài gốc, repost, quote, correction và deleted/updated item; tránh đếm bản sao thành nhiều ý kiến.
3. Với nội dung bị sửa/xóa, giữ lineage revision/tombstone, hash và thời điểm hệ thống nhận biết; replay chỉ thấy phiên bản đã khả dụng tại thời điểm đó, còn truy vấn hiện hành tôn trọng takedown.
4. Metadata-first; giữ raw body chỉ khi quyền/retention cho phép. Nếu không, chỉ lưu reference/hash hoặc excerpt tối thiểu được policy cho phép.
5. Tách metadata/event quan trọng khỏi blob; blob có checksum, storage URI và policy retention.

**Lát cắt WP05C metadata-only đã triển khai:** identity `(workspace_id, source_id, item_id)`; URL đã validate được giữ nguyên, nhóm theo URL revision cuối trong cùng workspace/source, không merge item khác source. Revision retry đồng nhất không ghi thêm, payload/predecessor/retention conflict bị từ chối; DB trigger kiểm thứ tự quan sát, predecessor và SHA-256. Ingestion chỉ được append revision; admin chỉ được thêm takedown; cả hai đọc metadata, không có UPDATE/DELETE. Runtime/trainer/gateway chưa được cấp quyền bảng này. Takedown có hiệu lực bền vững ngay với item chưa được ingest; tombstone trở thành hiện hành khi `available_at` đã tới. Retention deadline sớm nhất của item tiếp tục chặn replay, không được kéo dài bằng revision mới. DB trigger force `recorded_at`/`created_at` ở insert, kể cả raw SQL, và giữ thời gian này ngoài hash metadata. Legacy rows nhận `barrier_txid` của migration và không receipt; chỉ được dùng sau khi transaction riêng finalize bằng mốc quan sát mới. Không dùng audit timestamp cũ để dựng lại lịch sử commit. Trạng thái tombstone/takedown/retention hiện hành lấy từ toàn bộ history, không lọc theo cutoff. `take_down` refresh row sau flush để timestamp trả lần đầu bằng readback/retry. URL CHECK kiểm authority/host/port và URI chars/escape để raw SQL không bypass validation. Mọi TIMESTAMPTZ revision và takedown có CHECK `isfinite` + miền UTC từ năm 0001 đến hết 9999; NULL chỉ cho cột nullable. Timestamp vô hạn/BCE/năm 10000 không được tạo history Python không thể đọc; migration validate row cũ, fail rõ nếu sai và không sửa timestamp/hash. Không có body/excerpt/embedding/blob hoặc nguồn được fetch; metadata history/group phục vụ kiểm toán, không thay quyền sử dụng. HTTP/worker/WP06 verifier chưa nối.

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

### Tự động hóa expiry, revoke và takedown xuống WP-06

1. **WP05G.1 — Access envelope:** mở rộng `SourceItemRepository.select_accessible` để trả revision cùng decision reason, purpose, as-of/DB evaluation time, item `known_at`, historical policy id/sequence đã xét, current policy id/sequence, và retention/takedown facts. Thông tin lấy từ cùng workspace-scoped repository; không nhận quyền/tombstone/retention do caller khẳng định. Sequence policy khi item được ghi chưa persist; policy lineage phải được bổ sung và gate phải từ chối legacy revision không thể ràng buộc an toàn. `known_at` tiếp tục mang upper-bound observer semantics, không được gọi là exact gate visibility.
2. **WP05G.2 — Rights-change feed:** dùng transactional `OutboxEvent` hiện có, không tạo event table trùng chức năng. DB trigger/transaction path ghi event khi append policy revision, item revision/takedown; payload immutable, chỉ trạng thái delivery/lease được cập nhật. Event chứa workspace, source/item/revision/policy sequence, loại thay đổi và reason code, không body/secret. Unique idempotency key + `ConsumerReceipt` hỗ trợ retry/at-least-once. Quyền raw INSERT phải không bypass event generation.
3. **WP05G.3 — Expiry scanner:** worker query mọi policy grant `valid_until <= clock_timestamp()` theo workspace và append một outbox event idempotent cho từng policy sequence. Không sửa policy history sang `expired`; operation gate đã deny tức thời bằng DB clock. Scanner trễ/hỏng không được mở read/fetch/use. Policy renewal không làm event/grant cũ biến mất.
4. **WP06G.1 — Manifest lineage:** WP-06 sở hữu version manifest mới có tuple theo từng sample/source ref ghép `source_id/item_id/revision_id`, policy id/sequence đã xét, purpose và rights hash. Hai mảng `source_revision_refs`/`source_rights_hashes` v1 được normalize riêng nên không ghép chính xác; legacy research-dependent sample không map được phải quarantine/exclude, không phỏng đoán. WP06 thêm `dataset_manifest_source_ref` reverse index cùng transaction với canonical manifest append.
5. **WP06G.2 — Invalidation và consumer gate:** WP-06 sở hữu append-only `dataset_manifest_eligibility_event`/invalidation ledger và rights-event consumer. Consumer dùng source refs để append trạng thái invalid; trainer/evaluator phải kiểm event mới nhất lẫn live WP05 access envelope ngay trước mỗi use. Manifest canonical/hash không đổi. Consumer lag, dead letter hoặc thiếu source lookup thì fail closed.
6. **Giới hạn hiện tại:** chưa có outbox event source-rights, expiry scanner, WP05→WP06 verifier, manifest reverse index, invalidation consumer, trainer hoặc model registry. Do đó chưa thể liệt kê manifest/model bị ảnh hưởng; chưa có model unlearning/deletion. Không có body/blob repository hoặc deletion worker. Nếu license đòi xóa cả metadata/hash/ref nhưng manifest immutable chưa có retention/erasure design, không ingest/train source đó.

## Quan hệ với WP-06 và MB-08

- WP-06 có thể phát triển scorer/DSL/labeling độc lập, nhưng mọi dataset dùng research context phải pin source/policy/revision, thời điểm khả dụng, mục đích `training` được cấp phép, exclusions và retention trong manifest. Nếu quyền chưa rõ, row bị loại khỏi training; WP-05 không tự tạo label, train, đánh giá hoặc promote model.
- Trước khi WP-06 build/train bằng research data, cần WP05G.1-G.3 và WP06G.1-G.2: access envelope authoritative, outbox + expiry events, manifest ref/policy pairing, reverse index, append-only invalidation và live consumer gate. WP-06 hiện có `DatasetManifest` immutable nhưng chưa có source-ref index/trainer gate; hai arrays refs/hashes không ghép per revision, nên propagation chưa khả thi. Không mặc định hash/manifest/model được miễn nghĩa vụ xóa; nếu cần xóa lineage, chờ retention/erasure contract WP-06.
- WP-03 Binance private account/order/reconcile không phải dependency của public/official research ingestion; WP-05 không đọc account credentials hoặc endpoint riêng tư. MB-08 chỉ tiêu thụ immutable, approved reference từ WP-05/WP-06 và không nhân đôi connector/trainer. MB-05 market dispatch vẫn `not_configured` cho đến khi adapter point-in-time và readiness/coverage gate được nối, cấu hình và kiểm chứng.

## Hợp đồng observer sau commit — 2026-11-02

Source item và policy revision có `barrier_txid xid8` do DB ép bằng `pg_current_xact_id()`. Receipt append-only chỉ được tạo từ transaction top-level khác đã thấy source row commit; cả savepoint trong transaction ghi cũng bị từ chối. DB force `known_at`, kiểm predecessor/clock regression, RLS/workspace và role: item receipt chỉ ingestion ghi, policy receipt chỉ admin ghi; cả hai đọc được metadata, runtime/trainer/gateway không được cấp quyền.

`known_at` là **thời điểm một transaction observer nhìn thấy source đã commit**, một upper bound bảo thủ cho source commit. Đây **không phải exact commit time**, không phải commit time của receipt và **không chứng minh chính xác khi nào API/service gate đã publish hoặc visible**. Transaction ghi receipt có thể commit sau `known_at`; historical as-of hiện biểu diễn **observer-knowledge time**, không tái lập exact service availability. Cutoff bằng known_at bị loại; known_at + 1µs chỉ đủ điều kiện nếu mọi gate khác đạt. Nếu tương lai cần replay đúng gate-publication time, contract hiện tại chưa đáp ứng yêu cầu đó.

`SourceRegistryService` bắt buộc port `append_revision_and_observe`; không nhận raw transaction-scoped repository. Adapter `PostCommitSourceObserver` thực hiện ghi → commit thật → transaction mới finalize, giữ auth actor/reviewer ở application boundary. Quota cần factory ingestion tường minh, không mặc định sang role admin. App append drain từng page có cursor tiến; recovery/backfill có after_sequence/limit, predecessor-before-successor và latest reread trước khi báo complete. Lỗi sau source commit được raise; source/policy vẫn inaccessible tới retry/recovery. Retry append duplicate tiếp tục finalization và không đổi known_at đã ghi.

Thiếu receipt/gap hoặc latest policy chưa finalized đều fail closed; không fallback policy VERIFIED cũ. `reserve_quota` kiểm chuỗi policy receipt trước duplicate-return; duplicate không cấp phép fetch. Current tombstone/takedown/retention vẫn lấy từ toàn raw history, không bị lọc theo cutoff. Policy audit timestamps giữ nguyên; knowledge map riêng dùng để chọn policy historical. Mọi `create_database_engine` pin TimeZone UTC và giữ timeout options.

Không bật hoặc dùng track_commit_timestamp: PostgreSQL yêu cầu cấu hình server và vacuum có thể xóa lịch sử. Nguồn kiểm 2026-11-02: [transaction ID/top-level và commit information](https://www.postgresql.org/docs/18/functions-info.html), [track_commit_timestamp](https://www.postgresql.org/docs/18/runtime-config-replication.html), [MVCC read committed](https://www.postgresql.org/docs/18/transaction-iso.html). Chưa có provider/HTTP/connector/WP06 verifier composition hoặc quyền content thật.
