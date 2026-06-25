"""T2 (S5): SecretCipher must require a real Fernet key, not silently SHA256-derive
any string. Existing ciphertext (encrypted before T2 via the legacy sha256 path)
stays decryptable when the old raw value is supplied as a legacy key (MultiFernet).
context7 (cryptography): a key from an arbitrary string needs a real KDF/key — a
raw, unsalted sha256 of a low-entropy value is brute-forceable and must not be the
silent default.
"""

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet

from app.core.security import SecretCipher


def _legacy_pre_t2_token(old_raw: str, plaintext: str) -> str:
    """Reproduce a token written by the pre-T2 sha256-derivation path."""
    key = base64.urlsafe_b64encode(hashlib.sha256(old_raw.encode("utf-8")).digest())
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def test_round_trip_with_valid_fernet_key() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode("utf-8"))
    assert cipher.decrypt(cipher.encrypt("api-secret")) == "api-secret"


@pytest.mark.parametrize("bad_key", ["totp-test-key", "short", "x" * 40, "not base64 @@@"])
def test_rejects_non_fernet_key(bad_key: str) -> None:
    # The whole point of S5: an arbitrary/low-entropy string must fail loudly,
    # not get silently stretched into a "valid" key.
    with pytest.raises(ValueError):
        SecretCipher(bad_key)


def test_legacy_key_decrypts_pre_t2_ciphertext() -> None:
    old_raw = "old-deployment-encryption-key"
    token = _legacy_pre_t2_token(old_raw, "preserved-secret")

    cipher = SecretCipher(Fernet.generate_key().decode("utf-8"), legacy_keys=(old_raw,))

    assert cipher.decrypt(token) == "preserved-secret"


def test_new_encryption_uses_primary_not_legacy() -> None:
    old_raw = "old-deployment-encryption-key"
    primary = Fernet.generate_key().decode("utf-8")
    cipher = SecretCipher(primary, legacy_keys=(old_raw,))

    token = cipher.encrypt("fresh")
    # The legacy (sha256-derived) key alone must NOT be able to read new ciphertext.
    legacy_only = SecretCipher.legacy_fernet_key(old_raw)
    with pytest.raises(Exception):
        Fernet(legacy_only).decrypt(token.encode("utf-8"))
    assert cipher.decrypt(token) == "fresh"


def test_invalid_token_raises_value_error() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode("utf-8"))
    with pytest.raises(ValueError):
        cipher.decrypt("not-a-valid-token")
