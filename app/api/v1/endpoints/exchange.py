from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.exchange import (
    ExchangeAccountCreate,
    ExchangeAccountRead,
    ExchangeAccountsMetaResponse,
    ExchangeAccountUpdate,
    ExchangeAccountValidateResponse,
    ExchangeSecretIn,
    ExchangeSecretOut,
)
from app.schemas.exchange_trading import SUPPORTED_EXCHANGE_MODES, SUPPORTED_EXCHANGES
from app.services.exchange_credentials.service import ExchangeCredentialsService
from app.services.execution.errors import ExchangeServiceError, error_http_status
from app.services.secrets import SecretsService

router = APIRouter()
secrets_service = SecretsService()
credentials_service = ExchangeCredentialsService()


@router.post("/encrypt", response_model=ExchangeSecretOut, summary="Encrypt exchange secrets")
async def encrypt_exchange_secrets(payload: ExchangeSecretIn) -> ExchangeSecretOut:
    encrypted = secrets_service.encrypt_credentials(
        api_key=payload.api_key,
        api_secret=payload.api_secret,
        passphrase=payload.passphrase,
    )
    return ExchangeSecretOut.model_validate(encrypted)


@router.get(
    "/accounts/meta", response_model=ExchangeAccountsMetaResponse, summary="Exchange accounts meta"
)
async def get_exchange_accounts_meta() -> ExchangeAccountsMetaResponse:
    return ExchangeAccountsMetaResponse(
        supported_exchanges=list(SUPPORTED_EXCHANGES),
        supported_modes=list(SUPPORTED_EXCHANGE_MODES),
        default_mode="demo",
    )


@router.get("/accounts", response_model=list[ExchangeAccountRead], summary="List exchange accounts")
async def list_exchange_accounts(
    session: DbSession, current_user: CurrentUser
) -> list[ExchangeAccountRead]:
    return await credentials_service.list_accounts(session, user_id=current_user.id)


@router.post(
    "/accounts",
    response_model=ExchangeAccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create exchange account",
)
async def create_exchange_account(
    payload: ExchangeAccountCreate,
    session: DbSession,
    current_user: CurrentUser,
) -> ExchangeAccountRead:
    try:
        return await credentials_service.create_account(session, payload, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch(
    "/accounts/{account_id}", response_model=ExchangeAccountRead, summary="Update exchange account"
)
async def update_exchange_account(
    account_id: int,
    payload: ExchangeAccountUpdate,
    session: DbSession,
    current_user: CurrentUser,
) -> ExchangeAccountRead:
    try:
        return await credentials_service.update_account(
            session,
            account_id=account_id,
            payload=payload,
            user_id=current_user.id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete exchange account",
)
async def delete_exchange_account(
    account_id: int, session: DbSession, current_user: CurrentUser
) -> None:
    deleted = await credentials_service.delete_account(
        session, account_id=account_id, user_id=current_user.id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Exchange account not found."
        )


@router.post(
    "/accounts/{account_id}/validate",
    response_model=ExchangeAccountValidateResponse,
    summary="Validate exchange account credentials",
)
async def validate_exchange_account(
    account_id: int,
    session: DbSession,
    current_user: CurrentUser,
) -> ExchangeAccountValidateResponse:
    try:
        account_meta = await credentials_service.get_account(
            session=session,
            account_id=account_id,
            user_id=current_user.id,
        )
        account = await credentials_service.get_decrypted_credentials(
            session=session,
            account_id=account_id,
            user_id=current_user.id,
        )
        await credentials_service.validate_account(
            session=session,
            account_id=account_id,
            user_id=current_user.id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExchangeServiceError as exc:
        raise HTTPException(status_code=error_http_status(exc.code), detail=exc.message) from exc

    return ExchangeAccountValidateResponse(
        id=account_id,
        exchange_name=account.exchange_name,
        account_label=account_meta.account_label,
        mode=account.mode,
        status="ok",
    )
