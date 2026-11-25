"""Probe denial DB của từng chiều SourceUseScope PAPER cho scorer."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, "/e2e-tests/e2e/probes")
import wp06_scorer_job_source_scope_qa as scorer_qa_probe  # noqa: E402

from trading_bot.domain.research.license_scope import (  # noqa: E402
    SourceLicenseScope,
    canonical_scope_json,
    scope_sha256,
)
from trading_bot.domain.research.source_policy import SourceUsePurpose  # noqa: E402

_DENIAL = "WP06_SCORER_JOB_SOURCE_RIGHTS_DENIED"
_SOURCE_ID = "BINANCE_USDM_REST"
_EVIDENCE_BODY = b"synthetic scorer scope mismatch fixture; no market-data rights"


def _scope_variant(dimension: str, bar_open: datetime) -> SourceLicenseScope:
    scope = SourceLicenseScope(
        source_id=_SOURCE_ID,
        provider=_SOURCE_ID,
        product="USDM_FUTURES",
        venue="BINANCE",
        contract="PERPETUAL",
        symbols=("BTCUSDT",),
        intervals=("1h",),
        data_start=bar_open - timedelta(days=30),
        data_end=bar_open + timedelta(days=30),
        allowed_purposes=(SourceUsePurpose.PAPER_DEMO,),
        allowed_outputs=("paper_inference",),
        derived_use=("paper_inference",),
        max_retention_days=None,
        attribution="Fixture tổng hợp chỉ dùng để kiểm thử từ chối.",
    )
    mutations = {
        "source_id": {"source_id": "BINANCE_USDM_REST_OTHER"},
        "provider": {"provider": "OTHER_PROVIDER"},
        "product": {"product": "OTHER_PRODUCT"},
        "venue": {"venue": "OTHER_VENUE"},
        "contract": {"contract": "OTHER_CONTRACT"},
        "symbols": {"symbols": ("ETHUSDT",)},
        "intervals": {"intervals": ("4h",)},
        "data_start": {"data_start": bar_open + timedelta(microseconds=1)},
        "data_end": {
            "data_start": bar_open - timedelta(days=1),
            "data_end": bar_open,
        },
        "purpose": {"allowed_purposes": (SourceUsePurpose.TRAINING,)},
        "outputs": {"allowed_outputs": ("trained_model",)},
        "derived_use": {"derived_use": ("model_training",)},
        "retention": {"max_retention_days": 30},
    }
    return replace(scope, **mutations[dimension])


def _seed_synthetic_policy(engine, workspace_id, scope: SourceLicenseScope) -> None:
    """Seed policy/evidence tổng hợp chỉ để kiểm các nhánh từ chối, không cấp quyền."""
    now = datetime.now(UTC) - timedelta(seconds=3)
    scope_document = canonical_scope_json(scope)
    scope_digest = scope_sha256(scope)
    evidence_ref = "https://fixture.invalid/synthetic-scope-only"
    source_id = _SOURCE_ID
    discovered_id = f"synthetic:{workspace_id}:discovered"
    pending_id = f"synthetic:{workspace_id}:pending"
    verified_id = f"synthetic:{workspace_id}:verified"
    evidence_id = uuid4()
    common = {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "provider": _SOURCE_ID,
        "product": "USDM_FUTURES",
        "venue": "BINANCE",
        "contract": "PERPETUAL",
        "created_at": now,
        "publisher": "Synthetic E2E fixture",
        "endpoint_url": "https://fixture.invalid/source-rights-test",
        "access_method": "REST",
        "auth_scope": "synthetic-test-only",
        "attribution": scope.attribution,
        "retention_ref": "synthetic-test-only",
        "quota_window_seconds": 60,
        "quota_max_requests": 1,
        "quota_max_bytes": 1024,
        "quota_max_cost": 0,
        "quota_cost_unit": "USD",
        "actor_id": "synthetic-e2e-actor",
        "reason": "negative predicate test only; no rights claim",
    }
    purposes = json.dumps(sorted(purpose.value for purpose in scope.allowed_purposes))
    digest = hashlib.sha256(_EVIDENCE_BODY).hexdigest()
    with engine.begin() as connection:
        # Fixture dùng DB Compose dùng một lần; không chạy luồng cấp quyền sản phẩm.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        for sequence, policy_id, state in (
            (1, discovered_id, "discovered"),
            (2, pending_id, "rights_pending"),
            (3, verified_id, "verified"),
        ):
            connection.execute(
                text(
                    "INSERT INTO public.source_policy_revision ("
                    "id,workspace_id,source_id,sequence,policy_id,state,allowed_purposes,"
                    "provider,product,venue,contract,license_scope,license_scope_hash,"
                    "license_evidence_id,license_digest,evidence_ref,reviewed_at,valid_until,"
                    "publisher,endpoint_url,access_method,auth_scope,attribution,retention_ref,"
                    "quota_window_seconds,quota_max_requests,quota_max_bytes,quota_max_cost,"
                    "quota_cost_unit,actor_id,reason,approved_by,created_at) VALUES ("
                    ":id,:workspace_id,:source_id,:sequence,:policy_id,:state,"
                    "CAST(:purposes AS jsonb),:provider,:product,:venue,:contract,"
                    "CAST(:scope AS jsonb),:scope_hash,:evidence_id,:evidence_digest,"
                    ":evidence_ref,:reviewed_at,NULL,:publisher,:endpoint_url,:access_method,"
                    ":auth_scope,:attribution,:retention_ref,:quota_window_seconds,"
                    ":quota_max_requests,:quota_max_bytes,:quota_max_cost,:quota_cost_unit,"
                    ":actor_id,:reason,:approved_by,:created_at)"
                ),
                common
                | {
                    "id": uuid4(),
                    "sequence": sequence,
                    "policy_id": policy_id,
                    "state": state,
                    "purposes": purposes if state != "discovered" else "[]",
                    "scope": scope_document if state != "discovered" else None,
                    "scope_hash": scope_digest if state != "discovered" else None,
                    "evidence_id": evidence_id if state == "verified" else None,
                    "evidence_digest": digest if state == "verified" else None,
                    "evidence_ref": evidence_ref if state != "discovered" else None,
                    "reviewed_at": now if state == "verified" else None,
                    "approved_by": "synthetic-e2e-reviewer" if state == "verified" else None,
                },
            )
            if state == "rights_pending":
                connection.execute(
                    text(
                        "INSERT INTO public.source_license_evidence ("
                        "id,workspace_id,source_id,policy_id,idempotency_key,evidence_ref,"
                        "digest,byte_count,media_type,scope_version,license_scope,"
                        "canonical_scope,scope_hash,actor_id,created_at) VALUES ("
                        ":id,:workspace_id,:source_id,:policy_id,:idempotency_key,:evidence_ref,"
                        ":digest,:byte_count,'text/plain','wp06-license-scope-v1',"
                        "CAST(:scope AS jsonb),:canonical_scope,:scope_hash,:actor_id,:created_at)"
                    ),
                    {
                        "id": evidence_id,
                        "workspace_id": workspace_id,
                        "source_id": source_id,
                        "policy_id": pending_id,
                        "idempotency_key": uuid4(),
                        "evidence_ref": evidence_ref,
                        "digest": digest,
                        "byte_count": len(_EVIDENCE_BODY),
                        "scope": scope_document,
                        "canonical_scope": scope_document,
                        "scope_hash": scope_digest,
                        "actor_id": "synthetic-e2e-uploader",
                        "created_at": now,
                    },
                )
        connection.execute(
            text(
                "INSERT INTO public.source_policy_observation ("
                "workspace_id,source_id,sequence,policy_id,known_at,id) VALUES ("
                ":workspace_id,:source_id,3,:policy_id,:known_at,:id)"
            ),
            {
                "workspace_id": workspace_id,
                "source_id": source_id,
                "policy_id": verified_id,
                "known_at": datetime.now(UTC) - timedelta(seconds=1),
                "id": uuid4(),
            },
        )


def main() -> None:
    engine = scorer_qa_probe._engine()
    results: dict[str, bool] = {}
    try:
        for dimension in (
            "source_id",
            "provider",
            "product",
            "venue",
            "contract",
            "symbols",
            "intervals",
            "data_start",
            "data_end",
            "purpose",
            "outputs",
            "derived_use",
            "retention",
        ):
            workspace_id, bot_id, job_id = scorer_qa_probe._seed_pending_job(
                engine, source="BINANCE_USDM"
            )
            with engine.connect() as connection:
                bar_open = connection.scalar(
                    text(
                        "SELECT bar.open_time FROM public.market_bar AS bar "
                        "JOIN public.bot_decision_job_source_bar AS staged "
                        "ON staged.workspace_id=bar.workspace_id AND staged.market_bar_id=bar.id "
                        "WHERE staged.workspace_id=:workspace_id AND staged.job_id=:job_id"
                    ),
                    {"workspace_id": workspace_id, "job_id": job_id},
                )
            assert isinstance(bar_open, datetime)
            _seed_synthetic_policy(engine, workspace_id, _scope_variant(dimension, bar_open))
            marker: str | None = None
            try:
                with engine.connect() as connection, connection.begin():
                    connection.execute(
                        text("SET LOCAL ROLE crypidian_scorer_deployment_rights_reader")
                    )
                    connection.execute(
                        text("SELECT set_config('app.workspace_id', :value, true)"),
                        {"value": str(workspace_id)},
                    )
                    connection.execute(
                        text("SELECT set_config('app.bot_id', :value, true)"),
                        {"value": str(bot_id)},
                    )
                    connection.execute(
                        text(
                            "SELECT public.wp06_check_scorer_job_source_scope("
                            "CAST(:workspace_id AS uuid),CAST(:job_id AS uuid),false)"
                        ),
                        {"workspace_id": str(workspace_id), "job_id": str(job_id)},
                    ).scalar_one()
            except DBAPIError as error:
                diagnostic = getattr(error.orig, "diag", None)
                marker = getattr(diagnostic, "message_primary", None)
            after = scorer_qa_probe._counts(engine, workspace_id, job_id)
            with engine.connect() as connection:
                receipt_count = int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM public.bot_decision_source_rights_receipt "
                            "WHERE workspace_id=:workspace_id AND job_id=:job_id"
                        ),
                        {"workspace_id": workspace_id, "job_id": job_id},
                    )
                    or 0
                )
            results[dimension] = (
                marker == _DENIAL
                and all(value == 0 for value in after.values())
                and receipt_count == 0
            )

        outcome = {
            "synthetic_fixture_only": True,
            "checked_dimensions": sorted(results),
            "every_scope_near_miss_denied_without_writes": len(results) == 13
            and all(results.values()),
        }
        if not outcome["every_scope_near_miss_denied_without_writes"]:
            raise AssertionError(f"Scorer SourceUseScope mismatch matrix không đạt: {results}")
        print(json.dumps(outcome, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
