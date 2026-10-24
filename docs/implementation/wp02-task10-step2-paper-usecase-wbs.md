# WP-02 Task 10 Step 2 — PAPER application use-case wiring WBS

**Trạng thái:** WBS đã được duyệt; còn remediation bắt buộc theo review, chưa nghiệm thu.  
**Phạm vi:** nối luồng PAPER H03 qua application use case dùng account đã bootstrap ở `trading-runtime`.  
**Ranh giới:** không thêm market worker tự chạy, không gọi Binance/exchange và không bật giao dịch thật.

## 1. Mục tiêu và lý do tách WBS

H03 hiện chứng minh được luồng dữ liệu/ledger từ settings đã publish đến giao dịch PAPER đóng, nhưng probe E2E tự tạo `PaperExecutionGateway`, `PaperDecisionCycle`, `PaperLifecycleCoordinator` và repository. Runtime bootstrap đã tạo, hydrate, xác minh và công bố account state tại `app.state.paper_accounts`; hai luồng này chưa được nối.

Mục tiêu là để H03 đi qua một application-level service duy nhất, lấy account từ trạng thái bootstrap đã xác minh và thực hiện các command PAPER trong transaction. Probe vẫn được phép cung cấp fixture dữ liệu thị trường xác định; nó không được tự dựng gateway, coordinator, repository hoặc quyết định risk đã duyệt.

Tài liệu liên quan:

- [WP-02 implementation plan](../superpowers/plans/2026-10-22-wp02-implementation-plan.md) — Task 10 Step 2 và cổng hoàn thành WP-02.
- [PAPER recovery WBS](./wp02e-paper-recovery-wbs.md) — checkpoint, restore, ledger verification và transaction seam.
- [Runtime bootstrap WBS](./wp02e-runtime-bootstrap-wbs.md) — deployment workspace, account bootstrap, readiness và RB7.
- [`app.py`](../../src/trading_bot/app.py) — lifespan, `app.state.paper_accounts` và health endpoints hiện tại.
- [`paper_runtime_bootstrap.py`](../../src/trading_bot/application/paper_runtime_bootstrap.py) — account đã hydrate, execution profile và coordinator.
- [`paper_decision_cycle.py`](../../src/trading_bot/application/paper_decision_cycle.py) — quyết định strategy/plan/risk.
- [`paper_lifecycle.py`](../../src/trading_bot/application/paper_lifecycle.py) — submit, entry fill, monitor/exit, checkpoint và ledger.
- [`test_compose_boundary.py`](../../tests/e2e/test_compose_boundary.py) — H03 hiện tại cần chuyển sang use case.

## 2. Hiện trạng và kết luận kiểm tra source

1. `PaperRuntimeBootstrapService.bootstrap()` enumerate account PAPER/ENABLED trong một workspace deployment, nạp root `WorkspaceSettings` đã publish, resolve execution profile, hydrate + verify checkpoint/ledger, rồi chỉ công bố map `account_id -> PaperRuntimeAccount` sau commit. Nếu một account lỗi thì map active rỗng.
2. `PaperRuntimeAccount` giữ `account_id`, `gateway`, `coordinator`, `execution_profile`; nó chưa giữ application use case hay published context provider.
3. `create_app()` nối bootstrap vào lifespan cho role `trading-runtime`, công bố `paper_accounts`, và `/health/ready` chỉ xác nhận DB + PAPER recovery. Response hiện nêu rõ `market_dispatch=not_configured` và `exchange_execution=disabled`.
4. `PaperDecisionCycle.prepare_published_with_risk_profile()` đánh giá từ published settings, snapshot và account/risk/planner context. `submit_approved()` thuần có thể mutate gateway RAM; vì vậy không dùng phương thức này để persist production-intent.
5. `PaperLifecycleCoordinator.submit_approved()` là persistence boundary phù hợp: kiểm tra risk-approved result, khóa account, nạp/verify checkpoint, tạo ledger intent/checkpoint trong cùng UoW và publish gateway candidate bằng after-commit callback.
6. `apply_entry_market_event()` và `monitor_market_event()` cũng nhận UoW caller-owned; chúng nạp candidate từ checkpoint, xác minh lineage, ghi fills/position/trade/reservation liên quan và chỉ publish sau commit.
7. H03 probe hiện tự thực hiện decision context, ghi risk evaluation/reservation, submit, entry fill, hydrate, monitor/exit, recovery check và report assertion. Có hai transaction risk-approval và submit; WBS này yêu cầu gom approval/reservation/intent/checkpoint vào một application command và giữ mỗi lifecycle event trong một UoW riêng.

**Kết luận:** application orchestration là phần còn thiếu. Seam nhỏ nhất an toàn là một service application được tạo từ `trading-runtime` lifespan **sau khi toàn bộ bootstrap sẵn sàng**, chỉ nhận `account_id` và typed command/input từ caller; nó tra account từ map đã bootstrap, tự mở UoW tenant-scoped và gọi decision-cycle/coordinator. Caller không truyền gateway, coordinator, repository, execution profile, workspace scope hoặc cờ `risk_approved`.

## 3. ADR đề xuất — một application seam nội bộ cho PAPER

### ADR-01: `PaperRuntimeApplicationService` là điểm vào cho H03

- Tạo một application service cho một deployment workspace, ví dụ `PaperRuntimeApplicationService`.
- Factory tạo service trong lifespan chỉ khi `PaperRuntimeBootstrapResult.ready` là `true`; truyền vào đúng `workspace_id` từ `AppSettings.runtime_workspace_id`, `session_factory` và map `active_accounts` đã bootstrap.
- Lưu service ở `app.state.paper_application`; khi bootstrap fail, service không tồn tại/không sẵn sàng và mọi command đều từ chối với diagnostic code an toàn.
- `PaperRuntimeAccount` trong map là nguồn duy nhất để resolve account. Account ID không thuộc map, trạng thái chưa `READY` hoặc profile/settings không khớp thì từ chối trước khi thay đổi dữ liệu.
- Không thêm public HTTP route để submit signal trong slice này. H03 gọi application API trong process/lifespan của app Compose; endpoint health chỉ nói về recovery readiness.

### ADR-02: context từ dữ liệu có provenance, không từ quyền caller

- Command mang account ID cùng định danh input đã lưu (feature snapshot/signal context, symbol, timeframe, decision time, event/bar lineage và correlation ID nếu cần). Caller không truyền credential hay instance domain gateway.
- Application layer tải/kiểm tra root `WorkspaceSettings` hiệu lực và context dữ liệu theo workspace/account; sử dụng execution profile đã bootstrap/pin và so khớp hash/version trước khi đánh giá.
- Với H03, fixture hiện hữu được lưu qua cùng các repository tenant-scoped. Application service nhận một `PaperDecisionContextProvider` port để đọc snapshot/AccountSnapshot/RiskState/exposures/cost/planner facts từ nguồn có lineage. Test adapter chỉ được dùng cho deterministic fixture nếu context đó được xác thực với row đã lưu; không được inject risk-approved result.
- Trước khi implement, xác nhận phạm vi tối thiểu của context provider: nếu không thể tái tạo planner/risk context từ dữ liệu bền vững hoặc kiểm chứng provenance của fixture thì ghi blocker; không chuyển context tùy ý thành production contract.

### ADR-03: một transaction cho approval → reservation → intent/checkpoint

- Một `evaluate_and_submit()` mở đúng một `UnitOfWork(workspace_id=runtime_workspace_id)`.
- Trong transaction: kiểm tra account/map, published root settings/profile lineage, load context, gọi `PaperDecisionCycle.prepare_published_with_risk_profile()`, ghi `RiskDecision`/reservation nếu được duyệt, rồi gọi `PaperLifecycleCoordinator.submit_approved()` để ghi intent và checkpoint.
- Chỉ coordinator đăng ký callback publish gateway sau commit. Rollback phải giữ gateway RAM nguyên trạng; application service không tự mutate gateway và không dùng `PaperDecisionCycle.submit_approved()`.
- `NO_TRADE`, plan rejected hoặc risk rejected trả kết quả typed mà không tạo reservation/intent/checkpoint mới.
- Cùng command/idempotency lineage trả lại cùng kết quả/intent; payload cùng key nhưng hash khác trả conflict. Khóa idempotency được tạo/kiểm tra trong application service từ workspace, account, settings version, strategy release và feature/signal snapshot ID; không chỉ dựa vào chuỗi tùy chọn do caller đưa vào.
- Nếu repository hiện có không hỗ trợ đọc lại kết quả risk decision/reservation theo idempotency key trong cùng transaction, bổ sung một interface/query nhỏ có test; không chấp nhận tạo reservation trùng khi retry.

### ADR-04: mỗi lifecycle event cũng là application command

- `apply_entry_event()` và `monitor_position()` nhận account ID + ledger intent/position IDs + bar/event lineage; không nhận gateway/coordinator/repository.
- Service resolve runtime account, xác minh rằng event thuộc workspace/account và gọi coordinator trong một UoW riêng cho event. Coordinator chịu trách nhiệm checkpoint lock, ledger verifier, idempotent event key, append ledger, reservation transfer/release/unknown và after-commit publish.
- Retry cùng event/key/hash là idempotent; key trùng với payload khác, account/intent chéo tenant, event không đúng lineage hoặc checkpoint thiếu/lệch đều fail-closed.
- Application layer từ chối các input market chưa khả dụng ở thời điểm ra quyết định. Đây là command handler đồng bộ được test gọi tường minh; không thêm polling, scheduler, stream consumer hoặc event loop tự chạy.

### ADR-05: readiness là recovery readiness, không phải trading readiness

- `/health/ready` tiếp tục chỉ yêu cầu DB + hydrate/verify PAPER thành công cho toàn workspace deployment.
- Response giữ `market_dispatch=not_configured`, `exchange_execution=disabled`. Không được đặt hai capability này thành ready khi gắn application use case.
- Per-account command chỉ chấp nhận `READY` account trong `app.state.paper_accounts`; account bị recovery-required không có route/usecase nào được mutate state.
- Không có account, bootstrap fail, settings/profile mismatch hoặc ledger mismatch đều fail-closed; `/health/live` vẫn phản ánh process còn chạy.

## 4. Interface dự kiến

Tên có thể được điều chỉnh khi implement miễn không mở rộng trust boundary.

```python
@dataclass(frozen=True)
class PaperDecisionCommand:
    account_id: UUID
    feature_snapshot_id: str
    decision_time: datetime
    observed_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class PaperMarketEventCommand:
    account_id: UUID
    ledger_intent_id: UUID
    event: PaperBar


class PaperRuntimeApplicationService:
    async def evaluate_and_submit(
        self, command: PaperDecisionCommand
    ) -> PaperApplicationResult: ...

    async def apply_entry_event(self, command: PaperMarketEventCommand) -> EntryLifecycleResult: ...

    async def monitor_position(self, command: PaperMonitorCommand) -> PositionLifecycleResult: ...
```

Interface thật phải dùng model typed trong repo, không nhất thiết copy ví dụ trên. Decision context loader và service factory là application ports; SQLAlchemy repositories/UoW là adapters. Không để router, Compose test hoặc Telegram gọi adapter/repository/domain gateway trực tiếp.

## 5. WBS trước implement

| ID | Đầu việc | Bàn giao | Điều kiện nghiệm thu |
|---|---|---|---|
| WBS-1 | Chốt ADR và traceability | ADR-01…05 trong tài liệu này hoặc decision record riêng nếu project convention yêu cầu | Một workspace deployment identity; không dùng `WorkspaceSettings` làm tenant identity; application API nội bộ; market dispatch/exchange vẫn tắt. |
| WBS-2 | Định nghĩa typed commands/results và context port | Module application contract + protocol/provider | Request không nhận gateway, coordinator, repository, profile, workspace override hay risk approval; context có settings/feature/account/risk lineage, as-of và workspace/account scope. |
| WBS-3 | Implement decision+submission application use case | Service orchestration `evaluate_and_submit()` | Một UoW bao settings/context read, evaluation, risk decision/reservation, ledger intent/checkpoint; kết quả từ chối không sinh side effects; candidate state publish chỉ sau commit. |
| WBS-4 | Implement entry/monitor command use cases | Service methods gọi `PaperLifecycleCoordinator` | Mỗi event có một UoW; idempotency theo event lineage; tenant/account/intent match; checkpoint/latest verifier chạy; fill/trade/reservation và snapshot commit đồng bộ. |
| WBS-5 | Nối runtime lifespan sau bootstrap | Factory + `app.state.paper_application` | Service chỉ được công bố nếu bootstrap toàn workspace READY; thất bại không để lộ một phần account map hoặc cho phép command; health capability flags không đổi. |
| WBS-6 | Unit TDD cho use case, wiring và rollback | Test application service + app lifespan | Bao phủ account ngoài map, root settings/profile drift, no checkpoint, retry/conflicting idempotency, risk rejected/no trade, DB rollback, after-commit publish, event retry, event khác payload, tenant mismatch, service absent khi not-ready. Từng invariant có RED trước GREEN. |
| WBS-7 | Refactor H03 Compose probe | `test_compose_boundary.py` gọi service từ app đã bootstrap | Probe không tạo `PaperExecutionGateway`, `PaperLifecycleCoordinator`, repository, UoW, RiskEngine, hoặc gọi `cycle.submit_approved()` trực tiếp; dùng root settings, persisted feature fixture và active account runtime. Kết quả vẫn chứng minh intent, ENTRY/EXIT fills, closed trade, reservation transfer/release, ledger/report PnL, profile/settings lineage và tenant isolation. |
| WBS-8 | Rerun và ghi evidence | Test/static evidence; cập nhật memory/status/master plan ở lượt tích hợp cuối | Chạy unit liên quan + toàn unit suite, Compose H03, readiness E2E liên quan, Ruff/format/mypy/diff. Không chạy Compose song song; teardown stack sạch. Chỉ parent hợp nhất shared status/memory/master plan sau khi các luồng dùng chung kết thúc. |
| WBS-9 | Xử lý lỗi công bố sau commit | Invalidation hook trên runtime account/readiness | Nếu callback `after_commit` lỗi sau khi DB đã commit, vô hiệu hóa account đó ngay; không nhận command mới và `/health/ready` không còn READY cho tới khi bootstrap/hydrate + verifier thành công. Log chỉ dùng diagnostic code an toàn. Test trường hợp DB commit xong nhưng publish RAM lỗi. |
| WBS-10 | H03 dùng đúng service từ lifespan | Compose probe chạy app lifespan với provider factory của fixture | Probe phải lấy `app.state.paper_application` do `create_app` tạo sau bootstrap; không tự gọi bootstrap rồi tự khởi tạo application service. Fixture account/seed/genesis được chuẩn bị trước startup bằng setup cô lập; provider chỉ được tạo qua dependency factory và chỉ đọc lineage đã lưu. Capability flags giữ `market_dispatch=not_configured`, `exchange_execution=disabled`. |
| WBS-11 | Ràng buộc UUID strategy release | Resolver/adapter đọc release record đã lưu trong workspace | `strategy_release_id` ghi ledger phải được phân giải từ release record đã lưu và khớp release root settings chọn, workspace, release name/version và content hash. Provider/caller không tự gán UUID. Record thiếu, không duy nhất, khác workspace hoặc hash lệch thì fail-closed; có unit và Compose assertion. |
| WBS-12 | Xác minh provenance cho monitor inputs | Typed monitor evidence contract | Không nhận `ATR` hoặc `opposing_signal` tùy ý. Command chỉ nhận định danh feature/snapshot/signal đã lưu; service/provider tải lại trong đúng workspace/account/symbol/timeframe, kiểm tra content/version hash và as-of trước bar. Thiếu lineage, input tương lai hoặc scope lệch bị từ chối; thêm positive/negative unit tests. |
| WBS-13 | Chuyển profile drift thành readiness gate | Invalid account state + bootstrap/recovery path | Nếu root settings/profile hiệu lực lệch execution profile đã pin, vô hiệu hóa account và hạ readiness deployment; không chỉ trả command error trong khi `/health/ready` vẫn READY. Chỉ tái kích hoạt sau bootstrap/hydrate/verifier với profile mới; profile switch khi còn state hoạt động vẫn bị chặn. Bao phủ unit/readiness tests. |
| WBS-14 | Kiểm thử retry qua persisted intent | DB-backed idempotency acceptance | Compose/DB E2E nộp command, retry sau commit và xác minh cùng intent/reservation/order được trả lại, không thêm risk/reservation/intent và provider/cycle không chạy lại. Thêm conflict khi cùng idempotency lineage có evidence/hash khác: từ chối, không đổi gateway/ledger. Lặp hai lần với fake UoW không đủ nghiệm thu. |

## 6. Điều kiện nghiệm thu tổng thể

### Application boundary

- H03 lấy `PaperRuntimeAccount` từ map do `PaperRuntimeBootstrapService` publish; không tự dựng gateway/coordinator.
- Application service tự lấy workspace deployment identity và mở UoW có RLS; mọi repository read/write nằm bên trong boundary.
- Root `WorkspaceSettings`/Admin publish vẫn là nguồn cấu hình business duy nhất. Secret, exchange credential và execution-profile override không đi qua command.
- Strategy/release, settings version/hash, feature snapshot, risk decision, reservation, plan, fill và trade lineage đều đối chiếu được với record persisted thuộc cùng workspace/account.

### Atomicity, rollback, recovery, idempotency

- Lỗi sau khi lưu risk reservation nhưng trước khi intent/checkpoint hoàn tất làm transaction rollback cả hai; retry không sinh duplicate hoặc orphan reservation.
- `after_commit` là đường duy nhất cập nhật gateway instance đang phục vụ; rollback không đổi state RAM đã công bố.
- Nếu publish sau commit thất bại, account runtime bị vô hiệu hóa và readiness hạ ngay; DB đã commit phải được hydrate lại trước khi account nhận command.
- Startup sau H03 close hydrate đúng state từ checkpoint và verifier ledger; mismatch/no checkpoint trả not-ready và không cho command.
- Duplicate command cùng canonical payload không tạo thêm risk decision/reservation/intent/fill/trade; cùng key khác payload conflict.
- Retry được chứng minh trên dữ liệu DB đã commit ở cả nhánh duplicate và conflict; fake UoW chỉ xác nhận orchestration cơ bản, không thay thế E2E.
- Entry/monitor event duplicate xử lý qua checkpoint event key theo contract đang có; duplicate khi đã xử lý nhưng checkpoint hiện tại mới hơn không khôi phục snapshot cũ.

### Readiness và an toàn vận hành

- `/health/ready` 200 chỉ xác nhận recovery readiness theo RB5; không hàm ý có data feed, dispatcher, profitability hoặc exchange connection.
- Publish callback lỗi hoặc profile drift làm `/health/ready` không đạt; account chỉ READY lại sau bootstrap/recovery xác minh đầy đủ.
- Lỗi bootstrap/wiring trả safe diagnostic code, không trả secret, UUID account hoặc exception text.
- Không có Binance API/client/order endpoint, credential decrypt hoặc LIVE mode trong H03 command path.

### Unit + Compose E2E

- Unit: transaction boundary, UoW rollback, publish callback, risk reservation idempotency, root settings/profile drift, account map scope, fail-closed recovery và no-op result được chứng minh riêng.
- Unit: thêm commit xong nhưng publish lỗi, readiness invalidation, resolver release UUID theo workspace record, monitor ATR/signal provenance và profile drift chặn readiness.
- Compose: chạy cùng stack/content-addressed image theo fixture hiện có; bootstrap account qua DB runtime role/RLS; lấy service/provider từ app lifespan; quyết định → reservation+submit → retry duplicate/conflict → entry event → recovery bằng service mới → monitor/exit có provenance → persisted trade/report assertions; xác nhận tenant isolation.
- Compose failure capture/cleanup tuân test harness hiện hành; chỉ chạy theo slot dùng chung, không build image E2E mới cho mỗi lần.
- H03 success không được dùng làm bằng chứng production dispatcher; `/health/ready` phải giữ `market_dispatch=not_configured`, `exchange_execution=disabled`.

## 7. Không thuộc WBS này

- Market-data ingestion/dispatch loop, Kafka/Redis stream consumer, scheduler hoặc background task tự phát command.
- Binance Futures adapter, private API, exchange credentials, order placement, reconciliation với venue hoặc LIVE execution.
- Public HTTP mutation endpoint hoặc Telegram command giao dịch; authorization/audit contract của các channel này cần WBS riêng.
- Multi-workspace dispatcher, cross-tenant registry, multi-bot/account routing hoặc RLS bypass.
- Tự học/training AI, strategy auto-promotion hoặc tự sửa settings/strategy trên production.
- Thay đổi semantics ledger/checkpoint/recovery hoặc migrations không cần thiết cho application boundary. Nếu thiếu schema/transaction capability thì dừng và mở WBS/schema decision riêng.
- Mark toàn bộ WP-02 hoàn thành. Task 10 còn các bước full verification/review/docs/memory; các cổng Task 8/9 và WP-02 02A–G vẫn phải được audit riêng.

## 8. Rủi ro và điều kiện dừng

1. **Risk approval không idempotent:** nếu risk evaluation/reservation không thể retry theo key và payload hash trong cùng UoW, dừng trước wiring H03; không bỏ kiểm tra idempotency.
2. **Context không được tái tạo từ persisted lineage:** nếu snapshot/planner/risk facts chỉ tồn tại trong biến test hoặc caller có thể giả mạo risk state, H03 chưa chứng minh application trust boundary. Bổ sung provider/test fixture adapter được xác thực hoặc ghi blocker cụ thể; không nhận `RISK_APPROVED` từ test.
3. **Settings/profile drift trong command:** nếu root settings hiện hành không còn khớp execution profile đã bootstrap, reject an toàn và yêu cầu restart/recovery path; không tự thay policy trong gateway đang giữ active order/position.
4. **UoW callback:** xác minh rollback không gọi callback và callback exception không làm báo lỗi commit giả khi DB đã commit. Nếu callback failure có thể tạo trạng thái process mơ hồ, service phải invalidate account/readiness cho tới hydrate lại.
5. **Release identity:** nếu chưa ánh xạ được `strategy_release_id` tới release record đã lưu và được root settings chọn, không ghi ledger bằng UUID do fixture/provider tự chọn; dừng để bổ sung persistence/resolver.
6. **Monitor provenance:** nếu ATR/opposing signal chưa có nguồn lưu cùng mốc dữ liệu để tái kiểm chứng, không chấp nhận trực tiếp giá trị caller; giữ monitor command chưa khả dụng hoặc mở WBS dữ liệu riêng.
7. **Profile drift:** nếu chưa thể làm account/readiness mất hiệu lực ngay và chỉ khôi phục sau bootstrap/hydrate, không xử lý thêm command dưới trạng thái READY cũ.
8. **Shared working tree/Compose:** sửa H03 chỉ sau khi kiểm tra file ownership/agent coordination; không chồng patch hoặc chạy Compose đồng thời với luồng khác.

## 9. Thuật ngữ trong WBS

| English | Tiếng Việt | Mô tả ngắn |
|---|---|---|
| Application use case | Ca sử dụng tầng ứng dụng | Điểm điều phối một hành động nghiệp vụ; tại đây chịu trách nhiệm scope, transaction, domain logic và lưu trữ. [PAPER recovery WBS](./wp02e-paper-recovery-wbs.md) |
| Unit of Work (UoW) | Đơn vị công việc/giao dịch | Phạm vi commit hoặc rollback nguyên tử cho các thao tác DB liên quan. [SQLAlchemy session transaction](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html) |
| Idempotency | Tính lặp lại an toàn | Cùng một command retry không tạo thêm side effect; key giống nhưng payload khác phải bị từ chối. [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests) |
| After-commit publish | Công bố sau commit | Chỉ cập nhật state RAM sau khi dữ liệu bền vững đã commit thành công. [PAPER recovery WBS](./wp02e-paper-recovery-wbs.md) |
| Fail closed | Từ chối an toàn | Khi lineage, quyền hoặc trạng thái chưa xác minh, không cho phép thay đổi state. [Runtime bootstrap WBS](./wp02e-runtime-bootstrap-wbs.md) |
| Point in time / as-of | Đúng thời điểm / tại thời điểm | Chỉ sử dụng dữ liệu đã khả dụng tại mốc ra quyết định, không dùng dữ liệu tương lai. [WP-02 plan](../superpowers/plans/2026-10-22-wp02-implementation-plan.md) |
| Market dispatch | Phân phối sự kiện thị trường | Luồng chuyển bar/tín hiệu đã kiểm tra tới application command; WBS này chỉ kiểm thử gọi tường minh, không triển khai dispatcher. [Runtime bootstrap WBS](./wp02e-runtime-bootstrap-wbs.md) |

## 10. Gate remediation trước khi sửa source tiếp

- Review findings được chuyển thành WBS-9…14; không tiếp tục source cho tới khi parent duyệt thứ tự và phạm vi remediation.
- Unit phải chứng minh DB commit thành công nhưng callback publish thất bại làm account/readiness bị vô hiệu hóa, không tạo READY giả hoặc nhận command mới.
- Compose H03 phải đi qua `app.state.paper_application` do lifespan tạo và provider factory test-only; probe không được tự tạo bootstrap result/application service.
- Resolver phải đối chiếu UUID strategy release với persisted release record đã chọn trong cùng workspace và kiểm content hash/version.
- Monitor ATR/opposing-signal phải được tải từ input lineage đã lưu và kiểm as-of; caller chỉ gửi định danh, không gửi quyết định tín hiệu tùy ý.
- Profile drift phải chuyển readiness sang not-ready và buộc bootstrap/hydrate/verifier mới trước khi account nhận command.
- Retry acceptance phải đọc persisted duplicate intent/reservation/order; conflict lineage/payload không được ghi thêm ledger/risk state hoặc đổi gateway.
- Chạy unit, static và Compose theo slot độc quyền; không xem compile/collect-only/fake UoW là bằng chứng thay thế Compose E2E. Không sửa memory/status/master plan chung trong luồng này.
