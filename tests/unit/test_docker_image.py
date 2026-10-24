from pathlib import Path
from subprocess import CompletedProcess

import pytest
from scripts.docker_image import build_image_reference, ensure_image_available


def test_build_image_reference_is_stable_for_unchanged_inputs(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.14-slim\n", encoding="utf-8")
    source_directory = tmp_path / "src"
    source_directory.mkdir()
    (source_directory / "app.py").write_text("value = 1\n", encoding="utf-8")

    first_reference = build_image_reference(
        tmp_path,
        input_paths=(Path("Dockerfile"), Path("src")),
    )
    second_reference = build_image_reference(
        tmp_path,
        input_paths=(Path("Dockerfile"), Path("src")),
    )

    assert first_reference == second_reference
    assert first_reference.startswith("crypidian-app:e2e-")


def test_build_image_reference_changes_when_build_input_changes(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.14-slim\n", encoding="utf-8")

    original_reference = build_image_reference(
        tmp_path,
        input_paths=(Path("Dockerfile"),),
    )
    dockerfile.write_text("FROM python:3.14-alpine\n", encoding="utf-8")
    updated_reference = build_image_reference(
        tmp_path,
        input_paths=(Path("Dockerfile"),),
    )

    assert original_reference != updated_reference


def test_ensure_image_available_skips_build_for_existing_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
        commands.append(command)
        return CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr("scripts.docker_image.subprocess.run", fake_run)

    built = ensure_image_available(tmp_path, "crypidian-app:e2e-existing")

    assert built is False
    assert commands == [["docker", "image", "inspect", "crypidian-app:e2e-existing"]]


def test_ensure_image_available_builds_missing_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
        commands.append(command)
        return CompletedProcess(command, int(len(commands) == 1), stdout="", stderr="")

    monkeypatch.setattr("scripts.docker_image.subprocess.run", fake_run)

    built = ensure_image_available(tmp_path, "crypidian-app:e2e-new")

    assert built is True
    assert commands == [
        ["docker", "image", "inspect", "crypidian-app:e2e-new"],
        ["docker", "build", "--tag", "crypidian-app:e2e-new", "--file", "Dockerfile", "."],
    ]


def test_ensure_image_available_reports_build_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return CompletedProcess(command, 1 if calls == 1 else 2, stdout="", stderr="build failed")

    monkeypatch.setattr("scripts.docker_image.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="build failed"):
        ensure_image_available(tmp_path, "crypidian-app:e2e-broken")
