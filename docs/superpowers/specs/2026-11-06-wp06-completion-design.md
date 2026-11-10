# Thiết kế hoàn tất WP-06

**Ngày:** 2026-11-06 <br>
**Trạng thái:** triển khai theo các cổng tuần tự; không bật LIVE. <br>
**WBS gốc:** [WP-06](../plans/2026-11-01-wp06-learning-wbs.md)

## Mục tiêu

Hoàn tất luồng offline/PAPER từ dữ liệu replay đã lưu đến artifact có thể kiểm tra, registry có phê duyệt của người vận hành, shadow/PAPER có thể rollback và cảnh báo drift chỉ đề xuất huấn luyện lại. Mỗi bước có lineage, workspace isolation, trạng thái append-only và quyền database tối thiểu.

## Quyết định dữ liệu

1. **Đơn vị mẫu:** một trade PAPER/replay đã đóng, gắn với snapshot feature tại đúng quyết định vào lệnh. Nhãn là dấu net PnL sau fee, funding và slippage (`-1/0/1`). Một trade là một sample. Observation không vào lệnh không được biến thành nhãn 0; model đầu tiên ước lượng outcome có điều kiện trên quyết định entry của strategy.
2. **Liên kết entry:** `RuntimeReplayEvaluator` ghi symbol và ID của đúng runtime snapshot đưa vào `prepare_published`, decision time, feature values READY và hash canonical vào outcome khi intent được submit; row outcome giữ liên kết sau khi trade đóng. Đây là snapshot entry (`:runtime:`), tuyệt đối không phải snapshot exit. Symbol phải lấy từ snapshot đó, không suy từ caller/bundle ở bước build manifest. Feature hash gắn snapshot ID/time/schema/vector; canonical outcome hash còn bao gồm symbol. Outcome cũ hoặc generic callback không có bằng chứng entry không đủ điều kiện cho trainer.
3. **Candidate universe:** chỉ một trial BASE đã hoàn tất trong replay run được đóng băng cho mỗi manifest build. Adapter DB liệt kê toàn bộ outcome row của đúng trial, kiểm count/ordinal/report/trial lineage, hash và feature-entry link. Manifest builder không nhận danh sách candidate/exclusion do người gọi khai là đầy đủ. Tập đầy đủ ở đây chỉ là *mọi trade đã đóng trong trial BASE đã chọn*, không tuyên bố bao phủ mọi quyết định/no-trade của chiến lược. Proof scan giữ source-policy advisory lock tới lúc receipt được commit và khóa đúng trial row trong khi xác minh; outcome INSERT cũng khóa row đó và bị từ chối sau khi proof seal trial. Insert cạnh tranh trước proof phải hoàn tất trước scan và được tính vào count/hash; cách này tránh stale membership mà không khóa toàn bộ bảng source-policy.
4. **Split và holdout:** split policy là policy đã định trước theo thời gian, phê duyệt bởi admin có MFA trước khi manifest được seal. `DEVELOPMENT` không đồng nghĩa với train/validation: cùng một sample có thể đóng vai `TEST` ở fold này và `TRAIN` ở fold sau. Quyền trainer chỉ trả chính xác train+validation của fold được yêu cầu; test không phải final holdout. Holdout nằm ở đuôi khoảng và không được trả về trainer, scorer hoặc UI thông thường. Chỉ evaluator được ủy quyền mới có thể tiêu thụ receipt truy cập theo actor/purpose/manifest/split/evaluation-run. Gateway tin cậy giữ payload trong buffer riêng, ghi receipt và đọc payload dưới source lock trong cùng transaction; chỉ disclose buffer sau khi transaction commit thành công. Rollback trước commit không lộ bytes hay tiêu thụ lượt; crash sau commit làm lần thử lại bị từ chối và chấp nhận mất lượt. Hash/seal đơn lẻ không cấp quyền.
5. **Nguồn WP-05:** source-dependent manifests tiếp tục bị chặn ở trainer và report writer đến khi rights recheck và persist được tuần tự hóa an toàn với rights writers.
6. **Rights của replay data:** receipt `BACKTEST_RESEARCH` của bundle/replay không tự chứng minh được mục đích `TRAINING`. Candidate builder phải có bằng chứng quyền training riêng và kiểm tra hiện hành; khi không có, từ chối tạo training manifest. Tài liệu Binance developer được kiểm tra 2026-11-06 mô tả public API data access nhưng không được dùng để suy diễn quyền huấn luyện: <https://developers.binance.com/en/docs/products/spot/rest-api>.

## Ranh giới huấn luyện

- Baseline đầu tiên là **hồi quy logistic one-vs-rest** CPU viết bằng Python chuẩn, không thêm package ML trong lát cắt này: ba binary models cho các nhãn `-1/0/1`. Input numeric được chuẩn hóa chỉ bằng train split; sample order, feature schema, solver version, số vòng, learning rate, regularization và mọi checksum đều được đóng băng. Score/probability chưa calibrated không được chuyển sang scorer hoạt động; runtime phải `ABSTAIN` tới khi bước calibration/evaluation được duyệt riêng.
- Trainer chỉ nhận typed bundle do adapter trainer-worker dựng từ manifest đã được split-approval, completeness-proof và live eligibility check. Bundle chỉ chứa train/validation; không chứa test/holdout payload. Kết quả lưu model parameters dạng canonical JSON và SHA-256. Đây là baseline kỹ thuật, không hàm ý đạt lợi nhuận hay được phép giao dịch.
- Trainer worker không giữ exchange/source secrets. DB quyền chỉ SELECT dataset/approval evidence, INSERT training run/artifact; không sửa manifest, risk, strategy release hoặc active pointer.
- Mỗi attempt `queued` có đúng một job row bất biến về scope, trạng thái mutable chỉ qua lease generation. Nếu lease hết trước khi trainer receipt commit, job có thể được claim lại vì chưa disclose batch. Nếu receipt đã commit nhưng chưa có artifact+`succeeded`, worker phải kết thúc attempt/job là failed; không đọc lại batch đã tiêu thụ receipt. Muốn chạy lại cần một attempt mới với approval/rights check và receipt mới. Artifact, event `succeeded` và job success commit nguyên tử; worker generation cũ bị fence.

## Registry, vận hành và an toàn

- Registry nhận artifact bất biến khi các hash/lineage khớp. Mỗi lần train, evaluation, approval, reject, activate, rollback, retire hoặc holdout access có event append-only, workspace và actor/purpose.
- Tách `draft`, `evaluated`, `rejected`, `paper_active`, `retired`; chỉ admin MFA có thể approve/reject. Không có trạng thái LIVE hoặc route đến execution gateway.
- Shadow so challenger với incumbent trên cùng feature snapshot mà không tạo intent/order. PAPER promotion nếu được duyệt chỉ chấm điểm trước planner và vẫn đi qua RiskEngine/giới hạn PAPER hiện hữu. Rollback là cập nhật con trỏ có CAS/transaction; event history không bị xóa.
- Drift dashboard dùng score/outcome history khả dụng và interval tin cậy theo policy; cỡ mẫu thấp, coverage kém hoặc thiếu dữ liệu là `inconclusive`. Trigger chỉ tạo retrain proposal; không tự train/promote.
- Admin/CLI hỗ trợ enqueue, xem tiến trình, cancel an toàn, xem artifacts/lineage, approve/reject/rollback, drift và retrain proposal. Web UI tiếng Việt, mobile responsive, CSRF/MFA; các thao tác trainer và registry không làm lộ holdout.

## Phụ thuộc và fail-closed

Thứ tự bắt buộc: entry lineage → candidate universe proof → split/holdout governance → trainer/artifact → evaluation → registry approval → shadow/PAPER/rollback → drift proposal. Nếu thiếu provenance, split approval, rights, count/hash, resource cap, actor hoặc database capability thì từ chối trước khi gọi trainer hoặc thay đổi active reference. Migration phải có RLS, immutability và downgrade guard khi dữ liệu đã được ghi.

**Ranh giới DB cho holdout:** bảng manifest hiện lưu label trong JSONB lẫn canonical text; bảng outcome cũng giữ feature, label và PnL. Quyền `SELECT` toàn bảng không đủ để tách holdout. Trước khi trainer hoạt động, chuyển dữ liệu mới sang sample rows chuẩn hóa cùng fold assignments và quyền RLS chỉ cho phép đọc hàng có role `TRAIN`/`VALIDATION`; thu hồi toàn bộ quyền đọc payload manifest/outcome từ trainer, admin-web và app. Không dựa vào GUC tenant/fold do chính caller đặt để chứng minh danh tính; GUC chỉ dùng để thu hẹp scope sau khi principal đã được xác thực.

Quyền ghi proof/chuẩn hóa phải thuộc principal builder chuyên biệt, không phải trainer-model hay evaluator; worker replay không được dùng chung secret trainer cho capability đọc payload. Dịch vụ diagnostic/holdout dùng principal evaluator riêng. Gateway kiểm actor/purpose/manifest/split/evaluation-run và rights hiện hành, lấy source advisory lock, buffer payload riêng, ghi receipt duy nhất rồi commit transaction; chỉ sau commit mới trả buffer. Vì caller không thể đọc payload trực tiếp và gateway không trả bytes trước commit, rollback không lộ dữ liệu; crash sau commit làm receipt không thể dùng lại. Source lock giữ rights nhất quán đến commit. Direct SELECT từ trainer/admin-web/app bị từ chối; mọi loader legacy phải bị chặn hoặc chuyển qua boundary mới. E2E phải thử ACL thực, đường verifier cũ, hai access cạnh tranh, rollback không disclosure, crash/retry sau commit, source-rights revocation và cross-workspace, không chỉ kiểm tra endpoint.

## Ngoài phạm vi

WP-05 ingestion mới, Binance private API/demo/live order, tự động thay chiến lược/risk, model sinh lệnh, GPU/LLM, đọc holdout để chọn feature/threshold, source-data storage erasure hoặc machine unlearning. Không thêm package ML cho tới khi Python 3.14, license, serialization, resource và supply-chain được kiểm tra từ nguồn hiện hành.

## Nguồn nghiên cứu đã kiểm tra

- Cox, D. R. (1958), *The Regression Analysis of Binary Sequences*, Journal of the Royal Statistical Society: Series B, DOI 10.1111/j.2517-6161.1958.tb00292.x. Kiểm tra 2026-11-06; dùng để định danh hồi quy logistic như baseline phân loại, không xác nhận lợi nhuận giao dịch: <https://academic.oup.com/jrsssb/article/20/2/215/7027376>.
- Rifkin, R. & Klautau, A. (2004), *In Defense of One-Vs-All Classification*, Journal of Machine Learning Research 5, 101–141. Kiểm tra 2026-11-06; tham khảo cách phân rã đa lớp thành các binary classifier, không khẳng định đây là lựa chọn tối ưu cho dữ liệu tài chính: <https://jmlr.org/papers/v5/rifkin04a.html>.
- PostgreSQL 18 (đúng major của Compose, kiểm tra 2026-11-06): [Row Security Policies](https://www.postgresql.org/docs/18/ddl-rowsecurity.html), [CREATE POLICY](https://www.postgresql.org/docs/18/sql-createpolicy.html), [CREATE FUNCTION security](https://www.postgresql.org/docs/18/sql-createfunction.html), [Transaction Isolation](https://www.postgresql.org/docs/18/transaction-iso.html). RLS chỉ lọc hàng khi role có table privilege, mặc định deny khi không có policy; SECURITY DEFINER cần search_path khóa chặt; Repeatable Read không ngăn mọi serialization anomaly. Task 2 phải dùng các hợp đồng này và E2E kiểm trực tiếp bằng từng DB principal.
- Không có dependency/API mới trong thiết kế này; implementation phải kiểm tra lại nguồn chính thức cho mọi thư viện, API hoặc version-sensitive choice phát sinh.
