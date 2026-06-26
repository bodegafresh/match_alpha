import json
from asyncio import TimeoutError as AsyncTimeoutError
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.security import require_internal_key
from app.core.time import iso_utc
from app.db.session import engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(_: None = Depends(require_internal_key)) -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "timestamp": iso_utc(),
    }


@router.get("/health/deep")
async def deep_health(_: None = Depends(require_internal_key)) -> dict:
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "backend": "ok",
                "database": "not_configured",
                "timestamp": iso_utc(),
            },
        )

    started = perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
            row = await conn.execute(
                text(
                    """
                    select job_name, status, started_at, finished_at, records_processed
                    from pipeline_runs
                    order by started_at desc
                    limit 1
                    """
                )
            )
            latest = row.first()
            latency_ms = int((perf_counter() - started) * 1000)
    except (AsyncTimeoutError, TimeoutError, OSError, SQLAlchemyError) as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "backend": "ok",
                "database": "error",
                "error_type": type(exc).__name__,
                "latency_ms": latency_ms,
                "timestamp": iso_utc(),
            },
        ) from exc

    last_pipeline_run = None
    if latest:
        data = dict(latest._mapping)
        for key in ("started_at", "finished_at"):
            if data.get(key):
                data[key] = iso_utc(data[key])
        last_pipeline_run = data
    return {
        "status": "ok",
        "backend": "ok",
        "database": "ok",
        "database_latency_ms": latency_ms,
        "last_pipeline_run": last_pipeline_run,
        "timestamp": iso_utc(),
    }


@router.get("/health/supabase")
async def supabase_health(_: None = Depends(require_internal_key)) -> dict:
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "status": "NOT_CONFIGURED",
                "message": "DATABASE_URL is not configured",
                "checked_at": iso_utc(),
            },
        )

    started = perf_counter()
    try:
        async with engine.connect() as conn:
            counts = {}
            for table in ("competitions", "competition_seasons", "teams", "matches", "odds_snapshots", "model_predictions"):
                result = await conn.execute(text(f"select count(*) from {table}"))
                counts[table] = result.scalar_one()
            latency_ms = int((perf_counter() - started) * 1000)
        heartbeat_warning = None
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        insert into supabase_heartbeats (heartbeat_key, service_name, status, latency_ms, details)
                        values ('fastapi-health', :service_name, 'OK', :latency_ms, cast(:details as jsonb))
                        on conflict (heartbeat_key) do update
                        set checked_at = now(),
                            service_name = excluded.service_name,
                            status = excluded.status,
                            latency_ms = excluded.latency_ms,
                            details = excluded.details
                        """
                    ),
                    {
                        "service_name": get_settings().app_name,
                        "latency_ms": latency_ms,
                        "details": json.dumps({"counts": counts}),
                    },
                )
        except SQLAlchemyError as exc:
            heartbeat_warning = {
                "status": "HEARTBEAT_WRITE_SKIPPED",
                "error_type": type(exc).__name__,
                "detail": str(exc),
            }
    except (AsyncTimeoutError, TimeoutError, OSError, SQLAlchemyError) as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "status": "UNAVAILABLE",
                "message": "Could not complete Supabase/Postgres health check",
                "error_type": type(exc).__name__,
                "detail": str(exc),
                "latency_ms": latency_ms,
                "checked_at": iso_utc(),
                "hints": [
                    "Verify DATABASE_URL uses postgresql+asyncpg://",
                    "If you are using Supabase, prefer the transaction/session pooler URL when direct DB is unreachable",
                    "Check whether the Supabase Free project is paused",
                    "Check password, host, port, and network/VPN/firewall",
                ],
            },
        ) from exc
    response = {"ok": True, "status": "OK", "counts": counts, "latency_ms": latency_ms, "checked_at": iso_utc()}
    if heartbeat_warning:
        response["warning"] = heartbeat_warning
    return response
