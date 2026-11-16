"""Engine AI Analyst chạy CLI agent trên host (AI-2): Claude Code (chính) và Codex (dự phòng).

Cờ đã kiểm trên binary local 16/11/2026 (``--help``): Claude Code 2.1.321, Codex 0.169.0-alpha.
Tài liệu: https://code.claude.com/docs/en/headless ,
https://learn.chatgpt.com/docs/non-interactive-mode (truy cập 16/11/2026).

An toàn mỗi lượt chạy:
- thư mục làm việc tạm rỗng (không chứa repo/secret), biến môi trường tối thiểu (PATH, HOME,
  LANG — HOME cần cho credential đăng nhập của CLI);
- Claude: ``--tools ""`` (tắt mọi công cụ), ``--restricted``, ``--permission-prompts none``,
  ``--max-budget-usd``; Codex: ``-s read-only``, ``--skip-git-repo-check``;
- ``timeout`` của tiến trình; đầu ra ép theo JSON Schema.

Session: Claude tạo bằng ``--session-id <uuid>`` và tiếp tục bằng ``--resume <uuid>``; Codex lấy
``thread_id`` từ sự kiện ``thread.started`` và tiếp tục bằng ``codex exec resume <id>``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from trading_bot.ports.analyst import AnalysisRequest, AnalysisResult

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _minimal_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "TMPDIR")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _version(binary: str, runner: Runner) -> str | None:
    try:
        done = runner([binary, "--version"], capture_output=True, text=True, timeout=30)
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout.strip().splitlines()[0] if done.stdout.strip() else None


class ClaudeCliEngine:
    """``claude -p`` với JSON Schema; trả ``structured_output`` khi model tuân schema."""

    name = "claude-cli"

    def __init__(
        self, binary: str, *, model: str | None = None, runner: Runner = subprocess.run
    ) -> None:
        self._binary = binary
        self._model = model
        self._runner = runner

    def command(self, request: AnalysisRequest, session_id: str) -> list[str]:
        argv = [
            self._binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(request.schema, ensure_ascii=False),
            "--tools",
            "",
            "--restricted",
            "--permission-prompts",
            "none",
            "--max-budget-usd",
            str(request.max_budget_usd),
        ]
        argv += ["--resume", session_id] if request.resume else ["--session-id", session_id]
        if self._model:
            argv += ["--model", self._model]
        return argv

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        session_id = request.session_id or str(uuid.uuid4())
        version = _version(self._binary, self._runner)
        with tempfile.TemporaryDirectory(prefix="crypidian-analyst-") as workdir:
            try:
                done = self._runner(
                    self.command(request, session_id),
                    input=request.prompt,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout_seconds,
                    cwd=workdir,
                    env=_minimal_env(),
                )
            except subprocess.TimeoutExpired:
                return AnalysisResult(self.name, None, "", session_id, -1, version, error="TIMEOUT")
        payload = _json_object(done.stdout)
        structured = payload.get("structured_output") if payload else None
        usage = payload.get("usage") if payload else None
        model_usage = payload.get("modelUsage") if payload else None
        return AnalysisResult(
            engine=self.name,
            structured_output=structured if isinstance(structured, Mapping) else None,
            raw_stdout=done.stdout,
            session_id=str(payload.get("session_id") or session_id) if payload else session_id,
            exit_code=done.returncode,
            cli_version=version,
            model=self._model or _first_key(model_usage),
            usage=usage if isinstance(usage, Mapping) else {},
            cost_usd=_float(payload.get("total_cost_usd")) if payload else None,
            error=None
            if done.returncode == 0 and isinstance(structured, Mapping)
            else (_error_code(payload) or f"EXIT_{done.returncode}"),
        )


class CodexCliEngine:
    """``codex exec`` read-only với ``--output-schema``; phiên tiếp tục qua ``exec resume``."""

    name = "codex-cli"

    def __init__(
        self, binary: str, *, model: str | None = None, runner: Runner = subprocess.run
    ) -> None:
        self._binary = binary
        self._model = model
        self._runner = runner

    def command(self, request: AnalysisRequest, schema_path: Path, out_path: Path) -> list[str]:
        base = [self._binary, "exec"]
        if request.resume and request.session_id:
            base += ["resume", request.session_id]
        argv = base + [
            "--json",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(out_path),
        ]
        if self._model:
            argv += ["-m", self._model]
        return argv + ["-"]

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        version = _version(self._binary, self._runner)
        with tempfile.TemporaryDirectory(prefix="crypidian-analyst-") as workdir:
            schema_path = Path(workdir) / "schema.json"
            out_path = Path(workdir) / "last_message.json"
            schema_path.write_text(json.dumps(request.schema), encoding="utf-8")
            try:
                done = self._runner(
                    self.command(request, schema_path, out_path),
                    input=request.prompt,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout_seconds,
                    cwd=workdir,
                    env=_minimal_env(),
                )
            except subprocess.TimeoutExpired:
                return AnalysisResult(
                    self.name, None, "", request.session_id, -1, version, error="TIMEOUT"
                )
            last = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        thread_id, usage = _codex_events(done.stdout.splitlines())
        structured = _json_object(last)
        return AnalysisResult(
            engine=self.name,
            structured_output=structured,
            raw_stdout=done.stdout,
            session_id=thread_id or request.session_id,
            exit_code=done.returncode,
            cli_version=version,
            model=self._model,
            usage=usage,
            error=None if done.returncode == 0 and structured else f"EXIT_{done.returncode}",
        )


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError, TypeError:
        return None
    return value if isinstance(value, dict) else None


def _codex_events(lines: Sequence[str]) -> tuple[str | None, dict[str, Any]]:
    thread_id: str | None = None
    usage: dict[str, Any] = {}
    for line in lines:
        event = _json_object(line)
        if not event:
            continue
        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or "") or None
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return thread_id, usage


def _first_key(value: Any) -> str | None:
    return next(iter(value), None) if isinstance(value, Mapping) and value else None


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except TypeError, ValueError:
        return None


def _error_code(payload: Mapping[str, Any] | None) -> str | None:
    if not payload:
        return "NO_JSON_OUTPUT"
    if payload.get("is_error"):
        return str(payload.get("subtype") or "CLI_ERROR")[:80]
    if "structured_output" not in payload:
        return "NO_STRUCTURED_OUTPUT"
    return None
