import asyncio
import sys
import types
from typing import Any

# Keep this unit test isolated from registry optional dependencies.
_registry_stub = types.ModuleType("app.jobs.registry")


async def _noop_run_registered_job(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"status": "OK", "records_processed": 0}


_registry_stub.run_registered_job = _noop_run_registered_job
sys.modules.setdefault("app.jobs.registry", _registry_stub)

from app.jobs.orchestrator import JobOrchestrator, OrchestratedJob


class _DummySavepoint:
    async def __aenter__(self) -> "_DummySavepoint":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _DummyConn:
    def begin_nested(self) -> _DummySavepoint:
        return _DummySavepoint()


def test_run_plan_refreshes_context_for_finished_matches(monkeypatch: Any) -> None:
    orchestrator = JobOrchestrator(_DummyConn())

    async def _start_pipeline(*args: Any, **kwargs: Any) -> str:
        return "run-id"

    async def _finish_pipeline(*args: Any, **kwargs: Any) -> None:
        return None

    async def _acquire_job_lock(*args: Any, **kwargs: Any) -> bool:
        return True

    async def _should_run_job(*args: Any, **kwargs: Any) -> bool:
        return True

    refreshed_calls = {"count": 0}

    async def _build_context(*args: Any, **kwargs: Any) -> dict[str, Any]:
        refreshed_calls["count"] += 1
        return {
            "has_upcoming_matches": False,
            "has_finished_matches": True,
            "has_predictions": False,
            "has_odds": False,
            "live": True,
        }

    async def _fake_run_registered_job(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "OK", "records_processed": 1}

    orchestrator.obs.start_pipeline = _start_pipeline  # type: ignore[method-assign]
    orchestrator.obs.finish_pipeline = _finish_pipeline  # type: ignore[method-assign]
    orchestrator.acquire_job_lock = _acquire_job_lock  # type: ignore[method-assign]
    orchestrator.should_run_job = _should_run_job  # type: ignore[method-assign]
    orchestrator._build_context = _build_context  # type: ignore[method-assign]

    monkeypatch.setattr("app.jobs.orchestrator.run_registered_job", _fake_run_registered_job)

    result = asyncio.run(
        orchestrator._run_plan(
            orchestration_name="live",
            plan=[OrchestratedJob("results_settlement", requires_finished_matches=True)],
            context={
                "has_upcoming_matches": False,
                "has_finished_matches": False,
                "has_predictions": False,
                "has_odds": False,
                "live": True,
            },
        )
    )

    assert refreshed_calls["count"] == 1
    assert result["executed"] == ["results_settlement"]
    assert result["skipped"] == []
