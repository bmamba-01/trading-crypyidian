# Ghi nhớ triển khai

Tệp này là bộ nhớ bền vững của các vòng implement. Trước mỗi vòng phải đọc toàn bộ phần liên quan; sau vòng phải ghi nhật ký, rồi đọc lại mục vừa ghi để xác minh. Lưu quyết định, lỗi và bài học có ích cho vòng sau; không ghi secret, credential, dữ liệu cá nhân hoặc thông tin tài khoản sàn.

## Trạng thái ban đầu

- **Ngày lập:** 2026-10-21.
- **Phạm vi:** mới bổ sung quy trình skill, ngôn ngữ và kế hoạch; chưa có vòng implement mã nguồn nào được xác nhận trong bản ghi này.
- **Repo/Git:** workspace gốc `C:\Works\Trading-Crypidian` hiện chưa là Git repository. `freqtrade-develop` là bản Freqtrade upstream, remote `origin` trỏ `https://github.com/freqtrade/freqtrade.git`; không dùng remote này để đẩy sản phẩm độc lập. Thư mục `freqtrade` chỉ có `.git` dang dở, nhánh hiện tại hỏng/chưa có commit và origin cũng trỏ upstream. `gh auth status` báo credential của `konglee2401-hub` không hợp lệ.
- **Hệ quả:** chưa thể an toàn đồng bộ tài liệu lên GitHub `main`, tạo nhánh `dev` từ repo dự án, hoặc commit. Cần khôi phục xác thực và xác nhận/thiết lập repo đích do người dùng sở hữu trước bước push. Không sửa/push upstream công khai.
- **Kiểm tra tài liệu:** đã kiểm tra liên kết Markdown nội bộ và code fence ở vòng trước; chưa xác nhận fragment/anchor tự động.
- **Test:** chưa có mã nguồn sản phẩm; unit/E2E test cho sản phẩm chưa chạy.

## Nhật ký các vòng

### 2026-10-21 — Chuẩn hóa skill nghiên cứu và implement

- **Mục tiêu và phạm vi:** thêm hướng dẫn bền vững để AI tra cứu nguồn/tool cập nhật, dùng đúng ngôn ngữ, ghi memory sau mỗi vòng, và yêu cầu unit + E2E trước commit.
- **Plan/story liên quan:** chuẩn hóa workflow trước khi bắt đầu các plan đánh số; chưa thay đổi mã nguồn sản phẩm.
- **Nguồn/tài liệu hiện hành đã xác minh:** không có quyết định phụ thuộc API/thị trường trong vòng tài liệu này; không cần tra nguồn live. Đã đọc hướng dẫn nội bộ skill creator và quy trình test skill tại `C:\Users\Dell\.codex\plugins\cache\openai-curated-remote\superpowers\6.4.2\skills\writing-skills\testing-skills-with-subagents.md`.
- **Tool/dependency đã kiểm tra:** `git`/remote/status và `gh auth status`. Workspace gốc chưa là repo; `freqtrade-develop` remote trỏ upstream Freqtrade; thư mục `freqtrade` có Git metadata không hoàn chỉnh; GitHub CLI credential không hợp lệ. Không thay đổi remote hoặc nâng dependency.
- **Files/modules thay đổi:** `AGENTS.md`; `.agents/skills/futures-bot-implementation/SKILL.md`; `docs/README.md`; `docs/implementation/README.md`; tệp memory này.
- **Quyết định và giả định:** skill dự án nằm trong `.agents/skills/` và được chỉ dẫn từ `AGENTS.md`. Không push lên upstream Freqtrade. Chỉ đồng bộ `main`/tạo `dev` khi có repo đích do người dùng sở hữu/được ủy quyền và xác thực GitHub hoạt động.
- **Vấn đề phát sinh và nguyên nhân gốc:** không thể fork/push hoặc checkout `dev` an toàn vì repo gốc workspace chưa khởi tạo, repo clone có remote upstream công khai, thư mục `freqtrade` đang ở nhánh hỏng và `gh` token đã hết hiệu lực.
- **Cách xử lý/bài học cho lần sau:** trước triển khai, cần xác thực GitHub lại và làm rõ repo đích cá nhân. Tình huống áp lực baseline cho thấy các cách bỏ qua phổ biến là viện deadline để không tra API, trì hoãn cập nhật memory, hoặc bỏ E2E flaky; skill đã quy định rõ các điểm này. Rà soát sau đó làm rõ E2E phải bao phủ mọi ranh giới quan trọng bị tác động.
- **Unit test:** không áp dụng cho vòng chỉ sửa tài liệu/skill; kiểm tra cấu trúc tài liệu đã đạt: `PASS: 26 Markdown files; local link targets and code fences; skill frontmatter and glossary targets.`
- **E2E test:** không áp dụng cho sản phẩm vì chưa có mã nguồn sản phẩm trong workspace gốc; thay bằng tình huống kiểm chứng skill trước/sau, gồm deadline + API có thể đổi + E2E flaky, E2E runner không khả dụng, và đề xuất tự đưa model vào LIVE. Kết quả: yêu cầu tra nguồn, không commit khi E2E chưa chạy, và cấm tự promotion LIVE đều được nhận diện.
- **Giới hạn kiểm tra hyperlink:** lần thử kiểm tra anchor toàn bộ bằng thuật toán slug tự viết báo nhiều anchor không khớp; quy tắc slug chưa tương thích chắc chắn với GitHub Markdown nên không kết luận đó là liên kết hỏng. Đã xác nhận đường dẫn file cục bộ và ba anchor glossary mới dùng trong skill; cần xác minh anchor các tài liệu cũ bằng renderer/slug chuẩn trước khi đồng bộ tài liệu.
- **Regression/security/data checks:** xác nhận không thêm credential/secrets; chưa có lệnh trade hay dữ liệu mới.
- **Việc còn mở/rủi ro:** xác thực GitHub; thiết lập remote riêng; đồng bộ toàn bộ `docs/` lên `main`; tạo/checkout `dev`; bổ sung kiểm tra anchor Markdown tự động; sau đó mới bắt đầu implementation.
- **Commit/nhánh:** chưa commit; chưa có nhánh dự án hợp lệ.
- **Đã đọc lại mục ghi nhớ sau cập nhật:** có; đối chiếu với kết quả kiểm tra cấu trúc (26 Markdown), ba tình huống kiểm tra skill, trạng thái Git/gh và giới hạn kiểm tra anchor.

### 2026-10-21 — WP-01A: Bootstrap Python và cấu hình an toàn

- **Mục tiêu và phạm vi:** tạo package Python độc lập, khóa môi trường, cấu trúc module, cấu hình PAPER mặc định, health endpoint an toàn, test nền, dependency/license inventory và SBOM. Chưa triển khai database, auth hay gửi lệnh.
- **Plan/story liên quan:** [Plan 01 WP-01A](./01-foundation-database.md#wp-01a--quy-ước-repo-ngôn-ngữ-và-dependency); chuẩn bị các hợp đồng đầu vào cho WP-01B/01C.
- **Nguồn/tài liệu hiện hành đã xác minh (2026-10-21):** [CPython 3.14.8](https://www.python.org/downloads/release/python-3148/); [FastAPI 0.156.4 release notes](https://fastapi.tiangolo.com/release-notes/) (bản mới trong ngày); [uv releases](https://github.com/astral-sh/uv/releases) (0.13.4 phát hành 20/10, cập nhật CA bảo mật; 0.12.22 thêm CPython 3.14.8); [Pydantic Settings releases](https://github.com/pydantic/pydantic-settings/releases) và [advisory đã vá từ 2.14.2](https://github.com/pydantic/pydantic-settings/security/advisories/GHSA-4xgf-cpjx-pc3j); [SQLAlchemy 2.0 changelog](https://docs.sqlalchemy.org/en/20/changelog/changelog_20.html) (2.0.55 hỗ trợ Python 3.14, được ghi cho các bước DB tiếp theo); [pytest-asyncio releases](https://github.com/pytest-dev/pytest-asyncio/releases) (1.4.0); [FastAPI testing guide](https://fastapi.tiangolo.com/tutorial/testing/) và [Starlette TestClient](https://github.com/Kludex/starlette/blob/main/starlette/testclient.py) (hiện khuyến nghị `httpx2`).
- **Tool/dependency đã kiểm tra:** máy có CPython 3.14.3 và uv 0.11.26/0.11.15; PATH `uv` cũ không có bản tải 3.14.8. Cập nhật tại chỗ bị dừng vì Windows báo `uvx.exe` đang được tiến trình khác sử dụng; không kết thúc tiến trình đó. Dùng tạm `uv tool run --from uv==0.13.4 uv ...`, cài CPython 3.14.8 và lock với uv 0.13.4. `uv.lock` khóa 31 package runtime/dev gồm FastAPI 0.156.4, pydantic-settings 2.16.0, pytest 9.1.1, pytest-asyncio 1.4.0, httpx2 2.15.1, mypy 1.20.2, Ruff 0.17.1 và uv-build 0.13.4.
- **Files/modules thay đổi:** `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.env.example`, `README.md`, `src/trading_bot/` và package skeleton, `tests/unit/test_settings.py`, `tests/e2e/test_health_endpoint.py`, `docs/implementation/dependency-inventory.md`, `docs/implementation/sbom.cdx.json`, technical design và glossary links. Thêm kiểm tra nguồn Freqtrade bằng hyperlink cố định theo commit thay đường dẫn cục bộ không tồn tại trong repo clone.
- **Quyết định và giả định:** `uv 0.13.4` được dùng trong lần chạy project này; `uv.lock` là phiên bản/hash chính xác. App settings chỉ cho phép PAPER/DEMO và từ chối LIVE cho tới khi readiness gate được triển khai, kiểm định và phê duyệt. `/health/live` chỉ chứng minh process còn hoạt động; không đại diện readiness DB/feed/sàn.
- **Vấn đề phát sinh và nguyên nhân gốc:** (1) test đầu fail collection đúng vì chưa có package `trading_bot`; (2) test `.env` fail vì `SettingsConfigDict` chưa khai báo `env_file`; (3) test LIVE gate fail vì `Literal` ban đầu chấp nhận LIVE; cả ba được sửa từ nguyên nhân. TestClient phát cảnh báo khi chỉ cài `httpx`; dùng `httpx2` theo khuyến nghị của Starlette. `pytest` phát cảnh báo cấu hình asyncio khi plugin vắng mặt; thêm `pytest-asyncio` 1.4.0.
- **Cách xử lý/bài học cho lần sau:** giữ phiên bản uv chạy project tách khỏi executable global đang bị khóa; pin Python patch bằng `.python-version`; luôn kiểm tra template `.env` thật sự được nạp; không đưa LIVE vào cấu hình khả dụng trước hard gate; không dùng `httpx` cũ cho TestClient hiện hành.
- **Unit test:** RED ban đầu: `uv tool run --from uv==0.13.4 uv run --no-project --python 3.14.8 --with pytest --with fastapi==0.156.4 --with pydantic-settings --with httpx pytest tests/unit/test_settings.py tests/e2e/test_health_endpoint.py` → fail collection như dự kiến (`No module named trading_bot`). Hai kiểm tra bổ sung cũng fail đúng trước sửa: `.env` giữ PAPER thay vì DEMO; LIVE không raise `ValidationError`. GREEN cuối: `uv tool run --from uv==0.13.4 uv run --frozen pytest` → 5 passed (4 unit + 1 E2E), không warning.
- **E2E test:** `tests/e2e/test_health_endpoint.py` dùng TestClient qua app/config/HTTP route, xác nhận `GET /health/live` trả `{status: ok, mode: PAPER}`. Nằm trong lệnh test cuối: 1 passed.
- **Regression/security/data checks:** `uv run --frozen ruff check src tests` → đạt; `ruff format --check` → 31 file đã format; `mypy src` → không lỗi trên 29 source files; `uv sync --frozen` → 31 package; `uv build --no-build-isolation` → sdist và wheel thành công; cài wheel vào môi trường tạm và import/app smoke → đạt. `uv export --preview-features sbom-export --format cyclonedx1.5 --all-groups` tạo SBOM CycloneDX; `.env.example` không có credential; không có lệnh trade/network trong app import.
- **Việc còn mở/rủi ro:** WP-01B compose/process boundary; WP-01C schema/RLS/repository; WP-01D auth/MFA/secret provider; WP-01E UoW/audit/outbox. `uv` global chưa được nâng cấp vì tiến trình khác đang dùng `uvx.exe`; local instructions yêu cầu 0.13.4 trở lên. Kiểm tra link/anchor toàn bộ repo bằng slug chuẩn GitHub vẫn cần làm.
- **Commit/nhánh:** WP-01A trên `dev`; commit được tạo ngay sau khi cập nhật và đọc lại mục memory này; SHA được lưu trong Git history.
- **Đã đọc lại mục ghi nhớ sau cập nhật:** có; đã đối chiếu test cuối `5 passed` (4 unit, 1 E2E), Ruff pass, 31 file format, mypy pass trên 29 file, lock check pass và build wheel/sdist pass; không có thay đổi code sau lần kiểm tra này.

## Mẫu ghi cho mỗi vòng

### YYYY-MM-DD — Tên vòng / mã story

- **Mục tiêu và phạm vi:**
- **Plan/story liên quan:**
- **Nguồn/tài liệu hiện hành đã xác minh:** ghi URL, nguồn/tổ chức, ngày xuất bản hoặc phiên bản nếu có, ngày kiểm tra, và nội dung làm thay đổi quyết định.
- **Tool/dependency đã kiểm tra:** runtime, phiên bản và lý do giữ/nâng/thay.
- **Files/modules thay đổi:**
- **Quyết định và giả định:**
- **Vấn đề phát sinh và nguyên nhân gốc:**
- **Cách xử lý/bài học cho lần sau:**
- **Unit test:** command chính xác, số lượng/kết quả, pass/fail/skip.
- **E2E test:** command chính xác, luồng được xác nhận, kết quả và môi trường.
- **Regression/security/data checks:**
- **Việc còn mở/rủi ro:**
- **Commit/nhánh:** chỉ ghi sau khi đã thực hiện; không ghi credential.
- **Đã đọc lại mục ghi nhớ sau cập nhật:** có/không; ghi điểm đối chiếu.
