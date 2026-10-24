from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import provision_mfa_secret_volume


def test_mfa_volume_is_chmodded_before_ownership_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "mfa-secrets"
    operations: list[tuple[str, int, int | None]] = []
    monkeypatch.setattr(provision_mfa_secret_volume, "MFA_SECRET_DIRECTORY", directory)
    monkeypatch.setattr(
        provision_mfa_secret_volume,
        "os",
        SimpleNamespace(
            chmod=lambda path, mode: operations.append(("chmod", mode, None)),
            chown=lambda path, uid, gid: operations.append(("chown", uid, gid)),
            stat=lambda path: SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o40755),
        ),
    )

    provision_mfa_secret_volume.main()

    assert operations == [("chmod", 0o700, None), ("chown", 10001, 10001)]


def test_mfa_volume_setup_is_idempotent_when_permissions_are_already_correct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "mfa-secrets"
    operations: list[tuple[str, int, int | None]] = []
    monkeypatch.setattr(provision_mfa_secret_volume, "MFA_SECRET_DIRECTORY", directory)
    monkeypatch.setattr(
        provision_mfa_secret_volume,
        "os",
        SimpleNamespace(
            chmod=lambda path, mode: operations.append(("chmod", mode, None)),
            chown=lambda path, uid, gid: operations.append(("chown", uid, gid)),
            stat=lambda path: SimpleNamespace(
                st_uid=10001,
                st_gid=10001,
                st_mode=0o40700,
            ),
        ),
    )

    provision_mfa_secret_volume.main()

    assert operations == []


def test_mfa_volume_setup_reclaims_directory_before_fixing_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "mfa-secrets"
    operations: list[tuple[str, int, int | None]] = []
    monkeypatch.setattr(provision_mfa_secret_volume, "MFA_SECRET_DIRECTORY", directory)
    monkeypatch.setattr(
        provision_mfa_secret_volume,
        "os",
        SimpleNamespace(
            chmod=lambda path, mode: operations.append(("chmod", mode, None)),
            chown=lambda path, uid, gid: operations.append(("chown", uid, gid)),
            stat=lambda path: SimpleNamespace(
                st_uid=10001,
                st_gid=10001,
                st_mode=0o40755,
            ),
        ),
    )

    provision_mfa_secret_volume.main()

    assert operations == [
        ("chown", 0, 0),
        ("chmod", 0o700, None),
        ("chown", 10001, 10001),
    ]
