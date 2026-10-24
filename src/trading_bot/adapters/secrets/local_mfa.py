import base64
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trading_bot.ports.secrets import SecretAccessError, SecretLease


class LocalMfaSecretProvider:
    """Mã hóa bí mật TOTP local; adapter này không giải mã credential giao dịch."""

    def __init__(self, *, key_file: Path, storage_directory: Path) -> None:
        key_text = key_file.read_text(encoding="utf-8").strip()
        try:
            key = base64.urlsafe_b64decode(key_text.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise ValueError("Khóa mã hóa phải là Base64URL hợp lệ 32 byte") from error
        if len(key) != 32:
            raise ValueError("Khóa mã hóa phải dài chính xác 32 byte")
        self._cipher = AESGCM(key)
        self._storage_directory = storage_directory

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str:
        if not value or len(value) > 128:
            raise ValueError("Giá trị bí mật MFA không hợp lệ")
        reference_id = uuid4()
        reference = f"local-mfa:{reference_id}"
        nonce = os.urandom(12)
        associated_data = self._associated_data(workspace_id, reference)
        encrypted = self._cipher.encrypt(nonce, value.encode("utf-8"), associated_data)
        payload = json.dumps(
            {
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(encrypted).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._storage_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self._path(reference_id)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{reference_id.hex}.", dir=self._storage_directory
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return reference

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        reference_id = self._parse_reference(reference)
        try:
            payload = json.loads(self._path(reference_id).read_text(encoding="utf-8"))
            nonce = base64.b64decode(payload["nonce"], validate=True)
            ciphertext = base64.b64decode(payload["ciphertext"], validate=True)
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._associated_data(workspace_id, reference),
            )
            return plaintext.decode("utf-8")
        except (OSError, KeyError, ValueError, TypeError, InvalidTag) as error:
            raise SecretAccessError("Không thể đọc tham chiếu bí mật MFA") from error

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        self._parse_reference(reference)
        self._path(self._parse_reference(reference)).unlink(missing_ok=True)

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease:
        """Không mở quyền truy cập Binance từ adapter chỉ phục vụ MFA local."""
        del workspace_id, reference
        raise SecretAccessError("Adapter local MFA không cấp secret giao dịch cho gateway")

    @staticmethod
    def _parse_reference(reference: str) -> UUID:
        prefix, separator, identifier = reference.partition(":")
        if prefix != "local-mfa" or not separator:
            raise SecretAccessError("Loại tham chiếu bí mật không được hỗ trợ")
        try:
            return UUID(identifier)
        except ValueError as error:
            raise SecretAccessError("Tham chiếu bí mật không hợp lệ") from error

    def _path(self, reference_id: UUID) -> Path:
        return self._storage_directory / f"{reference_id.hex}.bin"

    @staticmethod
    def _associated_data(workspace_id: UUID, reference: str) -> bytes:
        return f"crypidian:mfa:{workspace_id}:{reference}".encode()


__all__ = ["LocalMfaSecretProvider", "SecretAccessError", "SecretLease"]
