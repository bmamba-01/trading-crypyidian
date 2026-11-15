"""Unit test cho script dev tạo người duyệt local (không chạm DB hay Docker)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import UUID

import pytest
from scripts import seed_local_reviewer as script

WORKSPACE = UUID("c486b52b-912a-446f-aeed-99685c06cad6")


def test_member_ids_are_deterministic_per_workspace_and_email() -> None:
    first = script._member_ids(WORKSPACE, "local.reviewer@example.test")
    assert first == script._member_ids(WORKSPACE, "local.reviewer@example.test")
    assert set(first) == {"user", "membership", "mfa_factor", "secret_reference"}
    assert len(set(first.values())) == 4
    other = script._member_ids(WORKSPACE, "other.reviewer@example.test")
    assert first["user"] != other["user"]


def test_credentials_file_is_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / ".secrets" / "local_reviewer.json"
    created = script._load_or_create_credentials(path, "local.reviewer@example.test")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["format"] == script.CREDENTIALS_FORMAT
    assert len(created["password"]) >= 24
    assert script._load_or_create_credentials(path, "ignored@example.test") == created


def test_credentials_file_with_wrong_format_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "local_reviewer.json"
    path.write_text(json.dumps({"format": "other"}))
    with pytest.raises(SystemExit, match="định dạng"):
        script._load_or_create_credentials(path, "x@example.test")
