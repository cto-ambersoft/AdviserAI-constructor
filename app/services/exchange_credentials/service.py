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
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
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
            encrypted = self._secrets.encrypt_credentials(
                api_key=payload.api_key or str(decrypted["api_key"]),
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

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
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
