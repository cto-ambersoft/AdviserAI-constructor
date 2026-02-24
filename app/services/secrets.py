from app.core.config import get_settings
from app.core.security import SecretCipher


class SecretsService:
    def __init__(self) -> None:
        self._cipher = SecretCipher(get_settings().encryption_key)

    def encrypt_credentials(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
    ) -> dict[str, str | None]:
        return {
            "encrypted_api_key": self._cipher.encrypt(api_key),
            "encrypted_api_secret": self._cipher.encrypt(api_secret),
            "encrypted_passphrase": self._cipher.encrypt(passphrase) if passphrase else None,
        }

    def decrypt_credentials(
        self,
        encrypted_api_key: str,
        encrypted_api_secret: str,
        encrypted_passphrase: str | None = None,
    ) -> dict[str, str | None]:
        return {
            "api_key": self._cipher.decrypt(encrypted_api_key),
            "api_secret": self._cipher.decrypt(encrypted_api_secret),
            "passphrase": self._cipher.decrypt(encrypted_passphrase)
            if encrypted_passphrase
            else None,
        }
