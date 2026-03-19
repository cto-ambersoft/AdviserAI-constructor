from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v1.endpoints import personal_analysis as personal_analysis_endpoint
from app.db.session import get_db_session
from app.main import app
from app.models.base import Base
from app.models.personal_analysis_history import PersonalAnalysisHistory
from app.models.personal_analysis_job import PersonalAnalysisJob
from app.models.personal_analysis_profile import PersonalAnalysisProfile
from app.models.user import User
from app.schemas.personal_analysis import PERSONAL_ANALYSIS_AGENT_NAMES
from app.services.personal_analysis.provider import CoreAcceptedJob


class _FakeProvider:
    async def request_analysis(self, payload: dict[str, object]) -> CoreAcceptedJob:
        return CoreAcceptedJob(
            job_id="core-job-1",
            status="pending",
            created_at=datetime.now(UTC),
            expires_at=None,
        )

    async def check_status_batch(self, job_ids: list[str]) -> list[object]:
        return []

    async def fetch_result(self, job_id: str) -> object:
        raise NotImplementedError

    async def delete_job(self, job_id: str) -> bool:
        return True


@pytest.fixture
async def personal_analysis_db(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "personal_analysis_endpoints.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def override_db_session(personal_analysis_db: async_sessionmaker[AsyncSession]) -> None:
    async def _get_test_db_session() -> AsyncIterator[AsyncSession]:
        async with personal_analysis_db() as session:
            yield session

    app.dependency_overrides[get_db_session] = _get_test_db_session
    yield
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture(autouse=True)
def override_current_user() -> None:
    async def _fake_current_user() -> User:
        return User(id=1, email="test@example.com", hashed_password="x", is_active=True)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def override_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        personal_analysis_endpoint.personal_analysis_service,
        "_provider",
        _FakeProvider(),
    )


async def test_personal_analysis_profile_crud_and_validation() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/analysis/personal/profiles",
            json={"symbol": "BTCUSDT", "interval_minutes": 60},
        )
        assert created.status_code == 200
        profile = created.json()
        assert profile["symbol"] == "BTCUSDT"
        assert profile["agents"]["twitterSentiment"] is True
        assert profile["agent_weights"]["twitterSentiment"] == 1.0

        invalid_update = await client.put(
            f"/api/v1/analysis/personal/profiles/{profile['id']}",
            json={"agents": {"unknownAgent": True}},
        )
        assert invalid_update.status_code == 422

        updated = await client.put(
            f"/api/v1/analysis/personal/profiles/{profile['id']}",
            json={
                "interval_minutes": 120,
                "agents": {"twitterSentiment": True, "newsSearch": True},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["interval_minutes"] == 120

        listed = await client.get("/api/v1/analysis/personal/profiles")
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        deleted = await client.delete(f"/api/v1/analysis/personal/profiles/{profile['id']}")
        assert deleted.status_code == 204

        listed_after = await client.get("/api/v1/analysis/personal/profiles")
        assert listed_after.status_code == 200
        assert listed_after.json()[0]["is_active"] is False


async def test_personal_analysis_defaults_endpoint_returns_expected_agents_and_weights() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/analysis/personal/defaults")
        assert response.status_code == 200
        payload = response.json()

    assert payload["available_agents"] == list(PERSONAL_ANALYSIS_AGENT_NAMES)
    assert set(payload["agents"].keys()) == set(PERSONAL_ANALYSIS_AGENT_NAMES)
    assert set(payload["agent_weights"].keys()) == set(PERSONAL_ANALYSIS_AGENT_NAMES)
    assert all(bool(payload["agents"][agent_name]) for agent_name in PERSONAL_ANALYSIS_AGENT_NAMES)
    assert all(
        float(payload["agent_weights"][agent_name]) == 1.0
        for agent_name in PERSONAL_ANALYSIS_AGENT_NAMES
    )


async def test_personal_analysis_manual_trigger_creates_job_and_status_endpoint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/analysis/personal/profiles",
            json={"symbol": "ETHUSDT", "interval_minutes": 60},
        )
        assert created.status_code == 200
        profile_id = int(created.json()["id"])

        triggered = await client.post(
            f"/api/v1/analysis/personal/profiles/{profile_id}/trigger",
            json={},
        )
        assert triggered.status_code == 202
        body = triggered.json()
        assert body["core_job_id"] == "core-job-1"
        assert body["status"] == "pending"

        job = await client.get(f"/api/v1/analysis/personal/jobs/{body['trade_job_id']}")
        assert job.status_code == 200
        assert job.json()["core_job_id"] == "core-job-1"
        assert job.json()["status"] == "pending"

        # Backward-compatible lookup by core job id.
        by_core = await client.get(f"/api/v1/analysis/personal/jobs/{body['core_job_id']}")
        assert by_core.status_code == 200
        assert by_core.json()["id"] == body["trade_job_id"]


async def test_personal_analysis_history_and_latest(
    personal_analysis_db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with personal_analysis_db() as session:
        profile = PersonalAnalysisProfile(
            user_id=1,
            symbol="BTCUSDT",
            query_prompt=None,
            agents={"twitterSentiment": True},
            agent_weights={"twitterSentiment": 1.0},
            interval_minutes=60,
            is_active=True,
            next_run_at=now,
            last_triggered_at=None,
            last_completed_at=None,
        )
        session.add(profile)
        await session.flush()

        job = PersonalAnalysisJob(
            id="job-1",
            user_id=1,
            profile_id=profile.id,
            core_job_id="core-job-history",
            status="completed",
            attempt=1,
            max_attempts=3,
            error=None,
            payload_json={"symbol": "BTCUSDT"},
            next_poll_at=now,
            completed_at=now,
            core_deleted_at=now,
        )
        session.add(job)
        await session.flush()

        history = PersonalAnalysisHistory(
            user_id=1,
            profile_id=profile.id,
            trade_job_id=job.id,
            symbol="BTCUSDT",
            analysis_data={
                "analysisReport": "ok",
                "analysisStructured": {
                    "bias": "NEUTRAL",
                    "confidence": 0.65,
                    "keyLevels": {
                        "support": 65821.97,
                        "resistance": 71777,
                    },
                },
                "trendExtraction": {
                    "neutral": {
                        "probabilityPct": 0,
                        "takeProfit": None,
                        "stopLoss": None,
                    }
                },
            },
            core_completed_at=now - timedelta(minutes=1),
        )
        session.add(history)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        history_resp = await client.get("/api/v1/analysis/personal/history")
        assert history_resp.status_code == 200
        assert len(history_resp.json()) == 1
        assert history_resp.json()[0]["analysis_data"]["analysisReport"] == "ok"
        flat = history_resp.json()[0]["analysis_data"]["trendExtraction"]["flat"]
        assert flat["probabilityPct"] == 65
        assert flat["takeProfit"] == 71777
        assert flat["stopLoss"] == 65821.97

        latest_resp = await client.get(
            "/api/v1/analysis/personal/latest",
            params={"symbol": "BTCUSDT"},
        )
        assert latest_resp.status_code == 200
        assert latest_resp.json()["trade_job_id"] == "job-1"
        latest_flat = latest_resp.json()["analysis_data"]["trendExtraction"]["flat"]
        assert latest_flat["probabilityPct"] == 65
