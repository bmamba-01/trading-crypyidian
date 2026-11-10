"""WP-07C: quyền backtest research và PAPER demo tách riêng trong source registry."""

from collections.abc import Sequence

from alembic import op

revision: str = "a2c4e6f8b0d2"
down_revision: str | None = "f8a2c4e6d0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_PURPOSES = '["metadata","display","embedding","training","redistribution",'
_JSON_PURPOSES += '"backtest_research","paper_demo"]'
_SQL_PURPOSES = (
    "'metadata', 'display', 'embedding', 'training', 'redistribution', "
    "'backtest_research', 'paper_demo'"
)
_POLICY_CHECK = "ck_source_policy_revision_source_policy_purposes_valid"
_QUOTA_CHECK = "ck_source_quota_event_source_quota_purpose_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(_POLICY_CHECK), "source_policy_revision", type_="check")
    op.create_check_constraint(
        op.f(_POLICY_CHECK),
        "source_policy_revision",
        "jsonb_typeof(allowed_purposes) = 'array' "
        f"AND allowed_purposes <@ '{_JSON_PURPOSES}'::jsonb",
    )
    op.drop_constraint(op.f(_QUOTA_CHECK), "source_quota_event", type_="check")
    op.create_check_constraint(
        op.f(_QUOTA_CHECK),
        "source_quota_event",
        f"purpose IN ({_SQL_PURPOSES})",
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM public.source_policy_revision
                WHERE allowed_purposes ?| ARRAY['backtest_research', 'paper_demo']
            ) OR EXISTS (
                SELECT 1 FROM public.source_quota_event
                WHERE purpose IN ('backtest_research', 'paper_demo')
            ) THEN
                RAISE EXCEPTION 'WP07_BACKFILL_PURPOSE_DOWNGRADE_BLOCKED';
            END IF;
        END $$"""
    )
    op.drop_constraint(op.f(_POLICY_CHECK), "source_policy_revision", type_="check")
    op.create_check_constraint(
        op.f(_POLICY_CHECK),
        "source_policy_revision",
        "jsonb_typeof(allowed_purposes) = 'array' "
        'AND allowed_purposes <@ \'["metadata","display","embedding",'
        '"training","redistribution"]\'::jsonb',
    )
    op.drop_constraint(op.f(_QUOTA_CHECK), "source_quota_event", type_="check")
    op.create_check_constraint(
        op.f(_QUOTA_CHECK),
        "source_quota_event",
        "purpose IN ('metadata', 'display', 'embedding', 'training', 'redistribution')",
    )
