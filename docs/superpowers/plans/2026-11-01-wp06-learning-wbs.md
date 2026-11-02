# WBS WP-06 — Học AI có kiểm soát, đánh giá temporal và promotion PAPER

**Ngày:** 2026-11-02 · **Plan:** [Plan 06 — AI learning](../../implementation/06-ai-learning.md) · **Nền:** WP-01/02 PAPER và reporting MB-06.
**Trạng thái:** WP06.1 có domain scoring/abstention contract và application scorer có deadline/preflight; WP06.3 có builder fail-closed, domain manifest canonical loader và PostgreSQL immutable store (`b8d0f1a3c5e7`) với trainer-worker SELECT-only; WP06.4 có fold plan v1 giữ role cố định và v2 suy role từ từng cửa sổ, đồng thời gắn manifest/split-policy/cutoff. Chưa có verifier adapter/completeness đầy đủ, trial accounting theo experiment/fold, evaluator/report integration, trainer, registry hay promotion.
**Chế độ:** offline/PAPER; không gọi Binance private API, không tạo order từ trainer/scorer, không promotion LIVE.

**Bằng chứng lát cắt hiện có (2026-11-02):** scorer preflight fail-closed trước model port, timeout bounded/cancel; dataset builder nhận authoritative sample từ verifier, so toàn bộ label/value/hash, timestamps, IDs, split và metadata schema/policy trước temporal gate; source rights hashes/revision refs chỉ lấy từ verifier. Manifest canonical loader tái dựng và kiểm hash/JSONB/denormalized fields; repository append idempotent trong workspace. Migration `b8d0f1a3c5e7` áp RLS/immutability, admin-web SELECT/INSERT, trainer-worker SELECT-only; Compose E2E kiểm quyền, cross-workspace, duplicate, update guard và downgrade guard. Verifier giới hạn 5 giây/sample, build 120 giây, tối đa 10.000 sample+exclusion và 32 verifier task chậm còn sống; giả định một event loop/process. Fold plan map lineage từ manifest, kiểm split-policy/range/cutoff, purge overlap và hash selection/exclusions. V2 chỉ nhận `DEVELOPMENT`/`HOLDOUT`, suy role train/validation/test theo từng fold, fail-closed khi holdout decision nằm trong cửa sổ hoặc interval outcome holdout giao interval của sample được chọn; khoảng nửa mở chỉ chạm biên không bị coi là giao. V1 giữ nguyên canonical payload/hash và fixed-role semantics. Compose E2E lưu/nạp lại cả hai phiên bản từ cùng store, dựng hai fold từ v2 và kiểm tra boundary leakage. Chưa có verifier adapter/completeness, split approval/access gate, trial accounting đầy đủ, evaluator/report wiring, trainer, registry hay promotion.

## 1. Mục tiêu

Xây chu trình giả thuyết có giới hạn → dữ liệu/nhãn được duyệt → huấn luyện và đánh giá temporal có lineage → artifact bất biến → người có thẩm quyền duyệt shadow/PAPER → theo dõi drift và rollback. Mọi kết quả phải truy ngược được tới sample, source, phiên bản logic, chi phí, trial và quyết định phê duyệt.

WP-06 được chia hai luồng phụ thuộc:

| Luồng | Có thể bắt đầu | Điều kiện giới hạn |
| --- | --- | --- |
| Offline trên WP-02 | scorer contract/abstention; DSL trên feature hiện có; sample-level leakage evaluator; schema label/manifest; replay PAPER | Không dùng nguồn nghiên cứu ngoài WP-02. Trước trainer phải chốt manifest/evaluator và đánh giá trạng thái cost/risk limitations của WP-02. |
| Có dữ liệu WP-05 | research feature, source-based labels, dataset đầy đủ quyền, coverage drift/retrain | Chờ WP-05 có source identity, rights, revision, event/available time, as-of và coverage/degradation contracts đã được kiểm thử. |

WP-03 Binance Demo không phải dependency cho offline/PAPER learning. WP-06 không cấp quyền execution; mọi score chỉ có thể bổ sung feature/ranking để planner xử lý và RiskEngine vẫn là cổng cuối.

## 2. Căn cứ hiện trạng và ranh giới

- WP-02 cung cấp `EvaluationTrade`, `ReplayManifest`, trial/report persistence, `BacktestRunner`, `RuntimeReplayEvaluator`, cost-aware PAPER replay và một số point-in-time checks. WBS WP-02/memory ghi replay đã nối qua strategy/risk/PAPER path và có giới hạn đang biết: realized-equity-only, daily/rolling risk-window semantics chưa hoàn tất, mark-to-market chưa có, một số funding/spread stress giả định chưa đủ tổng quát và replay hash không chứng minh profitability.
- `walk_forward_splits` trong `domain/learning/evaluation.py` hiện tạo các cửa sổ theo duration với purge/embargo khoảng trống cố định. Chưa có sample record định nghĩa khoảng feature được đọc và khoảng outcome/label được quan sát, nên chưa thể purge mọi sample overlap theo interval.
- `validate_replay_observation` kiểm event/available time so với decision time, nến đóng và availability feature. WP-06 phải tái dùng các invariant này và bổ sung cutoff/revision/label lineage; không được suy ra rằng mỗi join hoặc dataset row đã được kiểm đầy đủ chỉ vì snapshot có timestamp.
- Offline foundations đã có scorer domain/application contract, bounded DSL/compiler, label/dataset manifest contracts, builder/persistence và lineage/purge/fold plan. Manifest v1 giữ role cố định; v2 cho phép `DEVELOPMENT` được gán train/validation/test riêng theo cửa sổ từng fold. `HOLDOUT` bị loại khỏi cửa sổ theo decision; interval outcome của mọi sample được chọn phải tách khỏi interval holdout (khoảng nửa mở chỉ chạm biên thì không giao). Store bảo đảm payload đã ghi bất biến và tái kiểm hash, không chứng minh candidate completeness, rights approval, holdout access control hay split approval. Chưa có verifier adapter/completeness, trial accounting theo experiment/fold, evaluator/report, trainer, registry/promotion hoặc drift lifecycle. `pyproject.toml` hiện không khai báo ML library; lựa chọn package/framework là quyết định có điều kiện, cần kiểm tra tài liệu/license/runtime chính thức trước khi thêm.
- MB-08 chỉ tiêu thụ immutable model/source references và per-bot lineage. Không đặt ingestion hay trainer trong MB-08 và không mở MB-08 trước khi WP-05/WP-06 reference contracts được duyệt.

## 3. Ngoài phạm vi

- Binance private API, Demo execution integration, LIVE trading hoặc model tự gửi/sửa/hủy lệnh.
- Model tự publish hoặc tự chuyển active; tự thay đổi root risk cap, bot config, strategy release hoặc active pointer.
- Thu thập nguồn WP-05 trong WP-06; sao chép source ingestion/rights/revision pipeline.
- Coi accuracy, một Sharpe/PnL, bootstrap CI hay một holdout là bằng chứng chắc chắn về lợi nhuận tương lai.
- Bắt buộc LLM hoặc GPU. Scorer/trainer đầu tiên dùng interface trung lập và baseline CPU; framework chỉ chọn sau khi có yêu cầu đo được.

## 4. Quyết định thiết kế và safety gates

| # | Ruling |
| --- | --- |
| R1 | Scorer nhận snapshot/version đã đóng băng; không có credentials, tool call, filesystem/network, portfolio mutation hoặc execution port. Timeout, schema error, stale/OOD/missing inputs và coverage dưới policy đều `ABSTAIN`. Fallback deterministic phải được bật tường minh và version hóa. |
| R2 | DSL dùng grammar allowlist và interpreter giới hạn; cấm `eval`, import, tùy ý gọi Python, filesystem/network, recursion/vòng lặp không giới hạn. Giới hạn node/depth/runtime/input và tập symbol trước khi thực thi; biểu thức canonical có hash ổn định. |
| R3 | Sample lưu tối thiểu `decision_at`, feature/source availability và interval, `label_start_at`, `label_end_at`/outcome completion, source revision, as-of cutoff, recipe/feature/label versions. Sample không đủ lineage hoặc chưa hoàn tất bị loại, không tự lấp bằng nhãn tạm. |
| R4 | Tách **temporal window split** khỏi **sample interval purge**. Với từng sample, xác minh feature/source đã khả dụng tại decision/as-of cutoff; purge sample train khi label/outcome interval chồng lấn vùng label validation/test hoặc feature/source vượt cutoff theo policy đã version hóa. Lookback lịch sử dùng chung được phép nếu đã khả dụng trước decision, không purge máy móc chỉ vì cùng dùng nến lịch sử. Embargo theo duration là hàng rào bổ sung. Thiếu interval/availability phải fail-closed hoặc exclusion có reason. |
| R5 | Mỗi lần thử, kể cả invalid/failed/rejected, ghi trial ID/index/count, parameter-family hash, dataset/manifest hash và trạng thái. Threshold selection chỉ dùng train/validation; holdout được seal một lần và không phản hồi ngược vào thiết kế. |
| R6 | Dataset manifest immutable pin query/symbol/range/as-of, sample membership hoặc digest, source/right/version, exclusions/reasons, schema/feature/label/compiler, class counts, retention, split assignment và content hash. Trainer chỉ nhận manifest đã duyệt; mọi correction sinh version mới. |
| R7 | Reproducibility ghi code, dependency/library/runtime, data/manifest, settings, feature/label/compiler, seed, cost/fill assumptions, trial, artifact checksum và evaluation report hashes. Định nghĩa rõ tolerance; không hứa bitwise cross-platform khi framework không đảm bảo. |
| R8 | Promotion là human approval tới PAPER/shadow theo policy version và actor/reason audit. Gating failure tạo `rejected` reason. Challenger không được có execution capability. Safety incident có thể khóa entries và rollback pointer về artifact trước; mọi state transition append-only/auditable. Không có route auto-LIVE. |
| R9 | Drift/retrain chỉ tạo alert hoặc challenger draft. Cỡ mẫu thấp/mất coverage biểu diễn `inconclusive`; không tự chuyển active. WP-05 source coverage chỉ được đo khi WP-05 contract sẵn sàng. |
| R10 | Không thêm ML dependency trong phần contract/evaluator. Trước framework selection: kiểm Python 3.14 compatibility, license, CPU support, serialization/security, deterministic controls, artifact format, supply-chain and maintenance status; ghi nguồn/date trong memory khi implementation diễn ra. |

## 5. WBS và module ownership

| ID | Việc | Phạm vi/file boundary dự kiến | Dependency / đầu ra |
| --- | --- | --- | --- |
| WP06.0 | Contract inventory và freeze | Tài liệu Plan 06/WBS, mapping contracts từ `domain/strategy/contracts.py`, `application/backtest.py`, `runtime_replay_evaluator.py`, backtest outcome repository, reporting MB-06. | Bắt đầu ngay. Xác định scorer request/result, sample lineage, manifest, lifecycle, roles, MB-08 references. Không sửa schema nếu inventory chưa chứng minh cần. |
| WP06.1 | Scorer + abstention | **Foundation có:** `domain/learning/scoring.py`, `application/learning_scoring.py` và unit tests. Service preflight rights/as-of/schema trước khi gọi model port, deadline/cancel bounded; không sửa planner/RiskEngine quyền chặn cuối. | Còn runtime/DB integration và actual model adapter. Failure trả ABSTAIN; chứng minh scorer không có `OrderIntent`/exchange capability; transport phải tự enforce timeout và resource cleanup nếu task không hợp tác hủy. |
| WP06.2 | DSL + experiment specification | **Foundation có:** bounded `domain/learning/dsl.py` tokenizer/parser/type checker/compiler, hypothesis contract, canonical AST/hash và unit tests. **Còn lại:** bất kỳ integration runtime nào vẫn cần resource/review gate riêng. | Đã kiểm feature allowlist, quyền/time cutoff, syntax/type/resource bounds, literal division by zero; chưa có evaluator/execution. Chỉ dùng WP-02 feature tới khi WP-05 rights/as-of gate đạt. |
| WP06.3 | Outcome label contract và manifest | **Foundation có:** domain labels/manifest; builder so authoritative sample từ verifier; `DatasetManifestRecord` lưu canonical JSON/hash, counts, versions, as-of và retention; repository tenant-scoped idempotent; migration `b8d0f1a3c5e7` append-only/RLS; trainer-worker chỉ SELECT. | Chưa có verifier adapter nối WP05 source/item repository, completeness proof, split approval workflow hoặc trainer consumer. Chỉ dùng store khi adapter xác minh `TRAINING` tại as-of, completeness và record hashes authoritative. |
| WP06.4 | Sample lineage + overlap-aware evaluation | **Foundation có:** `purge_overlapping_samples`, temporal selector và fold plan deterministic. V1 giữ `TRAIN/VALIDATION/TEST/HOLDOUT` cố định, bảo toàn loader/hash; v2 dùng `DEVELOPMENT/HOLDOUT`, gán role theo từng fold, fail-closed khi holdout decision nằm trong cửa sổ hoặc interval outcome holdout giao interval sample được chọn. Khoảng nửa mở chạm biên được giữ hợp lệ. Compose E2E round-trip v1/v2, tạo nhiều fold từ v2 đã nạp và kiểm tra interval boundary. | Còn evaluator/report/metrics/trial integration; chưa fit model hoặc chứng minh candidate list completeness. Chưa có split approval hay holdout access gate. Giữ point-in-time availability, protected label overlap và historical-lookback semantics. |
| WP06.5 | CPU trainer + reproducible artifact | Tạo `application/learning/trainer.py`, trainer adapter/isolated `trainer-worker`, artifact manifest/storage port, test fixtures. Dependency files chỉ thay sau R10. | Chờ WP06.3/.4 gates. Bắt đầu supervised CPU baseline; worker role chỉ SELECT approved manifest + INSERT run/artifact metadata, không exchange secrets, strategy/risk/config writes hay gateway. |
| WP06.6 | Immutable model registry + approval | Tạo `domain/learning/registry.py`, `application/learning/registry.py`, repository/entities/migration và admin approval boundary nếu cần. | Chờ artifact/report contract. State `draft → evaluated → rejected/eligible → paper_active → retired`; immutable artifact, authorized actor, policy version, reason; human PAPER promotion only. |
| WP06.7 | Shadow/PAPER compare + rollback | Tích hợp scorer đã duyệt qua feature/ranking port tại boundary runtime đã chọn; persist challenger decisions/active release reference; add service/E2E. | Chờ WP06.1 và .6. Chạy shadow không order capability; PAPER quota/gates explicitly reviewed. Rollback giữ lineage/history; safety incident có thể block new entry. |
| WP06.8 | Drift/feedback/retrain proposal | Tạo `domain/learning/drift.py`, application monitor/retrain proposal, model card/report schema, tests. | Baseline PAPER outcomes/score history sau .7; research coverage metrics chờ WP-05. Low-N=`inconclusive`; trigger chỉ tạo challenger, không promotion. |
| WP06.9 | Integration, E2E, security/review | Unit + Compose/PostgreSQL E2E, DB role matrix, restart/recovery, regression, independent review; cập nhật memory/status khi thực thi vòng sau. | Chờ .1–.8 theo phần scope. Không nghiệm thu test skip/chưa chạy/flaky chưa điều tra. Trước commit phải theo AGENTS gates: unit và E2E liên quan pass, memory cập nhật/đọc lại, diff sạch, không secret. |

### Có thể triển khai trước khi WP-05 hoàn tất

1. WP06.0 inventory/freeze và WP06.1 scorer contract với feature WP-02.
2. WP06.2 DSL nếu allowlist chỉ chứa feature/recipe và source có sẵn.
3. WP06.4 interval-aware split/purge, cutoff/leakage tests và replay baseline trên PAPER event/outcome đã persist.
4. WP06.3 schema/manifest contract và builder prototype trên complete WP-02 trade outcomes; không gồm research records.

Không bắt đầu train/promotion dựa trên WP-05 data cho tới khi từng source có rights, revision, availability/as-of, retention và failure/coverage semantics rõ ràng. WP-05 collector không được giả định mọi output có thể train.

## 6. Kiểm thử theo failure mode

| Nhóm | Test bắt buộc trước khi nghiệm thu |
| --- | --- |
| Scorer | Timeout, malformed/extra fields, stale/missing feature, OOD, source not allowlisted, unavailable model, incomplete calibration → abstain và không planner submit; fallback chỉ khi policy/version bật. |
| DSL | `eval`/import/attribute access/file/network/loop, unknown feature/timeframe, excessive depth/nodes, divide-by-zero/non-finite, symbol ngoài universe → diagnostic rõ trước execution; canonical whitespace/commutative forms theo grammar chuẩn cho hash ổn định. |
| Label/manifest | Trade chưa đóng, fee/funding/fill chưa hoàn tất, recipe mismatch, correction/revision sau cutoff, right revoked/unknown, duplicate sample, hash mismatch, RLS cross-workspace → exclude/reject có reason; sửa nhãn tạo version/hash mới. |
| Temporal/leakage | Feature available sau decision; event revised sau as-of; label horizon kéo qua fold; outcome interval overlap test; holdout decision ngoài fold nhưng interval holdout giao interval outcome sample được chọn; feature/source không khả dụng tại cutoff; interval thiếu; timezone-naive; event ở đúng half-open boundary; embargo đơn độc không che được label overlap → evaluation reject/exclude theo policy. Holdout overlap fail-closed, còn hai khoảng nửa mở chỉ chạm biên thì không giao. Historical feature window overlap chỉ bị chặn nếu nó kéo input vượt cutoff; xác nhận lookback quá khứ hợp lệ vẫn được phép. Kiểm assignment không có sample vừa train vừa test. |
| Holdout/trials | Holdout re-use hoặc tune threshold bằng holdout bị chặn; failed/rejected trial vẫn tăng trial ledger/count; parameter family/hash mismatch không được gộp. |
| Reproducibility | Cùng manifest/code/config/seed tạo cùng sample IDs/assignment và report/artifact hashes trong tolerance; sửa dependency, seed, cost/fill, label/feature/compiler hoặc sample membership tạo lineage/hash mới. Hư artifact/checksum → reject. |
| Registry/promotion | Không đủ quyền/approval, gate thiếu hoặc fail, sample thấp, rights invalid, cost/risk/cap/calibration/OOD/coverage dưới ngưỡng → reject và audit; trainer/scorer principal không đổi active pointer; chỉ owner/risk policy đúng quyền activate PAPER. |
| Rollback/drift | Rollback phục hồi release trước, giữ đầy đủ lịch sử/actor/reason; duplicate rollback idempotent; low data tạo `inconclusive`; trigger/retrain chỉ tạo draft challenger, active release không đổi; incident blocks new entries khi policy yêu cầu. |
| WP-05 integration | Revision correction, late publication, missing/backfill gap, source unavailable/rights restriction, stale coverage → source-dependent sample bị loại/abstain; không rò dữ liệu ngoài license hoặc workspace. |

## 7. Trình tự, tích hợp và nghiệm thu tổng

`WP06.0 → WP06.1 + WP06.2 + WP06.4 (offline, có thể song song theo file ownership) → WP06.3 manifest/schema` → xác nhận leakage/rights/trial gates → WP06.5 trainer → WP06.6 registry/approval → WP06.7 shadow/PAPER → WP06.8 drift/retrain proposals → WP06.9 integration/review.

WP06.3 schema và WP06.4 evaluator có thể song song khi contracts `SampleLineage`/`DatasetManifest` được WP06.0 đóng băng. Chỉ một owner điều phối migration chain, ORM entity registration, app composition root, role grants và full E2E. WP-05 field integration được nối sau khi upstream source contract freeze; không tạo stub rights mặc định “allowed”.

**Nghiệm thu WP-06:** approved manifest tái tạo được; label-overlap/future/revision leakage fixture bị loại hoặc fail-closed; historical lookback dùng chung được chấp nhận chỉ khi point-in-time hợp lệ; toàn bộ trial accounting hiện diện; baseline/cost/coverage/holdout report có provenance; trainer chạy tách quyền; artifact bất biến; promotion chỉ bằng approval tới PAPER/shadow; rollback phục hồi release; drift ít dữ liệu là inconclusive; không có execution/LIVE path từ AI subsystem; các giới hạn WP-02 và sample-size được nêu trong model card.

## 8. Nguồn đã kiểm tra (2026-11-01)

- Scikit-learn, [`TimeSeriesSplit`](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html): tài liệu hiện hành nêu mẫu cần cách đều để các fold có duration so sánh được và `gap` là số sample bỏ khỏi cuối train trước test. Do đó không xem một fixed sample/time gap là kiểm tra đầy đủ cho sample có horizon/feature windows khác nhau. Đây là nguồn tham khảo semantics, **không phải dependency đã chọn**.
- Đối chiếu lại cho WP06.4 ngày **2026-11-02**: giữ `TimeSeriesSplit` chỉ làm tài liệu tham khảo về fold có thứ tự; implementation dùng cửa sổ thời gian tuyệt đối và purge interval nhãn riêng, không thêm scikit-learn hay coi fixed `gap` là đủ cho mọi label horizon.
- Bailey, Ger, López de Prado, Sim, Wu, [*Statistical Overfitting and Backtest Performance*](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf): bài nghiên cứu mô tả selection bias khi thử nhiều variants và giới hạn của holdout khi không lưu số lượt thử. Căn cứ cho trial ledger và holdout gate; không phải lời hứa phương pháp cụ thể loại bỏ mọi overfit.
- Python project dependency inventory: `pyproject.toml` hiện có FastAPI/Pydantic/SQLAlchemy/HTTP client và dev tooling nhưng chưa có ML framework. Chưa chọn package; kiểm tra version, Python 3.14 compatibility, license, determinism và serialization từ nguồn chính thức trước khi implementation/add dependency.

## 9. Thuật ngữ cần giữ thống nhất

Ưu tiên thuật ngữ đang có trong [glossary EN→VI](../../research/glossary-vi.md): OOD, concept drift, model registry, reproducibility/lineage, trial, walk-forward. Trước vòng implement đầu, bổ sung glossary cho các thuật ngữ mới nếu dùng như contract/API: **sample interval purging — loại mẫu theo giao khoảng thông tin**, **sealed holdout — tập kiểm định cuối được niêm phong**, **dataset manifest — bản kê bất biến thành viên/phạm vi dữ liệu**; định nghĩa phải dẫn nguồn phù hợp.
