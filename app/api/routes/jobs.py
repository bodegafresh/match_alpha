from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.security import require_internal_key
from app.core.time import iso_utc, utc_now
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


@router.post("/orchestrate/weekly")
async def orchestrate_weekly(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).weekly()


@router.get("/status/latest")
async def latest_job_status(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await JobOrchestrator(conn).latest_status()


@router.get("/status/health")
async def jobs_health(
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    """Freshness and health summary — ideal for monitoring dashboards and GAS callbacks."""
    result = await conn.execute(
        text(
            """
            select
              (select started_at from pipeline_runs
               where job_name = 'worldcup_daily_refresh' and status = 'OK'
               order by started_at desc limit 1) as last_daily_refresh_at,

              (select started_at from pipeline_runs
               where job_name = 'worldcup_live_refresh' and status = 'OK'
               order by started_at desc limit 1) as last_live_refresh_at,

              (select count(*) from data_quality_events
               where severity in ('ERROR', 'CRITICAL')
                 and created_at >= now() - interval '24 hours') as errors_24h,

              (select count(*) from data_quality_events
               where severity = 'WARN'
                 and created_at >= now() - interval '24 hours') as warnings_24h,

              (select count(*) from matches
               where kickoff_at::date = current_date
                 and status != 'CANCELLED') as matches_today,

              (select count(*) from matches
               where status = 'LIVE') as matches_live_now,

              (select message from data_quality_events
               where severity in ('ERROR', 'CRITICAL')
               order by created_at desc limit 1) as last_error_message,

              (select started_at from pipeline_runs
               where status = 'ERROR'
               order by started_at desc limit 1) as last_job_error_at,

              (select job_name from pipeline_runs
               where status = 'ERROR'
               order by started_at desc limit 1) as last_job_error_name
            """
        )
    )
    row = dict(result.first()._mapping)

    now = utc_now()
    last_daily = row["last_daily_refresh_at"]
    daily_age_hours: float | None = None
    if last_daily:
        daily_age_hours = round((now - last_daily).total_seconds() / 3600, 1)

    last_live = row["last_live_refresh_at"]
    live_age_minutes: float | None = None
    if last_live:
        live_age_minutes = round((now - last_live).total_seconds() / 60, 1)

    return {
        "ok": True,
        "data": {
            "daily_refresh": {
                "last_ran_at": iso_utc(last_daily) if last_daily else None,
                "age_hours": daily_age_hours,
                "is_fresh": daily_age_hours is not None and daily_age_hours < 26,
            },
            "live_refresh": {
                "last_ran_at": iso_utc(last_live) if last_live else None,
                "age_minutes": live_age_minutes,
                "is_fresh": live_age_minutes is not None and live_age_minutes < 20,
            },
            "quality": {
                "errors_24h": int(row["errors_24h"]),
                "warnings_24h": int(row["warnings_24h"]),
                "last_error_message": row["last_error_message"],
                "last_job_error_at": iso_utc(row["last_job_error_at"]) if row["last_job_error_at"] else None,
                "last_job_error_name": row["last_job_error_name"],
            },
            "matches": {
                "today": int(row["matches_today"]),
                "live_now": int(row["matches_live_now"]),
            },
            "checked_at": iso_utc(),
        },
    }


@router.post("/{job_name}/run")
async def run_job(
    job_name: str,
    payload: dict[str, Any] | None = Body(default=None),
    _: None = Depends(require_internal_key),
    conn: AsyncConnection = Depends(get_connection),
) -> dict:
    return await run_registered_job(job_name, conn, payload or {})
