# Use case toàn hệ thống trading bot

**Version:** 1.0 · **Ngày:** 21/10/2026 · **Trạng thái:** thiết kế cho review \
**Phạm vi:** bot Binance USDⓈ-M độc lập, paper/demo/live có phân quyền; source/news/trending; web admin; Telegram; AI train/evaluate/promote; review lịch sử. \
**Traceability:** User Story ID tương ứng trong [user-stories.md](./user-stories.md); luồng xuyên hệ thống trong [sequence-diagrams.md](./sequence-diagrams.md); thiết kế DB/framework trong [technical-design.md](../architecture/technical-design.md); bản dịch/định nghĩa thuật ngữ tại [glossary EN→VI](../research/glossary-vi.md).

## 1. Mục đích và ranh giới

User là quản trị viên/nhà vận hành, analyst/reviewer hoặc engine/connector/AI service. Binance, Telegram, nguồn tin, nguồn social và nhà cung cấp AI là hệ thống ngoài. Tác nhân ngoài không được quyền thay RiskEngine; nội dung web/social không phải instruction cho agent. Hành động quản trị được xác thực ở server và audit; dữ liệu quyết định được giữ theo `available_at`.

### Quy ước chung

- Một `Workspace` sở hữu người dùng, bot account, integration, watchlist, chiến lược, risk profile và báo cáo. `TradingAccount` xác định venue, mode PAPER/DEMO/LIVE và secret reference.
- Một lần ra quyết định luôn gắn `as_of`, snapshot dữ liệu, feature/model/strategy/risk version. Thiếu hoặc stale feature bắt buộc thì abstain/NO_TRADE.
- Strategy chỉ trả `Candidate`; AI chỉ score/veto/abstain; RiskEngine duyệt hoặc từ chối `TradePlan`; ExchangeGateway thực thi intent đã persist.
- Runtime phải bảo vệ vị thế đang mở kể cả khi web/Telegram/model không hoạt động. Pause entries không huỷ stop hay đóng vị thế.
- Mọi message/update/order có idempotency key phù hợp. Lỗi UNKNOWN cần reconcile trước khi gửi lại.

## 2. Vai trò

| Actor | Nhiệm vụ | Quyền mặc định |
| --- | --- | --- |
| Owner | Khởi tạo workspace, cấp role, integration, phê duyệt/live mode | Full control, MFA; dual approval khi nhiều người dùng |
| Operator | Theo dõi trạng thái và ứng phó vận hành | Đọc, pause entries, xác nhận incident; resume theo policy |
| Strategy Admin | Xây rule DSL, data source/watchlist, challenger | Publish paper/demo; đề xuất live promotion |
| Risk Admin | Cấu hình hard limits và duyệt exposure/risk policy | Không thể vượt org ceiling; tăng cap cần approval |
| Analyst/Reviewer | Đọc trade/source, đánh dấu chất lượng và outcome | Không đổi strategy, credential, risk hoặc execution |
| Scheduler/Trainer/Connector | Nền xử lý hệ thống | Service identity hẹp; không quyền web admin hay trading secret trừ gateway |
| Binance/Telegram/Source provider | External API/event source | Chỉ quyền API được cấp, không phải user/role trong workspace |

## 3. Danh mục use case

| UC | Use case | Actor chính | Stories |
| --- | --- | --- | --- |
| UC-01 | Khởi tạo workspace và chọn PAPER/DEMO/LIVE | Owner | US-01 |
| UC-02 | Kết nối và xác minh Binance USDT-M | Owner | US-02 |
| UC-03 | Quản lý secrets/rotate/revoke | Owner | US-03 |
| UC-04 | Quản lý user, role, MFA và Telegram identity binding | Owner | US-04, US-05 |
| UC-05 | Sửa draft và phát hành configuration version | Admin | US-06 |
| UC-06 | Quản lý risk caps, pause, resume và circuit breaker | Risk Admin/Operator | US-07, US-24 |
| UC-07 | Quản lý watchlist và eligibility của symbol | Strategy Admin | US-08 |
| UC-08 | Quản lý nguồn, quyền, quota và connector | Strategy Admin | US-09 |
| UC-09 | Nhận market feed, chuẩn hóa và backfill | MarketData Service | US-10 |
| UC-10 | Thu thập và phân loại tin/sự kiện crypto/macro | News/Source Connector | US-11 |
| UC-11 | Tính attention/trending đa nguồn | Feature Worker | US-12 |
| UC-12 | Ghi nhận và trích xuất nhận định trader | Source Connector/Reviewer | US-13 |
| UC-13 | Tạo candidate từ strategy và chart pattern | Strategy Engine | US-14 |
| UC-14 | AI đánh giá regime/candidate và abstain | Model Service | US-15 |
| UC-15 | Tính entry/exit/cost/size và risk gate | RiskEngine | US-16 |
| UC-16 | Gửi entry và bảo vệ partial fill | Execution Gateway | US-17, US-18 |
| UC-17 | Theo dõi vị thế, thay trailing, exit và đóng trade | Execution/Portfolio Worker | US-19 |
| UC-18 | Khởi động/reconnect/reconcile trạng thái order | Execution Gateway | US-20 |
| UC-19 | Gửi alert và xử lý Telegram command | Telegram Gateway | US-05, US-07, US-21 |
| UC-20 | Xem lịch sử decision/order/trade và report | Owner/Operator/Analyst | US-22, US-25 |
| UC-21 | Review một trade và ghi nhận chất lượng quyết định | Reviewer | US-23 |
| UC-22 | Tạo hypothesis/challenger có DSL giới hạn | AI Research/Strategy Admin | US-26 |
| UC-23 | Huấn luyện và đánh giá challenger walk-forward | Trainer | US-27 |
| UC-24 | Promotion paper/demo/live và rollback | Admin/Risk Admin | US-28 |
| UC-25 | Theo dõi drift, health, audit và incident | Operator/Owner | US-24, US-29 |

## 4. Chi tiết use case

### UC-01 — Khởi tạo workspace và chọn mode

**Mục tiêu:** tạo ranh giới quản trị/account trước khi nhận credential hoặc phát lệnh. **Actor:** Owner. **Trigger:** mở lần đầu. **Tiền điều kiện:** database và ứng dụng đang healthy.

**Luồng chính:** nhập bootstrap token → tạo owner/workspace trạng thái SETUP, mặc định PAPER hoặc chọn DEMO → thiết lập và xác minh MFA → kích hoạt user/workspace → tạo config version ban đầu và ghi audit. LIVE luôn bị khóa theo readiness. **Ngoại lệ:** token sai hoặc workspace đã tồn tại thì từ chối; lỗi DB rollback toàn transaction; nếu lỗi sau khi lưu seed MFA thì secret file được dọn. **Hậu điều kiện:** chỉ sau khi TOTP đúng, workspace/user chuyển ACTIVE và có `workspace_id`.

### UC-02 — Kết nối Binance USDⓈ-M

**Mục tiêu:** cấu hình account key an toàn rồi kiểm tra khả năng đọc dữ liệu/tài khoản. **Actor:** Owner. **Tiền điều kiện:** workspace tồn tại; mode được chọn; secrets service khả dụng.

**Luồng chính:** chọn venue/product/account → nhập key vào form secure → lưu secret reference mã hóa → chọn API base URL theo mode → gọi validate/read-only endpoints → query exchangeInfo, account mode/permissions, balance/position → ghi capability manifest version → hiển thị pass/failure đã redacted. Với DEMO, endpoint phải tách biệt production. **Ngoại lệ:** quyền không đủ, mode nhầm, API timeout hoặc key sai thì đánh dấu integration `invalid/degraded`, không bật order. **Hậu điều kiện:** integration version gắn secret ref và capability probe; raw key không xuất hiện lại.

### UC-03 — Lưu, xoay vòng hoặc thu hồi secret

**Actor:** Owner hoặc service identity được phân quyền. **Tiền điều kiện:** role/MFA hợp lệ.

**Luồng chính:** chọn integration → nhập key mới → validate bằng endpoint an toàn → cấp secret version/reference mới → gateway chuyển phiên bản sau khi có health check → thu hồi cũ → ghi audit (actor, time, version, fingerprint mask, outcome). **Ngoại lệ:** validation thất bại thì giữ ref cũ và không đánh dấu rotation thành công; nghi lộ key thì pause entries, revoke phía exchange và mở incident. **Hậu điều kiện:** key mới hoạt động hoặc integration chuyển sang disabled; secret value không được trả qua API/UI/log.

### UC-04 — Quản lý thành viên và quyền

**Actor:** Owner. **Tiền điều kiện:** user đã xác thực và Owner đạt MFA gần đây.

**Luồng chính:** invite email/login → gán workspace scope và role → user kích hoạt MFA → Owner có thể hạ quyền/disable/revoke session → audit từng thay đổi. **Ngoại lệ:** không xác nhận invite/MFA thì account pending; Owner không thể tự xoá admin cuối cùng nếu sẽ khóa workspace. **Hậu điều kiện:** role mới áp dụng ở backend mọi endpoint/object, không chỉ ẩn nút giao diện.

### UC-05 — Gắn Telegram user/chat và thông báo

**Actor:** Owner + người dùng Telegram. **Tiền điều kiện:** Telegram bot token đã kiểm tra getMe; user đã login web.

**Luồng chính:** web cấp one-time nonce ngắn hạn → user gửi nonce qua DM → bot kiểm tra Telegram `from.id` và `chat.id` → backend bind cặp ID số nguyên với workspace/user/role → Owner chọn command/notification cho user hoặc group được duyệt → ghi audit. **Ngoại lệ:** nonce hết hạn/sai, group chưa allowlist hoặc user đã bị revoke thì từ chối. **Hậu điều kiện:** chỉ user/chat ID đã bind sử dụng command; token không lộ trong Telegram response.

### UC-06 — Cấu hình và phát hành config version

**Actor:** Owner/role phù hợp. **Tiền điều kiện:** draft đúng schema; role có quyền từng field.

**Luồng chính:** chỉnh watchlist/strategy/risk/source/integration → validate giá trị và dependency → preview diff và impact → xin approval nếu critical → apply nguyên tử sang config version mới → runtime ack version → audit. **Ngoại lệ:** version stale, field vượt cap hoặc runtime chưa ack thì không báo thành công; rollback về version trước nếu health check thất bại. **Hậu điều kiện:** runtime đọc một version nhất quán; position hiện hữu giữ exit recipe tại thời điểm mở trừ migration được duyệt.

### UC-07 — Quản lý risk, pause và resume

**Actor:** Risk Admin/Operator. **Tiền điều kiện:** user active, workspace/account scope hợp lệ.

**Luồng chính:** cập nhật cap trong giới hạn hoặc yêu cầu tăng cap → validate exposure/model/live eligibility → publish config/risk version → operator có thể pause entries → worker chặn candidate mới nhưng giữ protection/position management → resume cần role, xác nhận health và approval policy. **Ngoại lệ:** circuit breaker hard hoặc critical incident không được resume chỉ bằng Telegram; cần giải quyết điều kiện và xác thực web/MFA. **Hậu điều kiện:** state machine lưu actor/reason/time; không có lệnh market đóng vị thế khi chỉ pause.

### UC-08 — Quản lý watchlist và eligibility

**Actor:** Strategy Admin. **Tiền điều kiện:** market metadata và liquidity freshness sẵn có.

**Luồng chính:** thêm manual symbol hoặc bật universe rule → validate contract/settlement/status/warmup/model readiness → tạo versioned snapshot/member reason → refresh theo lịch và giữ historical membership. **Ngoại lệ:** delisted/stale/illiquid symbol bị loại khỏi entry, nhưng symbol đang có vị thế vẫn nằm trong management list đến khi đóng và protection hủy an toàn. **Hậu điều kiện:** replay có thể dựng lại membership tại thời điểm lịch sử.

### UC-09 — Đăng ký nguồn và kiểm soát quyền/chi phí

**Actor:** Strategy Admin + connector worker. **Tiền điều kiện:** nguồn có URL/channel, identity evidence, điều khoản/allowed use và quota.

**Luồng chính:** thêm source → đánh dấu identity/channel/activity/rights status riêng → chọn ingest method (RSS/API/manual) → đặt rate/cost/retention cap → health test → sample-review → enable reference metadata hoặc feature theo quyền. **Ngoại lệ:** quyền/giấy phép chưa rõ thì chỉ lưu public link thủ công metadata; vượt budget/token/API quota thì giảm polling và báo alert. **Hậu điều kiện:** connector chỉ ingest scope được bật; audit rights/version/deletion status.

### UC-10 — Nhận market data và tạo snapshot point-in-time

**Actor:** Binance MarketData Service. **Tiền điều kiện:** instrument/watchlist/config versions; clock synchronized.

**Luồng chính:** subscribe streams/poll REST → kiểm tra sequence/schema/unit → normalize symbol/timeframe/contract → ghi event/receive/available clocks → tạo closed candle và derived feature inputs → phát quality status. **Ngoại lệ:** gap, stale, timestamp lệch hoặc schema đổi thì đánh invalid, resync/backfill, không dựng candle volume0 để trade. **Hậu điều kiện:** market snapshot bất biến, replayable và phân biệt OHLCV với mark/index/funding units.

### UC-11 — Thu thập news/event và xác minh trạng thái

**Actor:** News/Project/Regulator Connector. **Tiền điều kiện:** nguồn gốc/permission; instrument/entity map.

**Luồng chính:** lấy source item → dedup/resolve origin → nhận diện event type/coin/jurisdiction/time → trích structured claim và citations → gắn `rumor/reported/primary_confirmed/corrected/retracted` → lưu received/available time/rights → tính expiry và event feature. **Ngoại lệ:** entity ambiguous, source stale hoặc extractor confidence thấp thì đưa review queue, không tạo directional candidate. **Hậu điều kiện:** mỗi event truy về source item/hash và không trộn report time với availability.

### UC-12 — Tính trending/attention context

**Actor:** Feature Worker. **Tiền điều kiện:** đủ lịch sử theo symbol/source/window; spam/repost clusters sẵn có.

**Luồng chính:** count unique source mentions/search interest/exchange attention → normalize với trailing same-time baseline → cap bot/repost/origin concentration → join market response đã xảy ra → tính score và quality/age/coverage flags → ghi feature version. **Ngoại lệ:** API thiếu quyền hoặc feature stale thì gắn missing/stale và bỏ qua model input bắt buộc; không thay bằng số liệu sau thời điểm quyết định. **Hậu điều kiện:** attention score tạo context/watchlist reason, không tự tạo hướng lệnh.

### UC-13 — Ghi nhận/extract ý kiến trader

**Actor:** source connector + reviewer. **Tiền điều kiện:** source identity/rights trạng thái phù hợp; article/post đã xuất bản.

**Luồng chính:** giữ link và metadata được phép → extract author/source group/symbol/side/timeframe/horizon/conditional thesis/levels/invalidation/expiry → kiểm tra trích đoạn số → reviewer duyệt mẫu → dedup repost/cùng narrative → lưu ex-ante `Opinion`. **Ngoại lệ:** chỉ có opinion mơ hồ/ảnh không rõ thì chỉ lưu MethodCard/reference; rights unknown thì không copy/embed/train content. **Hậu điều kiện:** opinion có published_at, first_seen_at, available_at và source evidence; quality history chỉ cập nhật theo full cohort out-of-sample.

### UC-14 — Tạo chart/strategy candidate

**Actor:** deterministic Strategy Engine. **Tiền điều kiện:** candle đóng, feature warmup, watchlist eligible, version strategy được bật.

**Luồng chính:** evaluate `eligibility → regime → setup → trigger` → tính pattern đã xác nhận, entry/exit recipe, horizon, expiry, price geometry hash → persist candidate/reason. **Ngoại lệ:** pivot chưa confirmed, nến HTF đang mở, conflicting candidates hoặc data stale thì reject/hold/abstain theo rule. **Hậu điều kiện:** candidate cùng key `(workspace, strategy_version, symbol, candle, side)` chỉ tạo một lần.

### UC-15 — AI score candidate

**Actor:** Inference Service. **Tiền điều kiện:** model champion eligible với schema/config/feature version; recipe đã xác định.

**Luồng chính:** load candidate + point-in-time features + eligible expert/event context → validate OOD/age → score regime/opportunity/calibrated outcomes → return score/uncertainty/abstain/reasons/evidence refs and exact candidate hash. **Ngoại lệ:** timeout/provider error/OOD/feature schema mismatch thì abstain hoặc đã duyệt deterministic fallback; không gửi lệnh. **Hậu điều kiện:** immutable model decision and inference latency/audit.

### UC-16 — Tính TradePlan và RiskDecision

**Actor:** Trade Planner + RiskEngine. **Tiền điều kiện:** candidate/model decision; account position/balance/order state reconciled.

**Luồng chính:** estimate reference price/fees/slippage/funding → compute trigger/stop/targets/qty via exact contract filters → compute expected net outcome conditioned on recipe → check exposure/liquidation-distance/daily loss/spread/event/data health → approve/reject reason. **Ngoại lệ:** config/spec changes after score require new candidate/hash/score; minNotional fail không được round-up vượt risk budget; account drift blocks entries. **Hậu điều kiện:** approved plan immutable and sized within hard caps.

### UC-17 — Submit entry and protect partial fills

**Actor:** Execution Gateway. **Tiền điều kiện:** plan approved, idempotent intent persisted, mode endpoint/credential match.

**Luồng chính:** acquire account/symbol writer lock → validate config/version/order state → send clientOrderId → ingest ack/fill → persist event → for every nonzero partial fill ensure protective algorithmic order covers actual filled qty → reconcile remaining target quantities → notify. **Ngoại lệ:** UNKNOWN queries by client/algo ID and stream before retry; protection reject blocks more entry and runs incident policy. **Hậu điều kiện:** accurate position/order ledger and live stop coverage status.

### UC-18 — Monitor/modify protection/exit

**Actor:** Portfolio/Execution Worker + exchange. **Tiền điều kiện:** open position and last known protective order.

**Luồng chính:** update mark/contract price as configured → evaluate existing exit recipe and break-even/trailing only in permitted direction → quantize price/qty → update/cancel/replace according adapter capabilities → monitor Algo update and actual fill events → finalize closed trade after remaining qty is flat and funding/fees settle. **Ngoại lệ:** stale stream maintains exchange-side protection; modify unsupported means new supported order flow or hold protection and alert; trailing activation != fill. **Hậu điều kiện:** closed position summary with actual PnL/cost/exit reason and order chain.

### UC-19 — Startup/reconnect/UNKNOWN reconciliation

**Actor:** Execution Gateway. **Trigger:** process start, reconnect, timeout/503, stream gap. **Tiền điều kiện:** last DB checkpoint and credential reference.

**Luồng chính:** startup state `RECONCILING` → query balance/positions/open normal orders/open algo orders/recent fills → merge deduplicated stream by IDs/update time → compare exchange vs ledger → resume entries only when reconciled. **Ngoại lệ:** mismatch/unknown persists → quarantine symbol/account, preserve exchange protections, create incident, require operator resolution. **Hậu điều kiện:** `READY` or `DEGRADED` explicit; never assume “no position” from empty local DB.

### UC-20 — Telegram status, notification and bounded command

**Actor:** Operator/Owner via Telegram bot. **Tiền điều kiện:** signed webhook or polling setup, dedup store, identity/chat/role binding.

**Luồng chính:** receive verified update → check private/group scope + allowlisted command + role → check MFA/approval policy for resume → call same control API as web → audit and return minimal response. Alert outbox sends throttled notification with redaction/dedup. **Ngoại lệ:** unauthorized/duplicate/expired update fails closed; Telegram outage does not stop local protection or grant stale command replay. **Hậu điều kiện:** control action has actor/chat/update ID and outcome; status is masked by role.

### UC-21 — Trade ledger, history and report

**Actor:** Owner/Operator/Analyst. **Tiền điều kiện:** scoped read permission.

**Luồng chính:** filter by time/symbol/mode/strategy/model → show candidate→decision→plan→intent→order/fill→funding/fee chain → calculate net PnL/exposure/drawdown/calibration/execution metrics from canonical events → export redacted report. **Ngoại lệ:** in-flight/unknown order is labeled provisional; source material rights/user account privacy restrict columns. **Hậu điều kiện:** no operation edits historical fills/decisions; reports record calculation version.

### UC-22 — Review trade and process quality

**Actor:** Analyst/Strategy Admin. **Tiền điều kiện:** trade closed; no existing review for `(trade_id, review_version, reviewer)`.

**Luồng chính:** inspect as-of chart/data/strategy/AI/reasons and actual fill → annotate process adherence, setup, data/source/execution quality and optional counterfactual → save versioned review → queue eligible labels for next training dataset. **Ngoại lệ:** model-generated summary is draft only; future outcome/source edits cannot alter decision snapshot; disagreeing reviewers are stored separately. **Hậu điều kiện:** review auditable and only usable under dataset cutoff/policy.

### UC-23 — Generate DSL strategy hypothesis

**Actor:** AI Research Worker/Strategy Admin. **Tiền điều kiện:** allowlisted features/operators, source links/method cards, compute quota.

**Luồng chính:** summarize underperforming regimes/data gap → propose hypothesis + bounded parameter grid + exit recipe/failure regime → AST schema/compiler validates → save immutable challenger manifest with parent/hash. **Ngoại lệ:** unsupported operator, code/import/I/O, unbounded risk/parameter or absent data evidence is rejected; no direct source text becomes executable. **Hậu điều kiện:** candidate does not affect paper/live until simulation/promotion.

### UC-24 — Train and evaluate challenger

**Actor:** Training Worker. **Tiền điều kiện:** labeled data availability/rights, sealed time split, baseline configs and enough sample.

**Luồng chính:** load dataset manifest → point-in-time join → purge label overlap/embargo → fit only train folds → walk-forward and cost/latency stress → calculate calibration/CI/drawdown/exposure/ablation → compare same exposure baseline → register metrics/artifact hashes. **Ngoại lệ:** leakage, insufficient sample, training failure or quality threshold failure marks `rejected` with reason, keeps champion unchanged. **Hậu điều kiện:** immutable experiment report eligible or not for paper candidate.

### UC-25 — Promote/rollback and drift response

**Actor:** Strategy Admin + Risk Admin + Owner. **Tiền điều kiện:** challenger passes offline and paper gates; risk/model/schema compatible; live approval policy met.

**Luồng chính:** auto-publish to paper when configured → monitor shadow/paper → create signed promotion request with diff/metrics/rollback target → authorize per role/dual approval → deploy canary config/version → watch health/exposure/slippage/calibration → expand within preapproved bounds or auto-rollback. **Ngoại lệ:** stale approval, config drift, breaker or artifact signature mismatch prevents rollout; no automatic live cap increase. **Hậu điều kiện:** active bundle/version and every decision reference deployment ID.

### UC-26 — Drift, health, incident and audit

**Actor:** Operator/Owner + monitoring worker. **Tiền điều kiện:** telemetry/audit pipeline available.

**Luồng chính:** monitor connector age, gaps, order-reconcile delay, stop coverage, model OOD/calibration drift, risk and admin access → severity/state-machine → alert/dashboard → acknowledge/assign/resolve with evidence and action. **Ngoại lệ:** monitoring DB outage uses durable local spool and degraded state; alert outage escalates via dashboard/secondary channel. **Hậu điều kiện:** incident closed with timeline, versions, trade/account scope and postmortem reference.

## 5. Luồng nghiệp vụ xuyên use case

**Giao dịch:** UC-09/10/12 → UC-14 → UC-15 → UC-16 → UC-17/18 → UC-19 → UC-21/22. \
**Thông tin trader/news:** UC-08/10/11/12 → UC-13 → source context/FeatureSnapshot → UC-15 → offline source-effect analysis UC-22/24. \
**Tự học:** UC-21/22 → eligible DatasetManifest → UC-23/24 → challenger paper → UC-25 → versioned champion or rollback. \
**Control plane:** UC-01/02/03/04/05/06/07/08 → versioned configuration; UC-20 is only a role-limited front end to UC-06/07 and notifications.

## 6. MVP out-of-scope

Không có direct LLM-to-order; người dùng không nhập Python tùy ý vào admin; không mở Telegram command trading cho group công khai; không dùng message/trader content để train nếu thiếu quyền; không hỗ trợ COIN-M/hedge mode/multi-exchange/pair hedge/live auto-promotion ở bản đầu. Có thể thêm qua capability/story version riêng sau baseline và review.
