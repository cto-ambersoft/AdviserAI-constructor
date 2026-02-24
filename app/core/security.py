import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretCipher:
    def __init__(self, encryption_key: str) -> None:
        self._fernet = Fernet(self._normalize_key(encryption_key))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        try:
            raw = self._fernet.decrypt(encrypted_value.encode("utf-8"))
        except InvalidToken as exc:
            raise ValueError("Invalid encrypted payload.") from exc
        return raw.decode("utf-8")

    @staticmethod
    def _normalize_key(value: str) -> bytes:
        raw = value.encode("utf-8")
        if len(raw) == 44:
            try:
                Fernet(raw)
                return raw
            except (ValueError, TypeError):
                pass

        digest = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(digest)
