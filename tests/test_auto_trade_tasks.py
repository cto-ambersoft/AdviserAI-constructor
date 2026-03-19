import pytest

from app.worker import tasks as worker_tasks


class _DummySessionFactory:
    def __call__(self) -> "_DummySessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


async def test_process_auto_trade_signal_queue_task_uses_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_process(*, session: object) -> dict[str, int]:
        assert session is not None
        return {
            "polled": 3,
            "completed": 2,
            "skipped": 1,
            "retried": 0,
            "dead": 0,
            "errors": 0,
        }

    monkeypatch.setattr(worker_tasks, "AsyncSessionFactory", _DummySessionFactory())
    monkeypatch.setattr(
        worker_tasks.auto_trade_service,
        "process_signal_queue",
        _fake_process,
    )
    result = await worker_tasks.process_auto_trade_signal_queue()
    assert result["polled"] == 3
    assert result["completed"] == 2
    assert result["skipped"] == 1

async def test_sync_auto_trade_exchange_trades_task_uses_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_sync(*, session: object) -> dict[str, int]:
        assert session is not None
        return {
            "configs": 1,
            "synced": 1,
            "inserted_or_updated": 2,
            "errors": 0,
        }

    monkeypatch.setattr(worker_tasks, "AsyncSessionFactory", _DummySessionFactory())
    monkeypatch.setattr(
        worker_tasks.trade_sync_service,
        "sync_running_configs",
        _fake_sync,
    )
    result = await worker_tasks.sync_auto_trade_exchange_trades()
    assert result["synced"] == 1
    assert result["inserted_or_updated"] == 2

