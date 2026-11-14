# WP06 exact source-use consumer design addendum

Ngày: 2026-11-13. Bổ sung cho `2026-11-13-wp06-license-evidence-digest-design.md` sau khi rà consumer và lineage thực tế.

## Mục tiêu

Mọi WP06 TRAINING/PAPER/SHADOW/DRIFT consumer phải kiểm `SourceLicenseScope.covers(SourceUseScope)` trên dữ liệu có thẩm quyền. Evidence digest đơn thuần không cấp quyền. Consumer không gửi JSON scope; ứng dụng/DB dựng scope từ lineage đã lưu, purpose/output/derived-use từ operation cố định. Thiếu dimension hoặc lineage thì deny/INCONCLUSIVE.

## Identity và khoảng dữ liệu authoritative

- Không join `DatasetSample.feature_snapshot_hash` với `FeatureSnapshotRecord.snapshot_id`. `learning_split_candidate.py` gán trường đó bằng `BacktestTradeOutcomeRecord.entry_features_hash`, là hash feature values, không phải snapshot ID.
- `FeatureSnapshotRecord.input_bar_ids` cũng không phải `MarketBar.id`; chúng là hash của `open_time/symbol/timeframe`. Không dùng chúng để khẳng định provider/revision.
- Replay/backtest dùng `BacktestDatasetManifestRecord` và persisted bundle child làm nguồn cho `symbol`, `interval`, `[warmup_start, range_end)`, provider và ordered bar identity hash. Khi cần kiểm chứng từng bar, truy `MarketBar` bằng bar-record UUID trong identity lineage và xác minh source/revision/checksum đúng child.
- Binance USDⓈ-M mô tả Kline là duy nhất theo open time và phản hồi có close time ở độ chính xác mili giây. Vì vậy scope của bundle là khoảng truy vấn nến nửa mở `[warmup_start, range_end)` đã được manifest kiểm chứng; giữ nguyên `range_end` vốn exclusive. Không dùng `max(close_time)+1µs`, không mở rộng biên request và không áp dụng phép cộng vào `range_end`.
- Runtime vector lưu `DecisionSnapshotTimeframeLineage.source_bar_ids` theo dạng `market-bar-record:{UUID}` cùng event IDs canonical có identity/revision/checksum. Runtime use range biểu diễn các khóa nến thực tế `[min(open_time), max(open_time)+1µs)` để bao gồm open-time cuối trong khoảng nửa mở. Chỉ dùng cách này khi interval, symbol, source/provider, bar IDs và event lineage đều khớp.
- `DatasetSourceReference` hiện chỉ ghim source item/revision/policy, không có market bar/timeframe lineage. `DatasetManifest.data_start/data_end/symbols` không đủ để suy interval hay bar revision. Source-dependent manifest không được dùng các field đó thay thế; gate từ chối cho tới khi verifier lưu typed per-sample source-use lineage. Không coi manifest không có `source_references` là source-free nếu authoritative producer cho biết sample phụ thuộc nguồn.
- Producer WP06 chính phải ghim replay bundle trực tiếp: `BacktestTradeOutcomeRecord` có `run_id`/`trial_record_id` nhưng không có `bundle_hash`; bundle hash nằm trong `BacktestRunDatasetRightsRecord`. Repository join outcome→trial/run→mọi `backtest_run_dataset_rights`, kiểm đủ child set từ bundle, single bundle hash, provider/policy/run-manifest parity, rồi mới truyền giá trị authoritative vào `LearningCandidateOutcome`. Hiện `learning_split_candidate.py` bỏ mất đường lineage đó, ghi mọi `DatasetSample.source_dependent=False`, khiến Python/SQL gate bỏ qua rights. Nâng manifest version để mỗi sample giữ `source_replay_bundle_hash`; khi seal/claim DB đối chiếu lại sample→outcome→trial/run/right receipts/bundle/child manifests. Không suy lineage từ query hash/proof digest.
- Ánh xạ source/provider nội bộ phải là allowlist cụ thể dựa trên `market_source_for_rights_provider` và nguồn runtime đã đăng ký. Không so sánh chuỗi gần giống. QA fixture chỉ hợp lệ với đúng identity/policy fixture đã ghim; unknown source/provider deny.
- Source identity (`source_id`, `provider`, `product`, `venue`, `contract`) lấy từ policy VERIFIED hiện hành và source profile đã lưu. Symbols/timeframes/range chỉ lấy từ bar/bundle lineage có thẩm quyền.
- `retention_days=None` vì normalized data/model/lineage đang được giữ vô thời hạn. License có retention hữu hạn bị từ chối cho tới khi có retention/delete lifecycle.

## Mapping operation

Một nguồn có thể cần nhiều phép kiểm độc lập. Không dùng purpose trên bundle làm quyền cho mọi consumer, không gộp các quyền khác purpose thành một scope, và không suy ra quyền TEST từ quyền replay:

| Operation | Purpose cần kiểm | Output | Derived use | Phạm vi đầu vào |
|---|---|---|---|---|
| Tạo/chạy backtest research | `backtest_research` | `aggregate_report` | `aggregate_reporting` | Request authoritative khi backfill; mỗi child `[warmup_start, range_end)` khi replay |
| TRAINING reuse để trích feature/fit model | `training` | `derived_features`, `trained_model` | `feature_extraction`, `model_training` | Bundle child/bar lineage thực tế |
| TRAINING evaluation/TEST report | `training` | `aggregate_report` | `aggregate_reporting` | Toàn bộ child data đã đọc, gồm warm-up và TEST |
| Scorer PAPER/SHADOW inference | `paper_demo` | `paper_inference` | `paper_inference` | Các MarketBar của đúng candidate/decision |
| PAPER drift report | `paper_demo` | `aggregate_report` | `aggregate_reporting` | Hợp các member bars của cohort |

Các SQL gateway trong `e8f0a2c4d6b8_wp06_scorer_test_receipts_reports.py` gọi `wp06_assert_training_source_rights(..., access_kind='evaluation', ...)`; đây là operation TRAINING evaluation, phải kiểm `training` + `aggregate_report` + `aggregate_reporting`. Nó không phải quyền `backtest_research`, cũng không được đổi thành training model use. `DatabaseReplayTrainingRightsGate` phải nhận operation tường minh để caller không vô tình dùng TRAINING receipt cho backtest generation hoặc ngược lại.

## TRAINING manifest và normalized data

- `DatasetSourceEligibilityGate` kiểm current policy, observed source/item revision, invalidation backlog, evidence digest và typed use coverage trước khi cấp receipt.
- Verifier là producer duy nhất được gắn `SourceUseLineage` per sample: source reference, exact symbol/interval set, UTC half-open data range, cùng MarketBar/event identities nếu đầu vào là OHLCV. Hash manifest ghim payload này. DB repository phải đọc lại persisted lineage theo `(workspace, manifest_hash, sample_id)`; caller payload không phải nguồn thẩm quyền.
- Nếu source item có interval/range/bar identity thiếu, gate từ chối `source_use_lineage_missing`; không dùng `feature_snapshot_hash` hoặc `decision_at/outcome_end` thay thế. Source access envelope expose metadata typed minimal (policy identity/scope/evidence ID/digest/hash), không trả bytes/raw ledger.
- Với candidate replay hiện có, `learning_split_candidate.py` phải đưa `bundle_hash` từ mọi outcome vào `source_replay_bundle_hash` của sample và đánh dấu source-dependent. TRAINING receipt/check phải đi qua child manifest từ bundle, kể cả khi sample không có `DatasetSourceReference`.
- `learning_fold_samples` giữ source advisory-lock order và recheck trước insert, trước receipt, sau flush/retry. Receipt lưu hash use scope và policy fingerprint; `learning_fold_evaluation` gọi operation aggregate report riêng.
- Bundle-backed samples ghi immutable `learning_replay_training_receipt` theo normalized dataset/access run, bundle, từng child interval, exact use-scope hash và policy fingerprint. `wp06_assert_training_source_rights` kiểm lại current scope ngay trước normalization/evaluation/trainer payload; receipt proof cũ không thay current recheck.
- `wp06_assert_training_source_rights` dựng scope từ persisted manifest/source-use-lineage, không từ caller JSON. Receipt legacy thiếu evidence/use-scope hash bị deny.
- `DatabaseReplayTrainingRightsGate` và callers candidate-universe/scorer TEST phải kiểm typed scope cho từng child trước proof, receipt hoặc payload access; rights receipt không thay current recheck.

## Scorer TEST/deployment

- DB `wp06_recheck_scorer_current_rights` và `wp06_verify_scorer_deployment_rights` dựng quyền từ persisted TEST report/proof/source bundle; caller không chọn manifest, scope hay policy fields.
- Kiểm quyền `backtest_research` của bundle generation riêng với quyền `training` dùng lại cho TRAINING/TEST; từng child interval dùng `warmup_start` và `range_end`, không `evaluation_start`.
- Mọi SQL path `access_kind='evaluation'` và scorer test opportunity/access path phải phủ purpose/output/derived-use đúng bảng trên, bao gồm `e8f0a2c4d6b8` và mọi migration/wrapper sau đó.
- Chỉ trả metadata/eligibility/fingerprint; không lộ probabilities, label payload, trade rows hay evidence bytes qua rights gateway.

## Backfill và replay rights gate

- Đường tạo dữ liệu thực tế là `BinanceRestBackfillRunner.run_batch` → `SourceRegistryBackfillRightsGate.authorize`, không phải `DatabaseReplayTrainingRightsGate`. Gate dựng `SourceUseScope` từ request server đã xác thực và identity policy. `BACKTEST_RESEARCH` dùng `aggregate_report`/`aggregate_reporting`; `PAPER_DEMO` dùng `paper_inference`/`paper_inference`. Source scope thiếu dimension/evidence thì dừng trước HTTP fetch/write.
- Đường replay là `BacktestDatasetReplayLoader._authorize_current_rights`; request được dựng từ persisted READY manifest `[warmup_start, range_end)`. Mỗi page/revalidate phải so policy fingerprint và `covers` trước khi trả/đưa bars vào executor. Scope thay đổi sau proof nhưng trước training claim vẫn phải deny ở gate TRAINING độc lập.
- `BackfillRightsPurpose` không thay `SourceUsePurpose` và replay/backtest receipt không được dùng làm TRAINING quyền. Gate `DatabaseReplayTrainingRightsGate` kiểm purpose TRAINING cho child bundle riêng.

## Candidate PAPER, SHADOW và DRIFT

- Runtime scorer kiểm candidate-specific scope từ `LearningFeatureVector.timeframe_lineage`, parse chính xác MarketBar record UUID và DB xác minh workspace/source/symbol/timeframe/open time/revision/event identity. Check trước inference và lặp lại bên trong transaction ghi BotDecision + score event; policy đổi trước atomic write thì không commit decision/event.
- Score event chỉ giữ `source_event_digests`, không đủ để dựng lại member bars. Thêm bảng `bot_decision_source_bar` append-only (workspace, BotDecision, timeframe snapshot, MarketBar ID, event ID, ordinal); ghi toàn bộ strategy snapshot bars cùng BotDecision dù có scorer event hay baseline. Composite FK tenant-scoped và DB guard xác minh event/revision.
- Runtime source gate, score-event insert, Shadow và Drift dùng đúng `bot_decision_source_bar`. Như vậy observer vẫn có baseline decision chưa có score event. Decision thiếu lineage bị từ chối khỏi rights-dependent observer; positive E2E phải chứng minh cả baseline lẫn scorer decision được quan sát đúng.
- Shadow observer recheck rights trên từng decision-source-bar set ngay trước append; không nhận receipt/boolean source-wide cho batch.
- Drift assessor khóa nguồn, đóng exact member set, recheck current scope cho từng member, rồi mới persist. Thiếu hoặc uncovered member làm cohort `INCONCLUSIVE`; report không đổi bot/deployment/entry-hold state.
- Giữ nguyên QA deployment denial, workspace isolation, lock ordering, append-only, closed `search_path`, `REVOKE PUBLIC`, owner `NOLOGIN/NOBYPASSRLS`, và EXECUTE grant tối thiểu. Không cấp SELECT evidence table trực tiếp cho trainer/scorer/runtime/shadow/drift/admin.

## Phạm vi lỗi và race

- Sai/missing workspace, source/provider map, policy identity, symbol, interval, market bar revision/checksum, event identity, range, output, derived-use hoặc retention: deny.
- Missing source-use producer lineage không được thay bằng `DatasetManifest` global range hoặc feature hash; source-dependent use fail closed.
- Legacy policy/evidence/receipt chưa có scope hash bị deny trên WP06 gates. Unknown QA/runtime source không tự được cấp license.
- Revocation/policy/evidence change giữa preflight và write phải được kiểm lại dưới source lock/transaction. Runtime score deny không ghi Decision, score event, deployment pointer hay bot state; denied fold không ghi sample/receipt; denied report không append report.
- Tạo proof/approval ở thời điểm trước không đóng băng quyền hiện hành: revoke/thu hẹp scope trước normalize, training batch claim hoặc TEST read phải từ chối trước khi phát sample payload/receipt.
- Không thay đổi state machine/hard gate của SHADOW/PAPER_GATE, không tự động HOLD, không coi scope là xác minh pháp lý.

## Migration và kiểm thử

- Migration history giữ nguyên; nối lần lượt sau `a4c6e8f0b2d5`: TRAINING/use-lineage, replay/scorer TEST, runtime/shadow/drift. Alembic luôn một head.
- Kiểm từng identity `source_id/provider/product/venue/contract`, symbol, interval, start/end (exact start, exact exclusive end, lệch 1µs), purpose, output, derived-use, retention và mixed-child bundle. Test timestamp bar-open ở scope end phải deny; close time tại biên request không nới `range_end`.
- Python/SQL parity: canonical scope/fingerprint; exact source/revision/workspace joins; missing/duplicate/forged IDs; stale receipt; policy race trước atomic commit; no-write invariants.
- Compose upgrade phải bao database có license ledger/receipt cũ; kiểm owner/ACL, RLS FORCE, append-only, không còn role EXECUTE helper quyền rộng. Cập nhật mọi E2E assertion hardcode current head `c7e9a1b3d5f8` và mọi signature/probe/grant cũ, gồm fold, deployment, shadow/drift, candidate universe, model registry, entry lineage và receipt projection.
- E2E dùng ledger metadata fixture do migration tạo; không giả receipt QA thành license thật. SHADOW/PAPER_GATE vẫn khóa tới khi có external license review và evidence vận hành độc lập.

## Nguồn kiểm tra

- Binance USDⓈ-M Futures Kline Candlestick Data, kiểm tra 2026-11-13: https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data — endpoint `/fapi/v1/klines` định danh Kline theo open time; response có open time và close time millisecond. Dùng open-time range đã ghim trong bundle, không cộng microsecond vào `range_end`.
- Binance USDⓈ-M connector repository, kiểm tra 2026-11-13: https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/market.py — liên kết chính thức API connector tới endpoint Kline `/fapi/v1/klines`.
