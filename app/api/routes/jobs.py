from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.security import require_internal_key
from app.db.session import get_connection
from app.jobs.orchestrator import JobOrchestrator
from app.jobs.registry import run_registered_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/orchestrate/keepalive")
async def orchestrate_keepalive(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).keepalive()


@router.post("/orchestrate/daily")
async def orchestrate_daily(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).daily()


@router.post("/orchestrate/live")
async def orchestrate_live(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).live()


@router.get("/status/latest")
async def latest_job_status(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).latest_status()


@router.post("/{job_name}/run")
async def run_job(
    job_name: str,
    payload: dict[str, Any] | None = Body(default=None),
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await run_registered_job(job_name, conn, payload or {})
