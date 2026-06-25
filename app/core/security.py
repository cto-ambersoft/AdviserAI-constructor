import base64
import hashlib
from collections.abc import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretCipher:
    """Authenticated symmetric encryption for secrets at rest (Fernet / AES-128-CBC+HMAC).

    T2 (S5): the primary ``encryption_key`` MUST be a real Fernet key
    (``Fernet.generate_key()`` — 32 url-safe-base64 bytes). Arbitrary strings are
    rejected loudly instead of being silently SHA256-stretched into a key (an
    unsalted, single-iteration hash of a low-entropy value is brute-forceable;
    context7/cryptography prescribes a real KDF or a generated key).

    Backward compatibility: ciphertext written before T2 used the old sha256-
    derivation. Pass those old raw values via ``legacy_keys`` — they are derived
    the old way and added as additional *decrypt-only* keys through ``MultiFernet``
    so existing data stays readable. New ciphertext is always encrypted under the
    strong primary key, enabling a gradual re-encryption.
    """

    def __init__(self, encryption_key: str, *, legacy_keys: Iterable[str] = ()) -> None:
        primary = Fernet(self._require_fernet_key(encryption_key))
        legacy = [Fernet(self.legacy_fernet_key(raw)) for raw in legacy_keys]
        self._fernet = MultiFernet([primary, *legacy])

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        try:
            raw = self._fernet.decrypt(encrypted_value.encode("utf-8"))
        except InvalidToken as exc:
            raise ValueError("Invalid encrypted payload.") from exc
        return raw.decode("utf-8")

    @staticmethod
    def _require_fernet_key(value: str) -> bytes:
        raw = value.encode("utf-8")
        try:
            Fernet(raw)  # validates length (44 chars) + url-safe-base64 of 32 bytes
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "encryption_key must be a valid Fernet key "
                "(generate one with Fernet.generate_key()); arbitrary strings are "
                "no longer accepted."
            ) from exc
        return raw

    @staticmethod
    def legacy_fernet_key(value: str) -> bytes:
        """Reproduce the pre-T2 sha256 derivation — for decrypting OLD ciphertext only.

        DEPRECATED: kept solely so a deployment can supply its previous raw
        ``ENCRYPTION_KEY`` as a legacy decryptor during migration.
        """
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)
