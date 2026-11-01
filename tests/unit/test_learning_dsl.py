from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.domain.learning.dsl import (
    COMPILER_VERSION,
    DSL_VERSION,
    DSLCompileError,
    FeatureBinding,
    HypothesisSpecification,
    compile_expression,
    validate_hypothesis,
)

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
FEATURES = (
    FeatureBinding("ema_20", "1h", AS_OF - timedelta(seconds=1), True),
    FeatureBinding("adx_14", "1h", AS_OF, True),
)


def _compile(source: str):
    return compile_expression(source, features=FEATURES, as_of=AS_OF)


def test_compile_expression_returns_typed_canonical_hash_and_feature_refs() -> None:
    result = _compile("ema_20@1h > 100 and adx_14@1h >= 20")

    assert result.dsl_version == DSL_VERSION
    assert result.compiler_version == COMPILER_VERSION
    assert len(result.expression_hash) == 64
    assert result.feature_refs == (("adx_14", "1h"), ("ema_20", "1h"))
    assert result.result_type == "boolean"


def test_equivalent_whitespace_and_number_forms_have_same_canonical_hash() -> None:
    first = _compile("ema_20@1h>100.0 and adx_14@1h>=20")
    second = _compile("( ema_20@1h > 100 and adx_14@1h >= 20.00 )")

    assert first.canonical_ast == second.canonical_ast
    assert first.expression_hash == second.expression_hash


def test_canonical_hash_preserves_operator_semantics() -> None:
    greater = _compile("ema_20@1h > 100")
    less = _compile("ema_20@1h < 100")
    assert greater.canonical_ast != less.canonical_ast
    assert greater.expression_hash != less.expression_hash


def test_numeric_canonicalization_does_not_round_large_decimal_literals() -> None:
    large_literal = "123456789012345678901234567890123456789"
    result = _compile(f"ema_20@1h > {large_literal}")

    assert large_literal in result.canonical_ast


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os')",
        "ema_20@1h.__class__ > 0",
        "open('/tmp/file')",
        "while True",
        "ema_20@1h > 0; import os",
        "ema_20@1m > 0",
    ],
)
def test_rejects_python_syntax_calls_unknown_timeframes_and_statements(source: str) -> None:
    with pytest.raises(DSLCompileError):
        _compile(source)


def test_rejects_unknown_feature_and_unknown_rights() -> None:
    with pytest.raises(ValueError, match="Đặc trưng"):
        compile_expression(
            "unknown_3@1h > 0",
            features=FEATURES,
            as_of=AS_OF,
        )
    denied = FeatureBinding("ema_20", "1h", AS_OF, False)
    with pytest.raises(ValueError, match="Quyền"):
        compile_expression("ema_20@1h > 0", features=(denied,), as_of=AS_OF)


def test_rejects_feature_published_after_as_of_and_naive_as_of() -> None:
    future = FeatureBinding("ema_20", "1h", AS_OF + timedelta(seconds=1), True)
    with pytest.raises(ValueError, match="khả dụng"):
        compile_expression("ema_20@1h > 0", features=(future,), as_of=AS_OF)
    with pytest.raises(ValueError, match="múi giờ"):
        compile_expression("ema_20@1h > 0", features=FEATURES, as_of=AS_OF.replace(tzinfo=None))


def test_rejects_type_errors_non_boolean_root_and_resource_limits() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _compile("ema_20@1h and adx_14@1h")
    with pytest.raises(ValueError, match="boolean"):
        _compile("ema_20@1h + 1")
    with pytest.raises(ValueError, match="độ dài"):
        compile_expression(" " + "1" * 5000, features=FEATURES, as_of=AS_OF)
    with pytest.raises(ValueError, match="độ sâu"):
        _compile("not " * 100 + "ema_20@1h > 0")
    with pytest.raises(ValueError, match="chia cho 0"):
        _compile("ema_20@1h / 0 > 1")
    with pytest.raises(ValueError, match="chia cho 0"):
        _compile("ema_20@1h / (1 - 1) > 1")


def test_hypothesis_contract_requires_complete_allowlisted_specification() -> None:
    spec = HypothesisSpecification(
        hypothesis_id="ema_trend_v1",
        statement_vi="EMA ngắn hạn cắt lên EMA dài hạn khi xu hướng đủ mạnh.",
        strategy_id="H01",
        universe=("BTCUSDT", "ETHUSDT"),
        horizon_bars=24,
        entry_expression=_compile("ema_20@1h > 100 and adx_14@1h >= 20"),
        exit_recipe_id="h01_atr_exit_v1",
        baseline_id="buy_and_hold_v1",
        objective_vi="Đánh giá lợi nhuận sau chi phí và mức sụt giảm.",
    )
    validate_hypothesis(
        spec,
        strategy_ids={"H01"},
        exit_recipe_ids={"h01_atr_exit_v1"},
        baseline_ids={"buy_and_hold_v1"},
    )

    with pytest.raises(ValueError, match="allowlist"):
        validate_hypothesis(
            spec,
            strategy_ids={"H03"},
            exit_recipe_ids={"h01_atr_exit_v1"},
            baseline_ids={"buy_and_hold_v1"},
        )


def test_hypothesis_rejects_duplicate_universe_and_incomplete_text() -> None:
    spec = HypothesisSpecification(
        hypothesis_id="ema_trend_v1",
        statement_vi="Giả thuyết",
        strategy_id="H01",
        universe=("BTCUSDT", "BTCUSDT"),
        horizon_bars=0,
        entry_expression=_compile("ema_20@1h > 100"),
        exit_recipe_id="exit",
        baseline_id="baseline",
        objective_vi="Mục tiêu",
    )
    with pytest.raises(ValueError, match="Universe"):
        validate_hypothesis(
            spec,
            strategy_ids={"H01"},
            exit_recipe_ids={"exit"},
            baseline_ids={"baseline"},
        )


def test_hypothesis_rejects_tampered_compiled_expression_hash() -> None:
    expression = _compile("ema_20@1h > 100")
    tampered = expression.__class__(
        dsl_version=expression.dsl_version,
        compiler_version=expression.compiler_version,
        canonical_ast=expression.canonical_ast,
        expression_hash="0" * 64,
        feature_refs=expression.feature_refs,
        result_type=expression.result_type,
    )
    spec = HypothesisSpecification(
        hypothesis_id="ema_trend_v1",
        statement_vi="Giả thuyết kiểm tra EMA.",
        strategy_id="H01",
        universe=("BTCUSDT",),
        horizon_bars=24,
        entry_expression=tampered,
        exit_recipe_id="exit",
        baseline_id="baseline",
        objective_vi="So sánh kết quả sau chi phí.",
    )
    with pytest.raises(ValueError, match="hash"):
        validate_hypothesis(
            spec,
            strategy_ids={"H01"},
            exit_recipe_ids={"exit"},
            baseline_ids={"baseline"},
        )
