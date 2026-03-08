import pytest

from app.worker import tasks as worker_tasks


class _DummySessionFactory:
    def __call__(self) -> "_DummySessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


async def test_dispatch_due_personal_analysis_task_uses_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_dispatch(*, session: object) -> dict[str, int]:
        assert session is not None
        return {"triggered": 3, "errors": 0}

    monkeypatch.setattr(worker_tasks, "AsyncSessionFactory", _DummySessionFactory())
    monkeypatch.setattr(
        worker_tasks.personal_analysis_service,
        "dispatch_due_profiles",
        _fake_dispatch,
    )
    result = await worker_tasks.dispatch_due_personal_analysis()
    assert result == {"triggered": 3, "errors": 0}


async def test_poll_personal_analysis_jobs_task_uses_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_poll(*, session: object) -> dict[str, int]:
        assert session is not None
        return {
            "polled": 10,
            "completed": 5,
            "failed": 0,
            "retried": 1,
            "cleanup_pending": 0,
            "errors": 0,
        }

    monkeypatch.setattr(worker_tasks, "AsyncSessionFactory", _DummySessionFactory())
    monkeypatch.setattr(
        worker_tasks.personal_analysis_service,
        "poll_pending_jobs",
        _fake_poll,
    )
    result = await worker_tasks.poll_personal_analysis_jobs()
    assert result["polled"] == 10
    assert result["completed"] == 5
