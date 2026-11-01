# WBS WP-05 — Research ingestion có quyền và point-in-time

**Trạng thái:** WP05A registry persistence foundation đã có policy history, quota events, RLS/role split, DB-clock authorization, transition trigger và guarded downgrade. WP05C vẫn metadata/as-of contract thuần/offline. Chưa có source connector, takedown service hoặc pipeline research được enable.
**Ngày:** 2026-11-01
**Gói cha:** [Plan 05 — Research ingestion](../../implementation/05-research-ingestion.md); [kế hoạch tổng thể](./2026-10-21-independent-binance-futures-bot.md).
**Liên quan:** [WP-02 implementation plan](./2026-10-22-wp02-implementation-plan.md), [Plan 02](../../implementation/02-trading-core-paper.md), [Plan 06](../../implementation/06-ai-learning.md), [MB-05b runtime fanout](./2026-10-28-mb05b-runtime-fanout-wbs.md), [research source policy](../../research/execution-and-ingestion-2026.md), [glossary EN→VI](../../research/glossary-vi.md).
**Phụ thuộc:** Plan 01 và các hợp đồng market/event/as-of của WP-02. WP-03 không chặn nguồn research công khai/official; API riêng tư về tài khoản/lệnh thuộc WP-03 và nằm ngoài phạm vi này. Training trên research data phụ thuộc quyền và lineage ổn định từ WP-05; MB-08 phụ thuộc hợp đồng reference từ WP-05 và WP-06.

## Mục tiêu và ranh giới

Thu thập và chuẩn hóa market context, macro/news, attention và ý kiến công khai theo phương thức được phép; giữ nguồn gốc, policy, revision, retention/takedown và thời điểm hệ thống biết dữ liệu để replay đúng. External content chỉ là evidence/feature đầu vào; không trực tiếp tạo lệnh, sửa risk cap, train model hoặc promote model.

**Không thuộc phạm vi:** Binance private account/order API (WP-03); cào nội dung diện rộng, vượt paywall/đăng nhập/robots/rate/access-control; tự diễn giải nội dung thành lệnh; WP-06 labels/trainer/model registry/promotion; triển khai đầy đủ MB-08; bật market dispatch tự động trước khi adapter và readiness được kiểm chứng.

## Hợp đồng và hiện trạng đã kiểm tra

- Plan 02 có `event_at`/`received_at`/`available_at`, cursor, checksum/raw reference, revision và as-of cho dữ liệu thị trường; đây là mẫu thiết kế, không phải schema dành cho text/news.
- `SharedSignalEvidence` có source refs, hash, `observed_at`/`available_at`/validity; refs chưa thay thế source registry, quyền, item body/revision hoặc retention.
- `DecisionSnapshot` có macro evidence identifiers và freshness/unknown semantics; MB-05b chỉ hỗ trợ `DecisionSnapshotProvider` được inject. App mặc định hiện công bố `market_dispatch: not_configured`; không được xem WBS này tự làm dispatcher sẵn sàng.
- Domain research có pure rights-policy và revision metadata contracts; chưa có source registry persistence, collector, extractor hoặc test cho ingestion. Danh sách trader hiện có là discovery/reference, không phải nguồn đã được cấp quyền.
- Foundation WP05A hiện có `domain/research/source_policy.py::SourceRightsPolicy` với state, evidence reference, reviewed/valid-until, use-purpose; WP05C có `domain/research/source_items.py::SourceItemRevision` với timestamp được chuẩn hóa UTC, URI canonical và as-of eligibility. Đây chưa phải persistence, quyền được xác minh, takedown thực thi hay connector gate.
- WP-06 yêu cầu manifest pin input events, time boundary, rights, exclusions, hash và retention. Vì vậy WP-05 phải cung cấp lineage/takedown/eligibility trước khi research data được dùng làm training input.

## Lát cắt triển khai đầu tiên và cổng mở nguồn

**WP05-S1:** một nguồn lịch công bố/announcement chính thức, machine-readable, được duyệt cụ thể; metadata-first; không social feed, trader opinion, LLM extraction hoặc training. Nếu chưa chọn được nguồn có rights evidence hợp lệ, chỉ xây registry và fixture, connector production giữ disabled.

Trước lần gọi mạng đầu tiên cần: (1) xác định publisher, đúng endpoint và phương thức/auth scope; (2) xem điều khoản/tài liệu chính thức và ghi URL + ngày kiểm tra; (3) phân loại riêng quyền metadata/reference/embedding/training/redistribution; (4) xác định quota/rate/cost ceiling; (5) có owner/approver, attribution, retention/expiry, takedown/deletion và rebuild process; (6) hoàn tất giới hạn URL/redirect, kích thước, content-type, timeout và secret handling. Bất kỳ mục nào chưa rõ thì source ở `rights_pending`, không fetch hoặc lưu body.

## Bất biến về quyền, thời gian và dữ liệu

1. Connector mặc định disabled; chỉ policy `verified`, còn hiệu lực, đúng endpoint/method và đúng use case mới mở lịch fetch. `verified` cho reference không hàm ý quyền training/embedding/redistribution.
2. Metadata-first: không lưu body mặc định. Body/excerpt/blob chỉ lưu nếu quyền và thời hạn retention ghi rõ; blob tách khỏi metadata và phải có checksum, URI, expiry, deletion path.
3. Source policy/evidence có version, approver, checked/valid-until, endpoint, auth scope, quota, attribution, retention và allowed uses. `rights_pending`, `blocked`, `expired`, `revoked` không fetch, persist body, extract, tạo feature/evidence, embedding hay training.
4. Ghi riêng `published_at`, `edited_at`, `observed_at`, `available_at`, `event_at`, `expires_at`; datetime phải aware và causal. `available_at` là lúc hệ thống có thể biết dữ liệu, không thay bằng thời điểm xuất bản.
5. Revision/correction/deletion append-only, có hash và predecessor. Replay `as_of` chỉ trả revision khả dụng tại thời điểm đó; hiện trạng tôn trọng tombstone/takedown và retention.
6. Mỗi artifact dẫn xuất truy về source revision và policy version. Takedown dừng fetch trước, sau đó xóa/ẩn/đánh dấu ineligible blob, extracted event, embedding/dataset/model theo quyền; không dùng hash để mặc định miễn nghĩa vụ.
7. Nội dung ngoài là untrusted input. Parser/extractor không có tool/secret, có giới hạn byte/token/thời gian/schema; prompt injection hoặc lỗi parser chỉ được tạo `unknown/review`, không được tạo chỉ thị.
8. Thiếu, stale, outage, low coverage hoặc policy chưa rõ là `unknown/degraded`, không đồng nghĩa giá trị 0, “không có sự kiện” hoặc tín hiệu thuận lợi.

## WBS và tiêu chí nghiệm thu

| ID | Đầu việc | Tệp/module dự kiến | RED-first và tiêu chí nghiệm thu |
|---|---|---|---|
| WP05.0 | Chốt contract/source decision; map Plan02, evidence, DecisionSnapshot, registry CSV và WBS WP06. Chọn một S1 source hoặc quyết định fixture-only. | `docs/implementation/05-research-ingestion.md`, ADR/decision doc nếu cần, `docs/research/*` | Chưa gọi mạng. Có schema/state matrix, allowed-use matrix, threat/data-rights review, endpoint/method/quota/retention/owner cho S1. Candidate chưa đánh giá ở trạng thái disabled. |
| WP05A | Source registry, policy version, approval, quota/cost và lifecycle `discovered/rights_pending/verified/blocked/revoked/expired`; repository và least-privilege access. | **Foundation có:** `domain/research/source_policy.py`, `source_registry.py`, DB models/repository và migration `c6e8a0b2d4f6`; append-only policy/quota, RLS/roles, DB clock, transition trigger, self-approval inequality, downgrade guard. **Còn lại:** authenticated principal/approver service và connector gate. | RED/E2E: quyền theo purpose, idempotency/conflict, revoke blocks new use, quota bounds, cross-workspace/RLS, DB-level illegal transition/self-approval rejection, populated downgrade refusal. Approver fields vẫn free-text; application phải gắn authenticated reviewer. Chưa có worker/fetch boundary. |
| WP05B | Connector port/runtime cho đúng một nguồn S1: bounded request, allowlist, exact auth scope, quota/backoff/cursor, idempotent run, health/skip/error metadata. | `ports/research_sources.py`, `adapters/sources/<provider>.py`, application worker/scheduler, config | RED với transport fake: 429/Retry-After, timeout, auth expiry, malformed payload, cursor retry, restart, budget hit. Chặn host ngoài allowlist, redirect ngoài host, loopback/private/link-local/metadata IP, DNS rebinding, oversized body và content-type sai. Không log token/body. Không scraping/bypass. |
| WP05C | `SourceItem`/revision, dedup, canonical URL/source group, blob reference tùy policy, as-of query, tombstone/takedown. | **Foundation:** `domain/research/source_items.py::SourceItemRevision` + unit tests kiểm URL/timestamp/as-of/tombstone. **Còn lại:** group/dedup, repository/migration, storage, query/takedown service. | RED: duplicate/repost liên kết đúng; update tạo revision không overwrite; future/late revision ẩn khỏi as-of cũ; deletion/takedown ẩn current view; TTL xóa blob; checksum/lineage khớp; RLS và role collector đúng. `market_bar` không bị tái dùng cho text. |
| WP05D | News/macro event từ nguồn S1: event schema, timezone, source evidence span, extractor/schema version, confidence và review/unknown. | `domain/research/events.py`, extractor, repository/service, tests | Bắt đầu bằng parser deterministic; nếu sau này dùng model, giới hạn excerpt/token, tool-less, hostile-input separation và schema validation. RED: DST/zone ambiguity, correction late, invalid evidence offsets, contradictory item, malformed output, prompt injection, extractor outage. Event truy về source revision; lỗi thành `unknown/review`; không trực tiếp tạo order. |
| WP05E | Attention/trending sub-slice; ưu tiên tính từ market volume/OI/funding đã có quyền, chưa thêm social source. | `domain/research/attention.py`, formula/version, tests | RED: reproducible từ snapshot/formula version; freshness, baseline, coverage và missingness rõ; low coverage/outage là degraded, không zero; attention không bị hiểu thành hướng giá/chất lượng signal/order. Social/query provider cần rights review độc lập trước khi connector. |
| WP05F | Trader profile/opinion và reviewer feedback; chỉ sau source/channel rights và identity review. | `domain/research/opinions.py`, reviewer service/repository, tests | Giữ `verified_channel` khác `verified_performance`; forecast phải prospective với publish/observed/available times, asset/horizon/evidence; không hindsight. Disagreement/coverage giữ nguyên; reviewer correction append-only. Performance cần methodology và out-of-sample window. Chưa duyệt nguồn thì chưa ingest. |
| WP05G | Rights/privacy operations: runbook tiếng Việt, approval/recheck, attribution, retention, deletion/takedown, export và rebuild manifest; lineage dữ liệu dẫn xuất cho WP06. | `docs/research/` hoặc `docs/implementation/`, services/repositories, tests | RED: expiry/revoke dừng fetch; xóa/ẩn đúng body/excerpt/artifact theo policy; downstream research không còn eligible cho consumer; audit/rebuild giải thích được source-policy-revision; không body/credential trong log. WP06 không train nếu rights training false/unknown. |
| WP05H | As-of provider/evidence adapter và Compose integration. Chỉ nối MB-05 khi contract/freshness/coverage sẵn sàng. | research context provider, adapter tới `SharedSignalEvidence`/`DecisionSnapshot`, `app.py` chỉ khi đủ gate, unit + E2E | Replay cùng input/version xác định; source ref `available_at` không sau decision time; late revision không lọt replay; unavailable/stale/low coverage unknown; revoke/takedown không còn cấp dữ liệu mới. Readiness giữ `market_dispatch: not_configured` tới khi provider được cấu hình và gate đạt. E2E không gọi API ngoài hoặc exchange. |

## Threat model và data-rights cases

- Điều khoản, quota, giá, endpoint hoặc auth scope thay đổi; giấy phép hết hạn/thu hồi; quyền công khai chỉ cho đọc nhưng không lưu/train/phân phối.
- Vượt paywall/login, robots hoặc rate limit; redirect/SSRF/DNS rebinding tới private/internal/metadata endpoint; payload độc hại, parser bomb, HTML/RSS injection.
- Prompt injection, phishing/trading instructions trong bài; PII, yêu cầu xóa, bản quyền/excerpt/attribution; token hoặc raw body rò vào log.
- Publication/edit/delete/correction sai thứ tự, clock skew, timezone/DST; repost/quote/syndication, bot burst/astroturf làm giả coverage/consensus.
- Source outage so với giá trị thực bằng 0; thiếu/stale/low coverage; blob TTL/takedown không kéo được derived event/embedding/dataset/model.
- Tenant/process-role/RLS vượt scope; connector sử dụng dữ liệu hay mục đích khác rights grant; WP06 manifest thiếu exclusion/retention lineage.

## Kế hoạch RED-first và kiểm chứng khi triển khai

1. Pure unit: source policy/use state transitions, timestamp causality, revision/as-of, dedup, `unknown` semantics.
2. Repository/migration: append-only policy/revision, constraints/indexes, scope/RLS/process-role, retention/tombstone.
3. HTTP contract tests: fake transport only; rate/backoff/cursor/budget, SSRF/redirect/private IP, size/content-type/deadline, secret-redaction.
4. Extractor/security: hostile text/prompt injection, schema failure, invalid offsets, timezone ambiguity; không dùng live content fixture chưa có quyền.
5. Replay/leakage tests: late event/correction/deletion không xuất hiện trước `available_at`; WP06 training eligibility false khi quyền thiếu/hết hạn.
6. Compose E2E: verified fixture source → run/cursor → item revisions → event/evidence → as-of replay → revoke/takedown → xác nhận không cấp dữ liệu downstream; role/RLS/restart hoạt động. Không gọi provider thật trong test.
7. Khi đóng implementation: chạy unit/E2E liên quan, migration/DB-role, static và review; ghi lệnh/kết quả/mọi skip/flaky vào implementation-memory theo quy định dự án. Không xem bước chưa chạy là đạt.

## Trình tự, phụ thuộc và giới hạn vận hành

`WP05.0 → WP05A → WP05B/WP05C → WP05G tối thiểu → WP05D → WP05E/WP05F (từng nguồn được duyệt) → WP05H`.

- WP-03 không chặn WP-05 dùng nguồn công khai/official; mọi private exchange/account/order endpoint chờ WP-03 và không dùng credentials trong WP-05.
- WP-06 có thể làm scorer/DSL/ledger độc lập. Trước khi train bằng research data phải có WP05A/C/G: rights eligibility theo mục đích training, stable source revision/as-of, deletion/takedown propagation, exclusions và manifest/rebuild lineage. WP05D/E/F chỉ trở thành feature train sau khi rights/use/pit contract tương ứng đã rõ.
- MB-08 chỉ tiêu thụ approved immutable references/model releases sau WP-05/WP-06 contracts; không implement collector hoặc trainer thay hai module. Typed reference contract có thể độc lập nhưng không tuyên bố pipeline hoàn tất.
- WP05H có thể cấp context cho MB-05 nhưng không tự làm production dispatch ready; adapter phải được cấu hình và readiness phải phản ánh data freshness/coverage. Không tự bật execution/LIVE.
- Operational S1: một source, poll/batch thấp theo quota, request/byte/concurrency/cost budget tường minh, kill switch, TTL, metadata-first, connector disabled mặc định; lỗi mạng không tự chạy polling vô hạn.

## Nguồn chính thức đã kiểm tra — 2026-11-01

Kiểm tra trước khi triển khai WBS; đây là thông tin mutable nên phải xác minh lại đúng connector trước khi enable.

- X Developer Platform, billing theo credit/resource và pricing hiện hành: https://developer.x.com/?lang=en — không hardcode rate/giá; xác nhận console và quota tại thời điểm đăng ký.
- YouTube Data API `captions.download`, cần quyền edit video và quota cost 200: https://developers.google.com/youtube/v3/docs/captions/download — public video không tự cấp quyền tải transcript.
- Reddit Data API Terms (last revised 2026-07-20), quyền hạn chế/thu hồi, AI/ML training yêu cầu express permission, commercial/over-limit cần agreement, termination có xóa data/artifacts theo điều khoản: https://redditinc.com/policies/data-api-terms.
- Telegram Bot API/FAQ về update qua long polling/webhook, không phải API harvest tùy ý lịch sử kênh: https://core.telegram.org/bots/api và https://core.telegram.org/bots/faq.
- WP02 Binance public USDⓈ-M adapter là market-data foundation đã có; connector research cụ thể cần xem lại docs chính thức của đúng endpoint khi được chọn. Binance private account/order data thuộc WP-03, ngoài WBS này.
