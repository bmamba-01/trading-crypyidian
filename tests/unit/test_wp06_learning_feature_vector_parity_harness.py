"""Regression cho tài nguyên Compose C.1.1 không dùng chung secret/image với lượt khác."""

from pathlib import Path

from tests.e2e import test_wp06_learning_feature_vector_parity as harness

ROOT = Path(__file__).parents[2]


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/prepare_local_secrets.py").write_bytes(
        (ROOT / "scripts/prepare_local_secrets.py").read_bytes()
    )
    (root / "compose.yaml").write_bytes((ROOT / "compose.yaml").read_bytes())
    (root / ".secrets").mkdir()
    (root / ".secrets/garage.env").write_text(
        "GARAGE_DEFAULT_BUCKET=owner-custom-bucket\nOWNER_SETTING=keep-me\n", encoding="utf-8"
    )
    (root / ".secrets/postgres_password").write_text("owner-password", encoding="utf-8")
    return root


def test_isolated_compose_preserves_existing_user_secrets_and_garage_config(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    before = {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()}

    with harness.isolated_compose_resources(root) as resources:
        assert {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()} == before
        config = resources.compose_file.read_text(encoding="utf-8")
        assert "./.secrets/" not in config
        secret_directory = resources.compose_file.parent / ".secrets"
        assert f"{secret_directory}/garage.env" in config
        assert f"{secret_directory}/postgres_password" in config
        assert (secret_directory / "garage.env").exists()
        assert (secret_directory / "postgres_password").read_bytes() != before["postgres_password"]
        assert {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()} == before

    assert {path.name: path.read_bytes() for path in (root / ".secrets").iterdir()} == before
    assert not resources.compose_file.parent.exists()


def test_parallel_runs_own_distinct_image_tags_and_secret_directories(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)

    with harness.isolated_compose_resources(root) as first:
        with harness.isolated_compose_resources(root) as second:
            assert first.image != second.image
            assert first.project != second.project
            assert first.compose_file.parent != second.compose_file.parent
            assert first.image == f"crypidian-app:e2e-c11-{first.run_id}"
            assert second.image == f"crypidian-app:e2e-c11-{second.run_id}"
            assert first.run_id in first.project
            assert second.run_id in second.project
            assert (first.compose_file.parent / ".secrets/postgres_password").read_bytes() != (
                second.compose_file.parent / ".secrets/postgres_password"
            ).read_bytes()
        assert first.compose_file.exists()
        assert not second.compose_file.parent.exists()
    assert not first.compose_file.parent.exists()
