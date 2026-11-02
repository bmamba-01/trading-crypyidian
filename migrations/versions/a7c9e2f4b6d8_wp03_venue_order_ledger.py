"""WP-03: sổ lệnh venue Demo có transition audit bền vững."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e2f4b6d8"
down_revision: str | None = "c6e8a0b2d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN = "crypidian_admin_web"
RUNTIME = "crypidian_trading_runtime"
BOT_SCOPE = (
    "workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid "
    "AND bot_id = NULLIF(current_setting('app.bot_id', true), '')::uuid"
)
AMOUNT = sa.Numeric(38, 18, asdecimal=True)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_risk_reservation_account_id",
        "risk_reservation",
        ["workspace_id", "account_id", "id"],
    )
    op.create_table(
        "venue_order_intent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), server_default="DEMO", nullable=False),
        sa.Column("idempotency_key", sa.String(240), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("settings_version_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), server_default="MARKET", nullable=False),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("limit_price", AMOUNT, nullable=True),
        sa.Column("client_order_id", sa.String(240), nullable=False),
        sa.Column("state", sa.String(24), server_default="CREATED", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("venue_order_id", sa.String(240), nullable=True),
        sa.Column("filled_quantity", AMOUNT, server_default="0", nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transition_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column("transition_key", sa.String(240), nullable=False),
        sa.Column("transition_actor", sa.String(128), nullable=False),
        sa.Column("transition_source", sa.String(24), nullable=False),
        sa.Column("transition_evidence_hash", sa.String(64), nullable=True),
        sa.CheckConstraint("mode = 'DEMO'", name="venue_order_intent_demo_only"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="venue_order_intent_side_valid"),
        sa.CheckConstraint(
            "order_type IN ('MARKET', 'LIMIT')", name="venue_order_intent_type_valid"
        ),
        sa.CheckConstraint(
            "(order_type = 'MARKET' AND limit_price IS NULL) OR "
            "(order_type = 'LIMIT' AND limit_price > 0)",
            name="venue_order_intent_limit_price_valid",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="venue_order_intent_quantity_valid",
        ),
        sa.CheckConstraint(
            "state IN ('CREATED', 'SUBMITTING', 'RETRYABLE', 'ACKNOWLEDGED', "
            "'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="venue_order_intent_state_valid",
        ),
        sa.CheckConstraint(
            "(state = 'RETRYABLE' AND retry_not_before IS NOT NULL) OR "
            "(state <> 'RETRYABLE' AND retry_not_before IS NULL)",
            name="venue_order_intent_retry_deadline_consistent",
        ),
        sa.CheckConstraint(
            "(state = 'FILLED' AND filled_quantity = quantity) OR "
            "(state = 'PARTIALLY_FILLED' AND filled_quantity > 0 "
            "AND filled_quantity < quantity) OR "
            "(state IN ('CREATED', 'SUBMITTING', 'RETRYABLE', 'ACKNOWLEDGED') "
            "AND filled_quantity = 0) OR "
            "state IN ('CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="venue_order_intent_fill_state_consistent",
        ),
        sa.CheckConstraint(
            "(state IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED') "
            "AND venue_order_id IS NOT NULL) OR "
            "(state IN ('SUBMITTING', 'RETRYABLE', 'REJECTED') AND venue_order_id IS NULL) OR "
            "state IN ('CREATED', 'UNKNOWN')",
            name="venue_order_intent_venue_id_consistent",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 255 AND transition_sequence >= 1",
            name="venue_order_intent_counters_valid",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 240 "
            "AND length(client_order_id) BETWEEN 1 AND 240",
            name="venue_order_intent_keys_valid",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$' AND plan_hash ~ '^[0-9a-f]{64}$'",
            name="venue_order_intent_hashes_valid",
        ),
        sa.CheckConstraint(
            "length(symbol) BETWEEN 1 AND 64", name="venue_order_intent_symbol_valid"
        ),
        sa.CheckConstraint(
            "transition_source IN ('APPLICATION', 'SUBMISSION', 'RECONCILIATION', "
            "'USER_STREAM', 'OPERATOR')",
            name="venue_order_intent_transition_source_valid",
        ),
        sa.CheckConstraint(
            "length(transition_key) BETWEEN 1 AND 240 "
            "AND length(transition_actor) BETWEEN 1 AND 128",
            name="venue_order_intent_transition_metadata_valid",
        ),
        sa.CheckConstraint(
            "transition_evidence_hash IS NULL OR transition_evidence_hash ~ '^[0-9a-f]{64}$'",
            name="venue_order_intent_evidence_hash_valid",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venue_order_intent")),
        sa.UniqueConstraint(
            "workspace_id",
            "bot_id",
            "account_id",
            "id",
            name="uq_venue_order_intent_bot_account_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "idempotency_key",
            name="uq_venue_order_intent_workspace_account_idempotency",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "client_order_id",
            name="uq_venue_order_intent_workspace_account_client_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id"],
            ["bot_instance.workspace_id", "bot_instance.id", "bot_instance.trading_account_id"],
            name="fk_venue_order_intent_bot_account",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id", "reservation_id"],
            [
                "risk_reservation.workspace_id",
                "risk_reservation.bot_id",
                "risk_reservation.account_id",
                "risk_reservation.id",
            ],
            name="fk_venue_order_intent_reservation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "settings_version_id"],
            ["workspace_settings_version.workspace_id", "workspace_settings_version.id"],
            name="fk_venue_order_intent_settings",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="RESTRICT",
            name="fk_venue_order_intent_workspace",
        ),
    )
    op.create_index(
        "ix_venue_order_intent_workspace_account_state",
        "venue_order_intent",
        ["workspace_id", "account_id", "state"],
    )
    op.create_index(
        "ix_venue_order_intent_retry_due",
        "venue_order_intent",
        ["workspace_id", "state", "retry_not_before"],
    )
    op.create_table(
        "venue_order_transition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(240), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("filled_quantity", AMOUNT, nullable=False),
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("venue_order_id", sa.String(240), nullable=True),
        sa.Column("venue_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="venue_order_transition_sequence_positive"),
        sa.CheckConstraint(
            "to_state IN ('CREATED', 'SUBMITTING', 'RETRYABLE', 'ACKNOWLEDGED', "
            "'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="venue_order_transition_state_valid",
        ),
        sa.CheckConstraint(
            "source IN ('APPLICATION', 'SUBMISSION', 'RECONCILIATION', 'USER_STREAM', 'OPERATOR')",
            name="venue_order_transition_source_valid",
        ),
        sa.CheckConstraint(
            "length(event_key) BETWEEN 1 AND 240", name="venue_order_transition_key_valid"
        ),
        sa.CheckConstraint(
            "length(actor_id) BETWEEN 1 AND 128", name="venue_order_transition_actor_valid"
        ),
        sa.CheckConstraint(
            "evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$'",
            name="venue_order_transition_hash_valid",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$' AND attempt_count BETWEEN 0 AND 255 "
            "AND filled_quantity >= 0",
            name="venue_order_transition_payload_valid",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venue_order_transition")),
        sa.UniqueConstraint(
            "workspace_id", "intent_id", "sequence", name="uq_venue_order_transition_sequence"
        ),
        sa.UniqueConstraint(
            "workspace_id", "event_key", name="uq_venue_order_transition_event_key"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bot_id", "account_id", "intent_id"],
            [
                "venue_order_intent.workspace_id",
                "venue_order_intent.bot_id",
                "venue_order_intent.account_id",
                "venue_order_intent.id",
            ],
            ondelete="RESTRICT",
            name="fk_venue_order_transition_intent",
        ),
    )
    op.create_index(
        "ix_venue_order_transition_workspace_intent",
        "venue_order_transition",
        ["workspace_id", "intent_id", "sequence"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_risk_reservation_transition() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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
                NEW.reserved_initial_margin, NEW.created_at) THEN
                RAISE EXCEPTION 'risk reservation identity and budget are immutable';
            END IF;
            IF OLD.status = NEW.status THEN
                IF OLD.resolution_reason IS DISTINCT FROM NEW.resolution_reason THEN
                    RAISE EXCEPTION 'risk reservation resolution reason is immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'UNKNOWN'
               AND NEW.resolution_reason IS NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason IN (
                   'EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT',
                   'EXPIRED_NO_FILL', 'CANCELLED_NO_FILL'
               ) THEN
                IF EXISTS (
                    SELECT 1 FROM venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                       AND state IN (
                           'SUBMITTING', 'UNKNOWN', 'ACKNOWLEDGED',
                           'PARTIALLY_FILLED', 'FILLED'
                       )
                ) THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_RESERVATION_IN_USE';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                IF EXISTS (
                    SELECT 1 FROM venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                ) THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_TRANSFER_NEEDS_OPEN_RISK_RECORD';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason = 'ORDER_ABSENT' THEN
                IF EXISTS (
                    SELECT 1 FROM venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                ) THEN
                    RAISE EXCEPTION USING MESSAGE =
                        'WP03_VENUE_ORDER_RESERVATION_AUTHENTICATED_RECONCILIATION_REQUIRED';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM venue_order_intent
                     WHERE workspace_id = OLD.workspace_id
                       AND bot_id = OLD.bot_id
                       AND account_id = OLD.account_id
                       AND reservation_id = OLD.id
                ) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'WP03_VENUE_ORDER_TRANSFER_NEEDS_OPEN_RISK_RECORD';
            END IF;
            RAISE EXCEPTION 'invalid risk reservation status transition';
        END;
        $$
        """
    )

    for table in ("venue_order_intent", "venue_order_transition"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY runtime_bot_isolation ON "{table}" TO {RUNTIME} '
            f"USING ({BOT_SCOPE}) WITH CHECK ({BOT_SCOPE})"
        )
        op.execute(
            f'''CREATE POLICY admin_bot_member_isolation ON "{table}" TO {ADMIN}
                USING (EXISTS (
                    SELECT 1 FROM user_membership m
                     WHERE m.workspace_id = "{table}".workspace_id
                       AND m.id = NULLIF(current_setting('app.membership_id', true), '')::uuid
                       AND m.status = 'ACTIVE'
                       AND (
                           EXISTS (SELECT 1 FROM role_binding r
                                    WHERE r.workspace_id = m.workspace_id
                                      AND r.membership_id = m.id
                                      AND r.role IN ('owner', 'strategy_admin', 'risk_admin'))
                           OR EXISTS (SELECT 1 FROM bot_grant g
                                       WHERE g.workspace_id = m.workspace_id
                                         AND g.membership_id = m.id
                                         AND g.bot_id = "{table}".bot_id
                                         AND g.revoked_at IS NULL)
                       )
                ))'''
        )
    op.execute(f"GRANT SELECT ON TABLE venue_order_intent, venue_order_transition TO {ADMIN}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE venue_order_intent TO {RUNTIME}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE venue_order_transition TO {RUNTIME}")
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE venue_order_transition FROM PUBLIC, "
        f"{ADMIN}, {RUNTIME}"
    )
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE venue_order_intent FROM PUBLIC, "
        f"{ADMIN}, {RUNTIME}"
    )
    op.execute(
        f"GRANT UPDATE (state, attempt_count, last_attempt_at, retry_not_before, venue_order_id, "
        f"filled_quantity, last_event_at, transition_sequence, transition_key, transition_actor, "
        f"transition_source, transition_evidence_hash) ON venue_order_intent TO {RUNTIME}"
    )
    op.execute(f"REVOKE UPDATE ON TABLE risk_reservation FROM {RUNTIME}")
    op.execute(f"GRANT UPDATE (status, resolution_reason) ON TABLE risk_reservation TO {RUNTIME}")
    op.execute(
        """
        CREATE FUNCTION public.reject_venue_order_transition_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'WP03_VENUE_ORDER_HISTORY_APPEND_ONLY';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.validate_venue_order_transition_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE current_intent venue_order_intent%ROWTYPE;
        BEGIN
            SELECT * INTO current_intent FROM venue_order_intent
             WHERE workspace_id = NEW.workspace_id AND bot_id = NEW.bot_id
               AND account_id = NEW.account_id
               AND id = NEW.intent_id;
            IF NOT FOUND OR NEW.sequence <> current_intent.transition_sequence
               OR NEW.event_key <> current_intent.transition_key
               OR NEW.to_state <> current_intent.state
               OR NEW.actor_id <> current_intent.transition_actor
               OR NEW.source <> current_intent.transition_source
               OR NEW.attempt_count <> current_intent.attempt_count
               OR NEW.filled_quantity <> current_intent.filled_quantity
               OR NEW.retry_not_before IS DISTINCT FROM current_intent.retry_not_before
               OR NEW.venue_order_id IS DISTINCT FROM current_intent.venue_order_id
               OR NEW.venue_event_at IS DISTINCT FROM current_intent.last_event_at
               OR NEW.evidence_hash IS DISTINCT FROM current_intent.transition_evidence_hash
               OR NEW.request_fingerprint <> current_intent.request_fingerprint THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_EVENT_MUST_MATCH_INTENT';
            END IF;
            NEW.occurred_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.audit_venue_order_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE current_reservation risk_reservation%ROWTYPE;
        BEGIN
            IF NEW.state <> 'CREATED' OR NEW.attempt_count <> 0 OR NEW.filled_quantity <> 0
               OR NEW.transition_sequence <> 1 THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_INITIAL_STATE_INVALID';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM workspace_settings_version
                 WHERE workspace_id = NEW.workspace_id
                   AND id = NEW.settings_version_id
                   AND status = 'PUBLISHED'
                   AND effective_at <= clock_timestamp()
            ) THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_SETTINGS_NOT_EFFECTIVE';
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('workspace-risk:' || NEW.workspace_id::text, 0)
            );
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'account-risk:' || NEW.workspace_id::text || ':' || NEW.account_id::text,
                    0
                )
            );
            SELECT * INTO current_reservation FROM risk_reservation
             WHERE workspace_id = NEW.workspace_id AND bot_id = NEW.bot_id
               AND account_id = NEW.account_id AND id = NEW.reservation_id FOR UPDATE;
            IF NOT FOUND OR current_reservation.status <> 'RESERVED'
               OR current_reservation.expires_at <= clock_timestamp()
               OR current_reservation.plan_hash <> NEW.plan_hash
               OR current_reservation.symbol <> NEW.symbol THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_RESERVATION_NOT_ACTIVE';
            END IF;
            INSERT INTO venue_order_transition (
                id, workspace_id, bot_id, account_id, intent_id, sequence, event_key,
                from_state, to_state, actor_id, source, occurred_at, attempt_count,
                filled_quantity, retry_not_before, venue_order_id, venue_event_at,
                evidence_hash, request_fingerprint
            ) VALUES (
                gen_random_uuid(), NEW.workspace_id, NEW.bot_id, NEW.account_id, NEW.id,
                NEW.transition_sequence, NEW.transition_key, NULL, NEW.state,
                NEW.transition_actor, NEW.transition_source, clock_timestamp(),
                NEW.attempt_count, NEW.filled_quantity, NEW.retry_not_before,
                NEW.venue_order_id, NEW.last_event_at, NEW.transition_evidence_hash,
                NEW.request_fingerprint
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.guard_venue_order_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            allowed boolean := false;
            current_reservation risk_reservation%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_DELETE_FORBIDDEN';
            END IF;
            IF (to_jsonb(NEW) - ARRAY[
                    'state', 'attempt_count', 'last_attempt_at', 'retry_not_before',
                    'venue_order_id', 'filled_quantity', 'last_event_at',
                    'transition_sequence', 'transition_key', 'transition_actor',
                    'transition_source', 'transition_evidence_hash'
                ]) <> (to_jsonb(OLD) - ARRAY[
                    'state', 'attempt_count', 'last_attempt_at', 'retry_not_before',
                    'venue_order_id', 'filled_quantity', 'last_event_at',
                    'transition_sequence', 'transition_key', 'transition_actor',
                    'transition_source', 'transition_evidence_hash'
                ]) THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_IMMUTABLE_FIELDS';
            END IF;
            IF NEW.transition_sequence <> OLD.transition_sequence + 1 THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_SEQUENCE_INVALID';
            END IF;
            IF OLD.state = 'UNKNOWN' AND NEW.state = 'RETRYABLE' THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_AUTHENTICATED_RECONCILIATION_REQUIRED';
            END IF;
            allowed := CASE OLD.state
                WHEN 'CREATED' THEN NEW.state = 'SUBMITTING'
                WHEN 'SUBMITTING' THEN NEW.state IN (
                    'ACKNOWLEDGED', 'RETRYABLE', 'REJECTED', 'UNKNOWN'
                )
                WHEN 'RETRYABLE' THEN NEW.state = 'SUBMITTING'
                WHEN 'ACKNOWLEDGED' THEN NEW.state IN (
                    'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED'
                )
                WHEN 'PARTIALLY_FILLED' THEN NEW.state IN (
                    'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED'
                )
                WHEN 'UNKNOWN' THEN NEW.state IN (
                    'UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED',
                    'CANCELED', 'REJECTED', 'EXPIRED'
                )
                WHEN 'FILLED' THEN NEW.state = 'FILLED'
                WHEN 'CANCELED' THEN NEW.state = 'CANCELED'
                WHEN 'REJECTED' THEN NEW.state = 'REJECTED'
                WHEN 'EXPIRED' THEN NEW.state = 'EXPIRED'
                ELSE false
            END;
            IF NOT allowed THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_TRANSITION_INVALID';
            END IF;
            IF NEW.filled_quantity < OLD.filled_quantity OR NEW.filled_quantity > NEW.quantity THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_FILL_NOT_MONOTONIC';
            END IF;
            IF OLD.venue_order_id IS NOT NULL
               AND NEW.venue_order_id IS DISTINCT FROM OLD.venue_order_id THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_ID_IMMUTABLE';
            END IF;
            IF OLD.last_event_at IS NOT NULL
               AND (NEW.last_event_at IS NULL OR NEW.last_event_at < OLD.last_event_at) THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_EVENT_TIME_NOT_MONOTONIC';
            END IF;
            IF NEW.state = 'SUBMITTING' AND OLD.state <> 'SUBMITTING' THEN
                IF NEW.attempt_count <> OLD.attempt_count + 1 OR NEW.last_attempt_at IS NULL THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_ATTEMPT_INVALID';
                END IF;
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('workspace-risk:' || NEW.workspace_id::text, 0)
                );
                PERFORM pg_advisory_xact_lock(
                    hashtextextended(
                        'account-risk:' || NEW.workspace_id::text || ':' || NEW.account_id::text,
                        0
                    )
                );
                SELECT * INTO current_reservation FROM risk_reservation
                 WHERE workspace_id = NEW.workspace_id AND bot_id = NEW.bot_id
                   AND account_id = NEW.account_id AND id = NEW.reservation_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_RESERVATION_NOT_ACTIVE';
                END IF;
                IF OLD.state = 'CREATED' THEN
                    IF current_reservation.status <> 'RESERVED'
                       OR current_reservation.expires_at <= clock_timestamp() THEN
                        RAISE EXCEPTION 'WP03_VENUE_ORDER_RESERVATION_NOT_ACTIVE';
                    END IF;
                    UPDATE risk_reservation SET status = 'UNKNOWN', resolution_reason = NULL
                     WHERE workspace_id = NEW.workspace_id AND bot_id = NEW.bot_id
                       AND account_id = NEW.account_id AND id = NEW.reservation_id;
                ELSE
                    IF current_reservation.status <> 'UNKNOWN'
                       OR OLD.retry_not_before IS NULL
                       OR OLD.retry_not_before > clock_timestamp() THEN
                        RAISE EXCEPTION 'WP03_VENUE_ORDER_RETRY_NOT_DUE';
                    END IF;
                END IF;
            ELSIF NEW.attempt_count <> OLD.attempt_count THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_ATTEMPT_INVALID';
            END IF;
            IF OLD.state = 'UNKNOWN' AND NEW.state <> 'UNKNOWN' THEN
                IF NEW.transition_source NOT IN ('RECONCILIATION', 'USER_STREAM')
                   OR NEW.transition_evidence_hash IS NULL THEN
                    RAISE EXCEPTION 'WP03_VENUE_ORDER_UNKNOWN_NEEDS_EVIDENCE';
                END IF;
            END IF;
            IF OLD.state = 'SUBMITTING' AND NEW.state = 'RETRYABLE'
               AND (NEW.retry_not_before IS NULL
                    OR NEW.retry_not_before <= clock_timestamp()) THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_RETRY_BACKOFF_REQUIRED';
            END IF;
            IF OLD.state = 'UNKNOWN' AND NEW.state = 'RETRYABLE'
               AND NEW.last_event_at IS NOT NULL
               AND NEW.retry_not_before < NEW.last_event_at THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_RECONCILE_TIME_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.audit_venue_order_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO venue_order_transition (
                id, workspace_id, bot_id, account_id, intent_id, sequence, event_key,
                from_state, to_state, actor_id, source, occurred_at, attempt_count,
                filled_quantity, retry_not_before, venue_order_id, venue_event_at,
                evidence_hash, request_fingerprint
            ) VALUES (
                gen_random_uuid(), NEW.workspace_id, NEW.bot_id, NEW.account_id, NEW.id,
                NEW.transition_sequence, NEW.transition_key, OLD.state, NEW.state,
                NEW.transition_actor, NEW.transition_source, clock_timestamp(),
                NEW.attempt_count, NEW.filled_quantity, NEW.retry_not_before,
                NEW.venue_order_id, NEW.last_event_at, NEW.transition_evidence_hash,
                NEW.request_fingerprint
            );
            RETURN NEW;
        END;
        $$
        """
    )
    for trigger, table, function in (
        (
            "venue_order_transition_append_only",
            "venue_order_transition",
            "reject_venue_order_transition_mutation",
        ),
        (
            "venue_order_transition_insert_guard",
            "venue_order_transition",
            "validate_venue_order_transition_insert",
        ),
        ("venue_order_intent_insert_audit", "venue_order_intent", "audit_venue_order_insert"),
        ("venue_order_intent_transition_guard", "venue_order_intent", "guard_venue_order_update"),
    ):
        operation = (
            "INSERT"
            if trigger == "venue_order_intent_insert_audit"
            else (
                "BEFORE UPDATE OR DELETE"
                if trigger == "venue_order_intent_transition_guard"
                else "BEFORE UPDATE OR DELETE"
            )
        )
        if trigger == "venue_order_intent_insert_audit":
            op.execute(
                f"CREATE TRIGGER {trigger} AFTER INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
            )
        else:
            operation = (
                "BEFORE INSERT" if trigger == "venue_order_transition_insert_guard" else operation
            )
            op.execute(
                f"CREATE TRIGGER {trigger} {operation} ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
            )
    op.execute(
        "CREATE TRIGGER venue_order_intent_transition_audit AFTER UPDATE ON venue_order_intent "
        "FOR EACH ROW EXECUTE FUNCTION public.audit_venue_order_update()"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE venue_order_intent, venue_order_transition IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM venue_order_intent)
               OR EXISTS (SELECT 1 FROM venue_order_transition) THEN
                RAISE EXCEPTION 'WP03_VENUE_ORDER_HISTORY_DOWNGRADE_BLOCKED';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_risk_reservation_transition() RETURNS trigger
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
                NEW.reserved_initial_margin, NEW.created_at) THEN
                RAISE EXCEPTION 'risk reservation identity and budget are immutable';
            END IF;
            IF OLD.status = NEW.status THEN
                IF OLD.resolution_reason IS DISTINCT FROM NEW.resolution_reason THEN
                    RAISE EXCEPTION 'risk reservation resolution reason is immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'UNKNOWN'
               AND NEW.resolution_reason IS NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason IN (
                   'EXPIRED_BEFORE_SUBMIT', 'CANCELLED_BEFORE_SUBMIT',
                   'EXPIRED_NO_FILL', 'CANCELLED_NO_FILL'
               ) THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'RESERVED' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'RELEASED'
               AND NEW.resolution_reason = 'ORDER_ABSENT' THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'UNKNOWN' AND NEW.status = 'TRANSFERRED'
               AND NEW.resolution_reason = 'OPEN_RISK_RECORDED' THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid risk reservation status transition';
        END;
        $$
        """
    )
    op.execute(f"REVOKE UPDATE ON TABLE risk_reservation FROM {RUNTIME}")
    op.execute(f"GRANT UPDATE ON TABLE risk_reservation TO {RUNTIME}")
    op.execute("DROP TRIGGER venue_order_intent_transition_audit ON venue_order_intent")
    op.execute("DROP TRIGGER venue_order_intent_transition_guard ON venue_order_intent")
    op.execute("DROP TRIGGER venue_order_intent_insert_audit ON venue_order_intent")
    op.execute("DROP TRIGGER venue_order_transition_insert_guard ON venue_order_transition")
    op.execute("DROP TRIGGER venue_order_transition_append_only ON venue_order_transition")
    for function in (
        "guard_venue_order_update",
        "audit_venue_order_update",
        "audit_venue_order_insert",
        "validate_venue_order_transition_insert",
        "reject_venue_order_transition_mutation",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
    for table in ("venue_order_transition", "venue_order_intent"):
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM PUBLIC, {ADMIN}, {RUNTIME}')
        op.execute(f'DROP POLICY runtime_bot_isolation ON "{table}"')
        op.execute(f'DROP POLICY admin_bot_member_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
        op.drop_table(table)
    op.drop_constraint("uq_risk_reservation_account_id", "risk_reservation", type_="unique")
