"""FX-3 lát 3b: bundle replay schema 2 mang chuỗi funding đã chốt (CRY-198).

Owner chọn (17/11/2026) đưa funding lịch sử vào bundle để replay tái lập được. Bundle có funding
dùng ``schema_version = 2`` (thêm khóa ``funding`` trong JSON canonical, nằm trong ``bundle_hash``);
bundle không funding giữ schema 1 và bytes cũ. Nới CHECK schema và cấp ``SELECT market_funding`` cho
``crypidian_backtest_worker`` (worker chỉ có grant trực tiếp, cần đọc lại funding của bundle
schema 2; RLS workspace của bảng giữ nguyên).
Ghi chú: trigger WP-06 cho bundle TRAINING vẫn đòi đúng 9 khóa, nên bundle TRAINING tiếp tục là
schema 1 (funding chỉ dùng cho backtest nghiên cứu).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8f1b3a5c7e9"
down_revision: str | None = "f1b8e93fa47f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "backtest_dataset_replay_bundle"
_CHECK = "ck_backtest_dataset_replay_bundle_backtest_replay_bundle_schema_valid"
_WORKER = "crypidian_backtest_worker"


def upgrade() -> None:
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, "schema_version IN (1, 2)")
    op.execute(f"GRANT SELECT ON TABLE public.market_funding TO {_WORKER}")


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.backtest_dataset_replay_bundle
                       WHERE schema_version = 2) THEN
                RAISE EXCEPTION 'FX3_REPLAY_BUNDLE_FUNDING_DOWNGRADE_BLOCKED';
            END IF;
        END $$"""
    )
    op.execute(f"REVOKE SELECT ON TABLE public.market_funding FROM {_WORKER}")
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, "schema_version = 1")
