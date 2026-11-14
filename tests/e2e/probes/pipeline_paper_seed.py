"""Phần mở rộng probe: chạy mẫu Strategy Card L0–L6 qua đường PAPER thật để gieo ledger kiểm thử.

Tệp này KHÔNG chạy độc lập. `scripts/seed_pipeline_paper_ledger.py` chèn nội dung của nó vào
cuối `tests/e2e/probes/multibot_runtime.py` (ngay trước `_main()`), nên mọi tên như `_connect`,
`runtime_app`, `fill`, `STATE`, `PHASES` đến từ probe gốc.

Đường đi được kiểm: Strategy Card → `StrategyPipelineEngine` (L0–L6) → `StrategyCardStrategy` →
evidence fan-out → `bot_decision` CANDIDATE → TradePlanner → RiskEngine → giữ ngân sách workspace →
intent PAPER → fill → position monitor (TAKE_PROFIT / STOP_LOSS / TIME_STOP) → trade + trade_cost.
Release của thẻ đến từ catalog của sản phẩm (`application.strategy_catalog`), nên runtime thật
nhận diện được release và hash ghim trên vị thế. Bot A dùng release của thẻ, bot B dùng H03.

Dữ liệu là mô phỏng PAPER: không gọi Binance, giá quanh 100 theo quy ước của probe gốc, và không
phải bằng chứng về khả năng sinh lời.
"""
# ruff: noqa: F821  — các tên của probe gốc chỉ tồn tại sau khi ghép mã nguồn.

# Nến tín hiệu (open, high, low, close) cho thẻ LONG: búa có râu dưới dài, đóng gần đỉnh.
# Giá phải nằm trên bước giá 0,1 của instrument, nếu không RiskEngine từ chối kế hoạch.
PIPELINE_SIGNAL_BAR = ("100.0", "100.3", "99.0", "100.2")


def _pipeline_card_strategy() -> Any:
    """Release Strategy Card của sản phẩm (đúng rule mà runtime thật dùng)."""
    from trading_bot.application.strategy_catalog import strategy_card_rules

    return strategy_card_rules()[0][0]


_base_strategy_snapshot = _strategy_snapshot


def _strategy_snapshot(release_id: str, snapshot_id: str, decision_time: datetime) -> Any:
    """Với release của thẻ, thay nến cuối bằng nến tín hiệu để pipeline đánh giá L1–L6."""
    from dataclasses import replace

    if release_id != STATE.get("pipeline_card_release"):
        return _base_strategy_snapshot(release_id, snapshot_id, decision_time)
    # Giữ chuỗi nến và feature `atr_14` của snapshot H03; chỉ đổi hình dạng nến tín hiệu.
    snapshot = _base_strategy_snapshot(RELEASE, snapshot_id, decision_time)
    opened, high, low, close = (Decimal(value) for value in PIPELINE_SIGNAL_BAR)
    series = snapshot.series[0]
    signal_bar = replace(series.bars[-1], open=opened, high=high, low=low, close=close)
    return replace(
        snapshot,
        series=(replace(series, bars=(*series.bars[:-1], signal_bar)), *snapshot.series[1:]),
    )


def pipeline_seed_setup() -> dict[str, Any]:
    """Workspace PAPER mới + release/cấu hình bot của thẻ + quyền owner cho tài khoản đăng nhập."""
    from trading_bot.config.bot_settings import BotConfigOverlay, canonical_overlay, overlay_hash

    strategy = _pipeline_card_strategy()
    seeded = seed()
    STATE.update(seeded)
    ws = _ws()
    workspace = UUID(ws["workspace"])
    root_id = UUID(ws["settings_version_id"])
    slug = STATE["pipeline_workspace_slug"]
    with _connect() as connection, connection.transaction():
        connection.execute(
            "UPDATE workspace SET name=%s, slug=%s WHERE id=%s",
            ("Crypidian Pipeline Demo", slug, workspace),
        )
        for key, label in (("a", "strategy-card"), ("b", "h03-donchian"), ("c", "idle")):
            connection.execute(
                "UPDATE trading_account SET name=%s WHERE id=%s",
                (f"Bot {label}", UUID(ws["accounts"][key])),
            )
        login = connection.execute(
            "SELECT id FROM user_account WHERE email_normalized=%s",
            (STATE["pipeline_login_email"],),
        ).fetchone()
        if login is None:
            raise AssertionError("Không tìm thấy tài khoản đăng nhập để cấp quyền owner.")
        membership = uuid4()
        _insert(
            connection,
            "user_membership",
            id=membership,
            workspace_id=workspace,
            user_id=login[0],
            status="ACTIVE",
        )
        _insert(
            connection,
            "role_binding",
            id=uuid4(),
            workspace_id=workspace,
            membership_id=membership,
            role="owner",
        )
        root_hash, owner = connection.execute(
            "SELECT settings_hash, created_by FROM workspace_settings_version "
            "WHERE workspace_id=%s AND id=%s",
            (workspace, root_id),
        ).fetchone()
        _insert(
            connection,
            "strategy_release",
            id=uuid4(),
            workspace_id=workspace,
            name=strategy.release_id,
            version=1,
            status="PUBLISHED",
            recipe=Jsonb(strategy.recipe_manifest),
            content_hash=strategy.content_hash,
        )
        config_ids: dict[str, str] = {}
        # Bot A chọn release của Strategy Card; bot B chọn rule dựng sẵn H03 (đã có hàng release).
        for key, release_id in (("a", strategy.release_id), ("b", RELEASE)):
            overlay = canonical_overlay(
                BotConfigOverlay.model_validate(
                    {"schema_version": 1, "strategy": {"release_id": release_id}}
                )
            )
            config_id = uuid4()
            _insert(
                connection,
                "bot_config_version",
                id=config_id,
                workspace_id=workspace,
                bot_id=UUID(ws["bots"][key]),
                account_id=UUID(ws["accounts"][key]),
                version=1,
                schema_version=1,
                overlay_json=Jsonb(overlay),
                overlay_hash=overlay_hash(overlay),
                base_settings_version_id=root_id,
                base_settings_hash=root_hash,
                status="DRAFT",
                revision=1,
                created_by=owner,
                updated_by=owner,
            )
            connection.execute(
                "UPDATE bot_config_version SET status='PUBLISHED', approved_by=%s, "
                "published_at=%s, effective_at=%s WHERE workspace_id=%s AND id=%s",
                (owner, _at(-600), _at(-600), workspace, config_id),
            )
            config_ids[key] = str(config_id)
    return seeded | {
        "pipeline_card_release": strategy.release_id,
        "pipeline_config_ids": config_ids,
        "pipeline_workspace_slug": slug,
    }


async def _pipeline_fill(
    app: Any, account_id: UUID, submitted: dict[str, Any], moment: datetime
) -> dict[str, Any]:
    """Đưa các nến 1 phút kế tiếp cho tới khi lệnh vào được khớp theo mô hình next-bar."""
    from trading_bot.application.paper_runtime_application import PaperEntryEventCommand
    from trading_bot.domain.execution.paper import PaperBar

    outcome: dict[str, Any] = {"status": None, "fills": 0, "trade_id": None}
    for offset in range(6):
        opened = moment + timedelta(seconds=1, minutes=offset)
        result = await app.state.paper_application.apply_entry_event(
            PaperEntryEventCommand(
                account_id=account_id,
                ledger_intent_id=UUID(submitted["intent_id"]),
                paper_intent_id=submitted["paper_intent_id"],
                bar=PaperBar(
                    f"pipeline-fill-{submitted['intent_id'][-8:]}-{offset}",
                    "BTCUSDT",
                    opened,
                    opened + timedelta(minutes=1),
                    opened + timedelta(minutes=1),
                    Decimal("100.0"),
                    Decimal("100.5"),
                    Decimal("99.5"),
                    Decimal("100.0"),
                ),
            )
        )
        outcome = {
            "status": result.order.status.value,
            "fills": result.persisted_fill_count,
            "trade_id": str(result.trade_id) if result.trade_id else None,
            "filled_bar_opened_at": opened.isoformat(),
        }
        if result.trade_id:
            break
    return outcome


def _exit_bars(
    scenario: str, side: str, stop: Decimal, target: Decimal | None, start: datetime
) -> Any:
    """Sinh nến 1 phút sau khi vào lệnh để position monitor tự kết luận lý do đóng."""
    from trading_bot.domain.execution.paper import PaperBar

    def bar(index: int, opened: str, high: str, low: str, close: str) -> Any:
        begin = start + timedelta(minutes=index)
        return PaperBar(
            f"pipeline-{scenario}-{begin.isoformat()}",
            "BTCUSDT",
            begin,
            begin + timedelta(minutes=1),
            begin + timedelta(minutes=1),
            Decimal(opened),
            Decimal(high),
            Decimal(low),
            Decimal(close),
        )

    quiet = ("100.10", "100.40", "99.90", "100.20")
    if scenario == "TIME_STOP":
        # Đi ngang trong biên độ hẹp cho tới khi vượt `max_hold_bars` của recipe.
        return [bar(index, *quiet) for index in range(1, 40)]
    hit_target = scenario == "TAKE_PROFIT"
    if hit_target and target is None:
        raise AssertionError("Kịch bản TAKE_PROFIT cần recipe có mục tiêu chốt lời.")
    level = target if hit_target else stop
    upward = (side == "LONG") == hit_target
    if upward:
        last = (str(level - Decimal("0.6")), str(level + Decimal("0.4")), "100.00", str(level))
        drift = ("100.20", str(level - Decimal("0.5")), "100.00", str(level - Decimal("0.6")))
    else:
        last = (str(level + Decimal("0.6")), "100.40", str(level - Decimal("0.4")), str(level))
        drift = ("100.10", "100.40", str(level + Decimal("0.5")), str(level + Decimal("0.6")))
    return [bar(1, *quiet), bar(2, *drift), bar(3, *last)]


async def pipeline_seed_trades() -> dict[str, Any]:
    """Mỗi kịch bản: evidence → fan-out thật → fill vào lệnh → nến dẫn tới lý do đóng mong muốn."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.models import (
        BotDecisionRecord,
        OrderIntentRecord,
    )
    from trading_bot.adapters.database.models.risk_entities import TradePlanRecord
    from trading_bot.adapters.database.repositories.bot_decisions import (
        PostgresSignalDecisionStore,
    )
    from trading_bot.adapters.database.repositories.feature_snapshots import (
        FeatureSnapshotRepository,
    )
    from trading_bot.application.outbox import UnitOfWork
    from trading_bot.application.paper_runtime_application import PaperMonitorCommand
    from trading_bot.domain.market.signal_evidence import (
        EvidenceSourceRef,
        build_shared_signal_evidence,
    )

    workspace = UUID(_ws()["workspace"])
    engine = _engine(pool=8)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scenarios = STATE["pipeline_scenarios"]
    trades: list[dict[str, Any]] = []
    try:
        async with runtime_app(sessions=sessions, use_real_strategy=True) as app:
            store = PostgresSignalDecisionStore(sessions)
            for index, round_scenarios in enumerate(scenarios):
                # Một evidence được fan-out tới mọi bot đang bật, nên mỗi vòng xử lý cả hai bot.
                moment = datetime.now(UTC).replace(microsecond=0)
                async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                    feature = await FeatureSnapshotRepository(uow.session).get_as_of(
                        workspace_id=workspace, symbol="BTCUSDT", timeframe="1h", as_of=moment
                    )
                if feature is None:
                    raise AssertionError("Thiếu feature snapshot cho evidence của pipeline.")
                evidence = build_shared_signal_evidence(
                    feature_snapshot=feature,
                    setup_type=f"strategy_card.pipeline.{index}",
                    observed_at=moment,
                    available_at=moment,
                    validity_end=moment + timedelta(minutes=10),
                    sources=(
                        EvidenceSourceRef(
                            f"market_bar:binance:BTCUSDT:1h:pipeline:{index}:{moment.isoformat()}",
                            moment,
                        ),
                    ),
                )
                await store.record_evidence(evidence, evaluated_at=moment + timedelta(seconds=1))
                await app.state.paper_supervisor.tick_once()
                await app.state.paper_supervisor.tick_once()
                for key, scenario in round_scenarios.items():
                    account_id = UUID(_ws()["accounts"][key])
                    async with UnitOfWork(sessions, workspace_id=workspace) as uow:
                        bot_id = await uow.bind_account(account_id)
                        decision = await uow.session.scalar(
                            select(BotDecisionRecord).where(
                                BotDecisionRecord.workspace_id == workspace,
                                BotDecisionRecord.bot_id == bot_id,
                                BotDecisionRecord.evidence_id == evidence.evidence_id,
                            )
                        )
                        intent = (
                            await uow.session.scalar(
                                select(OrderIntentRecord).where(
                                    OrderIntentRecord.workspace_id == workspace,
                                    OrderIntentRecord.account_id == account_id,
                                    OrderIntentRecord.bot_decision_id == decision.id,
                                )
                            )
                            if decision is not None
                            else None
                        )
                        plan = (
                            await uow.session.scalar(
                                select(TradePlanRecord).where(
                                    TradePlanRecord.workspace_id == workspace,
                                    TradePlanRecord.account_id == account_id,
                                    TradePlanRecord.plan_hash == intent.plan_hash,
                                )
                            )
                            if intent is not None
                            else None
                        )
                    record: dict[str, Any] = {
                        "bot": key,
                        "scenario": scenario,
                        "decision_status": decision.status if decision is not None else None,
                        "decision_reason": decision.reason_code if decision is not None else None,
                    }
                    if intent is None and decision is not None and STATE.get("pipeline_debug"):
                        from trading_bot.application.paper_runtime_application import (
                            PaperBotDecisionCommand,
                        )

                        try:
                            direct = await app.state.paper_application.evaluate_and_submit(
                                PaperBotDecisionCommand(
                                    account_id=account_id, bot_decision_id=decision.id
                                )
                            )
                            record["direct_submit"] = {
                                "blocked": direct.entry_block_reason,
                                "submitted": direct.submission is not None,
                                "result": repr(getattr(direct.decision, "risk_decision", None))[
                                    :1500
                                ],
                                "plan": {
                                    k: str(getattr(direct.decision.plan, k, None))
                                    for k in (
                                        "quantity",
                                        "notional",
                                        "initial_margin",
                                        "risk_budget",
                                        "estimated_loss",
                                        "stop_price",
                                        "target_price",
                                        "entry_price",
                                    )
                                },
                            }
                        except Exception as error:
                            record["direct_submit"] = {
                                "error": f"{type(error).__name__}: {error}"[:600],
                                "cause": f"{type(error.__cause__).__name__}: {error.__cause__}"[
                                    :600
                                ],
                            }
                    if intent is None or plan is None:
                        record["error"] = "NO_INTENT"
                        record["supervisor_errors"] = {
                            str(account): entry.last_error_code
                            for account, entry in app.state.paper_registry.entries().items()
                        }
                        trades.append(record)
                        continue
                    payload = plan.payload
                    submitted = {
                        "intent_id": str(intent.id),
                        "paper_intent_id": f"paper:{intent.idempotency_key}",
                        "eligible_at": (moment + timedelta(seconds=1)).isoformat(),
                    }
                    filled = await _pipeline_fill(app, account_id, submitted, moment)
                    record |= {
                        "side": intent.side,
                        "entry_price": payload["entry_price"],
                        "stop_price": payload["stop_price"],
                        "target_price": payload["target_price"],
                        "quantity": payload["quantity"],
                        "leverage": payload["leverage"],
                        "risk_budget": payload["risk_budget"],
                        "trade_id": filled["trade_id"],
                        "fill_status": filled["status"],
                    }
                    if scenario != "OPEN" and filled["trade_id"]:
                        exit_reason = None
                        for bar in _exit_bars(
                            scenario,
                            intent.side,
                            Decimal(payload["stop_price"]),
                            Decimal(payload["target_price"]) if payload["target_price"] else None,
                            datetime.fromisoformat(filled["filled_bar_opened_at"]),
                        ):
                            result = await app.state.paper_application.monitor_position(
                                PaperMonitorCommand(
                                    account_id=account_id,
                                    ledger_intent_id=intent.id,
                                    trade_id=UUID(filled["trade_id"]),
                                    paper_position_id=submitted["paper_intent_id"],
                                    bar=bar,
                                    feature_snapshot_id=_ws()["snapshots"][0],
                                )
                            )
                            if result.trade_closed:
                                exit_reason = result.monitor_result.exit_reason
                                record["closed_at"] = bar.closed_at.isoformat()
                                break
                        record["exit_reason"] = str(exit_reason) if exit_reason else None
                    trades.append(record)
    finally:
        await engine.dispose()
    return {"pipeline_trades": trades}


def pipeline_seed_chart_bars() -> dict[str, Any]:
    """Nến 5 phút và 1 giờ nội bộ quanh các giao dịch để báo cáo và bot detail dựng biểu đồ.

    Chạy lại được nhiều lần: nến đã có (cùng nguồn, khung và mốc mở) được bỏ qua.
    """
    with _connect() as connection, connection.transaction():
        workspace = connection.execute(
            "SELECT id FROM workspace WHERE slug=%s", (STATE["pipeline_workspace_slug"],)
        ).fetchone()[0]
        instrument = connection.execute(
            "SELECT id FROM market_instrument WHERE workspace_id=%s AND symbol='BTCUSDT' LIMIT 1",
            (workspace,),
        ).fetchone()[0]
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        rows = []
        for timeframe, step, count in (
            ("5m", timedelta(minutes=5), 72),
            ("1h", timedelta(hours=1), 48),
        ):
            seconds = int(step.total_seconds())
            aligned = datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, UTC)
            for index in range(count):
                opened = aligned - step * (count - index)
                closed = opened + step
                # Dao động tất định quanh 100 để nến có thân và râu; không phải giá thị trường.
                base = Decimal("100") + Decimal(index % 7 - 3) * Decimal("0.15")
                close = base + Decimal((index * 3) % 5 - 2) * Decimal("0.10")
                key = f"pipeline-chart-{timeframe}-{opened.isoformat()}"
                rows.append(
                    (
                        uuid4(),
                        workspace,
                        instrument,
                        "BTCUSDT",
                        timeframe,
                        "CRYPIDIAN_PIPELINE_FIXTURE",
                        key,
                        key,
                        1,
                        hashlib.sha256(key.encode()).hexdigest(),
                        opened,
                        closed,
                        closed,
                        closed,
                        closed,
                        base,
                        max(base, close) + Decimal("0.35"),
                        min(base, close) - Decimal("0.35"),
                        close,
                        Decimal("12"),
                        True,
                    )
                )
        inserted = 0
        with connection.cursor() as cursor:
            for row in rows:
                cursor.execute(
                    "INSERT INTO market_bar (id, workspace_id, instrument_id, symbol, timeframe, "
                    "source, logical_key, idempotency_key, revision, checksum, open_time, "
                    "close_time, event_at, received_at, available_at, open, high, low, close, "
                    "volume, is_closed) "
                    "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
                    "WHERE NOT EXISTS (SELECT 1 FROM market_bar WHERE workspace_id=%s "
                    "AND source=%s AND idempotency_key=%s)",
                    (*row, row[1], row[5], row[7]),
                )
                inserted += cursor.rowcount
    return {"pipeline_chart_bars": inserted}


def pipeline_login_lookup() -> dict[str, Any]:
    """(migrator) Tìm membership ở workspace đích và factor TOTP đã xác minh của cùng tài khoản."""
    with _connect() as connection:
        target = connection.execute(
            "SELECT w.id, m.id FROM workspace w JOIN user_membership m ON m.workspace_id=w.id "
            "JOIN user_account u ON u.id=m.user_id WHERE w.slug=%s AND u.email_normalized=%s",
            (STATE["pipeline_workspace_slug"], STATE["pipeline_login_email"]),
        ).fetchone()
        if target is None:
            raise AssertionError("Tài khoản chưa là thành viên của workspace đích.")
        existing = connection.execute(
            "SELECT 1 FROM mfa_factor WHERE workspace_id=%s AND membership_id=%s", target
        ).fetchone()
        source = connection.execute(
            "SELECT f.workspace_id, f.secret_reference FROM mfa_factor f "
            "JOIN user_membership m ON m.id=f.membership_id "
            "JOIN user_account u ON u.id=m.user_id "
            "WHERE u.email_normalized=%s AND f.factor_type='TOTP' AND f.verified_at IS NOT NULL "
            "AND f.workspace_id<>%s ORDER BY f.created_at LIMIT 1",
            (STATE["pipeline_login_email"], target[0]),
        ).fetchone()
    if source is None and existing is None:
        raise AssertionError("Tài khoản chưa có factor TOTP đã xác minh để dùng lại.")
    return {
        "pipeline_login_target_workspace": str(target[0]),
        "pipeline_login_membership": str(target[1]),
        "pipeline_login_has_factor": existing is not None,
        "pipeline_login_source_workspace": str(source[0]) if source else None,
        "pipeline_login_source_reference": source[1] if source else None,
    }


def pipeline_login_rewrap() -> dict[str, Any]:
    """(admin-web) Mã hóa lại cùng khóa TOTP cho workspace đích; khóa không rời container."""
    from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider

    if STATE["pipeline_login_has_factor"]:
        return {"pipeline_login_new_reference": None}
    provider = LocalMfaSecretProvider(
        key_file=Path(os.environ["TRADING_BOT_MFA_ENCRYPTION_KEY_FILE"]),
        storage_directory=Path(os.environ["TRADING_BOT_MFA_SECRET_DIRECTORY"]),
    )
    secret = provider.resolve_mfa_secret(
        UUID(STATE["pipeline_login_source_workspace"]), STATE["pipeline_login_source_reference"]
    )
    reference = provider.store_mfa_secret(UUID(STATE["pipeline_login_target_workspace"]), secret)
    return {"pipeline_login_new_reference": reference}


def pipeline_login_attach() -> dict[str, Any]:
    """(migrator) Ghi factor TOTP đã xác minh cho membership ở workspace đích."""
    reference = STATE.get("pipeline_login_new_reference")
    if reference is None:
        return {"pipeline_login_attached": False}
    with _connect() as connection, connection.transaction():
        _insert(
            connection,
            "mfa_factor",
            id=uuid4(),
            workspace_id=UUID(STATE["pipeline_login_target_workspace"]),
            membership_id=UUID(STATE["pipeline_login_membership"]),
            factor_type="TOTP",
            secret_reference=reference,
            verified_at=datetime.now(UTC),
        )
    return {"pipeline_login_attached": True}


PHASES["pipeline_login_lookup"] = pipeline_login_lookup
PHASES["pipeline_login_rewrap"] = pipeline_login_rewrap
PHASES["pipeline_login_attach"] = pipeline_login_attach
PHASES["pipeline_seed_setup"] = pipeline_seed_setup
PHASES["pipeline_seed_trades"] = pipeline_seed_trades
PHASES["pipeline_seed_chart_bars"] = pipeline_seed_chart_bars
