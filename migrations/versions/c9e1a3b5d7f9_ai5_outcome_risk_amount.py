"""AI-5 (CRY-236): outcome replay mang khóa tùy chọn `risk_amount` (1R) cho nhãn dải R.

Revision ID: c9e1a3b5d7f9
Revises: a5c7e9b1d3f5

Owner chọn nhãn "Small R band" (17/11/2026): lớp 0 khi |net PnL / 1R| ≤ 0,1. Outcome replay chưa
lưu 1R nên trigger `wp06_validate_outcome_opportunity` (b2d4f6a8c0e1) khóa cứng tập khóa. Bản vá
cho phép thêm đúng một khóa `risk_amount` — chuỗi số thập phân dương — vào mọi dạng outcome.
Outcome cũ (không có khóa) giữ nguyên hash; canonical JSON SQL đã tổng quát theo khóa.

Rewrite ghim hash định nghĩa trước/sau (đo trên PostgreSQL thật); downgrade từ chối nếu đã có
outcome mang `risk_amount`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9e1a3b5d7f9"
down_revision: str | None = "a5c7e9b1d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "public.wp06_validate_outcome_opportunity()"
PREIMAGE = "f6cbad87816564cf0d6877c5458f19618a45a26f3ca0dc86468b47cd9231bfc8"
POSTIMAGE = "febd182b8c43a63a6bfb8cf55ee4d18c488070c333ab79605b5fa9154bce680b"

BEFORE = "          SELECT count(*) INTO actual_key_count FROM jsonb_object_keys(NEW.outcome);\n"
AFTER = (
    "          IF NEW.outcome ? 'risk_amount' THEN\n"
    "            IF jsonb_typeof(NEW.outcome->'risk_amount')<>'string'\n"
    "               OR NEW.outcome->>'risk_amount'\n"
    "                    !~ '^[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?$'\n"
    "               OR (NEW.outcome->>'risk_amount')::numeric<=0 THEN\n"
    "              RAISE EXCEPTION 'AI5_OUTCOME_RISK_AMOUNT_INVALID'; END IF;\n"
    "            expected_keys := array_append(expected_keys,'risk_amount');\n"
    "          END IF;\n" + BEFORE
)


def _hash_check(expected: str, exception: str) -> str:
    if expected == "PENDING":
        return ""
    return f"""
            IF pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.pg_get_functiondef('{SIGNATURE}'::regprocedure), 'UTF8')), 'hex')
                    IS DISTINCT FROM '{expected}' THEN
                RAISE EXCEPTION '{exception}';
            END IF;"""


def _rewrite(*, forward: bool) -> None:
    previous, replacement = (BEFORE, AFTER) if forward else (AFTER, BEFORE)
    before_hash, after_hash = (PREIMAGE, POSTIMAGE) if forward else (POSTIMAGE, PREIMAGE)
    for marker in ("$p$", "$r$"):
        if marker in previous or marker in replacement:
            raise ValueError("Patch chứa dollar tag.")
    op.execute(f"""DO $migration$
        DECLARE
            v_definition text;
        BEGIN
            {_hash_check(before_hash, "AI5_OUTCOME_RISK_PREIMAGE_UNEXPECTED")}
            v_definition := pg_catalog.pg_get_functiondef('{SIGNATURE}'::regprocedure);
            IF (length(v_definition) - length(replace(v_definition, $p${previous}$p$, '')))
                / length($p${previous}$p$) <> 1 THEN
                RAISE EXCEPTION 'AI5_OUTCOME_RISK_DEFINITION_UNEXPECTED';
            END IF;
            v_definition := replace(v_definition, $p${previous}$p$, $r${replacement}$r$);
            EXECUTE v_definition;
            {_hash_check(after_hash, "AI5_OUTCOME_RISK_POSTIMAGE_UNEXPECTED")}
        END $migration$""")


def upgrade() -> None:
    _rewrite(forward=True)


def downgrade() -> None:
    op.execute("""DO $migration$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.backtest_trade_outcome WHERE outcome ? 'risk_amount')
            THEN
                RAISE EXCEPTION 'AI5_OUTCOME_RISK_DOWNGRADE_HAS_DATA';
            END IF;
        END $migration$""")
    _rewrite(forward=False)
