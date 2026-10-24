import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import cast

MFA_SECRET_DIRECTORY = Path("/var/lib/crypidian/mfa-secrets")
PROVISIONER_UID = 0
PROVISIONER_GID = 0
APPLICATION_UID = 10001
APPLICATION_GID = 10001


def main() -> None:
    """Gán quyền volume MFA cho UID ứng dụng trước khi admin-web khởi động."""
    MFA_SECRET_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    platform_chown = getattr(os, "chown", None)
    if platform_chown is None:
        raise RuntimeError("Hệ điều hành container không hỗ trợ đổi owner volume")
    metadata = os.stat(MFA_SECRET_DIRECTORY)
    if (
        metadata.st_uid == APPLICATION_UID
        and metadata.st_gid == APPLICATION_GID
        and stat.S_IMODE(metadata.st_mode) == 0o700
    ):
        return

    if metadata.st_uid != PROVISIONER_UID or metadata.st_gid != PROVISIONER_GID:
        chown = cast(Callable[[Path, int, int], None], platform_chown)
        chown(MFA_SECRET_DIRECTORY, PROVISIONER_UID, PROVISIONER_GID)
    os.chmod(MFA_SECRET_DIRECTORY, 0o700)
    chown = cast(Callable[[Path, int, int], None], platform_chown)
    chown(MFA_SECRET_DIRECTORY, APPLICATION_UID, APPLICATION_GID)


if __name__ == "__main__":
    main()
