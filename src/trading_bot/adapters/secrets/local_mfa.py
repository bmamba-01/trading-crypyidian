import base64
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trading_bot.common.secret_inventory import derive_mfa_key_id
from trading_bot.ports.secrets import SecretAccessError, SecretLease

_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CIPHERTEXT_VERSION = 2


def _load_aes256_key(key_file: Path) -> bytes:
    key_text = key_file.read_text(encoding="utf-8").strip()
    try:
        key = base64.urlsafe_b64decode(key_text.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("Khóa mã hóa phải là Base64URL hợp lệ 32 byte") from error
    if len(key) != 32:
        raise ValueError("Khóa mã hóa phải dài chính xác 32 byte")
    return key


class LocalMfaSecretProvider:
    """Mã hóa bí mật TOTP local kèm key_id để xoay khóa không mất dữ liệu."""

    def __init__(
        self,
        *,
        key_file: Path,
        storage_directory: Path,
        key_id: str | None = None,
        previous_keys: Mapping[str, Path] | None = None,
    ) -> None:
        active_key = _load_aes256_key(key_file)
        resolved_key_id = key_id.strip() if key_id is not None else derive_mfa_key_id(active_key)
        if not _KEY_ID_PATTERN.fullmatch(resolved_key_id):
            raise ValueError("key_id khóa mã hóa MFA không hợp lệ")
        self._active_key_id = resolved_key_id
        self._cipher = AESGCM(active_key)
        self._ciphers: dict[str, AESGCM] = {resolved_key_id: self._cipher}
        if previous_keys is not None:
            for prev_id, prev_path in previous_keys.items():
                clean_id = prev_id.strip()
                if not _KEY_ID_PATTERN.fullmatch(clean_id):
                    raise ValueError(f"previous key_id '{prev_id}' không hợp lệ")
                if clean_id == resolved_key_id:
                    raise ValueError("previous_keys không được trùng với active key_id")
                self._ciphers[clean_id] = AESGCM(_load_aes256_key(prev_path))
        self._storage_directory = storage_directory

    @property
    def active_key_id(self) -> str:
        """Định danh khóa hiện hành dùng để mã hóa ciphertext mới."""
        return self._active_key_id

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str:
        if not value or len(value) > 128:
            raise ValueError("Giá trị bí mật MFA không hợp lệ")
        reference_id = uuid4()
        reference = f"local-mfa:{reference_id}"
        self._write_encrypted_payload(workspace_id, reference, reference_id, value)
        return reference

    def _write_encrypted_payload(
        self,
        workspace_id: UUID,
        reference: str,
        reference_id: UUID,
        value: str,
    ) -> None:
        nonce = os.urandom(12)
        associated_data = self._associated_data_v2(workspace_id, reference, self._active_key_id)
        encrypted = self._cipher.encrypt(nonce, value.encode("utf-8"), associated_data)
        payload = json.dumps(
            {
                "version": _CIPHERTEXT_VERSION,
                "key_id": self._active_key_id,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(encrypted).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._storage_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._storage_directory, 0o700)
        target = self._path(reference_id)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{reference_id.hex}.", dir=self._storage_directory
        )
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
            os.chmod(target, 0o600)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        reference_id = self._parse_reference(reference)
        try:
            payload = json.loads(self._path(reference_id).read_text(encoding="utf-8"))
            nonce = base64.b64decode(payload["nonce"], validate=True)
            ciphertext = base64.b64decode(payload["ciphertext"], validate=True)
            payload_key_id = payload.get("key_id")
            if payload_key_id is not None:
                if not isinstance(payload_key_id, str) or payload_key_id not in self._ciphers:
                    raise SecretAccessError("Không tìm thấy key_id để giải mã bí mật MFA")
                cipher = self._ciphers[payload_key_id]
                aad = (
                    self._associated_data_v2(workspace_id, reference, payload_key_id)
                    if payload.get("version") == _CIPHERTEXT_VERSION
                    else self._associated_data(workspace_id, reference)
                )
                plaintext = cipher.decrypt(nonce, ciphertext, aad)
                return plaintext.decode("utf-8")

            # Tương thích ngược với ciphertext v1 chưa có trường key_id
            legacy_aad = self._associated_data(workspace_id, reference)
            for cipher in self._ciphers.values():
                try:
                    plaintext = cipher.decrypt(nonce, ciphertext, legacy_aad)
                    return plaintext.decode("utf-8")
                except InvalidTag:
                    continue
            raise SecretAccessError("Không thể đọc tham chiếu bí mật MFA")
        except (OSError, KeyError, ValueError, TypeError, InvalidTag) as error:
            raise SecretAccessError("Không thể đọc tham chiếu bí mật MFA") from error

    def inspect_ciphertext_key_id(self, reference: str) -> str | None:
        """Đọc key_id gắn trong metadata ciphertext mà không giải mã bí mật."""
        reference_id = self._parse_reference(reference)
        try:
            payload = json.loads(self._path(reference_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SecretAccessError("Không thể đọc metadata tham chiếu bí mật MFA") from error
        key_id = payload.get("key_id")
        return key_id if isinstance(key_id, str) else None

    def rotate_mfa_secret(self, workspace_id: UUID, reference: str, *, force: bool = False) -> str:
        """Mã hóa lại ciphertext từ khóa cũ sang khóa hiện hành không mất dữ liệu."""
        reference_id = self._parse_reference(reference)
        try:
            payload = json.loads(self._path(reference_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SecretAccessError("Không thể đọc tham chiếu bí mật MFA để xoay khóa") from error
        if (
            not force
            and payload.get("version") == _CIPHERTEXT_VERSION
            and payload.get("key_id") == self._active_key_id
        ):
            os.chmod(self._path(reference_id), 0o600)
            return reference
        plaintext = self.resolve_mfa_secret(workspace_id, reference)
        self._write_encrypted_payload(workspace_id, reference, reference_id, plaintext)
        return reference

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        del workspace_id
        self._path(self._parse_reference(reference)).unlink(missing_ok=True)

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease:
        """Không mở quyền truy cập Binance từ adapter chỉ phục vụ MFA local."""
        del workspace_id, reference
        raise SecretAccessError("Adapter local MFA không cấp secret giao dịch cho gateway")

    def resolve_telegram_bot_token(
        self, reference: str, *, workspace_id: UUID | None = None
    ) -> SecretLease:
        """Không cấp token Telegram Bot API từ adapter chỉ phục vụ MFA local."""
        del reference, workspace_id
        raise SecretAccessError("Adapter local MFA không cấp token Telegram Bot API")

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

    @staticmethod
    def _associated_data_v2(workspace_id: UUID, reference: str, key_id: str) -> bytes:
        return f"crypidian:mfa:v2:{workspace_id}:{reference}:{key_id}".encode()


__all__ = ["LocalMfaSecretProvider", "SecretAccessError", "SecretLease"]
