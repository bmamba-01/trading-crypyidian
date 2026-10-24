"""Add explicit no-fill terminal reservation outcomes.

Revision ID: b2e7a9d4c631
Revises: a1f4c9d2e6b8
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2e7a9d4c631"
down_revision: str | None = "a1f4c9d2e6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "risk_reservation_resolution_reason_valid", "risk_reservation", type_="check"
    )
    op.drop_constraint(
        "risk_reservation_status_reason_consistent", "risk_reservation", type_="check"
    )
    op.create_check_constraint(
        "risk_reservation_resolution_reason_valid",
        "risk_reservation",
        "resolution_reason IS NULL OR resolution_reason IN "
        "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', 'EXPIRED_NO_FILL', "
        "'CANCELLED_NO_FILL', 'ORDER_ABSENT', 'OPEN_RISK_RECORDED')",
    )
    op.create_check_constraint(
        "risk_reservation_status_reason_consistent",
        "risk_reservation",
        "(status IN ('RESERVED', 'UNKNOWN') AND resolution_reason IS NULL) OR "
        "(status = 'RELEASED' AND resolution_reason IN "
        "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', 'EXPIRED_NO_FILL', "
        "'CANCELLED_NO_FILL', 'ORDER_ABSENT')) OR "
        "(status = 'TRANSFERRED' AND resolution_reason = 'OPEN_RISK_RECORDED')",
    )
    op.execute("DROP TRIGGER risk_reservation_transition_guard ON risk_reservation")
    op.execute("DROP FUNCTION protect_risk_reservation_transition()")
    op.execute(_transition_guard(allow_open_transfer_from_reserved=True))
    op.execute(
        "CREATE TRIGGER risk_reservation_transition_guard BEFORE UPDATE OR DELETE "
        "ON risk_reservation FOR EACH ROW "
        "EXECUTE FUNCTION protect_risk_reservation_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER risk_reservation_transition_guard ON risk_reservation")
    op.execute("DROP FUNCTION protect_risk_reservation_transition()")
    op.execute(_transition_guard(allow_open_transfer_from_reserved=False))
    op.execute(
        "CREATE TRIGGER risk_reservation_transition_guard BEFORE UPDATE OR DELETE "
        "ON risk_reservation FOR EACH ROW "
        "EXECUTE FUNCTION protect_risk_reservation_transition()"
    )
    op.drop_constraint(
        "risk_reservation_status_reason_consistent", "risk_reservation", type_="check"
    )
    op.drop_constraint(
        "risk_reservation_resolution_reason_valid", "risk_reservation", type_="check"
    )
    op.create_check_constraint(
        "risk_reservation_resolution_reason_valid",
        "risk_reservation",
        "resolution_reason IS NULL OR resolution_reason IN "
        "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', "
        "'ORDER_ABSENT', 'OPEN_RISK_RECORDED')",
    )
    op.create_check_constraint(
        "risk_reservation_status_reason_consistent",
        "risk_reservation",
        "(status IN ('RESERVED', 'UNKNOWN') AND resolution_reason IS NULL) OR "
        "(status = 'RELEASED' AND resolution_reason IN "
        "('EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', 'ORDER_ABSENT')) OR "
        "(status = 'TRANSFERRED' AND resolution_reason = 'OPEN_RISK_RECORDED')",
    )


def _transition_guard(*, allow_open_transfer_from_reserved: bool) -> str:
    """Tạo trigger giữ bất biến reservation và cho phép đúng trạng thái mới."""
    reserved_transfer = (
        "IF OLD.status = 'RESERVED' AND NEW.status = 'TRANSFERRED' "
        "AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN RETURN NEW; END IF;"
        if allow_open_transfer_from_reserved
        else ""
    )
    terminal_reasons = (
        "'EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT', 'EXPIRED_NO_FILL', 'CANCELLED_NO_FILL'"
        if allow_open_transfer_from_reserved
        else "'EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT'"
    )
    return f"""
        CREATE FUNCTION protect_risk_reservation_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'risk reservation cannot be deleted';
            END IF;
            IF (OLD.id, OLD.workspace_id, OLD.account_id, OLD.idempotency_key, OLD.plan_hash,
                OLD.risk_decision_id, OLD.symbol, OLD.reserved_risk, OLD.reserved_notional,
                OLD.reserved_initial_margin, OLD.created_at)
               IS DISTINCT FROM
               (NEW.id, NEW.workspace_id, NEW.account_id, NEW.idempotency_key, NEW.plan_hash,
                NEW.risk_decision_id, NEW.symbol, NEW.reserved_risk, NEW.reserved_notional,
                NEW.reserved_initial_margin, NEW.created_at)
            THEN RAISE EXCEPTION 'risk reservation identity and budget are immutable'; END IF;
            IF OLD.status = NEW.status THEN
                IF OLD.resolution_reason IS DISTINCT FROM NEW.resolution_reason THEN
                    RAISE EXCEPTION 'risk reservation resolution reason is immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'UNKNOWN'
               AND NEW.resolution_reason IS NULL THEN RETURN NEW; END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason IN ({terminal_reasons}) THEN RETURN NEW; END IF;
            {reserved_transfer}
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason = 'ORDER_ABSENT' THEN RETURN NEW; END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'invalid risk reservation status transition';
        END;
        $$
    """
