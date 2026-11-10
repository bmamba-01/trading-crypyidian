# Phân tích sâu khoảng trống WP-05 — research rights và ingestion

**Ngày đối chiếu:** 04/11/2026 <br>
**Phạm vi:** chỉ đọc AGENTS/skill dự án, implementation memory, Plan 05, WBS WP-05, project status, source code và tên/contract các test liên quan. Không chạy test, không gọi provider, không đổi dữ liệu hoặc code. <br>
**Kết luận ngắn:** phần lớn nền hiện có là **control plane quyền nguồn và metadata-only ledger**. Nó có policy version, approval boundary, quota, lịch sử item, quyền as-of/takedown/expiry và downstream manifest invalidation. Đây chưa phải đường ingest sản phẩm: chưa có provider được duyệt, connector, fetch scheduler/cursor, route quản trị policy, event extraction, blob store hay deletion worker. Không được mô tả WP-05 là đã “ingest research” hoặc bật research feature.

## 1. Bằng chứng hiện có và ranh giới thật

| Nhánh | Đã có trong repo/evidence | Còn thiếu hoặc giới hạn |
| --- | --- | --- |
| **WP05.0 — chọn nguồn** | WBS yêu cầu một nguồn S1 cụ thể hoặc quyết định rõ fixture-only; có checklist endpoint, auth, quyền từng purpose, quota, retention và owner. | Chưa có nguồn S1 được chọn/duyệt. Điều khoản/API được WBS khảo sát ngày 01/11/2026; chưa re-check cho một endpoint/purpose thực tế. Không thể suy quyền từ nội dung public hoặc từ việc có REST/RSS. |
| **WP05A — registry/policy/quota** | Domain state/purpose và transition; persistence append-only, RLS, role split và DB clock; quota reservation/idempotency; `SourceRegistryService` lấy actor/reviewer từ `Principal`, buộc reviewer độc lập khi policy chuyển `VERIFIED` (`src/trading_bot/application/source_registry.py:53-135`). Unit `test_source_registry*`; Compose `test_wp05_source_registry.py` kiểm workspace, role, transition và quota cạnh tranh. | Service chưa được nối vào HTTP/admin/CLI composition. Không thấy router/admin route nào cho operator duyệt, xem history, revoke hoặc theo dõi quota. `evidence_ref`/policy fields là evidence do người dùng nhập; cơ chế xác minh tài liệu/điều khoản bên ngoài không tự thực hiện. Reviewer auth chứng minh ai bấm duyệt, không chứng minh grant pháp lý đúng. |
| **WP05B — connector/fetch** | Có `SourceRegistryService.reserve_usage()` và gate kiểm policy/purpose/quota; có REST backfill rights gate riêng cho WP07C (`application/binance_rest_backfill_rights.py`). | Chưa có `ports/research_sources.py`, adapter provider research, HTTP transport boundary, fetch run/cursor, lịch chạy, retry/backoff, host/redirect/SSRF/body-size guard hoặc telemetry của connector. `ResearchRightsMaintenanceWorker` không fetch; worker chỉ scan expiry và consume rights outbox (`workers/research_rights_worker.py:33-94`, app wiring `app.py:253-271`). Binance historical backfill là luồng WP07C riêng, không phải source-item connector WP05. |
| **WP05C — item/revision** | `SourceItemRepository` giữ metadata-only append-only revision, hash/predecessor, DB clock/receipt, retention/tombstone/takedown, RLS và quyền hiện hành/as-of. `select_access_envelope` trả lineage và lý do cho phép/từ chối. Compose `test_wp05_source_items.py` cover restart, tenant, raw SQL guards, observer, as-of và takedown. | Chưa có source item ingestion facade/HTTP/worker sử dụng repository; không có nội dung/body/excerpt/blob. `find_group` gom theo URL revision mới nhất giống **chuỗi URL chính xác**, không rewrite/canonicalize URL (`source_items.py:112-133`); vì vậy yêu cầu “canonical URL/dedup” chưa đồng nghĩa đã có parser canonicalization/cluster repost. Takedown metadata không thể xóa blob/artifact vì chưa có storage/deletion path. |
| **WP05D — news/macro event** | WP-02 `DecisionSnapshot` có macro evidence identifiers/unknown semantics; có typed source references cho manifest ở WP06. | Không có schema/provider news event, parser/extractor, event-time correction workflow, evidence offsets, review queue hoặc context adapter. `DecisionSnapshot` contract không tự tạo dữ liệu macro. Lỗi/thiếu nguồn phải tiếp tục là unknown; không thêm LLM/extraction cho tới khi có source và evidence policy. |
| **WP05E — attention/trending** | Có thể dùng domain market data OHLCV/OI/funding đã persist làm input offline, nhưng đó là khả năng nền, không phải WP05 score. | Không có công thức/formula version, baseline/seasonality, coverage/freshness/missingness model hoặc score tests. Sử dụng đầu vào này cho purpose mới vẫn cần rights policy tương ứng; không được mặc định research permission = demo/product. |
| **WP05F — trader opinion/profile** | Có directory/reference material trong docs. | Không có opinion/performance schema, identity review, prospective forecast ledger, reviewer correction, source-specific rights, methodology/OOS performance. Directory/candidate không phải verified identity/performance. Đây là nhánh rủi ro pháp lý/hindsight cao; chưa nên ingest. |
| **WP05G.1 — access envelope** | Access envelope authoritative ghép item revision, historical/current policy, `known_at` upper bound, DB evaluation time, purpose, rights hash, retention/takedown facts. Missing/unobserved lineage deny. | `known_at` là upper bound bảo thủ lúc observer thấy commit, không phải exact commit/gate publication. Hệ thống phải giữ semantics này; không dùng nó làm timestamp nguồn hoặc lời chứng exact. |
| **WP05G.2 — rights-change feed** | Trigger DB ghi source policy/item/takedown events vào transactional outbox; payload hạn chế/immutable. `test_wp05_rights_change_outbox.py` kiểm atomicity, retry, forgery guard và privacy. | Không backfill event cho history trước migration. Consumer chỉ thấy event được ghi sau khi feed tồn tại; legacy inputs cần quarantine/eligibility policy rõ. |
| **WP05G.3/WP06G.2 — expiry/invalidation worker** | DB-clock expiry scanner và rights-change consumer được nối trong cùng transaction/workspace tick của ingestion-worker; readiness phụ thuộc tick. Compose `test_wp05_expiry_scanner.py`, `test_wp06_rights_invalidation.py` và role matrix xác nhận scanner/event/invalidation/gate. `DatasetSourceEligibilityGate` fail-closed với pending event, invalidation hoặc mismatch quyền hiện hành (`application/dataset_source_eligibility.py:53-134`). | Đây là rights-maintenance worker một workspace cấu hình mỗi process, không phải generic ingestion scheduler. Không có erasure cho blob/dataset/model. Evaluator gate chưa đồng nghĩa có trainer/composition root đang dùng research data; project status vẫn ghi trainer/evaluator/report wiring còn mở. |
| **WP05H — as-of provider/context** | `SharedSignalEvidence`/`DecisionSnapshot` có contracts, MB-05b nhận provider qua injection; health vẫn có thể báo `market_dispatch: not_configured`. | Không có research context provider composition từ item/event → `DecisionSnapshot`, freshness/coverage readiness hoặc pipeline test offline→app. Không nên gắn với market signal dispatch trước khi nguồn event/provenance đủ. |

### Phân loại bằng chứng test

- Có unit/E2E thật cho quyền, append-only metadata, as-of/observer receipts, quota, outbox, expiry worker và invalidation; tên E2E hiện tại gồm `test_wp05_source_registry.py`, `test_wp05_source_items.py`, `test_wp05_rights_change_outbox.py`, `test_wp05_expiry_scanner.py`, `test_wp06_rights_invalidation.py`.
- Các kết quả được ghi trong memory/WBS là evidence lịch sử, không phải test chạy lại trong vòng phân tích này. Ví dụ worker rights 03/11 ghi 2341 unit, DB-role E2E 1 pass và bốn E2E rights/manifest pass; source-item observer round trước ghi 2103 unit và E2E 1 pass. Không suy chúng thành provider/ingestion E2E.
- Chưa có test nào chứng minh provider thật được phép dùng, connector lấy dữ liệu, content bị xóa khỏi storage, artifact/model được xóa, hoặc signal/AI sử dụng source ngoài. Fixture rights pass chỉ chứng minh cơ chế trong database.

## 2. Trạng thái tài liệu và câu cần làm rõ

1. **Implementation status nhìn chung đúng nhưng cần thống nhất wording.** `05-research-ingestion.md` mở đầu liệt kê nền rights và nói chưa có connector/blob/deletion; WBS WP-05 dòng trạng thái cũng ghi thiếu HTTP/connector/deletion; project status WP-05 ghi provider, HTTP, artifact propagation và macro/news/attention/opinion còn mở. Chỗ “WP05A còn DB-backed service integration” ở WBS acceptance hiện dễ đọc thành chưa có service/repository dù `SourceRegistryService` và adapter `PostCommitSourceObserver` đã có. Nên diễn đạt chính xác: policy repository/service và unit/DB boundary có; **production composition/HTTP control plane** chưa nối.
2. **WP05 ID không đồng nhất giữa Plan 05 và WBS WP-05.** `05-research-ingestion.md` gọi WP05B là normalize/dedup, WP05C news/macro, WP05D attention, WP05E trader opinions và WP05F rights; WBS WP-05 gọi WP05B connector, WP05C source-item, WP05D news/macro, WP05E attention, WP05F opinions, WP05G rights và WP05H provider. Báo cáo này theo ID của WBS mới hơn; khi cập nhật module plan/status nên ghi cả tên nhánh cùng mã hoặc đồng bộ mã để tránh giao nhầm task.
3. **WP05C dùng từ “canonical URL” rộng hơn code.** Repository giữ URL đã validation, group theo exact URL cuối, không chuẩn hóa URL hay merge repost. Nên giữ “exact validated URL grouping” cho hiện trạng; canonicalization/dedup provider-specific là việc sau khi source được chọn.
4. **WP05G không bằng erasure.** Feed/invalidation ngăn một số lần dùng mới khi event/policy hiện hành được gate, nhưng không xóa dữ liệu/artifact/model. Outbox lịch sử không được backfill. Mọi status phải nêu riêng `rights propagation` và `storage/derived-data deletion`.
5. **Registry `VERIFIED` không phải kết luận máy móc đã xác minh.** Principal/reviewer audit bảo vệ quy trình phê duyệt; hệ thống chưa kiểm nội dung hợp đồng, account/provider, endpoint scope hoặc hiệu lực pháp lý. Cần giữ review checklist và người phê duyệt có thẩm quyền cho nguồn được chọn.
6. **Từ “ingestion worker” dễ gây hiểu nhầm.** `ingestion-worker` hiện chạy maintenance rights/manifest invalidation theo workspace; không có fetch loop. Ghi rõ để không tạo ấn tượng hệ thống đang poll news/social.
7. **Nguồn/điều khoản còn cũ so với lần bật.** WBS reference đã kiểm X/YouTube/Reddit/Telegram 01/11/2026, nhưng không chọn provider S1; vì vậy không sử dụng các lookup đó như bằng chứng quyền hiện hành. Khi chọn source phải tra đúng điều khoản và endpoint chính thức vào ngày triển khai.

## 3. Thứ tự triển khai đề xuất

### P0 — Chốt quyết định nguồn, không mở network

Ghi WP05.0 thành một trong hai trạng thái: **fixture-only** cho tới khi có yêu cầu nguồn cụ thể, hoặc chọn chính xác publisher/provider, endpoint, method/auth scope, data fields, allowed purpose, quota, retention, attribution, reviewer/accountable owner. Trước mọi lần gọi mạng, kiểm tra terms/API docs chính thức hiện hành. Không đưa key/credential vào chat và không cấp `VERIFIED` bằng fixture.

### P1 — Hoàn thiện WP05A control plane offline

Nối `SourceRegistryService` vào authenticated admin API/CLI: đọc policy history/quota, tạo `discovered`/`rights_pending`, submit revision, dual-principal approve/revoke/block, và audit actor/reviewer. Không nhận `workspace_id`, `actor_id` hoặc `approved_by` từ browser. Unit + PostgreSQL E2E phải chứng minh owner/reviewer separation, tenant isolation, CSRF/access policy, audit/readback và quyền DB trước khi connector được xây. Đây là lát đầu hợp lý khi chưa có provider, nhưng route không được fetch.

### P2 — Một connector S1 qua fake transport trước network

Chỉ sau P0/P1: tạo `ResearchSourcePort` và coordinator điều phối **quota reserve → bounded fetch → parse/validate → append metadata/revision → commit/cursor**, với request ID/idempotency ổn định. Fake transport kiểm timeout, 429/retry-after, redirect/SSRF/private IP, content-type/byte/deadline, cursor resume, retry crash, quota exhausted và secret redaction. Runtime network disabled cho tới policy/account evidence được reviewer kiểm tra đúng endpoint/purpose. Không ghép REST backfill market data vào source-item pipeline.

### P3 — Item/event model cho đúng nguồn, metadata-only

Sau khi contract cụ thể được duyệt, triển khai parser deterministic theo schema của nguồn, URL canonicalization có quy tắc/version, dedupe/repost lineage, `published/edited/observed/available/event_at`, evidence span và correction/tombstone. Lưu metadata/hash/reference trước; body, excerpt, embedding và blob tiếp tục disabled cho đến khi retention và deletion operation được thiết kế/test.

### P4 — Rights erase và derived lineage trước khi lưu content

Chốt retention contract, blob object store interface, deletion receipt/idempotency, failure/retry/dead-letter, takedown propagation tới extracted event, manifest, report/artifact và model registry. E2E phải chứng minh revoke/takedown không chỉ làm evaluator deny mà còn thực hiện đúng hành động xóa/ẩn theo policy; kiểm trạng thái backup/export và rebuild. Không tuyên bố “đã xóa” nếu chỉ có ledger event.

### P5 — WP05D/H data product sau rights + item schema

Tạo structured event provider deterministic, review/unknown semantics và offline replay test late event/correction; sau đó mới wire context provider tới `DecisionSnapshot` và health freshness/coverage. Market dispatch giữ `not_configured` cho tới khi provider/readiness được chứng minh. WP05E trader opinions để sau vì identity/performance/hindsight/rate/rights cần contract riêng. WP05D attention có thể được làm như hàm thuần từ synthetic/as-authorized snapshot riêng, nhưng chưa đẩy vào PAPER/demo nếu purpose chưa được cấp.

## 4. Phụ thuộc và cổng quyết định

```text
chọn nguồn + quyền hiện hành
          ↓
WP05A operator control plane → WP05B fake-transport connector → WP05C item/revision
                                                           ↓
                                                WP05G erasure contract
                                                           ↓
                                        event/extractor → WP05H as-of provider
                                                           ↓
                                      WP06 source-dependent evaluation/training
```

- **Bị chặn bởi quyết định con người/provider:** quyền lưu body/excerpt, training, product/demo/redistribution; source credentials; legal/account evidence; reviewer có thẩm quyền. Không hỏi để tiếp tục fixture-only/API abstraction.
- **Có thể làm offline ngay:** authenticated registry read/revision API; fake provider contract; deterministic metadata parser fixture; lifecycle/test harness; lineages/erase receipt design. Không được mô tả offline contract như production source integration.
- **Không nên bắt đầu trước:** social/trader opinion collection, body/embedding storage, LLM extraction, model training từ source; các mục này phụ thuộc quyền và takedown/xóa dẫn xuất.

## 5. Nguồn và giới hạn nghiên cứu vòng này

- Dùng hiện trạng repo và evidence trong `docs/implementation/implementation-memory.md`, `docs/implementation/05-research-ingestion.md`, `docs/superpowers/plans/2026-11-01-wp05-research-wbs.md`, `docs/implementation/05-research-rights-operations-runbook.md` và code/tests liệt kê trong bảng.
- Không browse terms/API hiện hành: chưa có provider/endpoint được chọn để kiểm đúng. Các URL provider ở WBS là khảo sát 01/11/2026, không dùng làm phê duyệt hiện tại.
- Không thay đổi glossary; report dùng thuật ngữ WP05 đã có. Không chạy test nên mọi số liệu test đều gắn rõ là memory evidence lịch sử.
