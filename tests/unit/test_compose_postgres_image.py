"""SEC-02: image PostgreSQL trong Compose phải là minor đã vá (>= 18.6) và ghim digest."""

import re
from pathlib import Path

COMPOSE_FILE = Path(__file__).parents[2] / "compose.yaml"
# Tag minor + biến thể Debian, rồi digest SHA-256 của image index đa kiến trúc.
POSTGRES_IMAGE = re.compile(
    r"^\s*image:\s*postgres:(?P<major>\d+)\.(?P<minor>\d+)-bookworm"
    r"@sha256:(?P<digest>[0-9a-f]{64})\s*$",
    re.MULTILINE,
)
MINIMUM_PATCHED_RELEASE = (18, 6)


def test_compose_pins_patched_postgres_release_by_digest() -> None:
    matches = list(POSTGRES_IMAGE.finditer(COMPOSE_FILE.read_text(encoding="utf-8")))

    assert len(matches) == 1, "compose.yaml phải có đúng một image postgres ghim digest"
    match = matches[0]
    release = (int(match["major"]), int(match["minor"]))
    assert release[0] == MINIMUM_PATCHED_RELEASE[0], "nâng major cần pg_upgrade, không ở đây"
    assert release >= MINIMUM_PATCHED_RELEASE, (
        f"PostgreSQL {release} thiếu bản vá CVE-2026-14666 (cần >= 18.6)"
    )
