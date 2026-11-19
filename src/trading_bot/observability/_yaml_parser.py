"""Trình phân tích YAML thuần Python tối giản cho Compose và GitHub Actions workflow."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

_INT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)\.[0-9]+$")
_TAG_PREFIX_RE = re.compile(r"^![A-Za-z0-9_.:-]+\s*(.*)$")


@dataclass(frozen=True, slots=True)
class _YamlLine:
    line_no: int
    indent: int
    text: str


def _strip_inline_comment(text: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if index == 0 or text[index - 1].isspace():
                return text[:index].rstrip()
    return text.rstrip()


def _tokenize_lines(source: str) -> list[_YamlLine]:
    tokens: list[_YamlLine] = []
    for line_no, raw in enumerate(source.splitlines(), start=1):
        stripped_end = raw.rstrip()
        if not stripped_end.strip():
            continue
        indent = len(stripped_end) - len(stripped_end.lstrip(" "))
        if "\t" in stripped_end[:indent]:
            raise ValueError(f"YAML dòng {line_no} dùng tab để thụt lề.")
        body = _strip_inline_comment(stripped_end[indent:])
        if not body:
            continue
        tokens.append(_YamlLine(line_no=line_no, indent=indent, text=body))
    return tokens


def _split_top_level_key_value(text: str) -> tuple[str, str] | None:
    in_single = False
    in_double = False
    escaped = False
    bracket_depth = 0
    for index, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double:
            if ch in "[{":
                bracket_depth += 1
            elif ch in "]}":
                bracket_depth = max(0, bracket_depth - 1)
            elif ch == ":" and bracket_depth == 0:
                if index + 1 == len(text) or text[index + 1].isspace():
                    key = text[:index].strip()
                    rest = text[index + 1 :].strip()
                    return key, rest
    return None


def _split_flow_items(inner: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    depth = 0
    for ch in inner:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_double and ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                items.append("".join(buf).strip())
                buf = []
                continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _unquote(text: str) -> str:
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        inner = text[1:-1]
        return (
            inner.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )
    return text


def _parse_scalar(text: str, anchors: dict[str, Any]) -> Any:
    raw = text.strip()
    tag_match = _TAG_PREFIX_RE.match(raw)
    if tag_match:
        raw = tag_match.group(1).strip()
    if raw.startswith("*"):
        alias = raw[1:].strip()
        if alias not in anchors:
            raise ValueError(f"Không tìm thấy anchor YAML '*{alias}'.")
        return copy.deepcopy(anchors[alias])
    if raw in {"", "null", "Null", "NULL", "~"}:
        return None
    if raw in {"true", "True", "TRUE"}:
        return True
    if raw in {"false", "False", "FALSE"}:
        return False
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        return _unquote(raw)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item, anchors) for item in _split_flow_items(inner)]
    if raw.startswith("{") and raw.endswith("}"):
        inner = raw[1:-1].strip()
        if not inner:
            return {}
        mapping: dict[str, Any] = {}
        for item in _split_flow_items(inner):
            kv = _split_top_level_key_value(item)
            if kv is None:
                raise ValueError(f"Flow mapping YAML không hợp lệ: {item}")
            mapping[_unquote(kv[0])] = _parse_scalar(kv[1], anchors)
        return mapping
    if _INT_RE.fullmatch(raw):
        return int(raw)
    if _FLOAT_RE.fullmatch(raw):
        return float(raw)
    return raw


def _extract_anchor(value_text: str) -> tuple[str | None, str]:
    stripped = value_text.strip()
    if stripped.startswith("&"):
        parts = stripped.split(None, 1)
        anchor = parts[0][1:]
        remainder = parts[1].strip() if len(parts) > 1 else ""
        return anchor, remainder
    return None, stripped


def _read_block_scalar(
    raw_lines: list[str],
    start_line_no: int,
    parent_indent: int,
    indicator: str,
) -> tuple[str, int]:
    collected: list[str] = []
    block_indent: int | None = None
    idx = start_line_no  # 1-based line_no -> index in raw_lines is start_line_no
    while idx < len(raw_lines):
        raw = raw_lines[idx].rstrip("\r\n")
        if not raw.strip():
            if block_indent is not None:
                collected.append("")
            idx += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = indent
        collected.append(raw[block_indent:] if len(raw) >= block_indent else "")
        idx += 1
    while collected and collected[-1] == "":
        collected.pop()
    if indicator.startswith(">"):
        text = " ".join(line.strip() if line else "\n" for line in collected)
    else:
        text = "\n".join(collected)
    if not indicator.endswith("-") and text:
        text += "\n"
    return text, idx


class _Parser:
    def __init__(self, source: str) -> None:
        self._raw_lines = source.splitlines()
        self._tokens = _tokenize_lines(source)
        self._anchors: dict[str, Any] = {}
        self._pos = 0

    def parse(self) -> Any:
        if not self._tokens:
            return {}
        return self._parse_node(self._tokens[0].indent)

    def _skip_before_line(self, next_raw_index: int) -> None:
        while self._pos < len(self._tokens) and self._tokens[self._pos].line_no <= next_raw_index:
            self._pos += 1

    def _parse_node(self, expected_indent: int) -> Any:
        if self._pos >= len(self._tokens):
            return None
        tok = self._tokens[self._pos]
        if tok.indent < expected_indent:
            return None
        if tok.text == "-" or tok.text.startswith("- "):
            return self._parse_sequence(tok.indent)
        return self._parse_mapping(tok.indent)

    def _parse_sequence(self, seq_indent: int) -> list[Any]:
        items: list[Any] = []
        while self._pos < len(self._tokens):
            tok = self._tokens[self._pos]
            if tok.indent != seq_indent or not (tok.text == "-" or tok.text.startswith("- ")):
                break
            after_dash = tok.text[1:].strip()
            line_no = tok.line_no
            self._pos += 1
            if not after_dash:
                if self._pos < len(self._tokens) and self._tokens[self._pos].indent > seq_indent:
                    items.append(self._parse_node(self._tokens[self._pos].indent))
                else:
                    items.append(None)
                continue

            anchor, value_part = _extract_anchor(after_dash)
            kv = _split_top_level_key_value(value_part)
            if kv is not None:
                # Sequence element is an inline/block mapping starting on the `- key: val` line.
                item_indent = seq_indent + 2
                first_key = _unquote(kv[0])
                first_val = self._resolve_value(kv[1], line_no, seq_indent)
                mapping: dict[str, Any] = {}
                if first_key == "<<":
                    if isinstance(first_val, dict):
                        mapping.update(first_val)
                else:
                    mapping[first_key] = first_val
                if self._pos < len(self._tokens) and self._tokens[self._pos].indent >= item_indent:
                    extra = self._parse_mapping(self._tokens[self._pos].indent)
                    for k, v in extra.items():
                        mapping[k] = v
                if anchor:
                    self._anchors[anchor] = mapping
                items.append(mapping)
                continue

            if value_part in {"|", "|-", ">", ">-"}:
                scalar, next_raw = _read_block_scalar(
                    self._raw_lines, line_no, seq_indent, value_part
                )
                self._skip_before_line(next_raw)
                val: Any = scalar
            else:
                val = _parse_scalar(value_part, self._anchors)
            if anchor:
                self._anchors[anchor] = val
            items.append(val)
        return items

    def _parse_mapping(self, map_indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self._pos < len(self._tokens):
            tok = self._tokens[self._pos]
            if tok.indent != map_indent or tok.text == "-" or tok.text.startswith("- "):
                break
            kv = _split_top_level_key_value(tok.text)
            if kv is None:
                raise ValueError(f"YAML dòng {tok.line_no} không hợp lệ: {tok.text}")
            key = _unquote(kv[0])
            raw_val = kv[1]
            line_no = tok.line_no
            self._pos += 1

            anchor, clean_val = _extract_anchor(raw_val)
            value = self._resolve_value(clean_val, line_no, map_indent)
            if anchor:
                self._anchors[anchor] = value

            if key == "<<":
                sources = value if isinstance(value, list) else [value]
                for src in sources:
                    if isinstance(src, dict):
                        for mk, mv in src.items():
                            result.setdefault(mk, copy.deepcopy(mv))
            else:
                result[key] = value
        return result

    def _resolve_value(self, clean_val: str, line_no: int, parent_indent: int) -> Any:
        if clean_val in {"|", "|-", ">", ">-"}:
            scalar, next_raw = _read_block_scalar(
                self._raw_lines, line_no, parent_indent, clean_val
            )
            self._skip_before_line(next_raw)
            return scalar
        if clean_val == "":
            if self._pos < len(self._tokens) and self._tokens[self._pos].indent > parent_indent:
                return self._parse_node(self._tokens[self._pos].indent)
            return None
        return _parse_scalar(clean_val, self._anchors)


def parse_yaml_document(source: str) -> Any:
    """Phân tích văn bản YAML thành cấu trúc dữ liệu Python chuẩn."""
    return _Parser(source).parse()
