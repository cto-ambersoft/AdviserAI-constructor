import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.auto_trade.service import (
    AutoTradeService,
    install_auto_trade_runtime,
)
from app.services.notifications.service import install_telegram_webhook

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.log_level)

    auto_trade_service = AutoTradeService()
    runtime_task: asyncio.Task[None] | None = None
    try:
        runtime_task = await install_auto_trade_runtime(auto_trade_service)
    except Exception:
        logger.exception("auto_trade runtime startup failed; continuing without it")
    try:
        await install_telegram_webhook()
    except Exception:
        logger.exception("telegram webhook install failed; continuing without it")
    try:
        yield
    finally:
        if runtime_task is not None and not runtime_task.done():
            runtime_task.cancel()
            try:
                await runtime_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)
app.include_router(api_router, prefix=settings.api_v1_prefix)
