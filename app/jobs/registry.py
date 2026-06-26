import json
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import iso_utc
from app.competitions.service import discover_competition_sources, seed_competition_catalog, sync_competition_fixtures
from app.db.repositories.betting import BettingRepository
from app.db.repositories.observability import ObservabilityRepository
from app.decision.decision_engine import evaluate_decision

JobFn = Callable[[AsyncConnection, dict[str, Any]], Awaitable[dict[str, Any]]]


async def placeholder_job(conn: AsyncConnection, job_name: str) -> dict[str, Any]:
    _ = conn
    return {
        "status": "WARN",
        "job_name": job_name,
        "records_processed": 0,
        "message": "Job scaffold created; source-specific ingestion/model logic must be filled in next iteration.",
        "generated_at": iso_utc(),
    }


async def ev_decision_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    _ = payload
    repo = BettingRepository(conn)
    candidates = await repo.eligible_prediction_odds()
    inserted = 0
    for candidate in candidates:
        decision = evaluate_decision(candidate)
        await repo.insert_decision(decision)
        inserted += 1
    return {"status": "OK", "job_name": "ev_decision", "records_processed": inserted}


async def drift_detection_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    _ = payload
    settings = get_settings()
    await conn.execute(
        text(
            """
        insert into drift_reports (competition_season_id, model_id, feature_set_version, drift_score, severity, payload)
        select cs.competition_season_id, null, null, 0, 'INFO', cast(:payload as jsonb)
        from competition_seasons cs
        where cs.slug = :season
        limit 1
        """,
        ),
        {"season": settings.default_season_slug, "payload": json.dumps({"method": "baseline_zero_drift", "generated_at": iso_utc()})},
    )
    return {"status": "OK", "job_name": "drift_detection", "records_processed": 1}


async def seed_competition_catalog_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    return await seed_competition_catalog(conn, payload.get("competition"))


async def discover_competition_sources_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    return await discover_competition_sources(conn, payload.get("competition"))


async def sync_competition_fixtures_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    return await sync_competition_fixtures(conn, payload.get("competition"))


async def run_registered_job(job_name: str, conn: AsyncConnection, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    jobs: dict[str, JobFn] = {
        "ev_decision": ev_decision_job,
        "drift_detection": drift_detection_job,
        "seed_competition_catalog": seed_competition_catalog_job,
        "discover_competition_sources": discover_competition_sources_job,
        "sync_competition_fixtures": sync_competition_fixtures_job,
    }
    scaffold_jobs = {
        "worldcup_daily_refresh",
        "worldcup_live_refresh",
        "odds_refresh",
        "feature_snapshot_build",
        "dataset_builder",
        "model_recompute",
        "settlement",
        "calibration_recompute",
        "backtest_walk_forward",
        "model_promotion",
    }
    obs = ObservabilityRepository(conn)
    pipeline_run_id = await obs.start_pipeline(job_name, {"runner": "fastapi", **payload})
    try:
        if job_name in jobs:
            result = await jobs[job_name](conn, payload)
        elif job_name in scaffold_jobs:
            result = await placeholder_job(conn, job_name)
        else:
            result = {"status": "ERROR", "job_name": job_name, "records_processed": 0, "error": "unknown job"}
        await obs.finish_pipeline(
            pipeline_run_id,
            result.get("status", "OK"),
            int(result.get("records_processed") or 0),
            result,
            result.get("error"),
        )
        return result
    except Exception as exc:
        await obs.data_quality_event("ANALYTICS", "ERROR", "JOB_ERROR", f"{job_name}: {exc}", {"job_name": job_name})
        await obs.finish_pipeline(pipeline_run_id, "ERROR", 0, {"job_name": job_name}, str(exc))
        raise
