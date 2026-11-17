# Theo dõi tiến độ trên It's a Plan (tự host local)

**Lập ngày:** 16/11/2026 · **Phiên bản tool:** It's a Plan v1.11.0 (phát hành 14/11/2026) · **Nguồn đã kiểm:** [self-hosting](https://github.com/croffasia/itsaplan/blob/main/docs/self-hosting.md), [runner/MCP](https://github.com/croffasia/itsaplan/tree/main/packages/runner), [Claude Code MCP](https://code.claude.com/docs/en/mcp) — kiểm ngày 16/11/2026.

[It's a Plan](https://github.com/croffasia/itsaplan) là issue tracker mã nguồn mở có [MCP](../research/glossary-vi.md#as-theo-dõi-tiến-độ-dự-án-và-agent-trên-its-a-plan-16112026) server tích hợp. Dự án dùng nó làm bảng theo dõi trực quan cho [Project plan](./project-status.md) và các WBS. **Tài liệu trong repo vẫn là nguồn bằng chứng** (test, commit, memory); tracker là bản phản chiếu để quan sát và để agent cập nhật tiến độ.

## Truy cập

| Thành phần | Địa chỉ / vị trí |
| --- | --- |
| Web | <http://localhost:3001> — board project `CRY` (Crypidian Futures Bot) |
| API / MCP | <http://localhost:3000>, MCP tại `http://localhost:3000/mcp` (Streamable HTTP) |
| Mã triển khai | `/Users/macbook/Work/itsaplan` (clone upstream, ngoài repo này), `.env` quyền 600, `docker-compose.override.yml` chỉ mở cổng trên `127.0.0.1` |
| Script import/sync | `/Users/macbook/Work/itsaplan/local/crypidian/` (`merge_wbs.py`, `sync_wbs.py`, `state.json` ánh xạ ref → issue id, dữ liệu `wbs-merged-2026-11-16.json`) |
| MCP cho Claude Code | Server `itsaplan`, phạm vi **user** (có hiệu lực ở mọi worktree trong `.claude/worktrees/`) |
| MCP cho Codex | `~/.codex/config.toml`, mục `[mcp_servers.itsaplan]` (`url` + `http_headers`; app GUI không đọc biến môi trường shell nên không dùng `bearer_token_env_var`) |
| MCP cho Antigravity | `~/.gemini/config/mcp_config.json`, mục `mcpServers.itsaplan` (`serverUrl` + `headers`); app Antigravity đọc đúng đường dẫn này |

Stack chạy bằng Docker Compose (postgres 17, RustFS, api, worker, bot, web), image ghim `VERSION=1.11.0`, telemetry tắt. Tài khoản đăng ký đầu tiên là admin instance.

```bash
cd /Users/macbook/Work/itsaplan && docker compose up -d      # khởi động
cd /Users/macbook/Work/itsaplan && docker compose ps         # kiểm tra
```

Cập nhật phiên bản: đổi `VERSION` trong `.env`, rồi `git pull && docker compose pull && docker compose up -d`. Service `migrate` tự dump DB vào volume `db-backups` trước khi áp migration. Đọc release notes trước khi nâng.

## Cấu trúc trên tracker

- **Initiative** (13): Plan 01–07, Trader workbench, Multi-Bot, Bảo mật, SIG/CK/RT, công thức/Strategy Card, AI-1…AI-5.
- **Epic** = work package (`WP-06`, `MB-05`, `SIG-3`…); **Task** = task/lát trong WBS. Sub-issue lồng tối đa 3 cấp.
- Tiêu đề luôn bắt đầu bằng `[REF]`, ví dụ `[WP-06/Task7B] Cửa sổ quan sát 30 ngày`; field `WBS Ref` chứa đúng mã. Liên kết `blocked by`/`relates` theo phụ thuộc trong WBS.
- Nhãn đáng chú ý: `main-only` (giao nộp mới có trên `origin/main`, chưa merge `dev`), `owner-decision`, `data-rights`, `live-locked`.

### Ánh xạ trạng thái

| Trạng thái trong tài liệu | Cột trên board | Loại trạng thái |
| --- | --- | --- |
| Chưa bắt đầu, chưa ưu tiên | Backlog | backlog |
| Việc kế tiếp đã ưu tiên | Todo | unstarted |
| Đang triển khai | In Progress | started |
| Bị chặn / tạm hoãn (license, owner, venue…) | Blocked | started |
| Hoàn thành phần nền (MVP) | Done (MVP) | completed |
| Hoàn thành (có test/commit evidence) | Done | completed |
| Hủy / bị thay thế | Canceled | canceled |

Tự động đóng subtask/parent của project đang **tắt** để trạng thái từng item phản ánh đúng bằng chứng.

## Quy trình cho agent khi làm task

1. **Tìm ticket:** dùng tool MCP `search_issues` (project `CRY`) với mã WBS, ví dụ `WP-06/Task7B` hoặc `SIG-3`. Nếu chưa có, tạo issue mới dưới Epic/initiative phù hợp, tiêu đề `[REF] …`, đặt field `WBS Ref`.
2. **Bắt đầu:** chuyển ticket sang `In Progress` và bình luận ngắn nêu nhánh/worktree, phạm vi vòng làm việc.
3. **Trong khi làm:** nếu bị chặn bởi quyết định owner, quyền dữ liệu hoặc phụ thuộc ngoài, chuyển `Blocked` và bình luận lý do cụ thể.
4. **Kết thúc vòng:** chỉ chuyển `Done` (hoặc `Done (MVP)` nếu còn phần ngoài phạm vi) khi đã đạt điều kiện của [AGENTS.md](../../AGENTS.md): unit + E2E liên quan chạy đạt, memory đã cập nhật và đọc kiểm chứng. Bình luận commit hash, lệnh test và kết quả; phần còn lại tách thành ticket con ở `Backlog`/`Todo`.
5. **Đồng bộ tài liệu:** cập nhật `project-status.md`/WBS trong cùng vòng như trước đây. Tracker không thay thế tài liệu.
6. **Khi MCP không dùng được** (stack tắt, key hết hạn): không chặn công việc. Ghi việc cần cập nhật ticket vào memory và cập nhật sau.

Không ghi secret, credential, dữ liệu cá nhân hay thông tin tài khoản sàn vào ticket hoặc bình luận.

## Đồng bộ lại hàng loạt từ tài liệu

Khi cần đối soát lại toàn bộ WBS (ví dụ sau khi hợp nhất `dev` và `main`):

1. Cập nhật file `group-*.json`/`extra.json` (định dạng trong `BRIEF.md` cùng thư mục).
2. `python3 -I merge_wbs.py <thư mục chứa group-*.json> wbs-merged-<ngày>.json`. Script báo ref trùng và tham chiếu không khớp.
3. `python3 -I sync_wbs.py wbs-merged-<ngày>.json --dry-run`, sau đó chạy không có `--dry-run`. Script đọc `state.json` nên item đã có sẽ được PATCH (tiêu đề, mô tả, cột, nhãn, parent), không tạo trùng.

Lưu ý: sync ghi đè tiêu đề/mô tả/cột theo dữ liệu tài liệu. Bình luận và lịch sử do agent thêm vẫn được giữ.

## API key và MCP

- Key cá nhân `claude-code-mcp` tạo ngày 16/11/2026, **hết hạn sau 365 ngày** (khoảng 16/11/2027). Key lưu ở `~/.claude.json` (cấu hình MCP user scope) và `/Users/macbook/Work/itsaplan/.mcp-api-key` (quyền 600, ngoài repo).
- Gia hạn: tạo key mới ở <http://localhost:3001/account/api-keys>, ghi đè file `.mcp-api-key`, rồi chạy:

```bash
claude mcp remove itsaplan -s user
```

```bash
claude mcp add --scope user --transport http itsaplan http://localhost:3000/mcp --header "Authorization: Bearer $(cat /Users/macbook/Work/itsaplan/.mcp-api-key)"
```

- Codex và Antigravity giữ key ngay trong file cấu hình (quyền 600). Khi gia hạn, thay chuỗi `Bearer itp_…` trong `~/.codex/config.toml` và `~/.gemini/config/mcp_config.json`, rồi khởi động lại app. Kiểm tra Codex bằng `codex mcp get itsaplan` (binary nằm trong `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`).
- Kiểm tra kết nối: `claude mcp get itsaplan` phải báo `✔ Connected`. Session Claude Code đang mở cần khởi động lại để thấy tool `mcp__itsaplan__*`.
- MCP bật ở cấp team và project `CRY`. OAuth của MCP chỉ dùng được qua HTTPS hoặc localhost; ở đây dùng key Bearer.
