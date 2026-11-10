# ruff: noqa: F821
"""Probe acceptance ledger cho stale preview, idempotency và phục hồi runtime flatten."""


def mb07b_flatten_stale_preview() -> dict[str, Any]:
    """Tạo preview trước khi migrator ghi mark mới."""
    with _admin_client() as client:
        response = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/preview",
            {"reason": "MB-07b stale/recovery E2E"},
        )
    assert response["status"] == 200, response
    return {"preview": response["body"]["preview"]}


def mb07b_flatten_stale_and_queue() -> dict[str, Any]:
    """Từ chối snapshot cũ, rồi tạo operation bền vững và phát lại confirm."""
    with _admin_client() as client:
        reason = "MB-07b stale/recovery E2E"
        stale_preview = STATE["mb07b_lifecycle_stale_preview"]
        stale_confirm = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/confirm",
            {
                "preview_id": stale_preview["id"],
                "preview_hash": stale_preview["preview_hash"],
                "idempotency_key": uuid4().hex,
                "reason": reason,
            },
        )
        assert stale_confirm["status"] == 409, stale_confirm
        assert stale_confirm["code"] == "FLATTEN_PREVIEW_STALE", stale_confirm

        fresh_response = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/preview",
            {"reason": reason},
        )
        assert fresh_response["status"] == 200, fresh_response
        fresh_preview = fresh_response["body"]["preview"]
        key = uuid4().hex
        confirm_body = {
            "preview_id": fresh_preview["id"],
            "preview_hash": fresh_preview["preview_hash"],
            "idempotency_key": key,
            "reason": reason,
        }
        confirmed = _call(
            client, "owner", "POST", "/api/v1/workspace/flatten/confirm", confirm_body
        )
        replayed = _call(client, "owner", "POST", "/api/v1/workspace/flatten/confirm", confirm_body)
        key_reused = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/confirm",
            confirm_body | {"reason": reason + " changed"},
        )
        assert confirmed["status"] == 200, confirmed
        assert confirmed["body"]["operation"]["status"] == "QUEUED", confirmed
        assert confirmed["body"]["operation"]["created"] is True, confirmed
        assert replayed["status"] == 200, replayed
        assert replayed["body"]["operation"]["id"] == confirmed["body"]["operation"]["id"]
        assert replayed["body"]["operation"]["created"] is False, replayed
        assert key_reused["status"] == 409, key_reused
        assert key_reused["code"] == "FLATTEN_IDEMPOTENCY_KEY_REUSED", key_reused
        return {
            "stale_code": stale_confirm["code"],
            "preview": fresh_preview,
            "operation": confirmed["body"]["operation"],
            "replay": replayed["body"]["operation"],
            "key_reuse_code": key_reused["code"],
        }


def mb07b_flatten_lifecycle_inspect() -> dict[str, Any]:
    """Đọc trạng thái qua migrator để khẳng định ledger sau phục hồi runtime."""
    operation_id = UUID(STATE["mb07b_lifecycle_operation_id"])
    workspace = _ws("main")
    with _connect() as connection:
        operation = connection.execute(
            "SELECT status FROM flatten_operation WHERE workspace_id=%s AND id=%s",
            (workspace["workspace"], operation_id),
        ).fetchone()
        items = connection.execute(
            "SELECT status, attempt_count, result, last_error_code "
            "FROM flatten_operation_item WHERE workspace_id=%s AND operation_id=%s",
            (workspace["workspace"], operation_id),
        ).fetchall()
        exits = connection.execute(
            "SELECT count(*) FROM fill WHERE workspace_id=%s AND account_id=%s "
            "AND fill_role='EXIT'",
            (workspace["workspace"], workspace["accounts"]["a"]),
        ).fetchone()[0]
    return {"status": operation[0], "items": [list(row) for row in items], "exit_fills": exits}


def mb07b_flatten_lifecycle_wait() -> dict[str, Any]:
    """Chờ API owner đọc trạng thái operation do worker khởi động lại xử lý."""
    import time

    operation_id = STATE["mb07b_lifecycle_operation_id"]
    deadline = time.monotonic() + 60
    last: dict[str, Any] = {}
    with _admin_client() as client:
        while time.monotonic() < deadline:
            response = _call(
                client,
                "owner",
                "GET",
                f"/api/v1/workspace/flatten/operations/{operation_id}",
            )
            assert response["status"] == 200, response
            last = response["body"]["operation"]
            if last["status"] in {"COMPLETED", "BLOCKED", "PARTIAL"}:
                return last
            time.sleep(0.5)
    return last


def mb07b_flatten_duplicate_stage() -> dict[str, Any]:
    """Owner tạo operation QUEUED qua admin-web role trước lúc runtime worker nhận lease."""
    with _admin_client() as client:
        reason = "MB-07b duplicate worker E2E"
        preview_response = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/preview",
            {"reason": reason},
        )
        assert preview_response["status"] == 200, preview_response
        preview = preview_response["body"]["preview"]
        confirm = _call(
            client,
            "owner",
            "POST",
            "/api/v1/workspace/flatten/confirm",
            {
                "preview_id": preview["id"],
                "preview_hash": preview["preview_hash"],
                "idempotency_key": uuid4().hex,
                "reason": reason,
            },
        )
        assert confirm["status"] == 200, confirm
        assert confirm["body"]["operation"]["status"] == "QUEUED", confirm
        return {
            "preview": preview,
            "operation": confirm["body"]["operation"],
        }


async def mb07b_flatten_duplicate_claim() -> dict[str, Any]:
    """Gọi hai claim worker đồng thời dưới fence rồi xác minh lần khởi động sau."""
    import asyncio

    from trading_bot.application.bot_runtime_supervisor import BotRuntimeSupervisor

    class ManualSupervisor(BotRuntimeSupervisor):
        async def tick_once(self) -> None:
            # Giữ lease thật nhưng để probe điều phối worker đồng thời một cách tất định.
            return None

        async def run(self) -> None:
            await self._stopping.wait()

    workspace = _ws("main")
    account_id = UUID(workspace["accounts"]["a"])
    operation_id = STATE["mb07b_duplicate_operation_id"]

    async with runtime_app(tick=20, supervisor_factory=ManualSupervisor) as app:
        application = app.state.paper_application
        account = application.runtime_account(account_id)
        assert account is not None
        control = await application.apply_runtime_control(account_id)
        assert control.bot_paused, control
        worker_results = await asyncio.gather(
            application.process_flatten_operations(account_id),
            application.process_flatten_operations(account_id),
        )
        first_operation = await _flatten_worker_db_state(operation_id, account_id)

    async with runtime_app(tick=20, supervisor_factory=ManualSupervisor) as app:
        application = app.state.paper_application
        assert application.runtime_account(account_id) is not None
        after_restart_result = await application.process_flatten_operations(account_id)
        restarted_operation = await _flatten_worker_db_state(operation_id, account_id)

    return {
        "operation_id": operation_id,
        "worker_results": worker_results,
        "first": first_operation,
        "after_restart_result": after_restart_result,
        "after_restart": restarted_operation,
    }


async def mb07b_flatten_single_position_setup() -> dict[str, Any]:
    """Tạo đúng một vị thế đang mở, không để lệnh entry dư làm nhiễu claim test."""
    async with runtime_app(tick=20) as app:
        entry_result = await entry(
            app,
            "a",
            0,
            1,
            at=datetime.now(UTC) - timedelta(minutes=1),
        )
        entry_fill = await fill(app, "a", entry_result)
    return {"single_position_entry": entry_result, "single_position_fill": entry_fill}


async def _flatten_worker_db_state(operation_id: str, account_id: UUID) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from trading_bot.adapters.database.models.flatten_entities import (
        FlattenOperationItemRecord,
        FlattenOperationRecord,
    )
    from trading_bot.adapters.database.models.paper_entities import FillRecord
    from trading_bot.application.outbox import UnitOfWork

    engine = _engine(pool=4)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with UnitOfWork(
            sessions,
            workspace_id=UUID(_ws("main")["workspace"]),
            bot_id=UUID(_ws("main")["bots"]["a"]),
        ) as uow:
            operation = await uow.session.scalar(
                select(FlattenOperationRecord).where(
                    FlattenOperationRecord.workspace_id == UUID(_ws("main")["workspace"]),
                    FlattenOperationRecord.id == UUID(operation_id),
                )
            )
            items = list(
                (
                    await uow.session.scalars(
                        select(FlattenOperationItemRecord).where(
                            FlattenOperationItemRecord.workspace_id
                            == UUID(_ws("main")["workspace"]),
                            FlattenOperationItemRecord.operation_id == UUID(operation_id),
                        )
                    )
                ).all()
            )
            exits = await uow.session.scalar(
                select(func.count(FillRecord.id)).where(
                    FillRecord.workspace_id == UUID(_ws("main")["workspace"]),
                    FillRecord.account_id == account_id,
                    FillRecord.fill_role == "EXIT",
                )
            )
            return {
                "status": operation.status if operation else None,
                "items": [(item.status, item.attempt_count) for item in items],
                "exits": int(exits or 0),
            }
    finally:
        await engine.dispose()


PHASES["mb07b_flatten_stale_preview"] = mb07b_flatten_stale_preview
PHASES["mb07b_flatten_stale_and_queue"] = mb07b_flatten_stale_and_queue
PHASES["mb07b_flatten_lifecycle_inspect"] = mb07b_flatten_lifecycle_inspect
PHASES["mb07b_flatten_lifecycle_wait"] = mb07b_flatten_lifecycle_wait
PHASES["mb07b_flatten_duplicate_claim"] = mb07b_flatten_duplicate_claim
PHASES["mb07b_flatten_duplicate_stage"] = mb07b_flatten_duplicate_stage
PHASES["mb07b_flatten_single_position_setup"] = mb07b_flatten_single_position_setup
