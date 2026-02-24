from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.base  # noqa: F401
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base


@pytest.fixture
async def auth_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "auth_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.pop(get_db_session, None)
    await engine.dispose()


async def test_protected_endpoint_requires_token(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/v1/market/meta")
    assert response.status_code == 401


async def test_signup_and_me_flow(auth_client: AsyncClient) -> None:
    signup_payload = {"email": "alice@example.com", "password": "StrongPass123"}
    signup_response = await auth_client.post("/api/v1/auth/signup", json=signup_payload)
    assert signup_response.status_code == 201
    body = signup_response.json()
    assert body["user"]["email"] == "alice@example.com"
    token = body["token"]["access_token"]
    assert isinstance(body["token"]["refresh_token"], str)

    me_response = await auth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "alice@example.com"


async def test_signup_duplicate_email_is_rejected(auth_client: AsyncClient) -> None:
    payload = {"email": "dupe@example.com", "password": "StrongPass123"}
    first = await auth_client.post("/api/v1/auth/signup", json=payload)
    second = await auth_client.post("/api/v1/auth/signup", json=payload)
    assert first.status_code == 201
    assert second.status_code == 409


async def test_signin_success_returns_token(auth_client: AsyncClient) -> None:
    payload = {"email": "login@example.com", "password": "StrongPass123"}
    signup = await auth_client.post("/api/v1/auth/signup", json=payload)
    assert signup.status_code == 201

    signin = await auth_client.post("/api/v1/auth/signin", json=payload)
    assert signin.status_code == 200
    body = signin.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)
    assert isinstance(body["refresh_token"], str)
    assert body["refresh_expires_in"] > body["expires_in"]


async def test_signin_wrong_password_returns_401(auth_client: AsyncClient) -> None:
    signup_payload = {"email": "wrongpass@example.com", "password": "StrongPass123"}
    signin_payload = {"email": "wrongpass@example.com", "password": "bad-password"}
    signup = await auth_client.post("/api/v1/auth/signup", json=signup_payload)
    assert signup.status_code == 201

    signin = await auth_client.post("/api/v1/auth/signin", json=signin_payload)
    assert signin.status_code == 401


async def test_refresh_token_flow_rotates_tokens(auth_client: AsyncClient) -> None:
    payload = {"email": "refresh@example.com", "password": "StrongPass123"}
    signup = await auth_client.post("/api/v1/auth/signup", json=payload)
    assert signup.status_code == 201
    refresh_token = signup.json()["token"]["refresh_token"]

    refresh_response = await auth_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_response.status_code == 200
    refreshed = refresh_response.json()
    assert isinstance(refreshed["access_token"], str)
    assert isinstance(refreshed["refresh_token"], str)
    assert refreshed["refresh_token"] != refresh_token

    replay_response = await auth_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert replay_response.status_code == 401
    assert replay_response.json()["detail"] == "Refresh token already used."


async def test_refresh_token_invalid_returns_401(auth_client: AsyncClient) -> None:
    payload = {"email": "invalid-refresh@example.com", "password": "StrongPass123"}
    signup = await auth_client.post("/api/v1/auth/signup", json=payload)
    assert signup.status_code == 201

    response = await auth_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "not-a-valid-token"},
    )
    assert response.status_code == 401
