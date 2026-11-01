"""DSL thí nghiệm có giới hạn; chỉ phân tích và kiểm tra hợp đồng thuần."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction

DSL_VERSION = "crypidian-learning-dsl-v1"
COMPILER_VERSION = "crypidian-learning-compiler-v1"
MAX_SOURCE_LENGTH = 4096
MAX_TOKEN_COUNT = 256
MAX_AST_NODES = 128
MAX_AST_DEPTH = 32
ALLOWED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


class ExpressionType(StrEnum):
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class DSLDiagnostic:
    """Lỗi phân tích tiếng Việt kèm vị trí ký tự trong biểu thức."""

    position: int
    message_vi: str


class DSLCompileError(ValueError):
    """Lỗi DSL đã được giới hạn, không chạy bất kỳ mã Python nào."""

    def __init__(self, diagnostic: DSLDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.message_vi} (vị trí {diagnostic.position}).")


@dataclass(frozen=True, slots=True)
class FeatureBinding:
    """Đặc trưng đã có, kèm quyền và thời điểm sẵn sàng do caller xác minh."""

    name: str
    timeframe: str
    available_at: datetime
    rights_verified: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError("Tên feature không hợp lệ.")
        if self.timeframe not in ALLOWED_TIMEFRAMES:
            raise ValueError("Timeframe không nằm trong allowlist.")
        if (
            not isinstance(self.available_at, datetime)
            or self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
        ):
            raise ValueError("available_at phải có múi giờ.")
        if type(self.rights_verified) is not bool:
            raise ValueError("rights_verified chỉ nhận bool.")


@dataclass(frozen=True, slots=True)
class CompiledExpression:
    """AST canonical và lineage compile; contract này không có evaluator."""

    dsl_version: str
    compiler_version: str
    canonical_ast: str
    expression_hash: str
    feature_refs: tuple[tuple[str, str], ...]
    result_type: ExpressionType


@dataclass(frozen=True, slots=True)
class HypothesisSpecification:
    """Mô tả giả thuyết đủ thông tin để review, chưa thể tạo lệnh."""

    hypothesis_id: str
    statement_vi: str
    strategy_id: str
    universe: tuple[str, ...]
    horizon_bars: int
    entry_expression: CompiledExpression
    exit_recipe_id: str
    baseline_id: str
    objective_vi: str


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    position: int


@dataclass(frozen=True, slots=True)
class _Node:
    kind: str
    value: str
    children: tuple[_Node, ...]
    position: int
    result_type: ExpressionType

    def canonical(self) -> dict[str, object]:
        if self.kind == "number":
            return {"type": "number", "value": self.value}
        if self.kind == "feature":
            name, timeframe = self.value.split("@", maxsplit=1)
            return {"type": "feature", "name": name, "timeframe": timeframe}
        return {
            "type": self.kind,
            "children": [child.canonical() for child in self.children],
        }

    @property
    def depth(self) -> int:
        return 1 + max((child.depth for child in self.children), default=0)


_TOKEN_PATTERN = re.compile(
    r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+))|"
    r"(?P<feature>[a-z][a-z0-9_]*@[0-9]+[mhd])|"
    r"(?P<identifier>[a-z][a-z0-9_]*)|"
    r"(?P<operator>>=|<=|==|!=|[()+\-*/><]))"
)
_FEATURE_NAME = re.compile(r"[a-z][a-z0-9_]*")
_HYPOTHESIS_ID = re.compile(r"[a-z][a-z0-9_]{1,63}")
_STRATEGY_ID = re.compile(r"[A-Z][A-Z0-9_]{1,31}")
_SYMBOL = re.compile(r"[A-Z0-9]{2,30}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")
    return value.astimezone(UTC)


def _tokenize(source: str) -> tuple[_Token, ...]:
    if len(source) > MAX_SOURCE_LENGTH:
        raise DSLCompileError(DSLDiagnostic(MAX_SOURCE_LENGTH, "Biểu thức vượt giới hạn độ dài"))
    tokens: list[_Token] = []
    position = 0
    while position < len(source):
        match = _TOKEN_PATTERN.match(source, position)
        if match is None:
            if source[position:].strip() == "":
                break
            raise DSLCompileError(DSLDiagnostic(position, "Ký tự hoặc cú pháp không được hỗ trợ"))
        kind = next(name for name, value in match.groupdict().items() if value is not None)
        value = match.group(kind)
        token_position = match.start(kind)
        if kind == "identifier" and value not in {"and", "or", "not"}:
            raise DSLCompileError(
                DSLDiagnostic(token_position, "Từ khóa hoặc feature không hợp lệ")
            )
        if kind == "feature":
            name, timeframe = value.rsplit("@", maxsplit=1)
            if not _FEATURE_NAME.fullmatch(name) or timeframe not in ALLOWED_TIMEFRAMES:
                raise DSLCompileError(
                    DSLDiagnostic(token_position, "Feature hoặc timeframe ngoài allowlist")
                )
        tokens.append(_Token(kind, value, token_position))
        if len(tokens) > MAX_TOKEN_COUNT:
            raise DSLCompileError(DSLDiagnostic(token_position, "Biểu thức vượt giới hạn số token"))
        position = match.end()
    tokens.append(_Token("eof", "", len(source)))
    return tuple(tokens)


class _Parser:
    def __init__(self, tokens: tuple[_Token, ...]) -> None:
        self._tokens = tokens
        self._index = 0
        self._node_count = 0

    def _current(self) -> _Token:
        return self._tokens[self._index]

    def _accept(self, value: str) -> _Token | None:
        if self._current().value == value:
            token = self._current()
            self._index += 1
            return token
        return None

    def _expect(self, value: str) -> _Token:
        token = self._accept(value)
        if token is None:
            current = self._current()
            raise DSLCompileError(
                DSLDiagnostic(current.position, f"Thiếu toán tử hoặc dấu '{value}'")
            )
        return token

    def _node(
        self,
        kind: str,
        value: str,
        children: tuple[_Node, ...],
        position: int,
        result_type: ExpressionType,
        depth: int,
    ) -> _Node:
        self._node_count += 1
        if self._node_count > MAX_AST_NODES:
            raise DSLCompileError(DSLDiagnostic(position, "AST vượt giới hạn số node"))
        if max(depth, 1 + max((child.depth for child in children), default=0)) > MAX_AST_DEPTH:
            raise DSLCompileError(DSLDiagnostic(position, "AST vượt giới hạn độ sâu"))
        return _Node(kind, value, children, position, result_type)

    def parse(self) -> _Node:
        node = self._parse_or(1)
        if self._current().kind != "eof":
            token = self._current()
            raise DSLCompileError(DSLDiagnostic(token.position, "Còn cú pháp thừa sau biểu thức"))
        if node.result_type is not ExpressionType.BOOLEAN:
            raise DSLCompileError(
                DSLDiagnostic(node.position, "Biểu thức gốc phải có kiểu boolean")
            )
        return node

    def _parse_or(self, depth: int) -> _Node:
        node = self._parse_and(depth)
        while token := self._accept("or"):
            right = self._parse_and(depth + 1)
            node = self._boolean_binary("or", node, right, token.position, depth)
        return node

    def _parse_and(self, depth: int) -> _Node:
        node = self._parse_not(depth)
        while token := self._accept("and"):
            right = self._parse_not(depth + 1)
            node = self._boolean_binary("and", node, right, token.position, depth)
        return node

    def _parse_not(self, depth: int) -> _Node:
        token = self._accept("not")
        if token is not None:
            child = self._parse_not(depth + 1)
            if child.result_type is not ExpressionType.BOOLEAN:
                raise DSLCompileError(
                    DSLDiagnostic(token.position, "Toán tử not yêu cầu kiểu boolean")
                )
            return self._node("not", "not", (child,), token.position, ExpressionType.BOOLEAN, depth)
        return self._parse_comparison(depth)

    def _parse_comparison(self, depth: int) -> _Node:
        node = self._parse_sum(depth)
        if self._current().value in {">", ">=", "<", "<=", "==", "!="}:
            token = self._current()
            self._index += 1
            right = self._parse_sum(depth)
            if (
                node.result_type is not ExpressionType.NUMBER
                or right.result_type is not ExpressionType.NUMBER
            ):
                raise DSLCompileError(DSLDiagnostic(token.position, "So sánh chỉ nhận giá trị số"))
            if self._current().value in {">", ">=", "<", "<=", "==", "!="}:
                raise DSLCompileError(
                    DSLDiagnostic(self._current().position, "Không hỗ trợ so sánh nối chuỗi")
                )
            return self._node(
                "compare:" + token.value,
                token.value,
                (node, right),
                token.position,
                ExpressionType.BOOLEAN,
                depth,
            )
        return node

    def _parse_sum(self, depth: int) -> _Node:
        node = self._parse_product(depth)
        while self._current().value in {"+", "-"}:
            token = self._current()
            self._index += 1
            right = self._parse_product(depth)
            node = self._numeric_binary(token, node, right, depth)
        return node

    def _parse_product(self, depth: int) -> _Node:
        node = self._parse_unary(depth)
        while self._current().value in {"*", "/"}:
            token = self._current()
            self._index += 1
            right = self._parse_unary(depth)
            node = self._numeric_binary(token, node, right, depth)
        return node

    def _parse_unary(self, depth: int) -> _Node:
        token = self._accept("+") or self._accept("-")
        if token is not None:
            child = self._parse_unary(depth + 1)
            if child.result_type is not ExpressionType.NUMBER:
                raise DSLCompileError(
                    DSLDiagnostic(token.position, "Toán tử dấu yêu cầu giá trị số")
                )
            return self._node(
                "unary:" + token.value,
                token.value,
                (child,),
                token.position,
                ExpressionType.NUMBER,
                depth,
            )
        return self._parse_primary(depth)

    def _parse_primary(self, depth: int) -> _Node:
        token = self._current()
        if token.kind == "number":
            self._index += 1
            try:
                number = Decimal(token.value)
            except InvalidOperation as error:
                raise DSLCompileError(
                    DSLDiagnostic(token.position, "Literal số không hợp lệ")
                ) from error
            if not number.is_finite():
                raise DSLCompileError(DSLDiagnostic(token.position, "Literal số phải hữu hạn"))
            normalized = format(number, "f")
            if "." in normalized:
                normalized = normalized.rstrip("0").rstrip(".")
            return self._node(
                "number", normalized, (), token.position, ExpressionType.NUMBER, depth
            )
        if token.kind == "feature":
            self._index += 1
            return self._node(
                "feature", token.value, (), token.position, ExpressionType.NUMBER, depth
            )
        if self._accept("("):
            if depth + 1 > MAX_AST_DEPTH:
                raise DSLCompileError(DSLDiagnostic(token.position, "AST vượt giới hạn độ sâu"))
            node = self._parse_or(depth + 1)
            self._expect(")")
            return node
        raise DSLCompileError(
            DSLDiagnostic(token.position, "Cần literal số, feature hoặc biểu thức trong ngoặc")
        )

    def _numeric_binary(self, token: _Token, left: _Node, right: _Node, depth: int) -> _Node:
        if (
            left.result_type is not ExpressionType.NUMBER
            or right.result_type is not ExpressionType.NUMBER
        ):
            raise DSLCompileError(
                DSLDiagnostic(token.position, "Toán tử số học chỉ nhận giá trị số")
            )
        if token.value == "/" and _constant_value(right) == Decimal(0):
            raise DSLCompileError(DSLDiagnostic(token.position, "Không cho phép chia cho 0"))
        return self._node(
            "arithmetic:" + token.value,
            token.value,
            (left, right),
            token.position,
            ExpressionType.NUMBER,
            depth,
        )

    def _boolean_binary(
        self, operator: str, left: _Node, right: _Node, position: int, depth: int
    ) -> _Node:
        if (
            left.result_type is not ExpressionType.BOOLEAN
            or right.result_type is not ExpressionType.BOOLEAN
        ):
            raise DSLCompileError(
                DSLDiagnostic(position, f"Toán tử {operator} chỉ nhận biểu thức boolean")
            )
        return self._node(
            "boolean:" + operator, operator, (left, right), position, ExpressionType.BOOLEAN, depth
        )


def compile_expression(
    source: str,
    *,
    features: tuple[FeatureBinding, ...],
    as_of: datetime,
) -> CompiledExpression:
    """Phân tích và kiểm tra feature; caller chịu trách nhiệm cung cấp bằng chứng đúng."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Biểu thức không được để trống.")
    cutoff = _utc(as_of, "as_of")
    bindings: dict[tuple[str, str], FeatureBinding] = {}
    for binding in features:
        if not isinstance(binding, FeatureBinding):
            raise ValueError("Feature bindings phải đúng kiểu FeatureBinding.")
        key = binding.name, binding.timeframe
        if key in bindings:
            raise ValueError("Không được lặp feature binding.")
        bindings[key] = binding

    root = _Parser(_tokenize(source)).parse()
    refs = tuple(sorted(_feature_refs(root)))
    for name, timeframe in refs:
        feature_binding = bindings.get((name, timeframe))
        node = _find_feature(root, name, timeframe)
        assert node is not None
        if feature_binding is None:
            raise DSLCompileError(
                DSLDiagnostic(
                    node.position, f"Đặc trưng '{name}@{timeframe}' không nằm trong allowlist"
                )
            )
        if not feature_binding.rights_verified:
            raise DSLCompileError(
                DSLDiagnostic(
                    node.position,
                    f"Quyền sử dụng đặc trưng '{name}@{timeframe}' chưa được xác minh",
                )
            )
        if _utc(feature_binding.available_at, "feature.available_at") > cutoff:
            raise DSLCompileError(
                DSLDiagnostic(
                    node.position, f"Đặc trưng '{name}@{timeframe}' chưa khả dụng tại as_of"
                )
            )

    canonical_ast = json.dumps(root.canonical(), sort_keys=True, separators=(",", ":"))
    hash_input = f"{DSL_VERSION}\n{COMPILER_VERSION}\n{canonical_ast}".encode()
    return CompiledExpression(
        dsl_version=DSL_VERSION,
        compiler_version=COMPILER_VERSION,
        canonical_ast=canonical_ast,
        expression_hash=hashlib.sha256(hash_input).hexdigest(),
        feature_refs=refs,
        result_type=root.result_type,
    )


def _feature_refs(node: _Node) -> set[tuple[str, str]]:
    if node.kind == "feature":
        name, timeframe = node.value.split("@", maxsplit=1)
        return {(name, timeframe)}
    refs: set[tuple[str, str]] = set()
    for child in node.children:
        refs.update(_feature_refs(child))
    return refs


def _constant_value(node: _Node) -> Fraction | None:
    if node.kind == "number":
        return Fraction(Decimal(node.value))
    if node.kind in {"unary:+", "unary:-"}:
        value = _constant_value(node.children[0])
        if value is None:
            return None
        return value if node.value == "+" else -value
    if node.kind.startswith("arithmetic:"):
        left = _constant_value(node.children[0])
        right = _constant_value(node.children[1])
        if left is None or right is None:
            return None
        operator = node.value
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/" and right != 0:
            return left / right
    return None


def _find_feature(node: _Node, name: str, timeframe: str) -> _Node | None:
    if node.kind == "feature" and node.value == f"{name}@{timeframe}":
        return node
    for child in node.children:
        match = _find_feature(child, name, timeframe)
        if match is not None:
            return match
    return None


def validate_hypothesis(
    specification: HypothesisSpecification,
    *,
    strategy_ids: set[str] | frozenset[str],
    exit_recipe_ids: set[str] | frozenset[str],
    baseline_ids: set[str] | frozenset[str],
) -> None:
    """Fail-closed kiểm tra giả thuyết với allowlist hiện hành."""
    if not isinstance(specification, HypothesisSpecification):
        raise ValueError("specification phải là HypothesisSpecification.")
    if not _HYPOTHESIS_ID.fullmatch(specification.hypothesis_id):
        raise ValueError("hypothesis_id không đúng định dạng identifier tiếng Anh.")
    if not specification.statement_vi.strip() or not specification.objective_vi.strip():
        raise ValueError("Giả thuyết và mục tiêu tiếng Việt không được để trống.")
    if len(specification.statement_vi) > 1000 or len(specification.objective_vi) > 1000:
        raise ValueError("Mô tả giả thuyết và mục tiêu vượt giới hạn độ dài.")
    if not _STRATEGY_ID.fullmatch(specification.strategy_id):
        raise ValueError("strategy_id không hợp lệ.")
    if specification.strategy_id not in strategy_ids:
        raise ValueError("strategy_id không nằm trong allowlist.")
    if not specification.universe or len(specification.universe) != len(
        set(specification.universe)
    ):
        raise ValueError("Universe phải không rỗng và không lặp symbol.")
    if any(not _SYMBOL.fullmatch(symbol) for symbol in specification.universe):
        raise ValueError("Universe chứa symbol không hợp lệ.")
    if type(specification.horizon_bars) is not int or specification.horizon_bars <= 0:
        raise ValueError("horizon_bars phải là số nguyên dương.")
    if specification.horizon_bars > 100_000:
        raise ValueError("horizon_bars vượt giới hạn cho phép.")
    if specification.exit_recipe_id not in exit_recipe_ids:
        raise ValueError("exit_recipe_id không nằm trong allowlist.")
    if specification.baseline_id not in baseline_ids:
        raise ValueError("baseline_id không nằm trong allowlist.")
    if not isinstance(specification.entry_expression, CompiledExpression):
        raise ValueError("entry_expression phải được compile trước.")
    expression = specification.entry_expression
    if (
        expression.dsl_version != DSL_VERSION
        or expression.compiler_version != COMPILER_VERSION
        or expression.result_type is not ExpressionType.BOOLEAN
        or len(expression.canonical_ast) > MAX_SOURCE_LENGTH * 8
        or not _SHA256.fullmatch(expression.expression_hash)
    ):
        raise ValueError("entry_expression không đúng phiên bản hoặc contract.")
    expected_hash = hashlib.sha256(
        f"{DSL_VERSION}\n{COMPILER_VERSION}\n{expression.canonical_ast}".encode()
    ).hexdigest()
    if expression.expression_hash != expected_hash:
        raise ValueError("entry_expression hash không khớp AST canonical.")
