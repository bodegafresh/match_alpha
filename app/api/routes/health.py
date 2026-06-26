import json
from asyncio import TimeoutError as AsyncTimeoutError
from time import perf_counter

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.time import iso_utc
from app.db.session import engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "service": settings.app_name,
        "status": "OK",
        "supabase": "configured" if settings.database_url else "not_configured",
        "checked_at": iso_utc(),
    }


@router.get("/health/supabase")
async def supabase_health() -> dict:
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
