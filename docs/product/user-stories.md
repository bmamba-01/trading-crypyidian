# Product backlog — Independent Binance Futures Bot

**Version:** 1.0 · **Ngày:** 21/10/2026 · **Trạng thái:** PO draft cho review \
Use case source: [use-cases.md](./use-cases.md) · Sequence: [sequence-diagrams.md](./sequence-diagrams.md) · DB/stack: [technical-design.md](../architecture/technical-design.md)

## Quy ước backlog

- Stories dùng tiêu chí Given/When/Then để có thể kiểm thử nghiệm thu. “AI quyết định” luôn có nghĩa là score/veto/abstain trong policy; AI không được tự gửi order, sửa hard risk cap, hoặc promote bản thân lên live.
- `P0` là điều kiện an toàn và vận hành tối thiểu của PAPER/DEMO; `P1` mở rộng nghiên cứu; `P2` nâng cao. Go-live là release gate riêng, không phải quyền mặc định.
- Mỗi AC ghi nhận được sự kiện, version, actor và timestamp cần thiết; secret, private key và dữ liệu cá nhân không được đưa vào log.
- Các tiêu chí kinh doanh/risk phải cấu hình theo workspace, nhưng không được vượt hard ceiling của hệ thống.
- Thuật ngữ EN trong story/AC được giải thích bằng tiếng Việt tại [glossary song ngữ](../research/glossary-vi.md); tên API, role ID và field máy đọc giữ English.

## Epic A — Workspace, identity, cấu hình và quyền

### US-01 — Tạo workspace và phân tách mode

**Ưu tiên:** P0 · **Use case:** UC-01 · **Actor:** Owner \
**Story:** Là owner, tôi muốn khởi tạo workspace và chọn PAPER/DEMO/LIVE riêng biệt để các cấu hình và trạng thái tài khoản không bị lẫn.

**Acceptance criteria**

- **Given** hệ thống chưa có workspace, **When** owner hoàn thành wizard, **Then** tạo workspace, user owner, audit và ConfigVersion đầu tiên trong cùng luồng; workspace mới mặc định PAPER hoặc DEMO.
- **Given** tài khoản PAPER, **When** người dùng xem hoặc gửi yêu cầu giao dịch, **Then** mode được hiển thị rõ và gateway không thể gửi request live.
- **Given** chọn LIVE, **When** readiness chưa đủ, **Then** hệ thống chặn kích hoạt và trả danh sách gate còn thiếu.
- **Given** thao tác khởi tạo lỗi giữa chừng, **When** owner tải lại, **Then** không có trạng thái “thành công” giả và request lặp không tạo workspace trùng.

### US-02 — Kết nối Binance USDⓈ-M

**Ưu tiên:** P0 cho paper/demo adapter; P1 cho live readiness · **Use case:** UC-02 · **Actor:** Owner

**Story:** Là owner, tôi muốn kiểm tra tài khoản Binance và quyền API trước khi runtime sử dụng credential.

**Acceptance criteria**

- **Given** credential hợp lệ, **When** kiểm tra kết nối, **Then** ứng dụng xác nhận venue, futures account capability, environment và quyền đọc/đặt lệnh cần thiết mà không gửi lệnh thật.
- **Given** credential sai, thiếu quyền, sai environment hoặc IP restriction, **When** kiểm tra, **Then** trả lỗi phân loại, không ghi secret vào response/log và không kích hoạt live.
- **Given** tài khoản kết nối, **When** config được lưu, **Then** account mode, endpoint, capability snapshot và thời điểm kiểm tra được lưu có version.

### US-03 — Quản lý/rotate/revoke secrets

**Ưu tiên:** P0 · **Use case:** UC-03 · **Actor:** Owner

**Story:** Là owner, tôi muốn secrets được lưu an toàn, xoay vòng hoặc thu hồi mà không lộ trên giao diện.

**Acceptance criteria**

- **Given** owner gửi API key/secret, **When** backend lưu, **Then** secret được chuyển thẳng tới vault/encryption service; DB chỉ lưu `secret_ref` và metadata đã redact.
- **Given** secret đã lưu, **When** trang cấu hình được mở lại, **Then** secret không được đọc lại; UI chỉ hiển thị provider, fingerprint một phần, trạng thái và ngày rotate.
- **Given** owner rotate hoặc revoke, **When** thao tác hoàn thành, **Then** version mới có hiệu lực theo policy, secret cũ bị revoke/xóa theo quy trình, audit ghi actor/reason.
- **Given** secret bị nghi rò rỉ, **When** owner khóa credential, **Then** entry gate đóng ngay, protective order/reconciliation tiếp tục, incident được tạo.

### US-04 — Tài khoản người dùng, role và MFA

**Ưu tiên:** P0 · **Use case:** UC-04, UC-25 · **Actor:** Owner

**Story:** Là owner, tôi muốn cấp quyền tối thiểu theo nhiệm vụ để một người không tự vượt qua các lớp kiểm soát.

**Acceptance criteria**

- **Given** owner mời user, **When** user chấp nhận, **Then** họ được gán workspace và role cụ thể, không có quyền mặc định ngoài read-only cần thiết.
- **Given** viewer/analyst gọi endpoint thay đổi risk/credential/live mode, **When** API nhận request, **Then** server trả 403 dù UI không hiển thị nút đó.
- **Given** owner/risk admin thay đổi quyền nhạy cảm, **When** lưu, **Then** xác thực MFA gần đây được yêu cầu theo policy và audit ghi before/after đã redact.
- **Given** user bị thu hồi quyền, **When** revoke thành công, **Then** session/API credential liên quan bị vô hiệu theo thời hạn policy.

### US-05 — Bind Telegram identity và quyền command

**Ưu tiên:** P0 · **Use case:** UC-04, UC-19 · **Actor:** User/Operator

**Story:** Là operator, tôi muốn liên kết tài khoản Telegram với danh tính đã xác thực để nhận cảnh báo và gửi lệnh vận hành có kiểm soát.

**Acceptance criteria**

- **Given** user đã đăng nhập web, **When** tạo bind code, **Then** code dùng một lần, ngắn hạn, không chứa token hoặc credential.
- **Given** webhook update, **When** signature/secret header sai, **Then** bị từ chối; nếu `update_id` đã xử lý, kết quả không tạo thêm side effect.
- **Given** Telegram identity đã bind, **When** user gửi command, **Then** server ánh xạ đúng user/workspace, kiểm tra role/function/resource ở backend và audit kết quả.
- **Given** user không có quyền `resume`, **When** gửi lệnh Telegram resume, **Then** lệnh bị từ chối có giải thích; không thay đổi trạng thái runtime.

### US-06 — Draft và phát hành configuration version

**Ưu tiên:** P0 · **Use case:** UC-05 · **Actor:** Admin

**Story:** Là admin, tôi muốn chỉnh sửa cấu hình dạng draft và phát hành phiên bản bất biến để mỗi quyết định có thể tái tạo.

**Acceptance criteria**

- **Given** cấu hình hiện hành version N, **When** admin sửa, **Then** tạo draft không làm runtime tự đổi cấu hình.
- **Given** draft sai schema/risk policy/reference, **When** validate, **Then** chỉ rõ field và quy tắc không đạt; không publish.
- **Given** draft hợp lệ, **When** publish được role phù hợp phê duyệt, **Then** tạo version N+1 immutable, effective time và audit; runtime nhận version theo policy.
- **Given** hai admin cùng sửa cùng version, **When** bản cũ được publish trước, **Then** request stale bị conflict thay vì ghi đè âm thầm.

### US-07 — Risk limits, pause và resume entries

**Ưu tiên:** P0 · **Use case:** UC-06 · **Actor:** Risk Admin/Operator

**Story:** Là risk admin/operator, tôi muốn giới hạn exposure và tạm ngăn entry mới ngay khi có rủi ro hoặc sự cố.

**Acceptance criteria**

- **Given** risk profile, **When** tính plan, **Then** per-trade, total open risk, leverage, notional, drawdown, symbol và correlation caps được kiểm tra server-side.
- **Given** pause entries được xác nhận, **When** candidate mới đến, **Then** bị từ chối với reason; protective orders, reduce-only exit và reconcile tiếp tục.
- **Given** yêu cầu resume, **When** feed/account/order reconciliation hoặc circuit breaker chưa healthy, **Then** resume bị chặn và liệt kê gate chưa đạt.
- **Given** mọi gate đạt, **When** role/approval policy chấp thuận, **Then** resume có audit, actor, reason và thời gian hiệu lực.

## Epic B — Watchlist, nguồn và dữ liệu thị trường

### US-08 — Watchlist và symbol eligibility

**Ưu tiên:** P0 · **Use case:** UC-07 · **Actor:** Strategy Admin

**Story:** Là strategy admin, tôi muốn quản lý danh sách symbol/timeframe được phép phân tích độc lập theo mode.

**Acceptance criteria**

- **Given** symbol thêm vào watchlist, **When** lưu, **Then** kiểm tra venue, contract status, tick/step size, min notional và mode eligibility.
- **Given** symbol bị delist, stale, liquidity thấp hoặc risk-blocked, **When** scheduler xét, **Then** không tạo entry candidate và ghi lý do.
- **Given** watchlist version đổi, **When** được publish, **Then** quyết định tiếp theo ghi version mới; quyết định cũ giữ nguyên lineage.

### US-09 — Registry nguồn và quyền sử dụng

**Ưu tiên:** P0 · **Use case:** UC-08 · **Actor:** Strategy Admin

**Story:** Là source admin, tôi muốn khai báo nguồn, phương thức truy cập, giới hạn và quyền dùng dữ liệu để tránh ingest trái quyền.

**Acceptance criteria**

- **Given** source được thêm, **When** bật connector, **Then** bắt buộc khai báo URL/provider, access method, quota, terms/rights status, retention, attribution và owner.
- **Given** quyền chưa xác minh, **When** scheduler chạy, **Then** chỉ lưu metadata tối thiểu được phép hoặc skip; không dùng body để train.
- **Given** quota/rate limit tới ngưỡng, **When** connector nhận response, **Then** backoff theo policy và ghi retry/skip reason.
- **Given** source bị thu hồi, **When** đổi status, **Then** connector ngừng fetch và dataset eligibility xử lý theo retention/license policy.

### US-10 — Ingest và point-in-time market data

**Ưu tiên:** P0 · **Use case:** UC-09 · **Actor:** Market Data Service

**Story:** Là hệ thống phân tích, tôi muốn dữ liệu chuẩn hóa, có timestamp nhận thực tế và được kiểm tra gap để tránh look-ahead bias.

**Acceptance criteria**

- **Given** kline/trade/funding/OI event, **When** normalize, **Then** lưu `event_at`, `available_at`, source, symbol, timeframe/cursor và quality flags.
- **Given** duplicate event, **When** ingest lại, **Then** unique key ngăn duplicate logic; lần nhận lại vẫn có thể ghi metric/retry audit.
- **Given** sequence gap, stale feed hoặc clock skew vượt ngưỡng, **When** phát hiện, **Then** đánh dấu khoảng thiếu/incomplete và chặn candidate cần dữ liệu đó.
- **Given** backfill thành công, **When** dữ liệu được sửa/bổ sung, **Then** lưu provenance/revision và không âm thầm đổi snapshot lịch sử đã dùng ra quyết định.

### US-11 — News, macro event và catalyst

**Ưu tiên:** P1 · **Use case:** UC-10 · **Actor:** Source Connector/Analyst

**Story:** Là analyst, tôi muốn tổng hợp tin crypto/macro, công bố lịch và catalyst có timestamp để hiểu rủi ro sự kiện.

**Acceptance criteria**

- **Given** nguồn hợp lệ trả item, **When** ingest, **Then** lưu canonical URL, publisher, published/edited/observed/available times, language, hash và source rights.
- **Given** nội dung repost/duplicate/correction, **When** normalize, **Then** liên kết bản gốc và phiên bản, không đếm thành nhiều sự kiện độc lập.
- **Given** extractor tạo event/entity/stance, **When** lưu, **Then** kết quả có model version, evidence span/reference, confidence và trạng thái review.
- **Given** dữ liệu tới sau thời điểm decision, **When** backtest/replay truy vấn as-of, **Then** event không được xuất hiện trước `available_at`.

### US-12 — Trending/attention score

**Ưu tiên:** P1 · **Use case:** UC-11 · **Actor:** Feature Worker

**Story:** Là strategy engine, tôi muốn attention score đa nguồn đã chuẩn hóa theo baseline để nhận biết mức độ chú ý bất thường.

**Acceptance criteria**

- **Given** volume/search/social metric được phép, **When** tính attention, **Then** score lưu cửa sổ thời gian, baseline, source coverage, freshness và công thức version.
- **Given** nguồn bị outage hoặc không đại diện, **When** coverage thấp hơn ngưỡng, **Then** feature đánh dấu degraded và không được hiểu thành attention bằng zero.
- **Given** trending tăng đột biến, **When** dùng cho quyết định, **Then** nó là feature/catalyst, không trực tiếp phát lệnh và không vượt RiskEngine.

### US-13 — Trader opinion registry và đối chiếu góc nhìn

**Ưu tiên:** P1 · **Use case:** UC-12 · **Actor:** Analyst/Reviewer

**Story:** Là analyst, tôi muốn lưu nhận định trader có nguồn dẫn và phân biệt ý kiến với dữ liệu hiệu suất đã xác minh.

**Acceptance criteria**

- **Given** nguồn công khai được phép, **When** lưu nhận định, **Then** giữ nguyên URL, thời gian công bố/thu thập, asset, horizon, stance, evidence và attribution.
- **Given** hồ sơ trader chưa có kết quả audited, **When** hiển thị hoặc đưa vào model, **Then** gắn nhãn “chưa xác minh hiệu suất”; không gọi họ là trader có lợi nhuận/top performance.
- **Given** nhiều ý kiến xung đột, **When** tạo aggregate, **Then** biểu diễn bất đồng, freshness và coverage; không biến thành một lệnh buy/sell.
- **Given** bản trích xuất AI sai, **When** reviewer sửa, **Then** bản gốc giữ nguyên và correction có actor/version.

## Epic C — Candidate, AI score và execution

### US-14 — Strategy và chart-pattern candidate

**Ưu tiên:** P0 · **Use case:** UC-13 · **Actor:** Strategy Engine

**Story:** Là strategy researcher, tôi muốn rule deterministic và chart-pattern hypothesis tạo candidate có thể giải thích và replay.

**Acceptance criteria**

- **Given** strategy bundle được enable, **When** đủ input hợp lệ, **Then** tạo candidate có direction, timeframe, setup/pattern, entry condition, invalidation, exit recipe và rule version.
- **Given** chart pattern được phát hiện, **When** candidate lưu, **Then** có điểm pivot/geometry, tolerance, lookback, confirmation time và confidence; không dùng pivot tương lai.
- **Given** điều kiện không đủ, **When** rule chạy, **Then** lưu NO_TRADE reason/diagnostics theo sampling policy thay vì tạo candidate giả.
- **Given** cùng input snapshot, bundle và code version, **When** replay, **Then** output giống nhau hoặc sai khác có giải thích phiên bản/runtime.

### US-15 — AI score, veto và abstain

**Ưu tiên:** P0 · **Use case:** UC-14 · **Actor:** Model Service

**Story:** Là operator, tôi muốn AI xếp hạng candidate và từ chối khi không đủ dữ liệu, thay vì tự do ra lệnh.

**Acceptance criteria**

- **Given** candidate/context, **When** model chạy, **Then** trả schema hữu hạn gồm score/calibration, regime, evidence refs, model version và recommendation `{approve, veto, abstain}`.
- **Given** timeout, invalid schema, OOD, missing feature, stale input hoặc unsupported source, **When** chấm điểm, **Then** abstain theo policy; deterministic fallback chỉ chạy nếu được bật và versioned.
- **Given** model output, **When** persisted, **Then** lưu feature/model/prompt-policy version và response hash; không lưu secret hoặc hidden chain-of-thought.
- **Given** candidate bị AI approve, **When** đi tiếp, **Then** vẫn phải qua planner và hard RiskEngine.

### US-16 — Trade plan và hard risk gate

**Ưu tiên:** P0 · **Use case:** UC-15 · **Actor:** Planner/RiskEngine

**Story:** Là risk owner, tôi muốn entry, stop, target, size và phí được tính nhất quán để giới hạn loss dự kiến.

**Acceptance criteria**

- **Given** candidate được chấp nhận, **When** tạo TradePlan, **Then** công thức ghi rõ reference price E0, stop/target, sizing, fee, funding, slippage scenario, precision và exit recipe version.
- **Given** giá/stop/target/cost thay đổi sau score, **When** plan reprice, **Then** score được tính lại hoặc plan bị reject; slippage chỉ tính một lần trong scenario.
- **Given** exchange balance/position snapshot stale hoặc hard limit vượt, **When** RiskEngine xét plan, **Then** reject và lưu reason; model không thể override.
- **Given** pass risk, **When** reservation tạo, **Then** tổng open risk tính cả intent đang pending/UNKNOWN để tránh cấp trùng vốn.

### US-17 — Entry order idempotent và reconciliation

**Ưu tiên:** P0 · **Use case:** UC-16 · **Actor:** Execution Gateway

**Story:** Là execution service, tôi muốn gửi order có client identity bền vững và xử lý timeout mà không tạo lệnh trùng.

**Acceptance criteria**

- **Given** risk-approved intent, **When** submit, **Then** intent/idempotency key được commit trước network call và client order ID gắn duy nhất với intent.
- **Given** exchange ack, **When** nhận response/stream, **Then** order state lưu id, status, timestamps và raw response đã redact.
- **Given** timeout/503 ambiguous, **When** chưa biết exchange đã nhận hay chưa, **Then** intent thành UNKNOWN, reservation còn giữ và gateway reconcile theo client ID trước khi retry.
- **Given** intent trùng event/retry, **When** consumer chạy lại, **Then** không submit side effect thứ hai.

### US-18 — Partial fill và protective stop

**Ưu tiên:** P0 · **Use case:** UC-16 · **Actor:** Execution/Portfolio

**Story:** Là operator, tôi muốn mọi phần khối lượng đã fill được bảo vệ ngay bằng stop/exit policy.

**Acceptance criteria**

- **Given** partial fill, **When** fill được xác nhận, **Then** cập nhật quantity/average/fee và tạo/cập nhật stop cho đúng filled quantity.
- **Given** stop chưa được exchange xác nhận, **When** có exposure, **Then** đánh dấu unprotected, block entries và alert khẩn.
- **Given** stop reject/mất, **When** remediation policy được áp dụng, **Then** retry/replace/reduce-only flatten đúng policy, lưu mọi kết quả và không giả định được bảo vệ.
- **Given** remaining entry bị cancel, **When** xác nhận, **Then** không để stop quantity lớn hơn position ngoài quy tắc close-position hợp lệ.

### US-19 — Position monitoring và exit recipe

**Ưu tiên:** P0 · **Use case:** UC-17 · **Actor:** Portfolio/Execution Worker

**Story:** Là hệ thống, tôi muốn áp dụng stop, target, trailing, time exit và rule đóng vị thế theo cùng một recipe đã version.

**Acceptance criteria**

- **Given** position đang mở, **When** giá/fill/funding đến, **Then** monitor đánh giá recipe version đã gắn với trade.
- **Given** trailing stop cập nhật, **When** lệnh thay đổi, **Then** không nới stop theo hướng tăng rủi ro trừ policy rõ ràng và audit.
- **Given** exit signal, **When** gửi close, **Then** order reduce-only và quantity không vượt vị thế reconcile được.
- **Given** position zero được xác nhận, **When** đóng trade, **Then** tổng hợp fill/fees/funding/realized PnL và enqueue review/label.

### US-20 — Startup/reconnect reconcile

**Ưu tiên:** P0 · **Use case:** UC-18 · **Actor:** Runtime

**Story:** Là runtime, tôi muốn so sánh trạng thái nội bộ với Binance trước khi nhận candidate sau restart hoặc stream gap.

**Acceptance criteria**

- **Given** startup/reconnect, **When** reconcile bắt đầu, **Then** entry gate mặc định đóng cho tới khi positions, open orders, fills và protective orders được đối chiếu.
- **Given** discrepancy/orphan/missing stop, **When** phát hiện, **Then** tạo incident, giữ entry gate đóng và thực hiện hành động bảo vệ trong policy.
- **Given** state khớp và health gates đạt, **When** owner policy cho phép, **Then** entries chỉ mở lại sau audit/reconciliation record.

### US-21 — Telegram notification và operator command

**Ưu tiên:** P0 · **Use case:** UC-19 · **Actor:** Operator

**Story:** Là operator, tôi muốn nhận alert cô đọng và thực hiện các command được role cho phép qua Telegram.

**Acceptance criteria**

- **Given** incident/order/trade event, **When** notifier gửi, **Then** message chứa workspace/mode/entity/reason/action link và không chứa secret.
- **Given** Telegram/API outage, **When** gửi thất bại, **Then** retry có giới hạn/idempotency và vẫn lưu alert trên web/audit.
- **Given** command lặp, **When** update_id hoặc command key đã xử lý, **Then** trả cùng kết quả mà không lặp action.
- **Given** `/status` hoặc `/pause_entries`, **When** user role đủ, **Then** response phản ánh trạng thái thực từ server; command nhạy cảm yêu cầu confirmation theo policy.

## Epic D — Ledger, đánh giá và vận hành

### US-22 — Trade/decision/order history

**Ưu tiên:** P0 · **Use case:** UC-20 · **Actor:** Owner/Operator/Analyst

**Story:** Là analyst, tôi muốn xem lịch sử từ candidate đến fill/exit để truy nguyên quyết định và hành vi bot.

**Acceptance criteria**

- **Given** trade hoặc NO_TRADE, **When** tìm theo thời gian/symbol/strategy/mode/status, **Then** kết quả có pagination, workspace scope và filter rõ.
- **Given** trade detail mở, **When** xem timeline, **Then** hiển thị snapshot IDs, strategy/model/risk/config versions, order attempts, fills, fees, funding, stop changes và actor/actions.
- **Given** người dùng không thuộc workspace, **When** gọi API theo ID đoán được, **Then** không lộ record và trả response policy an toàn.

### US-23 — Manual review và decision quality

**Ưu tiên:** P1 · **Use case:** UC-21 · **Actor:** Reviewer

**Story:** Là reviewer, tôi muốn đánh giá chất lượng setup và thực thi riêng biệt với PnL để label phục vụ phân tích không bị thiên lệch.

**Acceptance criteria**

- **Given** trade đóng, **When** reviewer ghi outcome/decision-quality/execution-quality tags, **Then** lưu append-only review và reviewer/version.
- **Given** label bị sửa, **When** correction tạo, **Then** history cũ được giữ, reason bắt buộc và dataset eligibility được tính lại.
- **Given** trade chưa đóng hoặc thiếu fill/funding, **When** worker tạo label, **Then** đánh dấu provisional/ineligible.

### US-24 — Health, breaker, incident và audit

**Ưu tiên:** P0 · **Use case:** UC-06, UC-25 · **Actor:** Operator/Owner

**Story:** Là operator, tôi muốn nhìn thấy health của feed/account/model/execution và hành động tự bảo vệ khi vượt ngưỡng.

**Acceptance criteria**

- **Given** feed stale, loss cap, API error burst, missing protection hoặc clock skew, **When** ngưỡng cấu hình bị vượt, **Then** breaker đóng entries, tạo incident và alert.
- **Given** breaker active, **When** query vị thế, **Then** protective monitoring/reconcile vẫn chạy theo policy.
- **Given** metric/log/tracing, **When** ghi, **Then** có correlation ID, workspace/account/mode và redaction; không chứa credential/raw personal data ngoài policy.
- **Given** operator resume, **When** unresolved incident còn tồn tại, **Then** không clear breaker và trả blocker cụ thể.

### US-25 — Dashboard và hiệu suất có attribution

**Ưu tiên:** P1 · **Use case:** UC-20 · **Actor:** Owner/Analyst

**Story:** Là owner, tôi muốn đánh giá strategy theo net performance, exposure và điều kiện thị trường thay vì chỉ xem win rate.

**Acceptance criteria**

- **Given** khoảng ngày/mode/strategy/symbol, **When** tạo báo cáo, **Then** tách gross/net PnL, fees, funding, slippage, drawdown, expectancy, turnover, exposure và trade count.
- **Given** dữ liệu chưa hoàn chỉnh hoặc paper khác live, **When** hiển thị metric, **Then** nêu coverage, mode và khoảng dữ liệu; không gộp lẫn.
- **Given** report export, **When** tạo, **Then** giữ filter, generated_at, dataset/version scope và quyền truy cập.

## Epic E — Self-learning an toàn và quản lý release

### US-26 — Tạo hypothesis bằng DSL giới hạn

**Ưu tiên:** P1 · **Use case:** UC-22 · **Actor:** Strategy Admin

**Story:** Là strategy researcher, tôi muốn tạo challenger từ rule/config có grammar cho phép để thử nghiệm mà không chạy code tùy ý.

**Acceptance criteria**

- **Given** hypothesis DSL, **When** compile, **Then** compiler chỉ cho phép toán tử/feature/window đã allowlist, giới hạn complexity và tạo canonical hash.
- **Given** DSL có import, filesystem, network, unbounded loop hoặc biểu thức ngoài allowlist, **When** compile, **Then** reject trước chạy và ghi diagnostics.
- **Given** hypothesis hợp lệ, **When** tạo experiment, **Then** pin data universe, timeframe, label/recipe version, objective, cost model và hypothesis hash.

### US-27 — Train và đánh giá temporal/walk-forward

**Ưu tiên:** P1 · **Use case:** UC-23 · **Actor:** Trainer

**Story:** Là model owner, tôi muốn train challenger trên dữ liệu lineage rõ, có kiểm định theo thời gian và chi phí thực tế.

**Acceptance criteria**

- **Given** dataset, **When** trainer lập split, **Then** train/validation/test theo thời gian, purging/embargo phù hợp holding period và test holdout được khóa.
- **Given** training run, **When** hoàn tất, **Then** lưu code/config/data/feature/label hashes, library versions, seed, metrics, calibration, costs, artifact URI và logs.
- **Given** leakage, inadequate sample, overfit, benchmark underperformance hoặc risk gate fail, **When** đánh giá, **Then** report đánh dấu không đủ điều kiện promotion kèm lý do.
- **Given** cùng manifest/seed/environment, **When** chạy lại, **Then** sai khác metric/artifact được phát hiện và ghi lại.

### US-28 — Promotion có phê duyệt và rollback

**Ưu tiên:** P1 paper; live là release gate riêng · **Use case:** UC-24 · **Actor:** Strategy/Risk Admin

**Story:** Là risk admin, tôi muốn chỉ phát hành model/strategy qua các môi trường có kiểm soát và quay về bản tốt đã biết.

**Acceptance criteria**

- **Given** evaluation đạt gates, **When** đề xuất promotion, **Then** version immutable được phát hành trước vào paper/canary, không tự động vào live.
- **Given** promotion live, **When** policy yêu cầu dual approval/readiness, **Then** hai principal khác nhau xác nhận, MFA và evidence checklist được lưu.
- **Given** risk regression/drift/incident, **When** rollback được gọi, **Then** previous release phục hồi, entry gate và config đồng bộ theo version; audit lưu actor/reason.
- **Given** model mới, **When** bị rollback, **Then** artifact và trade history không bị xóa.

### US-29 — Drift và re-training trigger

**Ưu tiên:** P1 · **Use case:** UC-25 · **Actor:** Model/Risk Owner

**Story:** Là model owner, tôi muốn theo dõi drift và chất lượng live-forward để quyết định retrain hoặc rollback, không tự update rule đang trade.

**Acceptance criteria**

- **Given** feature/outcome distribution, **When** vượt drift threshold hoặc confidence calibration suy giảm, **Then** tạo drift event gắn model/strategy/version/time range.
- **Given** drift event, **When** xử lý, **Then** có hành động đề xuất: monitor, abstain/fallback, retrain challenger hoặc rollback theo policy; không tự promote live.
- **Given** data coverage thấp, **When** đánh giá drift, **Then** trạng thái là inconclusive/degraded, không diễn giải là model khỏe.
- **Given** re-training trigger, **When** run mới tạo, **Then** giữ manifest và nguyên nhân trigger để so sánh với baseline.

## Thứ tự release gợi ý

| Release | Stories | Definition of done |
| --- | --- | --- |
| R0 — Safe paper foundation | US-01…08, US-10, US-14…20, US-22, US-24 | PAPER end-to-end, deterministic replay, risk gate, protection/reconcile, audit/ledger |
| R1 — Demo & operator control | US-02…07, US-21, US-25 | Binance demo, admin roles, Telegram identity/commands, dashboard; secret redaction |
| R2 — Research inputs | US-09, US-11…13, US-23 | Registry rights, point-in-time events/attention/opinions, reviewer corrections |
| R3 — Bounded self-learning | US-26…29 | DSL + walk-forward, promotion to paper/canary, drift and rollback; no live self-promotion |
| R4 — Live readiness review | selected P0/P1 + deployment gates | backup/restore, security, incident, prospective demo and approval evidence complete |

## Product guardrails

1. Không dùng “100–200 trader hàng đầu” như nhãn lợi nhuận nếu chưa có kết quả độc lập, audited; dùng registry nguồn/nhận định có provenance.
2. Self-learning tạo và đánh giá challenger; không ghi đè chiến lược đã active và không tự mở quyền live.
3. LLM output là dữ liệu không tin cậy, được schema validate và không có quyền thực thi lệnh.
4. Live trading phụ thuộc quyết định vận hành, điều kiện pháp lý/sàn, quyền API, test prospective và phê duyệt riêng; không nằm trong DoD của paper MVP.
