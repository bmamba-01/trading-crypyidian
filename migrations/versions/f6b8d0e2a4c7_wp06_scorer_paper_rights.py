"""WP06: scorer active cần TRAINING và PAPER_DEMO hiện hành cho mọi source."""

import hashlib
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "f6b8d0e2a4c7"
down_revision: str = "f5b7d9e1a3c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RIGHTS_ROLE = "crypidian_scorer_deployment_rights_reader"
SIGNATURE = "public.wp06_verify_scorer_deployment_rights(uuid,uuid,uuid)"
PREVIOUS_SOURCE_HASH = "76b032e258af08f6871181f10c83f0100a30507148af09ee64df9ba4aebba53b"
PREVIOUS_DECLARATION = "            accepted_paper boolean;\n        BEGIN"
CORRECTED_DECLARATION = (
    "            accepted_paper boolean;\n            paper_source_id text;\n"
    "            paper_source_ids text[];\n        BEGIN"
)
PREVIOUS_GATE = "            SELECT report.id, report.evaluation_run_id"
CORRECTED_GATE = """            -- Preflight quyền trên scope artifact trước khi lookup report.
            -- Thiếu purpose phải fail-closed trước các điều kiện eligible khác.
            SELECT pg_catalog.array_agg(paper_source.source_id ORDER BY paper_source.source_id)
              INTO paper_source_ids
              FROM (
                  SELECT reference.source_id
                    FROM public.dataset_manifest AS manifest
                    CROSS JOIN LATERAL public.wp06_scorer_manifest_source_refs(
                        target_workspace, manifest.id) AS reference
                   WHERE manifest.workspace_id = target_workspace
                     AND manifest.manifest_hash = artifact_row.manifest_hash
                  UNION
                  SELECT bundle.rights_provider
                    FROM public.learning_scorer_test_opportunity_proof AS source_proof
                    JOIN public.backtest_dataset_replay_bundle AS bundle
                      ON bundle.workspace_id = source_proof.workspace_id
                     AND bundle.bundle_hash = source_proof.bundle_hash
                   WHERE source_proof.workspace_id = target_workspace
                     AND source_proof.attempt_id = artifact_row.attempt_id
                     AND source_proof.fold_id = artifact_row.fold_id
              ) AS paper_source;
            -- Giữ toàn bộ tập khóa/thứ tự của verifier TRAINING, kể cả provider dự phòng.
            FOR paper_source_id IN
                SELECT lock_source.source_id
                  FROM (
                      SELECT unnest(paper_source_ids) AS source_id
                      UNION SELECT unnest(
                          ARRAY['BINANCE_USDM_REST','CRYPIDIAN_FIRST_PARTY_QA']::text[])
                          AS source_id
                  ) AS lock_source
                 ORDER BY lock_source.source_id
            LOOP
                IF paper_source_id IS NULL
                   OR paper_source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' THEN
                    RAISE EXCEPTION 'WP06_SCORER_RIGHTS_SOURCE_ID_INVALID';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                    'research-source:' || target_workspace::text || ':' || paper_source_id, 0));
            END LOOP;
            FOR paper_source_id IN
                SELECT paper_source.source_id
                  FROM unnest(paper_source_ids) AS paper_source(source_id)
                 ORDER BY paper_source.source_id
            LOOP
                PERFORM public.wp06_require_scorer_paper_source_rights(
                    target_workspace, paper_source_id);
            END LOOP;

            SELECT report.id, report.evaluation_run_id"""

PREVIOUS_FINAL_GATE = "            accepted_shadow := report_row.evaluation_status IN ("
CORRECTED_FINAL_GATE = """            -- Recheck scope report/proof sau verifier TRAINING.
            -- Không dùng cache preflight nếu proof/report vừa xuất hiện trong lúc chờ.
            FOR paper_source_id IN
                SELECT paper_source.source_id
                  FROM (
                      SELECT reference.source_id
                        FROM public.dataset_manifest AS manifest
                        CROSS JOIN LATERAL public.wp06_scorer_manifest_source_refs(
                            target_workspace, manifest.id) AS reference
                       WHERE manifest.workspace_id = target_workspace
                         AND manifest.manifest_hash = artifact_row.manifest_hash
                      UNION
                      SELECT bundle.rights_provider
                        FROM public.backtest_dataset_replay_bundle AS bundle
                       WHERE bundle.workspace_id = target_workspace
                         AND bundle.bundle_hash = proof_row.bundle_hash
                  ) AS paper_source
                 ORDER BY paper_source.source_id
            LOOP
                PERFORM public.wp06_require_scorer_paper_source_rights(
                    target_workspace, paper_source_id);
            END LOOP;

            accepted_shadow := report_row.evaluation_status IN ("""


def _rewrite_definition(definition: str, source: str, *, forward: bool) -> str:
    """Chỉ thêm gate khi toàn bộ function body vẫn đúng preimage đã kiểm chứng."""
    patches = (
        (PREVIOUS_DECLARATION, CORRECTED_DECLARATION),
        (PREVIOUS_GATE, CORRECTED_GATE),
        (PREVIOUS_FINAL_GATE, CORRECTED_FINAL_GATE),
    )
    historical_source = source
    if not forward:
        for previous, replacement in patches:
            if historical_source.count(replacement) != 1:
                raise RuntimeError("WP06_SCORER_PAPER_RIGHTS_PREIMAGE_UNEXPECTED")
            historical_source = historical_source.replace(replacement, previous, 1)
    if (
        hashlib.sha256(historical_source.encode("utf-8")).hexdigest() != PREVIOUS_SOURCE_HASH
        or definition.count(source) != 1
    ):
        raise RuntimeError("WP06_SCORER_PAPER_RIGHTS_PREIMAGE_UNEXPECTED")
    corrected = source
    for previous, replacement in patches:
        before, after = (previous, replacement) if forward else (replacement, previous)
        if corrected.count(before) != 1:
            raise RuntimeError("WP06_SCORER_PAPER_RIGHTS_PREIMAGE_UNEXPECTED")
        corrected = corrected.replace(before, after, 1)
    return definition.replace(source, corrected, 1)


def _rewrite(*, forward: bool) -> None:
    """CREATE OR REPLACE giữ owner, ACL và thuộc tính bảo mật của deployment gateway."""
    connection = op.get_bind()
    statement = text(
        "SELECT pg_catalog.pg_get_functiondef(oid), prosrc FROM pg_catalog.pg_proc "
        "WHERE oid=CAST(:signature AS regprocedure)"
    )
    definition, source = connection.execute(statement, {"signature": SIGNATURE}).one()
    if not isinstance(definition, str) or not isinstance(source, str):
        raise RuntimeError("WP06_SCORER_PAPER_RIGHTS_SOURCE_INVALID")
    corrected = _rewrite_definition(definition, source, forward=forward)
    connection.exec_driver_sql(corrected)
    installed_definition, installed_source = connection.execute(
        statement, {"signature": SIGNATURE}
    ).one()
    if not isinstance(installed_definition, str) or not isinstance(installed_source, str):
        raise RuntimeError("WP06_SCORER_PAPER_RIGHTS_INSTALLED_SOURCE_INVALID")
    if (
        _rewrite_definition(installed_definition, installed_source, forward=not forward)
        != definition
    ):
        raise RuntimeError("WP06_SCORER_PAPER_RIGHTS_POSTIMAGE_UNEXPECTED")


def _create_paper_source_gate() -> None:
    """Helper chỉ bổ sung purpose; verifier TRAINING phía sau vẫn giữ mọi kiểm tra lineage."""
    op.execute("""
        CREATE FUNCTION public.wp06_require_scorer_paper_source_rights(
            target_workspace uuid, target_source_id text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            policy_row record;
            checked_at timestamptz;
        BEGIN
            IF target_workspace IS NULL OR target_source_id IS NULL
               OR target_source_id !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
               OR target_workspace IS DISTINCT FROM
                   NULLIF(pg_catalog.current_setting('app.workspace_id', true), '')::uuid THEN
                RAISE EXCEPTION 'WP06_SCORER_PAPER_SOURCE_SCOPE_DENIED';
            END IF;
            IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'WP06_SCORER_RIGHTS_READ_COMMITTED_REQUIRED';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
                'research-source:' || target_workspace::text || ':' || target_source_id, 0));
            checked_at := pg_catalog.clock_timestamp();
            IF target_source_id = 'CRYPIDIAN_FIRST_PARTY_QA' THEN
                RAISE EXCEPTION 'WP06_SCORER_DEPLOYMENT_QA_SOURCE_DENIED';
            END IF;
            SELECT policy.policy_id, policy.sequence, policy.state, policy.allowed_purposes,
                   policy.reviewed_at, policy.valid_until, policy.evidence_ref, policy.created_at
              INTO policy_row
              FROM public.source_policy_revision AS policy
             WHERE policy.workspace_id = target_workspace
               AND policy.source_id = target_source_id
             ORDER BY policy.sequence DESC LIMIT 1;
            IF NOT FOUND OR policy_row.state IS DISTINCT FROM 'verified'
               OR (policy_row.allowed_purposes @> '["training","paper_demo"]'::jsonb)
                    IS DISTINCT FROM true
               OR policy_row.reviewed_at IS NULL
               OR policy_row.reviewed_at > checked_at
               OR (policy_row.valid_until IS NOT NULL AND policy_row.valid_until <= checked_at)
               OR policy_row.evidence_ref IS NULL
               OR policy_row.evidence_ref !~ '^https://[^/?#]+([/?#].*)?$'
               OR NOT EXISTS (
                   SELECT 1 FROM public.source_policy_observation AS observation
                    WHERE observation.workspace_id = target_workspace
                      AND observation.source_id = target_source_id
                      AND observation.sequence = policy_row.sequence
                      AND observation.policy_id = policy_row.policy_id
                      AND observation.known_at >= policy_row.created_at
                      AND observation.known_at >= policy_row.reviewed_at
                      AND observation.known_at < checked_at
               ) THEN
                RAISE EXCEPTION 'WP06_SCORER_PAPER_SOURCE_RIGHTS_DENIED';
            END IF;
        END $$
    """)
    op.execute(
        "ALTER FUNCTION public.wp06_require_scorer_paper_source_rights(uuid,text) "
        f"OWNER TO {RIGHTS_ROLE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.wp06_require_scorer_paper_source_rights(uuid,text) "
        "FROM PUBLIC"
    )


def upgrade() -> None:
    """Bổ sung quyền PAPER/SHADOW, không mở mode hoặc thay gate QA/report."""
    _create_paper_source_gate()
    _rewrite(forward=True)


def downgrade() -> None:
    """Khôi phục function lịch sử trước khi xóa helper; không thay ledger deployment."""
    op.execute("LOCK TABLE public.learning_scoring_deployment_event IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.learning_scoring_deployment_event
                    WHERE mode IN ('SHADOW','PAPER_GATE')) THEN
            RAISE EXCEPTION 'WP06_SCORER_PAPER_RIGHTS_DOWNGRADE_ACTIVE_DENIED';
        END IF;
    END $$""")
    _rewrite(forward=False)
    op.execute("DROP FUNCTION public.wp06_require_scorer_paper_source_rights(uuid,text)")
