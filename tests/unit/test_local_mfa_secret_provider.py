import base64
from uuid import uuid4

import pytest

from trading_bot.adapters.secrets.local_mfa import LocalMfaSecretProvider, SecretAccessError


def test_local_mfa_secret_is_encrypted_at_rest_and_scoped_to_workspace(tmp_path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(base64.urlsafe_b64encode(b"x" * 32).decode(), encoding="utf-8")
    provider = LocalMfaSecretProvider(key_file=key_file, storage_directory=tmp_path / "vault")
    workspace_id = uuid4()
    secret = "JBSWY3DPEHPK3PXP"

    reference = provider.store_mfa_secret(workspace_id, secret)
    stored_bytes = b"".join(path.read_bytes() for path in (tmp_path / "vault").glob("*.bin"))

    assert secret.encode() not in stored_bytes
    assert provider.resolve_mfa_secret(workspace_id, reference) == secret
    with pytest.raises(SecretAccessError):
        provider.resolve_mfa_secret(uuid4(), reference)
    with pytest.raises(SecretAccessError):
        provider.resolve_for_gateway(workspace_id, reference)


def test_local_mfa_provider_rejects_weak_encryption_key(tmp_path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("short", encoding="utf-8")

    with pytest.raises(ValueError, match="32 byte"):
        LocalMfaSecretProvider(key_file=key_file, storage_directory=tmp_path / "vault")
