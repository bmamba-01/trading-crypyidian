# AI CLI làm engine cho AI Analyst (AI-2)

Ngày nghiên cứu: 16/11/2026. Bối cảnh: owner quyết định AI-2 không gọi API trả phí trực tiếp mà điều khiển agent CLI đã cài trên máy (sau này trên VM).

## 1. Hiện trạng trên máy (chỉ kiểm `--version`/`--help`, không đăng nhập, không gửi prompt)

- `which claude codex gemini antigravity`: **không có binary nào trên PATH**.
- Claude Code **2.1.321** nằm trong bundle của Claude Desktop: `~/Library/Application Support/Claude/claude-code/2.1.321/.../claude.app/Contents/MacOS/claude`. Có các cờ `-p`, `--output-format`, `--json-schema`, `--session-id`, `--resume`, `--fork-session`, `--no-session-persistence`, `--max-budget-usd`, `--permission-mode` (gồm `dontAsk`, `plan`), `--restricted`, `--bare`.
- Codex CLI **0.169.0-alpha.11.1** nằm trong `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`. Có `exec`, `exec resume`, `exec fork`, `--json`, `--output-schema`, `-o`, `-s read-only`, `--ephemeral`.
- Antigravity.app **2.32.1** đã cài, nhưng **không có `agy` trên PATH**. Có `~/.gemini/antigravity/bin/agentapi` (lệnh `new-conversation`, `send-message`). Đây là binary nội bộ, không có tài liệu, **không nên dựa vào**.
- Gemini CLI chưa cài.

## 2. Claude Code CLI

- Headless: `claude -p` đọc stdin (giới hạn 10MB). `--output-format json` trả về `result`, `session_id`, `usage`, `total_cost_usd` và chi phí theo model. Hai con số chi phí là ước tính phía client [S1].
- Đầu ra có cấu trúc: `--json-schema '<schema>'` cùng `--output-format json` cho kết quả nằm ở `structured_output`. Nếu model từ chối thì trường này vắng, nên phải kiểm có trường trước khi dùng [S1].
- Phiên làm việc:
  - `--session-id <uuid>` đặt ID trước, `--resume <id>` tiếp tục, `--fork-session` rẽ nhánh;
  - hai tiến trình resume cùng một phiên có thể mất message [S1][S2];
  - transcript lưu tại `~/.claude/projects/<project>/<session-id>.jsonl` và **bị xóa sau 30 ngày** (`cleanupPeriodDays`); định dạng nội bộ, có thể đổi [S2].
- An toàn:
  - `--bare` bỏ qua hooks, MCP, CLAUDE.md và auto memory, nhưng **không dùng đăng nhập subscription** mà cần `ANTHROPIC_API_KEY` [S1];
  - `--permission-mode dontAsk` và `--permission-prompts none` từ chối mọi lệnh gọi cần duyệt [S1];
  - `--restricted` gỡ Bash và WebFetch (theo `--help` local);
  - `--max-turns` và `--max-budget-usd` giới hạn lượt và chi phí [S2].
- Agent SDK (Python) là lựa chọn thay thế, cùng vòng lặp agent [S1].
- Điều khoản [S3]:
  - OAuth của gói Free/Pro/Max/Ultra dành cho "ordinary use"; hạn mức gói giả định dùng "ordinary, individual";
  - sản phẩm xây bằng Agent SDK phải dùng API key;
  - kết luận: job định kỳ nhỏ cho chính owner, bằng binary gốc, là vùng xám chấp nhận được; chạy dày hoặc phục vụ người khác thì phải dùng API key.
- Claude Desktop: không tìm thấy giao diện scripting được tài liệu hóa (Desktop chỉ resume được phiên CLI [S2]), nên điều khiển qua binary CLI.

## 3. OpenAI Codex CLI

- Chế độ không tương tác `codex exec`:
  - prompt lấy từ đối số hoặc stdin;
  - `--json` cho luồng JSONL gồm `thread.started`, `turn.completed`, `item.*` và `error`;
  - `--output-schema <file>` ép kiểu đầu ra;
  - `-o <file>` ghi riêng message cuối [S4].
- **Sandbox mặc định của `exec` là read-only** [S4].
- Phiên: `codex exec resume <SESSION_ID>`, `--last` hoặc `exec fork`. `--ephemeral` để không lưu. Thư mục `~/.codex/sessions` có trên máy (quan sát local). Tài liệu không nêu đường dẫn.
- AGENTS.md: ghép từ gốc repo xuống thư mục làm việc, giới hạn `project_doc_max_bytes` mặc định 32 KiB [S5].
- Xác thực:
  - mặc định dùng đăng nhập đã lưu;
  - `CODEX_API_KEY` cho từng lần gọi;
  - đăng nhập ChatGPT trong CI chỉ nên dùng trên runner tin cậy;
  - `~/.codex/auth.json` phải giữ như mật khẩu [S4].

## 4. Google Antigravity / Gemini CLI

- Gemini CLI ngừng phục vụ tài khoản free và Google AI Pro/Ultra từ **18/06/2026**. Thông tin này theo báo chí; chưa đọc được bài gốc của Google [S6]. **Không chọn Gemini CLI.**
- Antigravity CLI `agy` hỗ trợ headless [S7]:
  - `-p` với `--output-format json`, trả về `conversation_id` và `usage` (token);
  - `--json-schema`;
  - `--conversation <ID>` hoặc `--continue` để tiếp tục hội thoại;
  - `--sandbox` và `--print-timeout`;
  - quyền mặc định là `request-review`, thao tác không được duyệt sẽ bị soft-deny;
  - xác thực dùng credential cache sau một lần `agy` tương tác.
- Vẫn đọc `GEMINI.md`/`AGENTS.md` và `~/.gemini/GEMINI.md` [S8].
- Phải cài `agy` riêng. App desktop không đưa binary này lên PATH.

## 5. Thiết kế `AnalystEngine`

```python
class AnalystEngine(Protocol):
    def analyze(self, req: AnalysisRequest) -> AnalysisResult: ...


# AnalysisRequest: topic_key, run_report_path, schema_path, session_ref|None, timeout_s, budget
# AnalysisResult: structured_output, raw_stdout, session_id, engine, cli_version, model, usage, cost_usd, exit_code
```

| Việc | Claude | Codex | Antigravity |
|---|---|---|---|
| Đầu vào | stdin, hoặc file trong thư mục làm việc tạm | stdin / file | file + `-p` |
| Ép schema | `--json-schema` | `--output-schema` | `--json-schema` |
| Chỉ đọc | `--restricted --permission-mode dontAsk --permission-prompts none`, `--disallowedTools WebFetch,WebSearch,Bash` | `-s read-only --ephemeral`* | `--sandbox`, không dùng `--dangerously-skip-permissions` |
| Phiên | `--session-id` → `--resume` | `thread.started` → `exec resume` | `conversation_id` → `--conversation` |

\* Khi cần lưu phiên thì bỏ `--ephemeral`.

- **Chủ đề phiên:** mỗi `topic_key` (`bot:<id>`, `family:<strategy>`) có một hàng trong bảng `analyst_session`, gồm:
  - engine, `session_id`, `created_at`, `turns`, `tokens_cum`;
  - `knowledge_version`, `status`.
- **Chạy:**
  - wrapper Python dùng `subprocess` với `timeout`, `cwd` là thư mục tạm chỉ chứa Run Report và knowledge file;
  - env tối thiểu, không truyền biến `.secrets`.
- **Kiểm toán:** lưu bảng `analyst_run`:
  - `input_sha256` (report + prompt + schema);
  - raw stdout, CLI version, model, usage, `session_id`.
- **Kiểm tra đầu ra:** validator bắt buộc mọi con số có `evidence_refs` trỏ đúng trường trong report. Sai thì loại và không gửi Telegram.
- **Xoay vòng:** khi `tokens_cum` hoặc số lượt vượt ngưỡng:
  - yêu cầu phiên tóm tắt bài học vào `knowledge/<topic>.md`;
  - owner duyệt bản tóm tắt;
  - mở phiên mới và nạp file đó.
  - Chủ động xoay trước mốc 30 ngày Claude tự xóa transcript.
- **Host hay container:** credential CLI nằm ở keychain/home của host. MVP nên chạy trên host bằng user riêng. Trên VM thì đăng nhập một lần với user dịch vụ. Container chỉ hợp lý khi dùng API key.

**Rủi ro và giảm thiểu:**

- **Kết quả không tất định:** lưu raw output và hash đầu vào; đề xuất chỉ là gợi ý, phải qua backtest ở AI-3.
- **Prompt injection từ tin tức (AI-4):** chỉ đưa digest đã chuẩn hóa, đánh dấu là dữ liệu; tắt mạng và shell; chỉ nhận đầu ra theo schema.
- **ToS và rate limit:** giới hạn số lần chạy mỗi ngày, xử lý lỗi `rate_limit`, fallback sang engine khác; quy mô lớn thì chuyển API key.
- **Chi phí:** dùng `--max-budget-usd` và trường `usage` (trên gói subscription chỉ là ước tính).
- **Lộ secret:** cwd tạm, không mount repo hay `.secrets/`, quét output.
- **Tái lập:** ghi CLI version, model, schema version; không parse `.jsonl`.

## 6. "Huấn luyện trên phiên": kỳ vọng đúng

- **Làm được:**
  - tích lũy trong ngữ cảnh: resume phiên cũ, nhưng phần cũ bị nén (compaction) và giới hạn bởi context window [S2];
  - file bộ nhớ (CLAUDE.md, AGENTS.md, GEMINI.md, `knowledge/<topic>.md`) nạp lại mỗi lần chạy;
  - tóm tắt định kỳ, tách "bài học đã kiểm chứng" (có backtest) khỏi "giả thuyết".
- **Không làm được:** fine-tune hay cập nhật trọng số model qua các CLI này; model không tự "giỏi lên".
- **Hệ quả:** chất lượng phụ thuộc knowledge file và dữ liệu đưa vào. Phiên có thể mất (TTL, đổi bản, đổi máy), nên nguồn sự thật là DB và knowledge file, không phải transcript. Học máy thực sự vẫn là AI-5 (scorer logistic).

## Đề xuất triển khai AI-2

1. Viết ADR: chọn Claude Code làm engine chính, Codex dự phòng, Antigravity (`agy`) để sau khi cài và kiểm. Loại Gemini CLI.
2. Viết JSON Schema `analyst_output.v1` gồm `findings`, `evidence_refs`, `hypotheses`, `proposals`, `confidence`, cùng validator số liệu và unit test.
3. Xây port `AnalystEngine` với hai adapter `ClaudeCliEngine` và `CodexCliEngine`, đọc đường dẫn binary từ config (không phụ thuộc PATH).
4. Tạo migration cho bảng `analyst_session`, `analyst_run` và `analyst_knowledge` (có phiên bản).
5. Dùng engine giả trong E2E, gửi kết quả sang thẻ Telegram "Phân tích AI".
6. Chạy thử trên Run Report thật của đợt 10 bot, đo token, thời gian và tỷ lệ pass validator.
7. Viết job xoay vòng/tóm tắt phiên, với bước owner duyệt knowledge file.

**Câu hỏi mở cho owner:**

- Chấp nhận rủi ro điều khoản khi dùng subscription cá nhân cho job định kỳ, hay dùng API key từ đầu?
- Tần suất chạy tối đa mỗi ngày?
- Topic theo bot hay theo họ chiến lược?
- Có cài `agy` không?
- VM dùng tài khoản nào để đăng nhập CLI?

## Nguồn (truy cập 16/11/2026)

- [S1] https://code.claude.com/docs/en/headless
- [S2] https://code.claude.com/docs/en/sessions.md
- [S3] https://code.claude.com/docs/en/legal-and-compliance.md
- [S4] https://learn.chatgpt.com/docs/non-interactive-mode (chuyển hướng từ developers.openai.com/codex/noninteractive)
- [S5] https://developers.openai.com/codex/guides/agents-md (qua kết quả tìm kiếm, chưa fetch trực tiếp)
- [S6] https://9to5google.com/2026/06/17/gemini-cli-code-assist-shutting-down/ và https://www.theregister.com/ai-ml/2026/05/20/bye-bye-gemini-cli-google-nudges-devs-toward-antigravity/5243605 (thứ cấp)
- [S7] https://antigravity.google/docs/cli/headless
- [S8] https://antigravity.google/docs/cli/gcli-migration/
- Local: kết quả `--version`/`--help` của các binary nêu ở mục 1.
