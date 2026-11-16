# WBS AI-1: luồng nguồn tin → AI phân tích kết quả chạy (16/11/2026)

**Lý do:** owner nhận xét 16/11/2026 rằng thu thập dữ liệu đa nguồn (mạng xã hội, blog, báo chí, news) và việc đưa kết quả lượt chạy vào AI phân tích mới dựng sơ bộ, chưa có dữ liệu hay luồng test thật, và chưa rõ luồng chạy cụ thể.

**Cơ sở:** khảo sát code 16/11/2026, có dẫn chứng file:dòng trong `implementation-memory.md` mục cùng ngày. Mở rộng các gói [WP-05](./2026-11-01-wp05-research-wbs.md), [WP-06](./2026-11-01-wp06-learning-wbs.md), RR-1..3 và MI-6 trong [khảo sát 14/11](./2026-11-14-feature-survey-and-manager-insights.md).

## 1. Hiện trạng thật

### 1.1 Thu thập nguồn nghiên cứu (WP-05)

| Thành phần | Có code | Có dữ liệu thật | Ghi chú |
|---|---|---|---|
| Adapter RSS/news/CryptoPanic, X/Reddit/Telegram channel, Binance announcements | Không | Không | `adapters/sources/__init__.py` chỉ có 1 dòng docstring |
| Lịch vĩ mô (CPI/FOMC…) | Không | Không | Do đó H06 luôn `H06_MACRO_UNKNOWN` |
| On-chain/ETF | Không | Không | Chỉ có trong tài liệu nghiên cứu |
| Source Registry, quota, license evidence, duyệt 2 người | Có | Chỉ dùng cho nến Binance | Chưa duyệt nguồn tin nào |
| Kho item (revision, takedown, rights worker) | Có | Không (chỉ test ghi) | Không lưu body, không có NLP |
| Sentiment/NLP, gắn symbol, feature tin tức cho chiến lược | Không | Không | Không chiến lược nào đọc tin tức |

### 1.2 "AI engine" (WP-06)

| Giai đoạn | Có code | Chạy dữ liệu thật | Bị chặn bởi |
|---|---|---|---|
| Replay/backtest → manifest → folds | Có | Không (QA/synthetic) | Thiếu quyền TRAINING cho nguồn dữ liệu |
| Huấn luyện: logistic regression 3 lớp, Python thuần | Có | Không | Như trên |
| Hiệu chỉnh + đánh giá TEST | Có | Fixture 9 mẫu → `INCONCLUSIVE` | Cần ≥ 1.000 mẫu, ≥ 100 mẫu mỗi lớp |
| Triển khai SHADOW/PAPER_GATE, score event | Có | Không | D8 cấm nguồn QA; SHADOW/PAPER_GATE khóa |
| Ledger PAPER → nhãn học lại | Chỉ có dict trong RAM | Không | Mất khi restart; không nối vào dataset |
| LLM / phân tích bằng ngôn ngữ / research brief | **Không có** | Không | Chỉ có trong kế hoạch (RR-1..3, MI-6) |
| Service `learning-evaluator` | Hỏng | — | Trỏ tới module `cli.learning_evaluation` không tồn tại |

**Kết luận:**
- Hiện chưa có "AI phân tích kết quả chạy" theo nghĩa owner mong đợi.
- Phần học máy WP-06 là hạ tầng kiểm soát quyền và kiểm định khá đầy đủ, nhưng chưa chạy được vì không có dữ liệu được phép.
- Chưa có nguồn tin tức nào.

## 2. Luồng đề xuất (cụ thể từng bước)

```
Nến Binance (đã có) ─┐
Ledger PAPER (có)  ──┼─► [AI-1] Run Report tất định ─► [AI-2] AI Analyst (LLM) ─► Đề xuất có cấu trúc
Quyết định/abstain ──┤        (JSON, số liệu gốc)        (chỉ diễn giải, trích số      │
Backtest (có)  ──────┘                                    từ report, có kiểm tra)      ▼
Nguồn tin S1 ─► [AI-4] Thu thập + chuẩn hóa sự kiện ─► digest sự kiện ──────┘   Owner duyệt
                                                                                    │
                                          [AI-3] Backtest đề xuất ◄─────────────────┘
                                                 (không tự áp vào bot)
Dataset có quyền TRAINING ─► [AI-5] WP-06 scorer (logistic) ─► SHADOW ─► PAPER_GATE (owner mở)
```

### AI-1 · Run Report tất định (RR-1), không cần LLM

- **Đầu vào:** ledger PAPER (trade, fill, cost), `bot_decision` (CANDIDATE/ABSTAIN kèm lý do), risk/plan reject, nến.
- **Đầu ra:** JSON phiên bản hóa theo bot × cửa sổ (4 giờ / ngày / đợt thử). Gồm:
  - số tín hiệu, tỷ lệ thành lệnh, phân bố lý do không vào lệnh;
  - win rate, PnL ròng, R trung bình;
  - MAE/MFE (biên lỗ/lãi lớn nhất khi đang giữ lệnh), thời gian giữ, chi phí, drawdown;
  - so sánh theo khung/coin/tham số.
- Lưu bảng `run_report` (bất biến, có hash) và hiển thị trên Admin cùng thẻ Telegram.
- **Nghiệm thu:** tái lập được từ ledger; con số khớp Admin/report; test với ledger thật của đợt 10 bot.

### AI-2 · AI Analyst bằng LLM (RR-2/RR-3)

- **Đầu vào:**
  - JSON của AI-1;
  - đặc tả chiến lược (rule, tham số, release);
  - bối cảnh thị trường (biến động, xu hướng từ nến đã có);
  - digest sự kiện (khi có AI-4).
- **Gọi Claude API qua tool-use/structured output.** Model và chi phí cần owner chốt. Đầu ra là JSON theo schema:
  - `findings[]`: nhận định + `evidence_refs` trỏ tới trường cụ thể trong report;
  - `hypotheses[]`;
  - `proposals[]`: đổi tham số/khung/coin, mỗi đề xuất kèm lý do và rủi ro;
  - `confidence`.
- **Rào chắn:**
  - Mọi con số trong lời văn phải trích từ report; validator đối chiếu, sai thì loại.
  - Không gửi secret hay dữ liệu cá nhân.
  - Lưu prompt hash, phiên bản model và token để kiểm toán.
  - Giới hạn chi phí theo ngày.
  - Không bao giờ tự đổi cấu hình bot.
- **Đầu ra cho người:** thẻ Telegram "Phân tích AI" sau mỗi tổng kết, kèm trang Admin.
- **Nghiệm thu:** chạy trên report thật của đợt 10 bot; 100% con số có evidence; đề xuất parse được thành job backtest.

### AI-3 · Vòng đề xuất → kiểm chứng

- Owner bấm duyệt một đề xuất trên Admin hoặc Telegram.
- Hệ thống tạo job backtest/replay (đã có) cho tham số mới trên cùng dữ liệu, rồi so với bản gốc theo out-of-sample.
- Chỉ owner mới publish bot config mới (MFA).
- **Nghiệm thu:** đề xuất → backtest → bảng so sánh → publish, có audit đầy đủ.

### AI-4 · Nguồn tin S1 (WP-05 lát đầu)

- **Nguyên tắc:** quyền dữ liệu rõ, metadata trước, không lưu bài viết đầy đủ.
- **Ứng viên** (phải kiểm điều khoản trước khi chọn):
  - (a) lịch công bố vĩ mô chính thức của cơ quan Mỹ: BLS/BEA/Fed. Đây là dữ liệu công, mở khóa H06.
  - (b) chỉ số Fear & Greed hoặc dữ liệu thị trường phái sinh công khai của Binance: funding, open interest, long/short. Cùng nhóm "dữ liệu thị trường công khai" owner đã duyệt cho PAPER.
  - (c) RSS thông báo chính thức của sàn hoặc dự án.
  - (d) mạng xã hội (X, Reddit, Telegram channel): làm sau, vì API trả phí và điều khoản hạn chế dùng cho ML.
- **Chuẩn hóa:** sự kiện có thời điểm công bố (không nhìn trước), symbol liên quan, loại sự kiện, nguồn.
- **Đưa vào:**
  - snapshot quyết định (H06 blackout, cờ sự kiện);
  - digest cho AI-2.
- **Nghiệm thu:** H06 không còn `MACRO_UNKNOWN`; có ≥ 1 nguồn chạy thật với policy đã duyệt.

### AI-5 · Mở khóa WP-06 bằng dữ liệu được phép

- Owner quyết định có cấp mục đích **TRAINING** (nội bộ, phi thương mại) cho dữ liệu Binance REST công khai và ledger PAPER của chính hệ thống hay không. Đây là điểm chặn duy nhất của dataset thật.
- Khi được cấp:
  - backtest 20 coin × nhiều tháng sinh dataset ≥ 1.000 mẫu;
  - chạy train → TEST → SHADOW;
  - nối nhãn post-mortem từ RAM vào dataset bền.
- Sửa service `learning-evaluator`.

## 3. Quyết định cần owner

| # | Quyết định | Ảnh hưởng |
|---|---|---|
| 1 | Dùng Claude API cho AI Analyst: model, ngân sách/ngày, nơi lưu API key (chỉ `.secrets/`) | AI-2 |
| 2 | Nguồn tin lát đầu: (a) lịch vĩ mô chính thức, (b) phái sinh/sentiment công khai, (c) RSS chính thức; mạng xã hội để sau | AI-4 |
| 3 | Cấp mục đích TRAINING nội bộ cho dữ liệu Binance công khai và ledger PAPER | AI-5 |
| 4 | Đề xuất AI chỉ là gợi ý + backtest, không tự áp | AI-3 (mặc định đề xuất: có) |

## 4. Thứ tự đề xuất

1. **AI-1** (không phụ thuộc quyết định, làm ngay trên dữ liệu đợt 10 bot).
2. **AI-2** (sau quyết định #1).
3. **AI-3**.
4. **AI-4** (sau quyết định #2).
5. **AI-5** (sau quyết định #3).

AI-1 và AI-4 có thể chạy song song.

## 5. Quyết định owner (16/11/2026)

1. **AI engine:** không gọi API trả phí trực tiếp. Dùng các CLI/desktop agent đã cài trên máy (Claude Code CLI / Claude Desktop, OpenAI Codex CLI, Google Antigravity), sau này có thể chạy trên máy ảo. Mỗi chủ đề phân tích là một session được lưu lại; "huấn luyện" là tích lũy ngữ cảnh ngay trên các session đó. Nghiên cứu cách gọi headless/lưu session: `docs/research/ai-cli-agent-engine-2026-11-16.md`.
2. **Nguồn tin:** toàn bộ tin tức công khai từ các trang uy tín, đặc biệt là rà X và các thread cộng đồng chia sẻ chỉ báo, lịch phát hành và update của **mọi coin trong danh sách Futures**. Nghiên cứu nguồn và điều khoản: `docs/research/news-social-sources-2026-11-16.md`.
3. **TRAINING:** **đồng ý** cấp mục đích TRAINING nội bộ cho dữ liệu Binance công khai và ledger PAPER của hệ thống. Vẫn phải ghi policy/evidence qua Source Registry.
4. **Đề xuất của AI:** chỉ là gợi ý; bắt buộc backtest trước, owner tự publish.

Owner yêu cầu: triển khai AI-1 ngay, đồng thời nghiên cứu các bước sau.

## 6. Tiến độ 16/11/2026

- **AI-1 lát 1 — đã triển khai:**
  - `domain/portfolio/run_report.py` (`ai1-run-report-v1`, JSON tất định + SHA-256), `application/run_report.py` (loader chỉ đọc) và CLI `python -m trading_bot.cli.run_report`.
  - Nội dung: cấu hình bot; phễu quyết định (tách `SYMBOL_OUT_OF_BOT_UNIVERSE`); tín hiệu → lệnh; hiệu suất từ `bot-report.v2`; R-multiple, MAE/MFE; theo coin; nhóm theo họ chiến lược/khung; xếp hạng; cảnh báo mẫu nhỏ.
  - Đã chạy trên ledger thật của đợt 10 bot.
  - **Còn mở:**
    - lưu report bền (bảng bất biến) cùng role đọc báo cáo riêng (CLI hiện chạy bằng role admin migrator vì RLS admin-web cần actor);
    - lưu bền lý do từ chối plan/risk của candidate (hiện chỉ có trong log runtime);
    - lịch tự sinh report.
- **AI-2 — nghiên cứu xong** (`docs/research/ai-cli-agent-engine-2026-11-16.md`):
  - Claude Code CLI (`-p`, `--output-format json`, `--json-schema`, `--session-id/--resume`) làm engine chính.
  - Codex CLI (`codex exec --output-schema`, `exec resume`) làm dự phòng.
  - Antigravity CLI (`agy`) sau khi cài.
  - Bỏ Gemini CLI vì đã ngừng cho tài khoản free/Pro/Ultra.
  - Các binary nằm trong app bundle, không có trên PATH, nên đường dẫn phải lấy từ config.
  - "Huấn luyện trên session" thực tế là tích lũy ngữ cảnh qua resume, file knowledge có owner duyệt và tóm tắt định kỳ. **Không phải fine-tune model.**
  - Điều khoản: subscription Claude dành cho dùng cá nhân thông thường; job định kỳ quy mô lớn nên dùng API key. Đây là câu hỏi cho owner.
- **AI-4 — nghiên cứu xong** (`docs/research/news-social-sources-2026-11-16.md`):
  - **Điều khoản bên thứ ba ưu tiên hơn quyền TRAINING nội bộ.** Cointelegraph/The Block/TradingView/Telegram/Discord/Reddit cấm hoặc hạn chế AI/ML; X cấm huấn luyện foundation model. Mặc định không cấp `training` cho X.
  - **S1 (làm ngay, hợp lệ):**
    - lịch vĩ mô chính thức BLS/BEA/FOMC (public domain), mở khóa H06;
    - phái sinh công khai Binance (funding, OI, long/short; lưu snapshot theo giờ vì chỉ giữ 30 ngày, giới hạn 1.000 request/5 phút);
    - diff `exchangeInfo` để phát hiện listing/delisting;
    - CoinGecko Demo để map coin → website/X/GitHub.
  - **S2:**
    - X API pay-per-use: khoảng 0,005 USD/bài; danh sách tài khoản chính thức theo coin + từ khóa sự kiện; ước tính khoảng 375–675 USD/tháng, có trần chi;
    - CryptoPanic API;
    - Tokenomist (lịch unlock, trả phí);
    - kênh thông báo Binance (cần API key chỉ đọc, owner phải duyệt).
  - **Tránh:** scraping X và bên bán lại dữ liệu X.

## 7. AI-2 lát 1 (16/11/2026)

- **Đã làm:**
  - `application/ai_analyst.py`: schema `analyst_output.v1` (findings/hypotheses/proposals có `evidence_refs`, confidence), prompt `ai2-prompt-v1` đánh dấu report là dữ liệu, `topic_report` theo họ chiến lược.
  - **Validator số liệu:**
    - JSON Pointer phải tồn tại;
    - mọi số trong câu phải bằng giá trị tại trường được trích, làm tròn đúng ≤ ½ đơn vị chữ số cuối;
    - chấp nhận kiểu VN/EN và % của tỷ lệ;
    - bỏ qua số nằm trong tên bot/coin/release.
  - Port `AnalystEngine`; adapter `ClaudeCliEngine` (`-p --output-format json --json-schema --tools "" --restricted --permission-prompts none --max-budget-usd`, `--session-id`/`--resume`) và `CodexCliEngine` (`exec -s read-only --output-schema --json -o`, `exec resume`). Chạy trong thư mục tạm, biến môi trường tối thiểu, có timeout.
  - Bảng `analyst_session` / `analyst_run` (migration `e7a9c1d3f5b8`, RLS bắt buộc, admin-web chỉ SELECT).
  - Script host `scripts/run_ai_analyst.py`: report → session theo `family:<H..>` → engine → validator → lưu → Telegram (chỉ kết quả hợp lệ).
  - Engine giả `--engine fake` để kiểm luồng.
- **Chạy thật 16/11:** cả hai engine bị chặn ở **tài khoản**:
  - Claude Code CLI trong bundle Claude Desktop báo `Not logged in · Please run /login` (CLI không dùng chung đăng nhập với app);
  - Codex đã đăng nhập ChatGPT nhưng `Your workspace is out of credits`.
  - Luồng với engine giả chạy thông suốt trên report 24 giờ của 10 bot: 3 chủ đề hợp lệ, lượt 2 tiếp tục session, `run_count` tăng.
- **Còn mở:**
  - owner đăng nhập Claude CLI hoặc nạp credit Codex rồi chạy thật;
  - ảnh thẻ "Phân tích AI" (hiện gửi văn bản);
  - lịch tự chạy sau mỗi tổng kết;
  - file knowledge theo chủ đề + xoay vòng session trước mốc 30 ngày;
  - trang Admin xem `analyst_run`;
  - AI-3 (đề xuất → backtest).

## 8. AI-3 lát 1 — đề xuất → backtest (16/11/2026)

- **Owner:** "Có, làm AI-3"; cấp quyền BACKTEST_RESEARCH cho nguồn Binance REST (policy revision 5, cùng `paper_demo` + `training`).
- **Đã làm:**
  - Schema `analyst_output.v2` / prompt `ai2-prompt-v2`: `changes.field` chỉ nhận `PROPOSAL_FIELDS` (lookback H03, release, rủi ro mỗi lệnh, số vị thế, đòn bẩy) trong khoảng cố định; `validate_proposals` loại bot lạ, trường lạ, giá trị ngoài khoảng; `apply_proposal` tạo overlay mới (không ghi DB).
  - Replay nhận tham số chiến lược (`BacktestReplayJobSubmission.strategy_parameters` → settings `strategy.parameters`, profile/family hash; job cũ không tham số giữ hash cũ).
  - Sửa replay: bằng chứng thoát H03-v3 (`h03-exit-v3-*`) và **snapshot tài khoản replay mang mốc của từng lượt** (trước đó backtest > 1 ngày bị `ACCOUNT_STATE_STALE` gần như toàn bộ).
  - `application/proposal_backtest.py`: bản gốc vs bản đề xuất trên cùng bundle nến đã lưu mỗi coin, gộp chỉ số, kết luận thận trọng (`MIN_TRADES_FOR_VERDICT = 10`).
  - Bảng `proposal_backtest` (migration `86afb7c1ba2d`, append-only, admin-web chỉ SELECT; mỗi đề xuất một lần).
  - Thẻ Telegram "Backtest đề xuất AI" + `scripts/run_proposal_backtest.py` (`--dry-run`, `--max-symbols`, `--send-telegram`).
- **Giới hạn đã biết:** replay một coin, 1 vị thế, 1x → `risk.max_positions`/`risk.max_leverage` là UNSUPPORTED; khớp ở giá mở nến kế tiếp của khung vào lệnh (nến 1m chỉ có ~1,5 ngày); cửa sổ theo dữ liệu đã backfill (15m ≈ 8 ngày, 1h ≈ 32 ngày, 4h ≈ 130 ngày sau warm-up); không phải kiểm định thống kê.
- **Còn mở:**
  - ~~nút "Tạo bản nháp config"~~ — **xong lát 2 (16/11):** mục "Đề xuất AI đã backtest" trên trang chi tiết bot, tạo DRAFT cho kết luận BETTER/INCONCLUSIVE nếu cấu hình chưa đổi; owner kiểm định + công bố (MFA khi đổi rủi ro);
  - backtest danh mục nhiều coin để kiểm `max_positions`;
  - chạy tự động sau mỗi lượt AI Analyst;
  - trang Admin xem `analyst_run` + `proposal_backtest`.

## 9. AI-4 lát 1 — lịch vĩ mô chính thức (16/11/2026)

- **Xong:** BEA + FOMC thu thật qua Source Registry (PAPER_DEMO, đã duyệt), bảng `macro_calendar_fetch` point-in-time, H06 nhận `macro_blackout`/bằng chứng/độ mới thay vì luôn `MACRO_UNKNOWN`. AI-3 tự backtest sau lượt AI theo lịch.
- **Chặn:** BLS cần thông tin liên hệ của chủ bot (quyết định owner).
- **Tiếp theo:** digest lịch vĩ mô cho AI/Telegram; (b) phái sinh Binance; (c) diff exchangeInfo (listing/delisting).
