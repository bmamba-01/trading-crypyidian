"""Persist risk reservation expiry and protect it from mutation.

Revision ID: 5b8c3f4a1d92
Revises: 9a7c2d5e1b40
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8c3f4a1d92"
down_revision: str | None = "9a7c2d5e1b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_reservation",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE risk_reservation AS reservation
        SET expires_at = (decision.payload ->> 'candidate_expires_at')::timestamptz
        FROM risk_decision AS decision
        WHERE decision.workspace_id = reservation.workspace_id
          AND decision.account_id = reservation.account_id
          AND decision.id = reservation.risk_decision_id
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM risk_reservation WHERE expires_at IS NULL) THEN
                RAISE EXCEPTION
                    'cannot migrate risk reservation: decision expiry is missing or invalid';
            END IF;
        END; $$
        """
    )
    op.alter_column("risk_reservation", "expires_at", nullable=False)
    op.create_check_constraint(
        "risk_reservation_expiry_after_creation",
        "risk_reservation",
        "expires_at > created_at",
    )
    op.execute(
        """
        CREATE FUNCTION reject_risk_reservation_expiry_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
                RAISE EXCEPTION 'risk reservation expiry is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER risk_reservation_expiry_immutable "
        "BEFORE UPDATE ON risk_reservation "
        "FOR EACH ROW EXECUTE FUNCTION reject_risk_reservation_expiry_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER risk_reservation_expiry_immutable ON risk_reservation")
    op.execute("DROP FUNCTION reject_risk_reservation_expiry_mutation()")
    op.drop_constraint(
        "risk_reservation_expiry_after_creation",
        "risk_reservation",
        type_="check",
    )
    op.drop_column("risk_reservation", "expires_at")
