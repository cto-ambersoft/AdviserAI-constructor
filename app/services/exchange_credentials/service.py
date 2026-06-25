import hashlib
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exchange import ExchangeCredential
from app.schemas.exchange import (
    ExchangeAccountCreate,
    ExchangeAccountRead,
    ExchangeAccountUpdate,
    validate_exchange_name,
    validate_mode,
)
from app.schemas.exchange_trading import ExchangeMode, ExchangeName
from app.services.execution.base import ExchangeCredentials
from app.services.execution.factory import create_cex_adapter
from app.services.secrets import SecretsService


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class DuplicateApiKeyError(ValueError):
    """Raised when a user tries to register the same physical api_key twice.

    Two ``ExchangeCredential`` rows pointing at the same exchange sub-account
    would undo the per-credential isolation that W7 multi-strategy support
    relies on (signals from two strategies would land on the same balance
    and fight for the same physical position).
    """


class ExchangeCredentialsService:
    def __init__(self) -> None:
        self._secrets = SecretsService()

    async def list_accounts(self, session: AsyncSession, user_id: int) -> list[ExchangeAccountRead]:
        rows = await session.scalars(
            select(ExchangeCredential)
            .where(ExchangeCredential.user_id == user_id)
            .order_by(ExchangeCredential.created_at.desc())
        )
        return [ExchangeAccountRead.model_validate(row) for row in rows.all()]

    async def create_account(
        self,
        session: AsyncSession,
        payload: ExchangeAccountCreate,
        user_id: int,
    ) -> ExchangeAccountRead:
        exchange_name = validate_exchange_name(payload.exchange_name)
        mode = validate_mode(payload.mode)
        api_key_hash = _hash_api_key(payload.api_key)
        # W7: reject the same physical sub-account being registered twice
        # (e.g. two different account_labels pointing at the same key).
        existing = await session.scalar(
            select(ExchangeCredential).where(
                ExchangeCredential.user_id == user_id,
                ExchangeCredential.api_key_hash == api_key_hash,
            )
        )
        if existing is not None:
            raise DuplicateApiKeyError(
                "This API key is already registered under another account label "
                f"({existing.account_label!r} on {existing.exchange_name}). "
                "Each strategy must run on its own exchange sub-account."
            )
        encrypted = self._secrets.encrypt_credentials(
            api_key=payload.api_key,
            api_secret=payload.api_secret,
            passphrase=payload.passphrase,
        )
        row = ExchangeCredential(
            user_id=user_id,
            exchange_name=exchange_name,
            account_label=payload.account_label,
            mode=mode,
            encrypted_api_key=str(encrypted["encrypted_api_key"]),
            encrypted_api_secret=str(encrypted["encrypted_api_secret"]),
            encrypted_passphrase=(
                str(encrypted["encrypted_passphrase"])
                if encrypted["encrypted_passphrase"]
                else None
            ),
            api_key_hash=api_key_hash,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            # Hash UQ violation is also possible if two concurrent inserts
            # race past the explicit check above.
            message = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
            if "api_key_hash" in message:
                raise DuplicateApiKeyError(
                    "This API key is already registered. Each strategy must run "
                    "on its own exchange sub-account."
                ) from exc
            raise ValueError("Account with the same exchange and label already exists.") from exc
        await session.refresh(row)
        return ExchangeAccountRead.model_validate(row)

    async def update_account(
        self,
        session: AsyncSession,
        account_id: int,
        payload: ExchangeAccountUpdate,
        user_id: int,
    ) -> ExchangeAccountRead:
        row = await self._get_owned_account(session, account_id=account_id, user_id=user_id)
        if row is None:
            raise LookupError("Exchange account not found.")

        if payload.account_label is not None:
            row.account_label = payload.account_label
        if payload.mode is not None:
            row.mode = validate_mode(payload.mode)

        if (
            payload.api_key is not None
            or payload.api_secret is not None
            or payload.passphrase is not None
        ):
            decrypted = self._secrets.decrypt_credentials(
                encrypted_api_key=row.encrypted_api_key,
                encrypted_api_secret=row.encrypted_api_secret,
                encrypted_passphrase=row.encrypted_passphrase,
            )
            new_api_key = payload.api_key or str(decrypted["api_key"])
            encrypted = self._secrets.encrypt_credentials(
                api_key=new_api_key,
                api_secret=payload.api_secret or str(decrypted["api_secret"]),
                passphrase=payload.passphrase
                if payload.passphrase is not None
                else decrypted["passphrase"],
            )
            row.encrypted_api_key = str(encrypted["encrypted_api_key"])
            row.encrypted_api_secret = str(encrypted["encrypted_api_secret"])
            row.encrypted_passphrase = (
                str(encrypted["encrypted_passphrase"])
                if encrypted["encrypted_passphrase"]
                else None
            )
            # W7: keep api_key_hash in sync so the UQ catches a rotation
            # that accidentally collides with another credential.
            new_hash = _hash_api_key(new_api_key)
            if new_hash != row.api_key_hash:
                existing = await session.scalar(
                    select(ExchangeCredential).where(
                        ExchangeCredential.user_id == user_id,
                        ExchangeCredential.api_key_hash == new_hash,
                        ExchangeCredential.id != row.id,
                    )
                )
                if existing is not None:
                    raise DuplicateApiKeyError(
                        "This API key is already registered under another account "
                        f"label ({existing.account_label!r})."
                    )
                row.api_key_hash = new_hash

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            message = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
            if "api_key_hash" in message:
                raise DuplicateApiKeyError(
                    "This API key is already registered. Each strategy must run "
                    "on its own exchange sub-account."
                ) from exc
            raise ValueError("Account with the same exchange and label already exists.") from exc
        await session.refresh(row)
        return ExchangeAccountRead.model_validate(row)

    async def delete_account(self, session: AsyncSession, account_id: int, user_id: int) -> bool:
        row = await self._get_owned_account(session, account_id=account_id, user_id=user_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True

    async def get_account(
        self, session: AsyncSession, account_id: int, user_id: int
    ) -> ExchangeAccountRead:
        row = await self._get_owned_account(session, account_id=account_id, user_id=user_id)
        if row is None:
            raise LookupError("Exchange account not found.")
        return ExchangeAccountRead.model_validate(row)

    async def validate_account(self, session: AsyncSession, account_id: int, user_id: int) -> None:
        credentials = await self.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=user_id,
        )
        adapter = create_cex_adapter(credentials)
        await adapter.ping()

    async def get_decrypted_credentials(
        self,
        session: AsyncSession,
        account_id: int,
        user_id: int,
    ) -> ExchangeCredentials:
        row = await self._get_owned_account(session, account_id=account_id, user_id=user_id)
        if row is None:
            raise LookupError("Exchange account not found.")
        decrypted = self._secrets.decrypt_credentials(
            encrypted_api_key=row.encrypted_api_key,
            encrypted_api_secret=row.encrypted_api_secret,
            encrypted_passphrase=row.encrypted_passphrase,
        )
        return ExchangeCredentials(
            exchange_name=cast(ExchangeName, validate_exchange_name(row.exchange_name)),
            api_key=str(decrypted["api_key"]),
            api_secret=str(decrypted["api_secret"]),
            mode=cast(ExchangeMode, validate_mode(row.mode)),
            passphrase=str(decrypted["passphrase"]) if decrypted["passphrase"] else None,
        )

    async def _get_owned_account(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        user_id: int,
    ) -> ExchangeCredential | None:
        return cast(
            ExchangeCredential | None,
            await session.scalar(
                select(ExchangeCredential).where(
                    ExchangeCredential.id == account_id,
                    ExchangeCredential.user_id == user_id,
                )
            ),
        )
