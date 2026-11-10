# WP-03 Binance USDⓈ-M Execution Implementation Plan

> **Trạng thái:** Cập nhật 2026-11-02; WP-03B có durable venue intent/transition ledger, PostgreSQL repository, bot-scoped RLS/FK, DB-backed `VenueOrderLedgerPort` adapter và Compose E2E đã đạt. `VenueOrderReconciliationService` qua verifier port kiểm scope/freshness/fill monotonicity và ghi transition verified; E2E fake verifier xác nhận partial fill vào audit, `ORDER_ABSENT` giữ UNKNOWN. Adapter chưa được wire vào composition/worker và chưa có verifier venue thật. Chưa có transaction bridge ghi open-risk rồi release/transfer an toàn; venue-linked risk vẫn bị giữ. Caller-provided evidence hash không thể release/transfer hoặc chuyển `UNKNOWN→RETRYABLE`. PAPER không liên kết venue vẫn giữ luồng cũ. HMAC process-local chưa có rehydrate contract. Chưa có production transport, secret provider, outbox bridge, authenticated stream/protection hoặc order Demo. Quyết định 2026-11-02: demo tạm thời trong sản phẩm dùng PAPER simulator/DB ledger; chạy trên Binance Futures Demo thật là phạm vi WP-03E riêng và được hoãn.

**Goal:** Bổ sung execution adapter USDⓈ-M Demo với intent bền vững, reservation không mất khi trạng thái chưa rõ, protective order và reconciliation có thể phục hồi; LIVE tiếp tục bị khóa.

**Architecture:** Domain/application chỉ gọi port trung lập. Adapter Binance sở hữu SDK/HTTP signing, host allowlist, parser REST/WebSocket và mapping venue. PAPER ledger/reservation/state machine là nền nhưng phải phân biệt dữ liệu PAPER và venue. `ALGO_UPDATE` cùng REST algo-order API quản lý conditional orders; lệnh thường sau trigger tiếp tục được reconcile bằng order/trade events. Outbox đảm bảo ghi ý định trước khi gọi mạng, còn retry bên ngoài cần idempotency/reconcile riêng.

**Tech Stack:** CPython 3.14, FastAPI, SQLAlchemy async, PostgreSQL 18, Alembic, pytest/pytest-asyncio, Ruff, mypy, Docker Compose. SDK và HTTP transport chưa được chọn; chốt sau capability/dependency/license review theo tài liệu hiện hành. Không thêm dependency chỉ để bắt đầu contract/offline work.

**Spec:** [WP-03 Binance execution](../../implementation/03-binance-execution.md)

## Trạng thái WBS tại 2026-11-01

| WP | Trạng thái | Bằng chứng hiện có | Còn thiếu để nghiệm thu |
| --- | --- | --- | --- |
| WP-03A capability/adapter | Đang triển khai một phần (offline) | `BinanceDemoTarget`, signed MARKET/LIMIT submit + query qua protocol fake-only; kiểm tra HTTP status/network failure và HMAC form/query. | SDK/license matrix, production HTTP transport, secret provider, credential provenance, readiness và app-boundary E2E còn thiếu. |
| WP-03B lifecycle/UNKNOWN | Đang triển khai một phần (domain + DB ledger + reconciliation application/adapter) | Intent/reservation HMAC proof, guarded lifecycle, error classifier, fake transport; migration/repository ghi intent, bot/workspace/account/reservation/settings scope, idempotency/client ID, DB transition graph, append-only audit và reservation locking. DB ledger port bind workspace/bot/account, translate command và sequence-guard ghi transaction. Compose E2E fake verifier xác nhận partial fill append transition nhưng giữ reservation UNKNOWN; absent giữ nguyên transition count. | Adapter chưa nối composition/worker và verifier venue thật chưa có. Chưa có transaction bridge ghi open-risk rồi transfer/release reservation an toàn. HMAC proof process-local nên row load không tái tạo domain intent sau restart; cross-process submit claim, history/stream reconcile, fill dedupe, protection và recovery còn mở. |
| WP-03C stream/protection | Chưa bắt đầu | PAPER simulator có partial-fill/cancel remainder/protective exit; account activity port có classifier. | `ORDER_TRADE_UPDATE`/`ACCOUNT_UPDATE`/`ALGO_UPDATE`, venue partial fill, algo lifecycle, protect đúng quantity và reject recovery. |
| WP-03D startup/reconcile | PAPER recovery nền; venue chưa bắt đầu | PAPER hydrate/checkpoint verify, runtime lease/fencing, foreign activity incident persistence. | Snapshot venue, event gap/reconnect reconciliation, readiness gate venue, orphan/mismatch handling/runbook. |
| WP-03E Binance Futures Demo run | Tạm hoãn theo quyết định 2026-11-02 | Flow tạo account/key và REST/WS host đã được nghiên cứu; hiện chưa cần account/key để chạy demo nội bộ. Hai file Binance secret local còn rỗng; không có handshake hay venue order nào được chạy. | Mở lại sau khi WP-03 readiness/risk/host gates đạt và có account/key riêng do owner cấp. Khi mở lại, triển khai secret scope tách khỏi PAPER, read-only handshake opt-in, rồi mới xét order scenarios và sanitized evidence. |

**Hiện có thể làm:** Task 0–2 contract inventory, offline adapter/test doubles, parser fixtures, schema/error contracts, state machine và fault simulation. Không cần gửi request tới Binance.

**Binance Futures Demo execution vẫn đóng:** PAPER/risk gate và daily/rolling loss-window semantics còn mở theo project status. Demo nội bộ hiện dùng PAPER simulator, không cần mở gate venue/API. Không dùng review tài liệu này như bằng chứng đã mở gate Binance.

## Global Constraints

- Chỉ USDⓈ-M Demo/testnet. LIVE bị loại khỏi enum/config/runtime và không thể được adapter tự chọn.
- Binance docs gọi Demo REST host là `https://demo-fapi.binance.com` và WS host là `wss://demo-fstream.binance.com`. Production host là host khác. Chỉ cặp Demo đã pin được phép trong mode Demo; mode, account ID, credential reference và hosts phải được kiểm tra cùng nhau trước readiness.
- Binance FAQ hướng dẫn người dùng đăng nhập `demo.binance.com`, mở Futures Demo, rồi tạo key tại API Management trong môi trường đó; availability phụ thuộc quốc gia/vùng. Người vận hành tự tạo key, không gửi secret qua chat. Key Demo cần secret reference/file riêng; tuyệt đối không dùng hai placeholder `binance_api_key`/`binance_api_secret` đang mount cho execution-gateway profile `paper`.
- Có thể kiểm tra API availability bằng signed USER_DATA read-only (`GET /fapi/v3/account`) sau khi Demo key được cấp và adapter/secret boundary pass. Đây không phải mở gate gửi order. Chỉ cấp trade permission cho key Demo ở bước test order có chủ đích; không cấp withdrawal.
- Không đưa key, secret, signature, listen key, raw private payload hoặc thông tin account nhạy cảm vào log, audit, fixture, report hay evidence package. Listen key không được persist như credential lâu dài.
- Không gọi mạng/đặt lệnh sàn trong unit test; contract test dùng transport fake/fixture. Demo tests riêng biệt, explicit opt-in, chỉ sau gate và account/key được xác nhận.
- Real Demo credential smoke phải là opt-in riêng, mặc định tắt, dùng credential Demo riêng và chỉ GET; kiểm tra timeout/redaction/account response schema. Không chạy test này như một phần mặc định của CI và không đưa response/account ID vào log/artifact.
- Ghi intent/outbox và risk reservation trước side effect ngoài DB. DB outbox receipt không bảo đảm exactly-once với API Binance.
- HTTP 503 “Unknown error…” và timeout sau khi request có thể đã tới sàn được giữ `UNKNOWN`; query bằng stream/order history trước retry. Phân loại riêng các 503 Binance mô tả thất bại; backoff khi 429, dừng khi 418, chỉ áp dụng `Retry-After` nếu endpoint/response hiện hành xác nhận header và ý nghĩa của nó.
- `UNKNOWN` giữ reservation; không release theo TTL khi đã submit. Chỉ release sau bằng chứng `ORDER_ABSENT`; chuyển sang exposure trong transaction không được tạo khoảng hở giữ risk.
- Conditional orders dùng Algo Service (`/fapi/v1/algoOrder`, query/cancel/open/all algo endpoints) và `ALGO_UPDATE`, không đặt stop/TP/trailing qua regular-order endpoint.
- Entry gate đóng khi startup, stream gap/expiry, account snapshot stale/incomplete, unknown order, missing/rejected protection, mismatch hoặc foreign activity chưa xử lý. Protective/reconcile không phụ thuộc Admin Web, Telegram, research hay AI.
- Không tự adopt, cancel hoặc flatten foreign orders/positions. Cap breach không tự flatten; tuân theo risk/account policy đã duyệt.
- Mọi thuật ngữ giao dịch/AI mới xuất hiện trong code hoặc tài liệu phải được thêm EN→VI, nguồn tham khảo vào glossary trong implementation round tương ứng. WBS này chỉ định nghĩa ngắn để mô tả contract, chưa thay glossary.
- Không commit trước khi unit và E2E liên quan đều chạy đạt, memory được cập nhật/readback và review độc lập đóng.

## Dependencies and execution order

```text
WP-02 PAPER lifecycle + risk semantics ───────────────┐
WP-03.0 baseline/contract freeze ─> 03.1 API audit ─> 03.2 host/credential boundary
                                                    └> 03.3 intent + UNKNOWN + reservation
                                                        └> 03.4 stream/fills/algo protection
                                                            └> 03.5 startup/reconcile/readiness
                                                                └─[gates]─> 03.6 Demo contract run
WP-05/06 analysis ───────────────────── parallel; no direct implementation dependency
All WP-03 tasks ─> review, evidence, project-status/memory update, completion decision
```

Có thể làm song song các contract/fixture không cùng ownership; migration và state-machine integration phải được tuần tự hóa sau contract freeze. Không chạy nhiều Compose E2E dùng chung Docker cùng lúc. Demo account execution là cổng riêng, không được suy ra từ contract tests hoặc mock.

## Review Focus

- Safety: API 503 unknown không retry mù; reservation không rơi; protection được xác nhận ở sàn; no LIVE fallback.
- Consistency: REST ack, fills, regular order update, algo update, account update và position snapshot hội tụ cùng trạng thái không lùi.
- Isolation: database workspace/account scope, RLS, process role, credential/mode/host, log redaction và không adopt foreign activity.
- API correctness: đúng API version và testnet support; fields/status/enum/rate limits/order retention/fill history; conditional migration và Algo Service.
- Recovery: restart, stream expiration/disconnect, partial fill trong lúc thay stop, trigger rejection, stale snapshot, rate-limit và account mismatch.
- Evidence: phân biệt kết quả fake/offline với Demo quan sát được; không tuyên bố simulator hỗ trợ scenario nếu chưa có evidence.

## Task 0 — Baseline và contract freeze (có thể làm ngay; offline)

**Files:**
- Read: `docs/implementation/implementation-memory.md`, `docs/implementation/03-binance-execution.md`, `docs/implementation/02-trading-core-paper.md`, Plan 01, sequence SD-06/09, risk WBS, relevant code/tests.
- Modify documentation at implementation time: WP-03 spec/WBS, `project-status.md`, `implementation-memory.md`, glossary nếu có thuật ngữ mới.
- Tests inspected: `tests/unit/test_paper_lifecycle.py`, `test_paper_lifecycle_checkpoints.py`, `test_risk_reservations.py`, `test_paper_reservation_lifecycle.py`, `test_account_activity.py`, `test_account_activity_guard.py`, `test_runtime_readiness.py`; E2E runtime/risk/account activity suites.

- [ ] **Step 1: Capture a clean baseline** for unit tests and impacted existing E2E before code. Record actual pass/fail/skip/time; diagnose flaky failures first. This step is for implementation kickoff, not evidence generated by this planning update.
- [ ] **Step 2: Inventory actual runtime wiring.** Record current migration head, `PaperLifecycleCoordinator`, risk reservation repository, outbox/UoW, runtime lease/fence, AccountActivityPort, adapter bootstrap and readiness dependencies.
- [ ] **Step 3: Trace every WP-03 acceptance to a port, state transition, storage owner and test.** Separate current PAPER behavior from missing venue behavior.
- [ ] **Step 4: Resolve the pre-Demo risk gate with WP-02.** Specify day/rolling loss windows/timezone/reset/freshness and how Demo config is constrained; verify RiskEngine enforces the chosen policy. A document-only waiver does not unlock Demo.
- [ ] **Step 5: Freeze contract ownership and migration sequence.** Reuse existing order/fill/position/risk tables where semantics fit; propose additive venue identifiers/events only after schema audit; preserve workspace RLS/role grants and immutable lineage.

**Acceptance:** baseline/evidence are recorded for execution kickoff; no tests are claimed from this analysis; every existing component and gap has an owner; WP-03 Demo gate has an explicit pass/fail checklist; one migration head plan is documented.

## Task 1 — WP-03A.1: Official API capability and SDK audit (có thể làm ngay; read-only)

**Files:**
- Create/update at implementation time: `docs/superpowers/plans/2026-11-01-wp03-execution-wbs.md` capability appendix/table; dependency inventory only if SDK is added.
- Tests: API schema fixtures under `tests/fixtures/binance_usdm/`; no network test required.

**Interfaces:** endpoint matrix separates public read, signed USER_DATA, TRADE, user-stream management, WebSocket account events, regular orders and algo conditional orders. Include endpoint path/method, mode support, auth permission, rate limit, timestamp/recvWindow, request/response enums, history windows, event source and recovery endpoint. Audit current official SDK version/API coverage and licensing; choose SDK only if event and Algo Service semantics are represented correctly. If HTTP transport fallback is needed, it stays inside `adapters/binance_usdm/`.

- [ ] **Step 1: Verify official USDⓈ-M General Info, REST catalog, User Data Streams and Change Log.** Capture checked date **2026-11-01** (re-check at implementation if docs have changed); record production/testnet endpoints, signed-request rules, status/error variants, conditional-order migration date and account-stream event schemas.
- [ ] **Step 2: Enumerate endpoints.** At minimum `exchangeInfo`; account/balance/position risk; create/query/cancel regular order; trade history; account configuration needed by readiness; mark/funding if risk/equity consumes them; listen-key lifecycle; user-data stream; create/query/cancel/open/history conditional algo orders.
- [ ] **Step 3: Verify retention, pagination and consistency limits** for orders/trades/positions and all snapshot endpoints; define which data source is authoritative for each reconcile decision.
- [ ] **Step 4: Compare official SDK to the matrix.** Record exact package/version/license/API gaps; do not add dependency until this review demonstrates a benefit and compatibility.
- [ ] **Step 5: Add glossary EN→VI/source links** for any new trading/API term that enters implementation or product documentation.

**Acceptance:** every required operation maps to official current documentation and a parser/schema fixture; there is no unresolved mismatch between regular and algo order semantics; unsupported Demo capabilities are labeled unknown rather than assumed.

### Task 1 next offline deliverable — response/event parser and fixtures

**Status:** chỉ chốt contract offline theo audit nguồn chính thức đã ghi trong mục này ngày 2026-11-02. Không tra cứu mới, không gọi Demo/production API, không chọn SDK và không bổ sung claim về endpoint support. Trước khi biến các trường cần tra cứu thành code/fixture đạt, chạy lại kiểm tra nguồn chính thức hiện hành và ghi ngày/URL.

**Files:**
- Parser/DTO implementation sau này: `src/trading_bot/adapters/binance_usdm/demo_rest.py`, module mới `src/trading_bot/adapters/binance_usdm/order_schemas.py` (nếu tách DTO giúp không trộn schema; tránh sửa domain `VenueOrderIntent` để phản ánh raw Binance enums).
- Unit tests sau này: `tests/unit/test_binance_usdm_order_schemas.py`, `tests/unit/test_demo_rest_adapter.py`.
- Fixtures sau này: `tests/fixtures/binance_usdm/regular_order_accepted.json`, `regular_order_partial.json`, `regular_order_unknown_status.json`, `algo_order_new.json`, `algo_order_triggered.json`, `algo_update_rejected.json`, `order_trade_update_partial.json`, `account_v3_minimal.json`.
- Không ghi account identifier, API key, secret, signature, listen key hoặc response lấy từ tài khoản vào fixture. Dùng symbol/order IDs giả lập và ghi fixture metadata: endpoint, method, API family, source URL, checked date, scenario, các trường đã lược bỏ.

**Raw schema và parser boundary đã biết từ audit:**

| Hợp đồng | Trường/enum đã được audit ghi nhận | Parser contract offline | Chưa được phép đóng băng nếu chưa tra lại nguồn |
| --- | --- | --- | --- |
| Regular order submit/query (`POST/GET /fapi/v1/order`) | `symbol`, `orderId`, `clientOrderId`, trường status regular order và `executedQty` được matrix ngày 2026-11-02 ghi nhận; adapter hiện gửi `MARKET` hoặc `LIMIT`. | Tạo `RegularOrderResponse` riêng; dùng các trường này làm schema candidate, nhưng exact required set phải đối chiếu endpoint page trước khi fixture thành golden. Query parser giữ `executedQty` là Decimal string hữu hạn/không âm và không vượt intent quantity. Raw state field được tách khỏi state projection nội bộ. | Audit 2026-11-02 không lưu exhaustive enum status. Cần tra lại enum regular status (gồm `EXPIRED_IN_MATCH`/`REJECTED` nếu supported), field `type`, `updateTime`, `avgPrice`, `origQty`, response variants theo `newOrderRespType`, retention và error body. Adapter hiện yêu cầu `updateTime` khi query; chưa được xem là official-required field cho tới khi tra endpoint page. |
| Conditional algo submit/query (`POST/GET /fapi/v1/algoOrder`) | Algo family dùng định danh riêng `algoId`, `clientAlgoId`, và ID lệnh thường sau trigger `actualOrderId`; response/status phân biệt algo với regular order. Các conditional types trong audit: `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`, `TRAILING_STOP_MARKET`. Field trigger được gọi là `triggerPrice`; Change Log 2026-10-23 tăng max `callbackRate` trailing stop lên 20. | Tạo `AlgoOrderResponse` riêng; parse algo ID/client ID/status/order type ở namespace riêng. `actualOrderId` là optional/unavailable trước trigger theo lifecycle và chỉ dùng để nối sang regular order sau khi có evidence. Không đưa algo status vào parser `_ORDER_STATUS` của regular order. | Đầy đủ required/optional fields, exact status casing/field name theo REST response, semantics absent/zero/null của `actualOrderId`, parameter constraints (`quantity`, `closePosition`, `reduceOnly`, working/trigger types), client ID rule, Demo support và các response lỗi. Không lấy schema Portfolio Margin `/papi/...` thay USDⓈ-M `/fapi/...`. |
| Algo lifecycle stream (`ALGO_UPDATE`) | Event family riêng; audit ghi các lifecycle status `NEW`, `CANCELED`, `TRIGGERING`, `TRIGGERED`, `FINISHED`, `REJECTED`, `EXPIRED`. | Tạo `AlgoUpdateEvent`/parser riêng. `NEW` xác nhận điều kiện được đặt; `TRIGGERING`/`TRIGGERED` yêu cầu query/reconcile regular order/position; `FINISHED` không tự đồng nghĩa fill; `REJECTED`/`EXPIRED`/`CANCELED` không bảo đảm vị thế đã đóng. | Exact WS nesting/casing, event ID/time fields, location of `algoId`/`clientAlgoId`/`actualOrderId`, rejection reason code and duplicate identity need fresh User Data Stream/schema lookup before creating accepted raw JSON fixture. |
| Regular order stream (`ORDER_TRADE_UPDATE`) | User Data Stream audit xác nhận event family riêng cho regular order update; regular status và fill quantities được đối soát với order thường. | Fixture/parser tách khỏi algo: event chỉ có thể cập nhật regular order/fill; event status/type không nhận từ enum algo. Cumulative fill phải monotonic/idempotent, mỗi fill/trade không được cộng lặp. | Exact envelope/nested `o` field names, trade ID, `lastFilledQty`/`cumFilledQty`, fee and timestamp keys/status enum must be rechecked in official stream schema before the fixture is considered authoritative. |
| Account read smoke (`GET /fapi/v3/account`, `USER_DATA`) | Audit 2026-11-02 xác nhận đây là proposed first signed read-only Demo connectivity smoke; không gửi order. | `account_v3_minimal.json` chỉ được thêm sau khi ghi chính xác các account fields cần cho readiness/equity. Trước đó contract fake có thể test `HTTP 200 + JSON object` và sanitized account scope mà không giả định field nghiệp vụ. | Exact balance/position fields, equity semantics, `availableBalance`, margin/unrealized/realized inclusion, optional arrays, permission behavior and Demo availability. Chưa dùng raw schema này để định risk/equity. |

**Enum boundary bắt buộc:**

- Raw DTO chia riêng `RegularOrderType` và `ConditionalAlgoOrderType`; không có một `OrderType` enum dùng chung cho submit/parse hai REST family.
- Với adapter hiện tại, regular submit enum chỉ hỗ trợ `MARKET` và `LIMIT`. Không suy ra các type khác được phép trên regular `/fapi/v1/order`; conditional type bị từ chối cục bộ tại boundary trước khi transport được gọi.
- Algo enum riêng gồm năm conditional type được Change Log/REST audit ghi nhận. Enum này không được chấp nhận ở `DemoUsdMRestAdapter.submit_order`.
- `RegularOrderStatus` và `AlgoOrderStatus` tách riêng. Audit 2026-11-02 không ghi enum regular đầy đủ; khóa exhaustive enum trước khi code. Chuỗi raw unknown, field bắt buộc thiếu, sai kiểu hoặc status không hợp lệ không được ép thành `REJECTED`, `CANCELED` hay `ORDER_ABSENT`; parser trả sanitized contract error, caller giữ intent/reservation `UNKNOWN` và đóng entry gate tới reconcile.
- Internal `VenueOrderState` là projection, không phải venue enum. `ALGO_UPDATE.TRIGGERED`/`FINISHED` không map thẳng sang `FILLED`; chỉ linked normal order/fill evidence có thể xác nhận quantity/terminal execution. `ALGO_UPDATE.REJECTED` báo conditional protection không thành công và phải tạo missing-protection path.
- Không đặt acceptance vào `RETRYABLE` chỉ vì schema parse fail. Sai/mới schema là unknown contract, phải giữ risk và dừng entry; retry API semantics được kiểm riêng bởi classifier hiện có.

**Test matrix sau này:**

1. Valid regular `MARKET NEW`, regular `LIMIT NEW`, regular partial query, regular filled/canceled terminal examples dùng các fields có nguồn chính thức.
2. Valid algo `NEW` trước trigger và event/query sau trigger có cả algo ID lẫn linked regular-order ID khi schema chính thức xác nhận; mỗi parser từ chối fixture của family kia.
3. Reject conditional type gửi qua regular adapter và regular `MARKET`/`LIMIT` gửi nhầm vào algo parser trước transport; assert transport call count bằng 0.
4. Unknown enum, thiếu required ID, `orderId`/`algoId` sai kiểu, Decimal malformed/nonfinite/negative, fill > requested, client/symbol mismatch, invalid event timestamps và malformed/non-object JSON đều fail closed, không lộ raw body.
5. Optional/unknown additive fields trong schema đã được audit không làm hỏng parser; breaking enum/required-field change không bị bỏ qua.
6. `ALGO_UPDATE` `REJECTED`, `EXPIRED`, `CANCELED`, `TRIGGERING`, `TRIGGERED`, `FINISHED` có assertion mapping/routing riêng như trên; không giả lập payload field chính xác cho tới khi có official schema snapshot.

**RED/GREEN sequence khi implement:** chốt official schema snapshot trước; viết fixtures tối thiểu chỉ từ trường đã xác minh; viết failing parser tests; implement raw DTO → canonical adapter result; chạy suite mục tiêu offline; thêm regression cho mỗi field mismatch; chỉ sau khi pass và independent review mới wire parser vào composition. Parser fixture không phải bằng chứng Binance Demo endpoint hỗ trợ order/stream.

**Acceptance/failure modes:** regular/algo enums và IDs không thể lẫn; unverified fields được đánh dấu follow-up thay vì được khẳng định; malformed/unknown schema không giải phóng risk hoặc tự chuyển thành reject; fixture traceable tới official USDⓈ-M page, API family, checked date và scenario; không phát sinh network call/credential requirement.

## Task 2 — WP-03A.2: ExchangeGateway, host/key boundary and signed readiness (contract/offline first)

**Files:**
- Create/modify under `src/trading_bot/ports/` and `src/trading_bot/adapters/binance_usdm/`; domain models under `src/trading_bot/domain/execution/` only if port-independent semantics are missing.
- Update only the runtime configuration/bootstrap/readiness modules required by the actual code graph.
- Tests: unit adapter contract/parser/signature/redaction tests; offline E2E around application wiring. No credential-based E2E until Demo gate.

**Interfaces:** introduce a venue-neutral `ExchangeGateway` port with explicit account/mode scope and operations from the capability matrix. Keep transport and Binance DTOs private to adapter. Resolve `mode`, exchange, `account_id`, credential reference, REST base URL and WS base URL from authoritative published settings/secret provider—not caller payload. Use allowlisted host constants/config; refuse Demo mode with production hosts or absent/ambiguous credentials. Readiness reports public-data, signed USER_DATA, TRADE permission, clock-skew and stream status separately; never log secret/signature/listen key.

- [ ] **Step 1: Write failing tests** for host/mode/key mismatch, absent/invalid secret reference, unknown host, production host in Demo, Demo host in any future LIVE mode, and transport not constructed before validation.
- [ ] **Step 2: Write failing contract tests** for decimal parsing, filters, enum/status, account scope, malformed/unknown fields, signed parameter serialization and response redaction.
- [ ] **Step 3: Implement pure config/contract validation and fake transport first.** Public data adapter is not a substitute for private execution adapter.
- [ ] **Step 4: Implement signed transport only after SDK/API audit.** Use server-time offset and bounded `recvWindow`; never retry the same signed TRADE request on ambiguous outcome from inside transport.
- [ ] **Step 5: Add readiness probes** that can prove environment/key scope without sending an order; classify insufficient permission, IP restriction, clock drift and stale metadata as not ready.
- [ ] **Step 6: Add offline app boundary E2E** proving no credentials/network are required and a malicious settings/caller override cannot change host/account/mode.

**Acceptance:** all contracts pass with injected transport; no production host or key can be reached in Demo mode; domain has no adapter imports; redaction is verified from captured logs/exceptions; readiness fails closed on each missing prerequisite.

## Task 3 — WP-03B.1: Durable venue intent, idempotency and reservation bridge

**Files:**
- Reuse `application/paper_lifecycle.py`, `domain/risk/reservations.py`, risk repositories and existing outbox/UoW where contracts match; add venue-specific application coordinator/adapter model only after audit.
- Add schema/migration only if an immutable venue receipt/identifier cannot fit existing records. Include workspace/account scope, indexes, RLS and least-privilege grants.
- Tests: `tests/unit/test_venue_order_lifecycle.py`, `test_venue_order_idempotency.py`, reservation tests; PostgreSQL Compose E2E using the runtime DB role.

**Interfaces:** durable intent owns local idempotency key, canonical request fingerprint, client order ID, account, mode, risk reservation, plan/config lineage, submit attempt ID and state. Outbox event means “submission requested,” not “exchange accepted.” Client ID is deterministic/unique in local account scope and translated to Binance allowed shape; local idempotency is authoritative because venue client-ID uniqueness is limited to venue rules/open orders. Every request has a bounded attempt record without secret material.

- [ ] **Step 1: Add RED domain tests** for legal transitions, duplicate intent/same payload, same key/different payload conflict, event de-dup, cumulative fills monotonicity and no state rollback on stale event.
- [ ] **Step 2: Add RED reservation tests** proving reserve-before-submit; accepted order retains/transfers exposure safely; pre-submit cancellation can release; UNKNOWN keeps risk beyond intent expiry; only reconcile can resolve UNKNOWN.
- [ ] **Step 3: Implement canonical intent and state transition service** without Binance imports or I/O.
- [ ] **Step 4: Add PostgreSQL idempotency/concurrency E2E** for competing worker attempts, outbox redelivery, transaction rollback, workspace RLS, and reservation transfer atomicity.
- [ ] **Step 5: Verify all external call side effects happen after commit** and callback failure cannot publish an in-memory state that DB rolled back.

**Acceptance:** same intent cannot submit twice under retry/race; conflicting payload is rejected; no UNKNOWN path releases risk automatically; state, actor, source and time are durable; database rollback leaves runtime snapshot unchanged.

## Task 4 — WP-03B.2: Submit/query/cancel and ambiguous API outcomes

**Files:** regular order operations in Binance adapter; application submission/reconciliation worker; API result/error taxonomy; unit transport fault fixtures.

**Interfaces:** normalized results distinguish acknowledged, definitive rejection, definitive failure-not-sent, throttled, rate-banned, and unknown. A generic 503 is not enough to determine retry. Parse Binance message/code variants: “Unknown error…” is UNKNOWN; documented “Service Unavailable” / internal processing failure is failure where docs say safe to resend, with bounded backoff. HTTP client timeout/reset after bytes may have been sent is UNKNOWN. 429 requires backoff; stop requests on 418; apply `Retry-After` only when the endpoint/response contract confirms the header semantics; expose degraded readiness rather than hammer.

- [ ] **Step 1: Add RED fault cases** for timeout before connect, timeout after request write, server 408, each documented 503 body, malformed 5xx, 429 with/without Retry-After, 418 and definitive 4xx rejection.
- [ ] **Step 2: Implement result classification** from official response behavior and retain sanitized response fingerprint/request attempt metadata.
- [ ] **Step 3: On ambiguous submit, mark intent and reservation UNKNOWN; do not retry.** Query by client ID/order ID, read user-stream event and recent trades; repeat reconciliation with bounded schedule.
- [ ] **Step 4: Implement cancel/query independently.** Cancel acknowledgment is not proof no fill raced; fetch/reduce against cumulative execution and fills.
- [ ] **Step 5: Test timeout-after-accept** with fake venue that accepts then drops response; prove one venue order after recovery. Also test order absent then safe retry using same local intent policy.
- [ ] **Step 6: Test API rate-limit handling** with deterministic clock/sleep injection; no real wait/network in unit suite.

**Acceptance:** no duplicate after ambiguous acceptance; retry only after authoritative absence; cumulative quantity/fill and cancel race reconcile to venue truth; a documented `Retry-After` value is observed when present; no unsafe 503 blanket-retry.

## Task 5 — WP-03C.1: User stream lifecycle and canonical venue events

**Files:** private stream port/client and adapter; stream supervision in runtime worker; event normalizer; unit fixtures from official docs.
- Create/modify tests: `tests/unit/test_binance_usdm_user_stream.py`, `test_venue_event_reducer.py`; stream-disconnect E2E after offline state contract.

**Interfaces:** listen-key lifecycle is a credential-bearing boundary with in-memory handling and redacted metrics only. Event envelope stores source event type, venue event/transaction time, receive time, account scope, event ID/trade ID/order ID/client ID, schema version and sanitized canonical facts. Same-type same-connection `E`/`T` ordering can inform reducers; reconnect means gaps are possible, not proof of continuity. Consume `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `ALGO_UPDATE`, listen-key expiry and relevant rejection/update events.

- [ ] **Step 1: Test parsing/order semantics** for normal order fills and statuses, account/position changes, algo lifecycle status including TRIGGERING/REJECTED/EXPIRED, duplicate ID, unknown enum/field, stale timestamps and listenKeyExpired.
- [ ] **Step 2: Implement listen-key create/renew/close**, secret-safe URL construction, heartbeat and proactive 24-hour connection rotation based on current official docs.
- [ ] **Step 3: Implement connection supervision** that closes entry gate immediately on stream loss/expiry and requests reconcile before gate can reopen.
- [ ] **Step 4: Handle out-of-order events** via monotonic cumulative fill and terminal-state rules; never use event time alone to discard a distinct fill trade.
- [ ] **Step 5: Simulate disconnect/reconnect and event gap offline** to prove event replay plus REST snapshot converges idempotently.

**Acceptance:** no secret in logs/URL telemetry; stream expiry and 24-hour disconnect are handled; reconnect closes entry gate; repeated and late events do not double fills or regress state; stream recovery requires reconciliation.

## Task 6 — WP-03C.2: Partial fill and protective Algo Service orders

**Files:** adapter algo operations; application protection coordinator; persistent relation for entry intent ↔ venue algo order ↔ triggered normal order; incident integration. Update glossary EN→VI for “algo order/conditional order” if not already present when implementing.

**Interfaces:** create/modify/cancel/query `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`, `TRAILING_STOP_MARKET` only through current Algo Service endpoints. Keep algo identifier/status distinct from `orderId` generated after trigger. Track requested/confirmed/triggered quantity, side, position side, `workingType`, trigger/activate price, reduce-only/close-position semantics, client algo ID, and linked fills. Prove stop coverage by exchange state, not local intent.

- [ ] **Step 1: Write RED protective policy tests** for entry unfilled, partial fill, successive fill, cancel remainder, entry cancellation race, full exit, partial exit and stop trigger during replacement.
- [ ] **Step 2: Verify each parameter constraint against official Algo Service REST spec.** In particular `closePosition`, `quantity`, `reduceOnly`, hedge/one-way mode, trigger source, conditional limits and Demo support. Do not infer these from Portfolio Margin docs.
- [ ] **Step 3: Implement algo adapter/parser** and schema fixtures; keep conditional state separate from normal order lifecycle.
- [ ] **Step 4: Implement protection coordinator** so confirmed exposure increase triggers protection update; only exchange ACK/state confirms coverage; temporary uncovered quantity blocks new entries and raises urgent incident.
- [ ] **Step 5: Implement conservative cancel/replace sequencing** and post-action reconciliation. If safe replacement cannot be established, use configured reduce-only/close-position path only when account mode and official constraints validate it; otherwise keep incident/gate closed for operator decision.
- [ ] **Step 6: Test trigger rejection** (`ALGO_UPDATE.REJECTED` and any documented reason event); do not rely on the deprecated conditional-reject event after migration.
- [ ] **Step 7: Test orphan protection cleanup** only after confirmed zero position and authoritative no-fill/no-open-trigger evidence.

**Acceptance:** every open filled quantity is covered by confirmed protective policy; rejected/missing protection closes gate and escalates; cancel/replace does not create unprotected race or reverse exposure; zero position has no unintended orphan algo order; normal-order and algo IDs reconcile correctly.

## Task 7 — WP-03D: Startup, restart, stream-gap and foreign-activity reconciliation

**Files:** venue snapshot implementation for `AccountActivityPort` or successor contract; reconcile service; readiness integration; incident repository use; Vietnamese runbooks.
- Tests: `tests/unit/test_venue_reconciliation.py`, `test_venue_readiness.py`; PostgreSQL Compose E2E for startup/restart, RLS, incident and recovery.

**Interfaces:** authoritative snapshot includes account/environment, observed time, completeness, cursor/coverage, balances, position side/quantity/entry/mark facts, normal open orders, open algo orders and recent fills/trades. Risk equity must pin official balance basis and avoid double counting realized PnL; mark-to-market and non-USDT conversion use existing approved source/as-of/freshness contract. Unknown/missing data remains unknown. Reconcile results are explicit: MATCHED, ORDER_ABSENT, OPEN_EXPOSURE_RECORDED, FOREIGN_ACTIVITY, PROTECTION_MISSING, MISMATCH, STALE/INCOMPLETE; only valid results can transition reservation.

- [ ] **Step 1: Freeze equity basis** from official USDⓈ-M account/balance/position docs and compare with MB-04 contract before using venue equity in `RiskEngine`. Document included/excluded unrealized PnL, realized PnL, fees/funding and assets; avoid double count.
- [ ] **Step 2: Add RED tests** for snapshot missing/stale/future/incomplete/wrong account/environment, open normal order, open algo order, orphan position, foreign order, fill history gap, quantity mismatch, duplicate fill and missing protective order.
- [ ] **Step 3: Implement startup gate** closed by default; fetch snapshots and reconcile every outstanding intent, reservation, normal order, algo order, position and trade before entry enabled.
- [ ] **Step 4: Implement reconnect recovery** for stream cursor/time gap: obtain snapshot + bounded recent history, dedupe, reconcile and mark incidents; keep gate closed if history cannot close the gap.
- [ ] **Step 5: Verify foreign activity classification** through existing account activity guard/repository. Do not mutate/adopt/cancel external items automatically.
- [ ] **Step 6: Add real-role Compose E2E** for workspace/account isolation, runtime least-privilege DB role, restart with open position/order, unknown retention, protection mismatch and operator resolution.
- [ ] **Step 7: Write Vietnamese runbooks** for restart, key rotation/revoke, API outage/429/418/503, stream gap, missing/rejected protection and manual conflict resolution.

**Acceptance:** runtime cannot open entries before snapshot/reconcile/protection pass; restart makes no duplicates; foreign activity creates persistent incident; stale/incomplete equity never becomes zero/known; opening gate requires current approved risk policy and fully resolved required incidents.

## Task 8 — WP-03E: Binance Futures Demo account validation and contract run (TẠM HOÃN TỚI KHI GATE PASS)

**Files:** explicit opt-in Demo E2E configuration and probe under `tests/e2e/`; sanitized evidence report under a non-secret test-artifact location; update project status/memory on implementation.
- Do not put actual key/account identifiers or signed payloads in repository artifacts.

**Prerequisites (all required):**

- [ ] PAPER lifecycle gate passes, including restart, idempotent intent and protective exit contract.
- [ ] Risk policy semantics are implemented and enforced for Demo; stale/unknown balance/mark/exposure holds entry.
- [ ] Offline host/key/mode isolation and UNKNOWN fault injection pass.
- [ ] Algo conditional lifecycle/protection and startup/reconnect reconcile pass on fake transport.
- [ ] A dedicated Binance USDⓈ-M Demo account/key is supplied by owner and verified to be non-production; minimum permissions and IP policy confirmed; no secret is checked in or printed.
- [ ] Demo host/schema support for each test endpoint verified. If Demo does not support a scenario, record limitation and retain offline fault test; do not silently skip while marking acceptance.

- [ ] **Step 1: Perform read-only Demo readiness**: server time, exchangeInfo, permissions/account snapshot, position mode and empty/open-order baseline. Verify DNS/host exactly equals allowlisted Demo environment.
- [ ] **Step 2: Run bounded order scenarios** market/limit, rejected precision/filter case, query/cancel; use small explicit test sizing and verify postcondition by REST + user stream.
- [ ] **Step 3: Run protective algo scenarios** stop/TP supported order types, trigger/reject/cancel where the simulator safely supports deterministic triggering. Verify algo ID/status, triggered regular order and resulting position.
- [ ] **Step 4: Run partial-fill scenario only if the simulator can produce one**, and reconcile actual fill/fee/protective quantity. Otherwise mark not supported with official/account evidence and do not claim Demo partial-fill acceptance.
- [ ] **Step 5: Run controlled timeout/reconnect/duplicate/rate-limit drills** without intentionally causing an account/IP ban; use mock/fault proxy for ambiguous response if a real Demo fault cannot be safely induced.
- [ ] **Step 6: Verify all test orders, fills, positions and algo orders are closed/reconciled**; no background operation continues after suite.
- [ ] **Step 7: Produce sanitized evidence package** containing API/SDK version, mode/host, checked docs/date, scenario result, timestamps, redacted response/event samples, state transitions, reservation outcomes and simulator limitations.

**Acceptance:** only approved Demo hosts/accounts used; every request has a reconciled result or remains UNKNOWN with risk held; every residual position/order is handled and verified; no real-money action; live host/key cannot be selected by any result or fallback.

## Task 9 — Full WP-03 integration, review and completion gate

**Files:** update `docs/implementation/03-binance-execution.md`, `project-status.md`, `README.md` if needed, relevant product/technical contracts, glossary, `implementation-memory.md`, dependency inventory/SBOM if SDK changes.

- [ ] **Step 1: Run focused unit tests** for every touched port/domain/adapter/application/repository.
- [ ] **Step 2: Run related E2E suites** for DB/RLS/outbox/runtime/restart plus offline adapter and Demo E2E only after gate; record exact pass/fail/skip/time. No skipped or unrun test is acceptance evidence.
- [ ] **Step 3: Run full unit and appropriate full E2E**, Ruff check/format, mypy, lock/build, Compose config, migration upgrade/check/downgrade rehearsal and `git diff --check` as affected.
- [ ] **Step 4: Independent review** of security boundary, order state machine, API schema version, ambiguous HTTP handling, exposure/protection math, RLS, runtime recovery and evidence package. Fix findings test-first and rerun affected gates.
- [ ] **Step 5: Update project status and implementation memory**, include checked official URLs/date, dependency/version decision, failures/root causes/fixes, commands/results, Demo limitations, open risks and next step. Read memory entry back and reconcile every statement with diff/output.
- [ ] **Step 6: Confirm no secret/private account data in diff/artifacts, one Alembic head, branch/remote policy and commit readiness.** Do not commit until mandatory unit/E2E and memory review pass.

**Acceptance:** WP-03A–D offline and integration contracts pass; Demo-only acceptance is marked complete only with real Demo evidence, or explicitly incomplete if account/simulator cannot exercise required scenario; project status, plan, memory, glossary and technical trace are consistent; LIVE remains locked.

## API sources checked 2026-11-01

- [USDⓈ-M General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) — production `fapi.binance.com`, testnet REST `demo-fapi.binance.com`, testnet WS `demo-fstream.binance.com`; signed request and HTTP 503/429/418 behavior.
- [USDⓈ-M User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams) — listen key validity/renewal, WS base/path, 24-hour connection, event ordering, `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `ALGO_UPDATE`.
- [USDⓈ-M Change Log](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log) — conditional order migration effective 2025-12-09; new `/fapi/v1/algoOrder` family; old regular order conditional types rejected; current updates to algo behavior/rate limits.
- [USDⓈ-M REST API catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api) and [WebSocket Streams catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams) — endpoint schema references; re-verify exact operations and Demo coverage before code.
- [How to Use Binance Demo Trading?](https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd) and [How to Test Futures Demo Trading](https://www.binance.com/en-IA/support/faq/detail/ab78f9a1b8824cf0a106b4229c76496d) — checked **2026-11-02**; user logs into `demo.binance.com`, selects Futures Demo, creates an API key in Demo API Management; eligibility is region-dependent and simulator may differ from Live.
- [USDⓈ-M Account Information V3](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#account-information-v3) — checked **2026-11-02**; signed read-only `GET /fapi/v3/account` (`USER_DATA`) is the proposed first account connectivity smoke, before any order permission/test.
- [USDⓈ-M General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) — rechecked **2026-11-02**; Demo REST/WS hosts and “most endpoints” qualification confirmed.

### Ma trận năng lực API đã đối chiếu — kiểm tra 2026-11-01

Ma trận dưới đây đối chiếu endpoint REST với [Trade REST API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade), [Account REST API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account), [User Data Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams), [General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) và [Change Log](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log). `TRADE`/`USER_DATA` là signed requests; `USER_STREAM` chỉ yêu cầu API key theo security type. Bảng là contract tài liệu, không phải bằng chứng endpoint cụ thể được Demo hỗ trợ.

| Năng lực | Endpoint/contract được tài liệu xác nhận | Hạn chế/quyết định triển khai |
| --- | --- | --- |
| Host Demo | REST `https://demo-fapi.binance.com`; WebSocket base `wss://demo-fstream.binance.com`. General Info nói “most endpoints” có thể dùng trên testnet. | Pin đúng host; không suy diễn path, credential, Algo Service hay private-account support từ việc host tồn tại. Xác nhận từng endpoint/method trên Demo trước khi chạy credential test. |
| Metadata public | `GET /fapi/v1/exchangeInfo` và server time `GET /fapi/v1/time` thuộc Market Data; exchange info là nguồn symbol/status/filter và giới hạn công khai. | Metadata phải có `observed_at`/freshness; không cache vô hạn. Chưa có evidence offline hay Demo cho filter cụ thể của tài khoản/symbol tại thời điểm chạy. |
| Lệnh thường | `POST /fapi/v1/order` (`TRADE`), `GET /fapi/v1/order` (`USER_DATA`), `DELETE /fapi/v1/order` (`TRADE`); client/order ID, symbol, trạng thái, `executedQty` trong contract. Open orders: `GET /fapi/v1/openOrders`; toàn bộ orders: `GET /fapi/v1/allOrders`. | Ack/cancel response không kết thúc reconcile: fill có thể chạy đua với cancel. Conditional TP/SL/trailing được chuyển sang Algo Service; không dùng regular-order route cho loại đó. |
| Lệnh algo/conditional | `POST /fapi/v1/algoOrder`, `GET /fapi/v1/algoOrder`, `DELETE /fapi/v1/algoOrder`; mở hiện hành `GET /fapi/v1/openAlgoOrders`; lịch sử tất cả `GET /fapi/v1/allAlgoOrders`. Algo ID/client algo ID và `actualOrderId` của lệnh thường sau trigger là định danh khác nhau. | Algo lifecycle/reconcile tách với lệnh thường. Các loại được hỗ trợ theo REST page: `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`, `TRAILING_STOP_MARKET`; conditional order dùng `triggerPrice`. Change Log nêu không có margin check trước khi conditional order trigger và `ALGO_UPDATE` báo trạng thái/rejection; ack tạo algo không tự chứng minh protection đã hoạt động. Từ change log hiệu lực 2026-10-23, `callbackRate` tối đa 20 cho trailing-stop; vẫn phải kiểm toàn bộ parameter constraints tại thời điểm triển khai. |
| Lịch sử order/fill | `allOrders`: canceled/expired không-fill không còn sau 3 ngày; mọi order không còn sau 90 ngày. Query window ngắn hơn 7 ngày. `userTrades`: chỉ ba tháng gần nhất; mặc định 7 ngày, mỗi khoảng truy vấn tối đa 7 ngày. | REST history không phải ledger dài hạn. Persist facts/events cần reconcile trong app; thiếu coverage ngoài retention hoặc gap chưa lấp được thì giữ UNKNOWN/gate đóng. |
| User data stream | REST `POST/PUT/DELETE /fapi/v1/listenKey` tạo/renew/đóng; listen key validity 60 phút. Stream có `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `ALGO_UPDATE`, `listenKeyExpired`; kết nối bị ngắt sau 24 giờ. Cùng event type/connection có ordering theo `T`/`E`; khác loại event không có cùng bảo đảm thứ tự. | Ưu tiên stream cho live update nhưng không thay snapshot/reconcile sau gap. Tài liệu đang nêu production URL dạng `wss://fstream.binance.com/private/ws/<listenKey>`; Demo chỉ được ghi base host, chưa xác minh path private-stream cụ thể. Không log hoặc persist listen key. |
| Rate/error semantics | REST docs nêu 429 phải backoff; 418 là IP auto-ban nếu tiếp tục vi phạm. 503 `Unknown error...` có thể đã được sàn nhận và phải query/stream trước retry; `Service Unavailable.` và `Internal error; unable...` được mô tả là failure có thể gửi lại; `-1008` system throttle là failure. | Phân loại theo status **và message/code**, không retry blanket 5xx. Khi response/transport không xác định bytes đã tới sàn chưa, side-effect result UNKNOWN; policy local vẫn query trước retry. Header `Retry-After` chưa được xác minh từ trang USDⓈ-M đã đọc; kiểm tra endpoint/rate-limit docs trước khi chốt chính sách. Test ban bằng fixture, không cố ý gây 418 thật. |

**Chưa được xác nhận qua tài liệu hoặc Demo evidence:** Demo hỗ trợ riêng cho từng signed endpoint, Algo order/event và listen-key private-stream path; schema/retention Demo có khác production không; cấp quyền API key tối thiểu và endpoint nào chứng minh permission mà không đặt order; simulator có thể tạo partial fill hoặc trigger/reject tất định không; SDK được chọn có coverage tương ứng `ALGO_UPDATE`; user stream có replay/cursor để chứng minh không mất event khi reconnect hay không; `Retry-After` có được trả/ý nghĩa thế nào trên từng rate-limit response. Mọi hạng mục trên vẫn cần fake-transport fixture trước, rồi evidence Demo explicit-opt-in sau khi WP-03E gate đủ điều kiện.

Trang General Info cũng ghi các SDK Python/Java là do partner/người dùng cung cấp, không phải phần mềm chính thức Binance và Binance không cam kết an toàn/hiệu năng. Vì vậy chưa chọn hoặc thêm SDK; WP03A.1 cần bước so sánh chính xác coverage/license/phiên bản nếu định dùng thư viện.

**Lưu ý API hiện hành:** trang USDⓈ-M Change Log được kiểm tra ngày 2026-11-01 ghi ngày 2026-10-23 đổi max `callbackRate` từ 10 lên 20 cho REST/WS algo trailing-stop. Trang User Data Streams sửa ngày 2026-10-30; trang General Info được đọc cùng ngày kiểm tra này. Recheck Change Log ngay trước khi khóa parser/SDK/schema fixtures, vì các con số rate/constraint có thể đổi.

**Research limits:** WBS does not claim the Demo simulator supports partial fills, forced timeout-after-accept, every algo type or every account endpoint. Those are explicit capability-audit and evidence items. Production documentation is not by itself proof of Demo behavior.
