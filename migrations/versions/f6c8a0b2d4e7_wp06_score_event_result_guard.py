"""Bổ sung ràng buộc DB cho kết quả score event WP06."""

from collections.abc import Sequence

from alembic import op

revision: str = "f6c8a0b2d4e7"
down_revision: str | None = "55c504fe5b86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER = "crypidian_market_signal_worker"
RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
TABLE = "learning_score_event"
CONSTRAINT = "ck_learning_score_event_learning_score_event_contract_consistent"
VALIDATOR = "public.wp06_score_event_result_is_valid"
VALIDATOR_SIGNATURE = "text,text,text,jsonb,double precision,double precision,double precision"
GUARD = "public.wp06_learning_score_event_result_guard"
GUARD_TRIGGER = "learning_score_event_00_result_guard"


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION {VALIDATOR}(
            target_mode text,
            target_decision text,
            target_final_status text,
            target_class_probabilities jsonb,
            target_probability double precision,
            target_confidence double precision,
            target_uncertainty double precision
        ) RETURNS boolean LANGUAGE plpgsql IMMUTABLE
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            expected_labels text[] := ARRAY['-1','0','1'];
            expected_status text;
            entry jsonb;
            label text;
            probability numeric;
            probabilities numeric[] := ARRAY[0::numeric,0::numeric,0::numeric];
            idx integer;
        BEGIN
            IF target_mode IS NULL OR target_mode NOT IN ('SHADOW','PAPER_GATE')
               OR target_decision IS NULL
               OR target_decision NOT IN ('APPROVE','VETO','ABSTAIN')
               OR target_final_status IS NULL THEN
                RETURN false;
            END IF;

            IF target_mode = 'SHADOW' THEN
                IF target_final_status <> 'CANDIDATE' THEN
                    RETURN false;
                END IF;
            ELSE
                expected_status := CASE target_decision
                    WHEN 'APPROVE' THEN 'CANDIDATE'
                    WHEN 'VETO' THEN 'VETO'
                    WHEN 'ABSTAIN' THEN 'ABSTAIN'
                END;
                IF target_final_status IS DISTINCT FROM expected_status THEN
                    RETURN false;
                END IF;
            END IF;

            IF target_decision = 'ABSTAIN' THEN
                RETURN target_class_probabilities = '[]'::jsonb
                   AND target_probability IS NULL
                   AND target_confidence IS NULL
                   AND target_uncertainty IS NULL;
            END IF;

            IF pg_catalog.jsonb_typeof(target_class_probabilities) IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(target_class_probabilities) <> 3 THEN
                RETURN false;
            END IF;
            FOR idx IN 0..2 LOOP
                entry := target_class_probabilities -> idx;
                IF pg_catalog.jsonb_typeof(entry) IS DISTINCT FROM 'array'
                   OR pg_catalog.jsonb_array_length(entry) <> 2
                   OR pg_catalog.jsonb_typeof(entry->0) IS DISTINCT FROM 'number'
                   OR pg_catalog.jsonb_typeof(entry->1) IS DISTINCT FROM 'number' THEN
                    RETURN false;
                END IF;
                label := entry->>0;
                IF label IS DISTINCT FROM expected_labels[idx + 1]
                   OR label !~ '^-?[0-9]+$' THEN
                    RETURN false;
                END IF;
                probability := (entry->>1)::numeric;
                IF probability < 0 OR probability > 1 THEN
                    RETURN false;
                END IF;
                probabilities[idx + 1] := probability;
            END LOOP;

            IF pg_catalog.abs(probabilities[1] + probabilities[2] + probabilities[3] - 1)
                    > 0.000000000001::numeric
               OR target_probability IS NULL
               OR target_confidence IS NULL
               OR target_uncertainty IS NULL
               OR target_probability NOT BETWEEN 0 AND 1
               OR target_confidence NOT BETWEEN 0 AND 1
               OR target_uncertainty NOT BETWEEN 0 AND 1
               OR target_probability IS DISTINCT FROM probabilities[3]::double precision
               OR target_confidence IS DISTINCT FROM
                    GREATEST(probabilities[1], probabilities[2], probabilities[3])::double precision
               OR target_uncertainty IS DISTINCT FROM 1::double precision - target_confidence THEN
                RETURN false;
            END IF;
            RETURN true;
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {VALIDATOR}({VALIDATOR_SIGNATURE}) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {VALIDATOR}({VALIDATOR_SIGNATURE}) TO {WORKER}, {RIGHTS_ROLE}"
    )
    op.create_check_constraint(
        constraint_name=op.f(CONSTRAINT),
        table_name=TABLE,
        condition=(
            f"{VALIDATOR}(mode,decision,final_status,class_probabilities,"
            "probability,confidence,uncertainty)"
        ),
    )

    op.execute(f"""
        CREATE FUNCTION {GUARD}()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF session_user <> '{WORKER}' THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_CALLER_DENIED';
            END IF;
            IF NOT {VALIDATOR}(
                NEW.mode,
                NEW.decision,
                NEW.final_status,
                NEW.class_probabilities,
                NEW.probability,
                NEW.confidence,
                NEW.uncertainty
            ) THEN
                RAISE EXCEPTION 'WP06_SCORE_EVENT_RESULT_INVALID';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(f"ALTER FUNCTION {GUARD}() OWNER TO {RIGHTS_ROLE}")
    op.execute(f"REVOKE ALL ON FUNCTION {GUARD}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {GUARD_TRIGGER} BEFORE INSERT ON public.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {GUARD}()"
    )


def downgrade() -> None:
    op.execute(f"LOCK TABLE public.{TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.{TABLE}) THEN
            RAISE EXCEPTION 'WP06_SCORE_EVENT_INTEGRITY_POPULATED_DOWNGRADE_DENIED';
        END IF;
    END $$""")
    op.execute(f"DROP TRIGGER {GUARD_TRIGGER} ON public.{TABLE}")
    op.execute(f"DROP FUNCTION {GUARD}()")
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION {VALIDATOR}({VALIDATOR_SIGNATURE}) "
        f"FROM {WORKER}, {RIGHTS_ROLE}"
    )
    op.execute(f"DROP FUNCTION {VALIDATOR}({VALIDATOR_SIGNATURE})")
