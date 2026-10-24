"""Tạo định danh image Docker dùng chung theo nội dung build context."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

IMAGE_BUILD_INPUTS = (
    Path(".dockerignore"),
    Path("Dockerfile"),
    Path("README.md"),
    Path("alembic.ini"),
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("migrations"),
    Path("scripts/provision_database_roles.py"),
    Path("scripts/provision_mfa_secret_volume.py"),
    Path("src"),
)


def build_image_reference(
    repository_root: Path,
    input_paths: tuple[Path, ...] = IMAGE_BUILD_INPUTS,
) -> str:
    """Tạo tag ổn định từ các tệp thực sự được Dockerfile đưa vào image."""
    digest = hashlib.sha256()
    for input_path in input_paths:
        source = repository_root / input_path
        if source.is_dir():
            source_files = sorted(path for path in source.rglob("*") if path.is_file())
        elif source.is_file():
            source_files = [source]
        else:
            raise FileNotFoundError(f"Thiếu đầu vào Docker build: {source}")

        for file_path in source_files:
            relative_path = file_path.relative_to(repository_root).as_posix().encode("utf-8")
            content = file_path.read_bytes()
            digest.update(len(relative_path).to_bytes(4, "big"))
            digest.update(relative_path)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)

    return f"crypidian-app:e2e-{digest.hexdigest()[:20]}"


def ensure_image_available(repository_root: Path, image_reference: str) -> bool:
    """Chỉ build image khi chưa có tag tương ứng với mã nguồn hiện tại."""
    inspection = subprocess.run(
        ["docker", "image", "inspect", image_reference],
        check=False,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if inspection.returncode == 0:
        return False

    build = subprocess.run(
        ["docker", "build", "--tag", image_reference, "--file", "Dockerfile", "."],
        check=False,
        capture_output=True,
        cwd=repository_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if build.returncode != 0:
        output = (build.stderr or build.stdout)[-4000:]
        raise RuntimeError(f"Không thể build image {image_reference}:\n{output}")
    return True
