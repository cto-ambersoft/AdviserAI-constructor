from fastapi import APIRouter

router = APIRouter()


@router.get("/health", summary="Readiness probe")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
