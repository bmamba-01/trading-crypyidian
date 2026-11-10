# Thiết kế — Lưu bundle replay và bằng chứng quyền theo lần chạy

**Ngày:** 2026-11-03 · **Trạng thái:** thiết kế được duyệt; bundle persistence và application orchestration đã triển khai, job/UI lifecycle còn mở · **Phạm vi:** WP-07C.

## Mục tiêu

Cho phép một bộ dữ liệu replay đa khung thời gian được lưu thành định danh bất biến theo workspace, tra cứu lại và dùng chung cho nhiều lần chạy. Bundle chỉ giữ tham chiếu/hash các manifest con; không sao chép OHLCV. Mỗi kết quả backtest còn lưu bằng chứng quyền thực tế đã được kiểm cho từng manifest con tại lần chạy đó. Khi nạp để replay, hệ thống phải dùng `BacktestDatasetReplayLoader` hiện có để kiểm quyền hiện hành và kiểm lại bars trước khi dựng bundle chạy. Bằng chứng lịch sử chỉ phục vụ audit, không thay thế kiểm quyền hiện hành và không tự cấp quyền replay.

## Hiện trạng

`BacktestDatasetManifestRepository` lưu từng manifest con. `build_backtest_dataset_replay_bundle()` kiểm cùng workspace, symbol, evaluation window, rights fingerprint, timeframe duy nhất, bars hash và tạo hash tổng hợp. Bundle hiện chỉ ở memory; `BacktestRunRecord` giữ `dataset_hash` nhưng chưa có repository giải ngược hash bundle thành danh sách manifest con. `BacktestDatasetReplayInput` đã mang `current_rights` gồm provider, purpose, terms URL, policy fingerprint, `verified_at`, `checked_at` và `valid_until`; outcome repository hiện chưa ghi snapshot này cùng lần chạy. Manifest con WP-07C hiện chỉ cho `crypidian_admin_web` đọc/ghi; `crypidian_trainer_worker` ghi outcome backtest nhưng chưa có quyền đọc dataset manifest. Alembic hiện có một head `c9d7e5f3a1b2`.

## Kết quả mong muốn

- Lưu một row bundle canonical theo `(workspace_id, bundle_hash)`; cùng hash + cùng bytes thì retry idempotent, khác payload thì conflict.
- Lưu danh sách child manifest hash, timeframe và `bars_content_hash`, cùng symbol, workspace, evaluation window, purpose/provider và rights fingerprint.
- Với từng run mới ghi outcome, lưu bằng chứng quyền trả về bởi rights gate cho từng child manifest: provider, purpose, terms URL, policy fingerprint, thời điểm xác minh policy, thời điểm kiểm quyền từ database clock (`checked_at`) và thời điểm hết lease evidence (`valid_until`).
- Retry idempotent cùng run giữ nguyên bằng chứng lần ghi đầu; không ghi đè audit history bằng một lease mới.
- Tái dựng descriptor từ row bằng canonical JSON + SHA-256; kiểm các cột chỉ mục, schema version, thứ tự và tính duy nhất của timeframe.
- Khi load để replay, đọc bundle theo workspace; nạp từng child manifest qua loader hiện có để revalidate rights hiện hành và kiểm bars trong DB; ghép bundle lại rồi yêu cầu hash tổng khớp row.
- Bảo vệ tenant bằng PostgreSQL RLS/FORCE RLS, append-only trigger, unique workspace/hash và quyền tối thiểu.
- Downgrade bị chặn khi bundle đã được lưu.

## Ngoài phạm vi

- Đã có application service load → PAPER runtime → persist nguyên tử; chưa có job runner, CLI/API/dashboard, queue, tiến độ/hủy hay wiring vào process lifecycle.
- Chưa cấp quyền `crypidian_trainer_worker` đọc dataset manifest/bundle; thay đổi trust boundary đó cần được thiết kế cùng worker orchestration riêng.
- Không thay đổi `BacktestRunRecord` hoặc thêm FK bắt buộc vì dữ liệu backtest hiện có có thể chứa `dataset_hash` không phải bundle WP-07C.
- Không lưu bars/raw payload; bundle giữ policy fingerprint ổn định, còn replay loader revalidate policy ngay trước khi trả dữ liệu.
- Bằng chứng quyền theo run là snapshot dữ liệu tối thiểu lấy từ `BackfillRightsEvidence`, không lưu thông tin tài khoản/cá nhân hay bản sao policy đầy đủ. Nó không thay thế quyền đang hiệu lực và không được dùng để cho phép replay sau khi policy đổi, hết lease hoặc bị thu hồi. Các run có trước migration sẽ không được điền bằng chứng hồi tố; retry legacy không được gán `checked_at` hiện tại như thể đó là thời điểm chạy ban đầu.
- Không tải market data thật, không gửi Telegram, không phát lệnh sàn và không tuyên bố profitability.

## Cách xây dựng đã chọn

### A — Bảng bundle bất biến riêng (đã chọn)

Tạo bảng `backtest_dataset_replay_bundle` với JSONB/canonical text, hash và các cột chỉ mục cần cho scope/query. Một repository append/get đọc theo workspace. Một application loader nhận bundle hash, resolve các child hash qua `BacktestDatasetReplayLoader`, dựng lại bundle và so hash.

Ưu điểm: bundle chuẩn bị sẵn có thể dùng lại; report/run có định danh ổn định tới các manifest con; quyền hiện hành được kiểm lại khi dùng. Chi phí: thêm một migration, model, repository, loader và E2E RLS/immutability.

### B — Gắn danh sách manifest con vào từng backtest run

Lưu bundle payload trực tiếp vào JSON của từng `BacktestRunRecord`. Cách này ít bảng hơn nhưng nhân bản payload giữa các lần chạy, không tạo được bộ dữ liệu dùng chung trước khi có run và cần đổi hash/canonical schema của replay manifest.

Phương án A được người dùng chọn vì dataset lịch sử có thể phục vụ nhiều chiến thuật/lần chạy.

## Quyết định triển khai đề xuất

1. Thêm contract canonical `BacktestDatasetReplayBundleReference` với `schema_version=1`, workspace, symbol, evaluation window, rights provider/purpose/fingerprint và danh sách component đã sort theo interval; mỗi component chứa `dataset_hash` và `bars_content_hash`.
2. Hash canonical reference bằng cùng nội dung hiện dùng trong `_bundle_hash`; không đưa thời điểm lease `checked_at` vào hash để retry cùng dataset/policy không tạo bundle khác.
3. Tạo ORM table/migration mới kế tiếp Alembic head hiện tại cho bundle và bảng `backtest_run_dataset_rights` liên kết `(workspace_id, run_id, child_dataset_hash)`. Bundle áp dụng RLS/FORCE RLS cho `crypidian_admin_web`; bảng rights áp dụng RLS/FORCE RLS cho `crypidian_trainer_worker` ghi/đọc theo workspace và `crypidian_admin_web` đọc. Cả hai append-only, có unique keys, downgrade guard khi có dữ liệu.
4. `BacktestDatasetReplayBundleRepository.append/get_by_hash` kiểm JSONB, canonical bytes, hash và cột chỉ mục. Workspace mismatch, bytes tamper, component/timeframe trùng hoặc cùng hash khác payload đều fail-closed.
5. `BacktestDatasetReplayBundleLoader.load_for_replay` lấy reference rồi gọi loader hiện hành cho từng child manifest tại thời điểm replay. Nó dựng bundle từ inputs mới đã revalidate và so bundle hash, symbol/window/purpose/fingerprint trước khi trả.
6. `BacktestOutcomeRepository.append_sensitivity_run` nhận bằng chứng quyền từ run vừa replay và ghi atomically cùng outcome, một row cho mỗi child manifest. Xác minh evidence khớp workspace, run, child manifest và manifest policy fingerprint; khi outcome mới đã tồn tại, retry chỉ đọc/so hash evidence cũ, tuyệt đối không cập nhật. Với run legacy chưa có receipt, giữ thiếu receipt để thể hiện đúng lịch sử; không tạo bằng chứng hồi tố.
7. E2E Compose dùng fixture offline, xác minh bundle append/retry/readback, tenant isolation, immutable trigger, run rights evidence được persist và giữ nguyên khi retry, loader thành công khi rights còn hiệu lực và bị từ chối sau revoke. Unit bao phủ parser/canonical/hash, component mismatch, thứ tự/timeframe, canonical evidence và revalidation lỗi.

## Quyết định bổ sung — retry deterministic và receipt quyền

`BacktestRunner` tạo cùng `trial_id` khi chạy lại cùng manifest. Mỗi lần gọi vẫn phải nạp/revalidate policy hiện hành; nếu quyền bị revoke, lease hết hạn hoặc fingerprint đổi thì từ chối trước khi persist. Khi cùng run đã có receipt, retry so workspace/run/bundle/child/timeframe/provider/purpose/terms/fingerprint/`verified_at`, đồng thời xác minh receipt cũ tự nhất quán giữa cột, JSONB, canonical text và hash; `checked_at`/`valid_until` mới không thay thế snapshot ban đầu. Khác biệt về trade/run/scenario vẫn bị chặn bởi canonical idempotency hiện có; policy-bound evidence khác cũng conflict. Như vậy retry cùng một run giữ nguyên audit snapshot đầu, còn mỗi run mới nhận receipt với thời điểm kiểm quyền mới.

## Quyết định triển khai — application orchestration

`BacktestDatasetReplayExecutionService` nhận workspace, bundle hash và `ReplayManifest`. `load_unit_of_work_factory` nạp bundle qua loader có rights gate hiện hành; service kiểm scope/hash/evaluation window rồi chạy executor bằng `asyncio.to_thread` sau khi đóng transaction load. `persist_unit_of_work_factory` mở transaction mới, revalidate policy hiện hành trên bundle đã nạp mà không đọc lại OHLCV, rồi append mọi sensitivity child qua `BacktestOutcomeRepository`. Lỗi ở child bất kỳ làm Unit of Work rollback toàn lượt. Kết quả trả run/report/scenario identifiers sau commit. Hai factory tách boundary: `admin_web` đọc bundle/manifests/bars; `trainer_worker` revalidate Source Registry bằng quyền workspace-scoped đã có từ WP-06 và ghi outcomes/receipts nhưng không được đọc bundle hoặc manifest.

## Quyền và tương thích

Migrations mới nối tiếp head duy nhất `c9d7e5f3a1b2`. Bundle table theo quyền `admin_web` giống manifest WP-07C hiện có. `crypidian_trainer_worker` có thể ghi/đọc receipt theo workspace nhưng không đọc manifest con hoặc bundle; quyền Source Registry SELECT workspace-scoped đã tồn tại từ WP-06 và được dùng để revalidate trong transaction persist. Hai factory Unit of Work giữ riêng quyền load và persist; runtime chạy ngoài cả hai transaction. Bundle hash tiếp tục là `ReplayManifest.dataset_hash`; không thêm FK bắt buộc tới bundle để tránh hỏng replay record cũ. Bằng chứng được ghi cùng outcome mới; khóa `(workspace_id, run_id, child_dataset_hash)` khiến retry idempotent và snapshot đầu tiên bất biến.

## Tiêu chí nghiệm thu

- Bundle hai timeframe lưu/đọc lại thành cùng canonical bytes/hash; retry không tạo row mới.
- Mỗi lần chạy backtest mới lưu đủ snapshot quyền theo từng child manifest; đọc lại xác minh hash/canonical payload, retry không làm thay đổi bằng chứng đầu tiên. Run legacy không được gán evidence hồi tố.
- Manifest con không tồn tại/READY không đủ/khác workspace/bar thay đổi/timeframe trùng/hash sai đều bị từ chối.
- Rights policy đổi, hết hạn hoặc bị revoke sau khi lưu bundle khiến loader từ chối trước khi replay.
- PostgreSQL che row workspace khác và từ chối UPDATE/DELETE cho cả bundle lẫn rights evidence.
- Service Compose E2E load bằng `crypidian_admin_web`, persist bằng `crypidian_trainer_worker`, chạy runtime ngoài transaction; fault injection rollback toàn lượt, retry giữ receipt gốc và revoke chặn lần chạy kế tiếp.
- Alembic upgrade/head/check đạt; downgrade khi có bundle bị chặn; test không dùng mạng.

## Thuật ngữ

- **Replay bundle → bundle replay:** tập tham chiếu đến các dataset manifest nhiều khung thời gian, được ghép cho cùng symbol/evaluation window và rights policy; không chứa bản sao nến. Thuật ngữ nội bộ của Crypidian; glossary sẽ được cập nhật cùng implementation.
