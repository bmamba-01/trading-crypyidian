"""MB05.7: guard hoạt động account — incident, chặn entry, không adopt/đóng, resolution có quyền."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.paper.account_activity import ScriptedAccountActivityAdapter
from trading_bot.application.account_activity_guard import (
    AccountActivityGuard,
    AccountActivityResolutionService,
    ActivityResolutionError,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.domain.execution.runtime_control import FencingToken, RuntimeFence
from trading_bot.ports.account_activity import (
    AccountActivitySnapshot,
    ActivityVerdict,
    ObservedOrder,
    ObservedPosition,
)

NOW = datetime(2026, 10, 26, 11, 0, tzinfo=UTC)
WORKSPACE = uuid4()


class Db:
    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []
        self.incidents: list[SimpleNamespace] = []
        self.audits: list[dict[str, Any]] = []
        self.ledger_writes: list[object] = []
        self.resolved: list[dict[str, Any]] = []


class Uow:
    def __init__(self, db: Db, _sessions: object, **kwargs: Any) -> None:
        self.db = db
        self.workspace_id = kwargs["workspace_id"]
        self.bot_id = kwargs.get("bot_id")
        self.session = SimpleNamespace(db=db)
        self.pending: list[dict[str, Any]] = []

    async def __aenter__(self) -> Uow:
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            self.db.audits.extend(self.pending)

    async def bind_account(self, account_id: UUID, **_: object) -> UUID:
        return self.bot_id

    def record_audit(self, **values: Any) -> None:
        self.pending.append(values)


class States:
    def __init__(self, session: Any) -> None:
        self.db: Db = session.db

    async def get(self, *, workspace_id: UUID, bot_id: UUID) -> Any:
        rows = [s for s in self.db.states if s["bot_id"] == bot_id]
        return SimpleNamespace(**rows[-1]) if rows else None

    async def upsert(self, **values: Any) -> None:
        assert isinstance(values["fence"], RuntimeFence)
        self.db.states.append(values)


class Incidents:
    def __init__(self, session: Any) -> None:
        self.db: Db = session.db

    async def list_open(self, *, workspace_id: UUID, bot_id: UUID) -> list[SimpleNamespace]:
        return [i for i in self.db.incidents if i.bot_id == bot_id and i.status == "OPEN"]

    async def open_incident(self, *, fence: RuntimeFence, classification: Any, **kwargs: Any):
        for incident in self.db.incidents:
            if (
                incident.bot_id == kwargs["bot_id"]
                and incident.fingerprint == classification.fingerprint
                and incident.status == "OPEN"
            ):
                return incident, False
        incident = SimpleNamespace(
            id=uuid4(), status="OPEN", fingerprint=classification.fingerprint, **kwargs
        )
        self.db.incidents.append(incident)
        return incident, True

    async def resolve(self, **kwargs: Any) -> SimpleNamespace:
        incident = next(i for i in self.db.incidents if i.id == kwargs["incident_id"])
        incident.status = "RESOLVED"
        self.db.resolved.append(kwargs)
        return incident


def _account(bot_id: UUID | None = None) -> SimpleNamespace:
    gateway = SimpleNamespace(open_orders=lambda: (), open_positions=lambda: ())
    return SimpleNamespace(
        account_id=uuid4(),
        bot_id=bot_id or uuid4(),
        gateway=gateway,
        fence=RuntimeFence("runtime-1", FencingToken(3)),
    )


def _snapshot(account_id: UUID, **kwargs: Any) -> AccountActivitySnapshot:
    return AccountActivitySnapshot(
        workspace_id=WORKSPACE,
        account_id=account_id,
        source="SCRIPTED",
        cursor="c-1",
        observed_at=kwargs.pop("observed_at", NOW),
        complete=kwargs.pop("complete", True),
        **kwargs,
    )


def _guard(db: Db, accounts: dict[UUID, Any], adapter: Any) -> AccountActivityGuard:
    return AccountActivityGuard(
        sessions=object(),  # type: ignore[arg-type]
        workspace_id=WORKSPACE,
        accounts=accounts.get,
        adapter=adapter,
        clock=lambda: NOW,
        unit_of_work_factory=lambda sessions, **kw: Uow(db, sessions, **kw),
        state_repository_factory=States,
        incident_repository_factory=Incidents,
    )


FOREIGN_ORDER = ObservedOrder("manual-123", "BTCUSDT", "LONG", Decimal("0.5"), "NEW")
FOREIGN_POSITION = ObservedPosition("ETHUSDT", "SHORT", Decimal("2"))


def test_known_state_passes_and_marks_activity_verified() -> None:
    db, account = Db(), _account()
    adapter = ScriptedAccountActivityAdapter()
    adapter.script(_snapshot(account.account_id))

    result = asyncio.run(
        _guard(db, {account.account_id: account}, adapter).inspect_once(account.account_id)
    )

    assert result.verdict is ActivityVerdict.CLEAN
    assert db.states[-1]["activity_status"].value == "VERIFIED"
    assert db.states[-1]["reason_code"] is None
    assert db.incidents == []


@pytest.mark.parametrize(
    "foreign", [{"orders": (FOREIGN_ORDER,)}, {"positions": (FOREIGN_POSITION,)}]
)
def test_foreign_order_or_position_opens_incident_blocks_and_audits_without_adopting(
    foreign: dict[str, Any],
) -> None:
    db, account, sibling = Db(), _account(), _account()
    adapter = ScriptedAccountActivityAdapter()
    adapter.script(_snapshot(account.account_id, **foreign))
    adapter.script(_snapshot(sibling.account_id))
    guard = _guard(db, {account.account_id: account, sibling.account_id: sibling}, adapter)

    first = asyncio.run(guard.inspect_once(account.account_id))
    repeat = asyncio.run(guard.inspect_once(account.account_id))
    sibling_result = asyncio.run(guard.inspect_once(sibling.account_id))

    assert first.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED
    assert repeat.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED
    assert len(db.incidents) == 1  # cùng fingerprint không mở incident trùng
    assert db.incidents[0].source == "SCRIPTED"
    account_states = [s for s in db.states if s["bot_id"] == account.bot_id]
    assert account_states[-1]["reason_code"] == "EXTERNAL_ACTIVITY_UNRECONCILED"
    assert [a["action"] for a in db.audits] == ["bot.activity.external_detected"]
    assert sibling_result.verdict is ActivityVerdict.CLEAN
    sibling_states = [s for s in db.states if s["bot_id"] == sibling.bot_id]
    assert sibling_states[-1]["reason_code"] is None
    # Guard không có đường ghi ledger/hủy/đóng state ngoại lai.
    assert not hasattr(guard, "close_position")
    assert not hasattr(guard, "cancel_order")
    assert db.ledger_writes == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda adapter, account: adapter.fail_with(account, "VENUE_TIMEOUT"), "VENUE_TIMEOUT"),
        (
            lambda adapter, account: adapter.script(_snapshot(account, complete=False)),
            "ACTIVITY_SNAPSHOT_INCOMPLETE",
        ),
        (
            lambda adapter, account: adapter.script(
                _snapshot(account, observed_at=NOW - timedelta(hours=1))
            ),
            "ACTIVITY_SNAPSHOT_STALE",
        ),
    ],
)
def test_adapter_failure_or_unusable_snapshot_is_unverified_without_incident(
    setup: Any, code: str
) -> None:
    db, account = Db(), _account()
    adapter = ScriptedAccountActivityAdapter()
    setup(adapter, account.account_id)

    result = asyncio.run(
        _guard(db, {account.account_id: account}, adapter).inspect_once(account.account_id)
    )

    assert result.verdict is ActivityVerdict.ACCOUNT_ACTIVITY_UNVERIFIED
    assert result.reason_code == code
    assert db.incidents == []
    assert db.states[-1]["activity_status"].value == "UNVERIFIED"
    assert db.states[-1]["reason_code"] == "ACCOUNT_ACTIVITY_UNVERIFIED"


def test_snapshot_taken_after_guard_started_is_not_future() -> None:
    """Hồi quy E2E: adapter PAPER đóng dấu `observed_at` lúc chụp; guard phải đọc đồng hồ sau."""
    from trading_bot.adapters.paper.account_activity import PaperLedgerAccountActivityAdapter

    db, account = Db(), _account()
    ticks = iter(NOW + timedelta(milliseconds=step) for step in range(10))
    clock = lambda: next(ticks)  # noqa: E731
    adapter = PaperLedgerAccountActivityAdapter(
        workspace_id=WORKSPACE, gateways={account.account_id: account.gateway}, clock=clock
    )
    guard = _guard(db, {account.account_id: account}, adapter)
    guard._clock = clock

    result = asyncio.run(guard.inspect_once(account.account_id))

    assert result.verdict is ActivityVerdict.CLEAN


def test_known_bot_position_is_not_foreign() -> None:
    db = Db()
    position = SimpleNamespace(symbol="BTCUSDT", direction="long", quantity=Decimal("1"))
    order = SimpleNamespace(
        intent_id="paper:key-1",
        symbol="BTCUSDT",
        direction="long",
        requested_quantity=Decimal("1"),
        status="PARTIALLY_FILLED",
    )
    account = _account()
    account.gateway = SimpleNamespace(
        open_orders=lambda: (order,), open_positions=lambda: (position,)
    )
    adapter = ScriptedAccountActivityAdapter()
    adapter.script(
        _snapshot(
            account.account_id,
            orders=(ObservedOrder("paper:key-1", "BTCUSDT", "LONG", Decimal("1"), "PARTIAL"),),
            positions=(ObservedPosition("BTCUSDT", "LONG", Decimal("1")),),
        )
    )

    result = asyncio.run(
        _guard(db, {account.account_id: account}, adapter).inspect_once(account.account_id)
    )

    assert result.verdict is ActivityVerdict.CLEAN


def test_after_resolution_entry_stays_blocked_until_a_new_clean_inspect() -> None:
    db, account = Db(), _account()
    adapter = ScriptedAccountActivityAdapter()
    adapter.script(_snapshot(account.account_id, orders=(FOREIGN_ORDER,)))
    guard = _guard(db, {account.account_id: account}, adapter)
    asyncio.run(guard.inspect_once(account.account_id))
    db.incidents[0].status = "RESOLVED"  # resolution do admin-web ghi

    # State vẫn mang reason EXTERNAL cho tới khi runtime inspect sạch.
    assert db.states[-1]["reason_code"] == "EXTERNAL_ACTIVITY_UNRECONCILED"
    adapter.script(_snapshot(account.account_id))
    clean = asyncio.run(guard.inspect_once(account.account_id))

    assert clean.verdict is ActivityVerdict.CLEAN
    assert db.states[-1]["reason_code"] is None


def test_open_incident_keeps_bot_blocked_even_if_snapshot_turns_clean() -> None:
    db, account = Db(), _account()
    adapter = ScriptedAccountActivityAdapter()
    adapter.script(_snapshot(account.account_id, orders=(FOREIGN_ORDER,)))
    guard = _guard(db, {account.account_id: account}, adapter)
    asyncio.run(guard.inspect_once(account.account_id))
    adapter.script(_snapshot(account.account_id))

    result = asyncio.run(guard.inspect_once(account.account_id))

    assert result.verdict is ActivityVerdict.EXTERNAL_ACTIVITY_UNRECONCILED
    assert db.states[-1]["reason_code"] == "EXTERNAL_ACTIVITY_UNRECONCILED"


# --- resolution ---------------------------------------------------------------------------


def _principal(*roles: Role, mfa_age: timedelta = timedelta(minutes=1)) -> Principal:
    return Principal(uuid4(), uuid4(), WORKSPACE, frozenset(roles), NOW - mfa_age)


def _resolution(db: Db, visible: bool = True) -> AccountActivityResolutionService:
    async def visibility(_session: object, **_: object) -> bool:
        return visible

    return AccountActivityResolutionService(
        object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        unit_of_work_factory=lambda sessions, **kw: Uow(db, sessions, **kw),
        incident_repository_factory=Incidents,
        bot_visibility=visibility,
    )


def _open_incident(db: Db) -> SimpleNamespace:
    incident = SimpleNamespace(id=uuid4(), bot_id=uuid4(), status="OPEN", fingerprint="f" * 64)
    db.incidents.append(incident)
    return incident


@pytest.mark.parametrize("role", [Role.OWNER, Role.RISK_ADMIN])
def test_owner_or_risk_admin_resolves_with_recent_mfa_reason_and_allowed_code(role: Role) -> None:
    db = Db()
    incident = _open_incident(db)
    principal = _principal(role)

    asyncio.run(
        _resolution(db).resolve_incident(
            principal,
            bot_id=incident.bot_id,
            incident_id=incident.id,
            resolution_code="EXTERNAL_STATE_REMOVED_BY_OPERATOR",
            reason="Đã đóng lệnh tay trên sàn",
        )
    )

    assert incident.status == "RESOLVED"
    assert db.resolved[0]["resolved_by"] == principal.membership_id
    assert db.audits[-1]["action"] == "bot.activity.resolved"
    assert db.audits[-1]["reason"] == "Đã đóng lệnh tay trên sàn"


@pytest.mark.parametrize(
    ("principal", "code", "reason", "resolution"),
    [
        (_principal(Role.OPERATOR), "CONTROL_PERMISSION_DENIED", "r", "FALSE_POSITIVE_CONFIRMED"),
        (
            _principal(Role.STRATEGY_ADMIN),
            "CONTROL_PERMISSION_DENIED",
            "r",
            "FALSE_POSITIVE_CONFIRMED",
        ),
        (
            _principal(Role.OWNER, mfa_age=timedelta(hours=2)),
            "RECENT_MFA_REQUIRED",
            "r",
            "FALSE_POSITIVE_CONFIRMED",
        ),
        (
            _principal(Role.OWNER),
            "INCIDENT_RESOLUTION_REASON_REQUIRED",
            " ",
            "FALSE_POSITIVE_CONFIRMED",
        ),
        (_principal(Role.OWNER), "INCIDENT_RESOLUTION_CODE_INVALID", "r", "ADOPT_FOREIGN_STATE"),
    ],
)
def test_resolution_rejections_are_audited_and_change_nothing(
    principal: Principal, code: str, reason: str, resolution: str
) -> None:
    db = Db()
    incident = _open_incident(db)

    with pytest.raises(ActivityResolutionError) as error:
        asyncio.run(
            _resolution(db).resolve_incident(
                principal,
                bot_id=incident.bot_id,
                incident_id=incident.id,
                resolution_code=resolution,
                reason=reason,
            )
        )

    assert error.value.reason_code == code
    assert incident.status == "OPEN"
    assert db.audits[-1]["action"] == "bot.runtime.control_denied"


def test_resolution_of_invisible_bot_is_not_found() -> None:
    db = Db()
    incident = _open_incident(db)

    with pytest.raises(ActivityResolutionError) as error:
        asyncio.run(
            _resolution(db, visible=False).resolve_incident(
                _principal(Role.OWNER),
                bot_id=incident.bot_id,
                incident_id=incident.id,
                resolution_code="FALSE_POSITIVE_CONFIRMED",
                reason="r",
            )
        )
    assert error.value.reason_code == "BOT_NOT_FOUND"


def test_venue_adapter_factory_stays_disabled() -> None:
    from trading_bot.adapters.paper.account_activity import venue_account_activity_adapter
    from trading_bot.ports.account_activity import AccountActivityError

    with pytest.raises(AccountActivityError, match="VENUE_ACTIVITY_ADAPTER_DISABLED"):
        venue_account_activity_adapter()
