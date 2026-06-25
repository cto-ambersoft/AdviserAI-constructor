import pytest

from app.worker import tasks as worker_tasks


class _DummySessionFactory:
    def __call__(self) -> "_DummySessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


async def test_dispatch_trade_notifications_task_uses_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_dispatch(*, session: object) -> dict[str, int]:
        assert session is not None
        return {"polled": 2, "sent": 1, "skipped": 1, "failed": 0, "errors": 0}

    monkeypatch.setattr(worker_tasks, "AsyncSessionFactory", _DummySessionFactory())
    monkeypatch.setattr(
        worker_tasks.telegram_notify_service,
        "dispatch_pending",
        _fake_dispatch,
    )
    result = await worker_tasks.dispatch_trade_notifications()
    assert result["sent"] == 1
    assert result["polled"] == 2
