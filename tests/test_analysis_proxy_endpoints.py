import pytest
from fastapi import HTTPException, status
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.v1.endpoints import analysis
from app.main import app
from app.models.user import User


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="test@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def test_trigger_analysis_now_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_trigger_now():
        from fastapi.responses import JSONResponse

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "Manual BTC analysis started",
                "jobId": "abc123",
            },
            status_code=status.HTTP_202_ACCEPTED,
        )

    monkeypatch.setattr(analysis.analysis_proxy_service, "trigger_now", _fake_trigger_now)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/analysis/trigger-now")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["jobId"] == "abc123"


async def test_get_analysis_runs_forward_query(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_runs(*, date: str | None, limit: str | None):
        from fastapi.responses import JSONResponse

        assert date == "2026-02-20"
        assert limit == "1"
        return JSONResponse(content={"total": 1, "runs": [{"_id": "r1"}]}, status_code=200)

    monkeypatch.setattr(analysis.analysis_proxy_service, "get_runs", _fake_get_runs)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/analysis/runs", params={"date": "2026-02-20", "limit": "1"}
        )
    assert response.status_code == 200
    assert response.json()["runs"][0]["_id"] == "r1"


async def test_market_state_route_preferred_over_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_market_state():
        from fastapi.responses import JSONResponse

        return JSONResponse(content={"mode": "risk-off"}, status_code=200)

    async def _fake_symbol_analysis(*, symbol: str):
        from fastapi.responses import JSONResponse

        return JSONResponse(content={"symbol": symbol}, status_code=200)

    monkeypatch.setattr(analysis.analysis_proxy_service, "get_market_state", _fake_market_state)
    monkeypatch.setattr(
        analysis.analysis_proxy_service, "get_symbol_analysis", _fake_symbol_analysis
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/analysis/market-state")
    assert response.status_code == 200
    assert response.json() == {"mode": "risk-off"}


async def test_get_symbol_analysis_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_symbol_analysis(*, symbol: str):
        from fastapi.responses import JSONResponse

        return JSONResponse(content={"symbol": f"{symbol}USDT", "bias": "BEARISH"}, status_code=200)

    monkeypatch.setattr(
        analysis.analysis_proxy_service, "get_symbol_analysis", _fake_symbol_analysis
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/analysis/BTC")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["bias"] == "BEARISH"


async def test_analysis_proxy_error_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_market_state():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="downstream unavailable",
        )

    monkeypatch.setattr(analysis.analysis_proxy_service, "get_market_state", _fake_market_state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/analysis/market-state")
    assert response.status_code == 502
    assert response.json()["detail"] == "downstream unavailable"
