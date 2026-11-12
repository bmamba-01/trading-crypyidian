"""Giới hạn cohort drift trong 30 ngày theo đồng hồ giao dịch PostgreSQL."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f5a7b9d1e3"
down_revision: str = "b2e4a6c8d0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Giữ nguyên ledger cũ, chỉ cho append policy 30 ngày mới."""
    op.add_column(
        "learning_drift_assessment",
        sa.Column("observation_window_days", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "learning_drift_assessment_observation_window_valid",
        "learning_drift_assessment",
        "(policy_version = 'coverage-v2-30d' AND observation_window_days IS NOT NULL "
        "AND observation_window_days = 30) OR "
        "(policy_version = 'coverage-v1' AND observation_window_days IS NULL)",
    )
    # Index hiện hành đã có đúng thứ tự khóa và thời gian cần dùng.
    op.execute(
        "ALTER INDEX public.ix_learning_shadow_observation_registry_revision "
        "RENAME TO ix_learning_shadow_observation_cohort_time"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_lock_learning_drift_assessment_cohort()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.created_at IS DISTINCT FROM transaction_timestamp() THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_DATABASE_TIME_REQUIRED';
            END IF;
            IF NEW.policy_version IS DISTINCT FROM 'coverage-v2-30d'
                OR NEW.observation_window_days IS DISTINCT FROM 30
                OR NEW.minimum_observations <> 3
                OR NEW.minimum_snapshot_coverage <> 0.8
                OR NEW.minimum_quality_rate <> 0.9 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_POLICY_INVALID';
            END IF;
            IF NEW.member_count = 0 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_EMPTY_COHORT';
            END IF;
            IF NEW.cohort_registry_id IS NOT NULL THEN
                PERFORM public.wp06_lock_learning_shadow_cohort(
                    NEW.workspace_id,
                    NEW.cohort_registry_id,
                    NEW.cohort_revision
                );
            END IF;
            RETURN NEW;
        END $$
        """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_validate_learning_drift_assessment_membership()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            target_workspace uuid;
            target_assessment uuid;
            assessment_row record;
            actual_member_ids json;
            actual_count integer;
            actual_eligible_count integer;
            actual_window_start timestamptz;
            actual_window_end timestamptz;
            actual_digest text;
            actual_coverage float8;
            actual_quality float8;
            actual_abstention float8;
            required_complete boolean;
            expected_status text;
            expected_reasons jsonb;
            reason_list text[] := ARRAY['performance_not_evaluable']::text[];
            has_low_coverage boolean := false;
            has_low_quality boolean := false;
            invalid_ordinal boolean;
            actual_cohort_count integer;
            actual_cohort_digest text;
            actual_cohort_ids text;
            expected_idempotency_key text;
            expected_content_hash text;
        BEGIN
            IF TG_TABLE_NAME = 'learning_drift_assessment' THEN
                target_workspace := NEW.workspace_id;
                target_assessment := NEW.id;
            ELSE
                target_workspace := NEW.workspace_id;
                target_assessment := NEW.assessment_id;
            END IF;
            SELECT assessment.* INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            SELECT pg_catalog.count(*)::integer,
                   pg_catalog.count(*) FILTER (WHERE member.eligible)::integer,
                   pg_catalog.min(member.observed_at),
                   pg_catalog.max(member.observed_at),
                   pg_catalog.round(
                       pg_catalog.sum(member.snapshot_coverage::text::numeric)
                           FILTER (WHERE member.eligible)
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.eligible), 0),
                       12
                   )::float8,
                   pg_catalog.round(
                       (pg_catalog.count(*) FILTER (
                           WHERE member.eligible AND member.snapshot_quality_ok
                       ))::numeric
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.eligible), 0),
                       12
                   )::float8,
                   pg_catalog.round(
                       (pg_catalog.count(*) FILTER (
                           WHERE member.challenger_decision = 'abstain'
                       ))::numeric / NULLIF(pg_catalog.count(*), 0),
                       12
                   )::float8,
                   COALESCE(
                       pg_catalog.json_agg(
                           member.observation_id::text
                           ORDER BY member.observation_id::text
                       ),
                       '[]'::json
                   ),
                   '[' || COALESCE(
                       pg_catalog.string_agg(
                           '"' || member.observation_id::text || '"', ','
                           ORDER BY member.observation_id::text
                       ),
                       ''
                   ) || ']'
              INTO actual_count, actual_eligible_count, actual_window_start, actual_window_end,
                   actual_coverage, actual_quality, actual_abstention, actual_member_ids,
                   actual_digest
              FROM public.learning_drift_assessment_member AS member
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = target_assessment;

            SELECT EXISTS (
                SELECT 1 FROM (
                    SELECT member.ordinal,
                           pg_catalog.row_number() OVER (ORDER BY member.observation_id::text) - 1
                               AS expected_ordinal
                      FROM public.learning_drift_assessment_member AS member
                     WHERE member.workspace_id = target_workspace
                       AND member.assessment_id = target_assessment
                ) AS ordered
                 WHERE ordered.ordinal <> ordered.expected_ordinal
            ) INTO invalid_ordinal;

            actual_digest := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(actual_digest, 'UTF8')),
                'hex'
            );
            SELECT public.wp06_drift_assessment_idempotency_key(assessment)
              INTO expected_idempotency_key
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF assessment_row.idempotency_key IS DISTINCT FROM expected_idempotency_key THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_IDEMPOTENCY_INVALID';
            END IF;
            SELECT public.wp06_drift_assessment_content_hash(assessment, actual_member_ids)
              INTO expected_content_hash
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF assessment_row.content_hash IS DISTINCT FROM expected_content_hash THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_CONTENT_HASH_INVALID';
            END IF;
            IF assessment_row.cohort_registry_id IS NULL THEN
                actual_cohort_count := 0;
                actual_cohort_digest := pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to('[]', 'UTF8')),
                    'hex'
                );
            ELSE
                SELECT pg_catalog.count(*)::integer,
                       '[' || COALESCE(
                           pg_catalog.string_agg(
                               '"' || observation.id::text || '"', ','
                               ORDER BY observation.id::text
                           ),
                           ''
                       ) || ']'
                  INTO actual_cohort_count, actual_cohort_ids
                  FROM public.learning_shadow_observation AS observation
                 WHERE observation.workspace_id = target_workspace
                   AND observation.challenger_registry_id = assessment_row.cohort_registry_id
                   AND observation.challenger_revision = assessment_row.cohort_revision
                   AND observation.observed_at >= assessment_row.created_at - interval '720 hours'
                   AND observation.observed_at <= assessment_row.created_at;
                actual_cohort_digest := pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to(actual_cohort_ids, 'UTF8')),
                    'hex'
                );
            END IF;
            required_complete := actual_count >= assessment_row.minimum_observations
                AND actual_count = actual_eligible_count;
            IF actual_count < assessment_row.minimum_observations THEN
                reason_list := pg_catalog.array_append(reason_list, 'low_sample');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.baseline_available
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'baseline_missing');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.schema_compatible
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'schema_incompatible');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.rights_eligible
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'rights_unverified');
            END IF;

            IF required_complete THEN
                has_low_coverage := actual_coverage < assessment_row.minimum_snapshot_coverage;
                has_low_quality := actual_quality < assessment_row.minimum_quality_rate;
                IF has_low_coverage THEN
                    reason_list := pg_catalog.array_append(reason_list, 'coverage_below_threshold');
                END IF;
                IF has_low_quality THEN
                    reason_list := pg_catalog.array_append(reason_list, 'quality_below_threshold');
                END IF;
                expected_status := CASE
                    WHEN has_low_coverage OR has_low_quality THEN 'detected'
                    ELSE 'no_drift'
                END;
            ELSE
                actual_coverage := NULL;
                actual_quality := NULL;
                expected_status := 'inconclusive';
            END IF;
            SELECT pg_catalog.jsonb_agg(reason ORDER BY reason)
              INTO expected_reasons
              FROM pg_catalog.unnest(reason_list) AS reason;

            IF actual_count <> assessment_row.member_count
                OR actual_count <> actual_cohort_count
                OR actual_eligible_count <> assessment_row.eligible_count
                OR assessment_row.window_start IS DISTINCT FROM actual_window_start
                OR assessment_row.window_end IS DISTINCT FROM assessment_row.created_at
                OR actual_window_start < assessment_row.created_at - interval '720 hours'
                OR assessment_row.created_at < actual_window_end
                OR assessment_row.membership_digest <> actual_digest
                OR actual_digest <> actual_cohort_digest
                OR invalid_ordinal
                OR assessment_row.performance_status <> 'inconclusive'
                OR assessment_row.data_quality_status <> expected_status
                OR assessment_row.reason_codes <> expected_reasons
                OR assessment_row.snapshot_coverage_rate IS DISTINCT FROM actual_coverage
                OR assessment_row.quality_rate IS DISTINCT FROM actual_quality
                OR assessment_row.abstention_rate IS DISTINCT FROM actual_abstention THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBERSHIP_INVALID';
            END IF;
            RETURN NULL;
        END $$
        """)


def downgrade() -> None:
    """Từ chối hạ schema khi đã có assessment dùng chính sách mới."""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.learning_drift_assessment
                       WHERE policy_version = 'coverage-v2-30d') THEN
                RAISE EXCEPTION 'WP06_BOUNDED_DRIFT_DOWNGRADE_POPULATED';
            END IF;
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_lock_learning_drift_assessment_cohort()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NEW.created_at IS DISTINCT FROM transaction_timestamp() THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_DATABASE_TIME_REQUIRED';
            END IF;
            IF NEW.policy_version IS DISTINCT FROM 'coverage-v1'
                OR NEW.minimum_observations <> 3
                OR NEW.minimum_snapshot_coverage <> 0.8
                OR NEW.minimum_quality_rate <> 0.9 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_POLICY_INVALID';
            END IF;
            IF NEW.member_count = 0 THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_EMPTY_COHORT';
            END IF;
            IF NEW.cohort_registry_id IS NOT NULL THEN
                PERFORM public.wp06_lock_learning_shadow_cohort(
                    NEW.workspace_id,
                    NEW.cohort_registry_id,
                    NEW.cohort_revision
                );
            END IF;
            RETURN NEW;
        END $$
        """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.wp06_validate_learning_drift_assessment_membership()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            target_workspace uuid;
            target_assessment uuid;
            assessment_row record;
            actual_member_ids json;
            actual_count integer;
            actual_eligible_count integer;
            actual_window_start timestamptz;
            actual_window_end timestamptz;
            actual_digest text;
            actual_coverage float8;
            actual_quality float8;
            actual_abstention float8;
            required_complete boolean;
            expected_status text;
            expected_reasons jsonb;
            reason_list text[] := ARRAY['performance_not_evaluable']::text[];
            has_low_coverage boolean := false;
            has_low_quality boolean := false;
            invalid_ordinal boolean;
            actual_cohort_count integer;
            actual_cohort_digest text;
            actual_cohort_ids text;
            expected_idempotency_key text;
            expected_content_hash text;
        BEGIN
            IF TG_TABLE_NAME = 'learning_drift_assessment' THEN
                target_workspace := NEW.workspace_id;
                target_assessment := NEW.id;
            ELSE
                target_workspace := NEW.workspace_id;
                target_assessment := NEW.assessment_id;
            END IF;
            SELECT assessment.* INTO assessment_row
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            SELECT pg_catalog.count(*)::integer,
                   pg_catalog.count(*) FILTER (WHERE member.eligible)::integer,
                   pg_catalog.min(member.observed_at),
                   pg_catalog.max(member.observed_at),
                   pg_catalog.round(
                       pg_catalog.sum(member.snapshot_coverage::text::numeric)
                           FILTER (WHERE member.eligible)
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.eligible), 0),
                       12
                   )::float8,
                   pg_catalog.round(
                       (pg_catalog.count(*) FILTER (
                           WHERE member.eligible AND member.snapshot_quality_ok
                       ))::numeric
                       / NULLIF(pg_catalog.count(*) FILTER (WHERE member.eligible), 0),
                       12
                   )::float8,
                   pg_catalog.round(
                       (pg_catalog.count(*) FILTER (
                           WHERE member.challenger_decision = 'abstain'
                       ))::numeric / NULLIF(pg_catalog.count(*), 0),
                       12
                   )::float8,
                   COALESCE(
                       pg_catalog.json_agg(
                           member.observation_id::text
                           ORDER BY member.observation_id::text
                       ),
                       '[]'::json
                   ),
                   '[' || COALESCE(
                       pg_catalog.string_agg(
                           '"' || member.observation_id::text || '"', ','
                           ORDER BY member.observation_id::text
                       ),
                       ''
                   ) || ']'
              INTO actual_count, actual_eligible_count, actual_window_start, actual_window_end,
                   actual_coverage, actual_quality, actual_abstention, actual_member_ids,
                   actual_digest
              FROM public.learning_drift_assessment_member AS member
             WHERE member.workspace_id = target_workspace
               AND member.assessment_id = target_assessment;

            SELECT EXISTS (
                SELECT 1 FROM (
                    SELECT member.ordinal,
                           pg_catalog.row_number() OVER (ORDER BY member.observation_id::text) - 1
                               AS expected_ordinal
                      FROM public.learning_drift_assessment_member AS member
                     WHERE member.workspace_id = target_workspace
                       AND member.assessment_id = target_assessment
                ) AS ordered
                 WHERE ordered.ordinal <> ordered.expected_ordinal
            ) INTO invalid_ordinal;

            actual_digest := pg_catalog.encode(
                pg_catalog.sha256(pg_catalog.convert_to(actual_digest, 'UTF8')),
                'hex'
            );
            SELECT public.wp06_drift_assessment_idempotency_key(assessment)
              INTO expected_idempotency_key
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF assessment_row.idempotency_key IS DISTINCT FROM expected_idempotency_key THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_IDEMPOTENCY_INVALID';
            END IF;
            SELECT public.wp06_drift_assessment_content_hash(assessment, actual_member_ids)
              INTO expected_content_hash
              FROM public.learning_drift_assessment AS assessment
             WHERE assessment.workspace_id = target_workspace
               AND assessment.id = target_assessment;
            IF assessment_row.content_hash IS DISTINCT FROM expected_content_hash THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_CONTENT_HASH_INVALID';
            END IF;
            IF assessment_row.cohort_registry_id IS NULL THEN
                actual_cohort_count := 0;
                actual_cohort_digest := pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to('[]', 'UTF8')),
                    'hex'
                );
            ELSE
                SELECT pg_catalog.count(*)::integer,
                       '[' || COALESCE(
                           pg_catalog.string_agg(
                               '"' || observation.id::text || '"', ','
                               ORDER BY observation.id::text
                           ),
                           ''
                       ) || ']'
                  INTO actual_cohort_count, actual_cohort_ids
                  FROM public.learning_shadow_observation AS observation
                 WHERE observation.workspace_id = target_workspace
                   AND observation.challenger_registry_id = assessment_row.cohort_registry_id
                   AND observation.challenger_revision = assessment_row.cohort_revision;
                actual_cohort_digest := pg_catalog.encode(
                    pg_catalog.sha256(pg_catalog.convert_to(actual_cohort_ids, 'UTF8')),
                    'hex'
                );
            END IF;
            required_complete := actual_count >= assessment_row.minimum_observations
                AND actual_count = actual_eligible_count;
            IF actual_count < assessment_row.minimum_observations THEN
                reason_list := pg_catalog.array_append(reason_list, 'low_sample');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.baseline_available
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'baseline_missing');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.schema_compatible
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'schema_incompatible');
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.learning_drift_assessment_member AS member
                 WHERE member.workspace_id = target_workspace
                   AND member.assessment_id = target_assessment
                   AND NOT member.rights_eligible
            ) THEN
                reason_list := pg_catalog.array_append(reason_list, 'rights_unverified');
            END IF;

            IF required_complete THEN
                has_low_coverage := actual_coverage < assessment_row.minimum_snapshot_coverage;
                has_low_quality := actual_quality < assessment_row.minimum_quality_rate;
                IF has_low_coverage THEN
                    reason_list := pg_catalog.array_append(reason_list, 'coverage_below_threshold');
                END IF;
                IF has_low_quality THEN
                    reason_list := pg_catalog.array_append(reason_list, 'quality_below_threshold');
                END IF;
                expected_status := CASE
                    WHEN has_low_coverage OR has_low_quality THEN 'detected'
                    ELSE 'no_drift'
                END;
            ELSE
                actual_coverage := NULL;
                actual_quality := NULL;
                expected_status := 'inconclusive';
            END IF;
            SELECT pg_catalog.jsonb_agg(reason ORDER BY reason)
              INTO expected_reasons
              FROM pg_catalog.unnest(reason_list) AS reason;

            IF actual_count <> assessment_row.member_count
                OR actual_count <> actual_cohort_count
                OR actual_eligible_count <> assessment_row.eligible_count
                OR assessment_row.window_start IS DISTINCT FROM actual_window_start
                OR assessment_row.window_end IS DISTINCT FROM actual_window_end
                OR assessment_row.created_at < actual_window_end
                OR assessment_row.membership_digest <> actual_digest
                OR actual_digest <> actual_cohort_digest
                OR invalid_ordinal
                OR assessment_row.performance_status <> 'inconclusive'
                OR assessment_row.data_quality_status <> expected_status
                OR assessment_row.reason_codes <> expected_reasons
                OR assessment_row.snapshot_coverage_rate IS DISTINCT FROM actual_coverage
                OR assessment_row.quality_rate IS DISTINCT FROM actual_quality
                OR assessment_row.abstention_rate IS DISTINCT FROM actual_abstention THEN
                RAISE EXCEPTION 'WP06_SHADOW_DRIFT_MEMBERSHIP_INVALID';
            END IF;
            RETURN NULL;
        END $$
        """)
    op.execute(
        "ALTER INDEX public.ix_learning_shadow_observation_cohort_time "
        "RENAME TO ix_learning_shadow_observation_registry_revision"
    )
    op.drop_constraint(
        "learning_drift_assessment_observation_window_valid",
        "learning_drift_assessment",
        type_="check",
    )
    op.drop_column("learning_drift_assessment", "observation_window_days")
