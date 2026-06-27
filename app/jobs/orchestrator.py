from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import iso_utc, utc_now
from app.db.repositories.observability import ObservabilityRepository
from app.jobs.registry import run_registered_job

ORCHESTRATOR_VERSION = "canonical_ingestion_v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratedJob:
    name: str
    critical: bool = False
    requires_upcoming_matches: bool = False
    requires_predictions: bool = False
    requires_odds: bool = False
    requires_finished_matches: bool = False


class JobOrchestrator:
    def __init__(self, conn: AsyncConnection):
        self.conn = conn
        self.obs = ObservabilityRepository(conn)
        self.settings = get_settings()

    async def keepalive(self) -> dict[str, Any]:
        started = perf_counter()
        await self._database_ping()
        latency_ms = int((perf_counter() - started) * 1000)
        return {
            "status": "ok",
            "action": "keepalive_only",
            "database_ping": "ok",
            "database_latency_ms": latency_ms,
            "jobs_triggered": [],
            "timestamp": iso_utc(),
        }

    async def daily(self) -> dict[str, Any]:
        context = await self._build_context()
        plan = [
            OrchestratedJob("worldcup_daily_refresh", critical=True),
            OrchestratedJob("standings_refresh"),
            OrchestratedJob("qualification_resolver"),
            OrchestratedJob("odds_refresh", requires_upcoming_matches=True),
            OrchestratedJob("results_settlement", requires_finished_matches=True),
            OrchestratedJob("elo_ratings_update", requires_finished_matches=True),
            OrchestratedJob("feature_snapshot_build", requires_upcoming_matches=True),
            OrchestratedJob("model_recompute", requires_upcoming_matches=True),
            OrchestratedJob("ev_decision", requires_predictions=True, requires_odds=False),
            OrchestratedJob("calibration_recompute", requires_finished_matches=True),
            OrchestratedJob("clv_compute", requires_finished_matches=True),
            OrchestratedJob("drift_detection_full", requires_finished_matches=True),
            OrchestratedJob("pipeline_cleanup"),
            # Phase 3 — triggered manually / CI only (NOT in daily auto-plan):
            # OrchestratedJob("model_promotion")    → payload: {auto_promote: true}
            # OrchestratedJob("backtest_walk_forward")

        ]
        return await self._run_plan("daily", plan, context)

    async def live(self) -> dict[str, Any]:
        context = await self._build_context(live=True)
        plan = [
            OrchestratedJob("worldcup_live_refresh", requires_upcoming_matches=True),
            OrchestratedJob("odds_refresh", requires_upcoming_matches=True),
            OrchestratedJob("results_settlement", requires_finished_matches=True),
            OrchestratedJob("qualification_resolver"),
        ]
        return await self._run_plan("live", plan, context)

    async def should_run_job(self, job_name: str, window: str) -> bool:
        # Uses the generated column idempotency_key (migration 010) + index for fast lookup.
        row = await self.conn.execute(
            text(
                """
                select 1
                from pipeline_runs
                where job_name = :job_name
                  and status in ('OK', 'WARN')
                  and idempotency_key = :idempotency_key
                limit 1
                """
            ),
            {
                "job_name": job_name,
                "idempotency_key": f"{job_name}:{window}",
            },
        )
        return row.first() is None

    async def acquire_job_lock(self, job_name: str, window: str) -> bool:
        lock_key = self._lock_key(job_name, window)
        row = await self.conn.execute(text("select pg_try_advisory_xact_lock(hashtext(:lock_key)) as locked"), {"lock_key": lock_key})
        return bool(row.scalar_one())

    async def record_pipeline_run(
        self,
        job_name: str,
        status: str,
        records_processed: int,
        payload: dict[str, Any],
        error_message: str | None = None,
    ) -> None:
        pipeline_run_id = await self.obs.start_pipeline(job_name, payload)
        await self.obs.finish_pipeline(pipeline_run_id, status, records_processed, payload, error_message)

    async def latest_status(self) -> dict[str, Any]:
        rows = await self.conn.execute(
            text(
                """
                select job_name, status, started_at, finished_at, records_processed, error_message, payload
                from pipeline_runs
                order by started_at desc
                limit 20
                """
            )
        )
        return {
            "status": "ok",
            "runs": [self._serialize_row(dict(row._mapping)) for row in rows],
            "timestamp": iso_utc(),
        }

    async def gas_callback_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.obs.data_quality_event(
            "ANALYTICS",
            "INFO" if str(payload.get("status", "ok")).lower() in {"ok", "success"} else "WARN",
            "GAS_CALLBACK_STATUS",
            str(payload.get("message") or "GAS callback received"),
            payload,
        )
        return {"status": "ok", "received": True, "timestamp": iso_utc()}

    async def _run_plan(self, orchestration_name: str, plan: list[OrchestratedJob], context: dict[str, Any]) -> dict[str, Any]:
        window = self._window(orchestration_name)
        orchestrator_run_id = await self.obs.start_pipeline(
            f"orchestrate_{orchestration_name}",
            {
                "window": window,
                "context": context,
                "idempotency_key": f"orchestrate_{orchestration_name}:{window}",
                "orchestrator_version": ORCHESTRATOR_VERSION,
            },
        )
        executed: list[str] = []
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        records_updated = 0

        try:
            if not await self.acquire_job_lock(f"orchestrate_{orchestration_name}", window):
                return self._orchestration_response("ok", executed, [{"job": orchestration_name, "reason": "LOCKED"}], failed, 0)

            for item in plan:
                reason = await self._skip_reason(item, window, context)
                if reason:
                    skipped.append({"job": item.name, "reason": reason})
                    continue

                try:
                    logger.info("orchestrated job starting job=%s orchestrator=%s window=%s", item.name, orchestration_name, window)
                    # Each job runs inside its own savepoint so a DBAPIError in one job
                    # does not abort the outer transaction and cascade to subsequent jobs.
                    async with self.conn.begin_nested() as sp:
                        try:
                            result = await run_registered_job(
                                item.name,
                                self.conn,
                                {
                                    "orchestrator": orchestration_name,
                                    "idempotency_key": f"{item.name}:{window}",
                                    "orchestrator_version": ORCHESTRATOR_VERSION,
                                },
                            )
                            await sp.commit()
                        except Exception:
                            await sp.rollback()
                            raise
                    status = str(result.get("status", "OK")).upper()
                    records = int(result.get("records_processed") or 0)
                    records_updated += records
                    if status == "ERROR":
                        failed.append({"job": item.name, "reason": str(result.get("error") or "JOB_ERROR")})
                        if item.critical:
                            break
                    else:
                        executed.append(item.name)
                    logger.info(
                        "orchestrated job finished job=%s status=%s records=%s orchestrator=%s window=%s",
                        item.name,
                        status,
                        records,
                        orchestration_name,
                        window,
                    )
                except Exception as exc:
                    error_payload = self._error_payload(item.name, exc)
                    failed.append(error_payload)
                    logger.exception("orchestrated job failed job=%s orchestrator=%s window=%s", item.name, orchestration_name, window)
                    try:
                        await self.obs.data_quality_event(
                            "ANALYTICS",
                            "ERROR",
                            "ORCHESTRATED_JOB_ERROR",
                            f"{item.name}: {exc}",
                            {"job_name": item.name, "orchestrator": orchestration_name, "window": window, "error": error_payload},
                        )
                    except Exception:
                        logger.exception("failed to record data_quality_event for job=%s", item.name)
                    if item.critical:
                        break

            status = "ok" if not failed else "warn"
            response = self._orchestration_response(status, executed, skipped, failed, records_updated)
            await self.obs.finish_pipeline(orchestrator_run_id, "OK" if status == "ok" else "WARN", records_updated, response)
            return response
        except Exception as exc:
            await self.obs.finish_pipeline(orchestrator_run_id, "ERROR", records_updated, {"failed": failed}, str(exc))
            raise

    async def _skip_reason(self, item: OrchestratedJob, window: str, context: dict[str, Any]) -> str | None:
        if not await self.should_run_job(item.name, window):
            return "ALREADY_RAN_FOR_WINDOW"
        if item.requires_upcoming_matches and not context["has_upcoming_matches"]:
            return "NO_UPCOMING_MATCHES"
        if item.requires_finished_matches and not context["has_finished_matches"]:
            return "NO_FINISHED_MATCHES"
        if item.requires_predictions and not context["has_predictions"]:
            return "NO_PREDICTIONS"
        if item.requires_odds and not context["has_odds"]:
            return "NO_ODDS"
        return None

    async def _build_context(self, live: bool = False) -> dict[str, Any]:
        await self._database_ping()
        # For live: narrow window [-2h, +6h]. For daily: wider window [-2h, +72h].
        # Scoped to the default competition season to avoid false positives from other competitions.
        lookahead_hours = 6 if live else self.settings.odds_refresh_window_hours
        row = await self.conn.execute(
            text(
                """
                select
                  exists(
                    select 1 from matches m
                    join competition_seasons cs on cs.competition_season_id = m.competition_season_id
                    where cs.slug = :season
                      and m.kickoff_at >= now() - interval '2 hours'
                      and m.kickoff_at <= now() + make_interval(hours => :lookahead_hours)
                  ) as has_upcoming_matches,
                  exists(
                    select 1 from matches m
                    join competition_seasons cs on cs.competition_season_id = m.competition_season_id
                    where cs.slug = :season
                      and m.status = 'FINISHED'
                      and m.kickoff_at >= now() - interval '2 days'
                  ) as has_finished_matches,
                  exists(
                    select 1 from model_predictions mp
                    join matches m on m.match_id = mp.match_id
                    join competition_seasons cs on cs.competition_season_id = mp.competition_season_id
                    where cs.slug = :season
                      and mp.as_of >= now() - interval '7 days'
                  ) as has_predictions,
                  exists(
                    select 1 from odds_snapshots os
                    join matches m on m.match_id = os.match_id
                    join competition_seasons cs on cs.competition_season_id = m.competition_season_id
                    where cs.slug = :season
                      and os.captured_at >= now() - interval '24 hours'
                  ) as has_odds
                """
            ),
            {
                "season": self.settings.default_season_slug,
                "lookahead_hours": lookahead_hours,
            },
        )
        data = dict(row.first()._mapping)
        data["live"] = live
        return data

    async def _database_ping(self) -> None:
        await self.conn.execute(text("select 1"))

    def _window(self, orchestration_name: str) -> str:
        now = utc_now()
        if orchestration_name == "live":
            minute_window = (now.minute // 5) * 5
            return now.replace(minute=minute_window, second=0, microsecond=0).isoformat()
        return now.date().isoformat()

    def _orchestration_response(
        self,
        status: str,
        executed: list[str],
        skipped: list[dict[str, str]],
        failed: list[dict[str, str]],
        records_updated: int,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "executed": executed,
            "skipped": skipped,
            "failed": failed,
            "records_updated": records_updated,
            "timestamp": iso_utc(),
        }

    def _lock_key(self, job_name: str, window: str) -> str:
        return f"match-alpha:{job_name}:{window}"

    def _serialize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                row["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                pass
        for key in ("started_at", "finished_at"):
            if row.get(key):
                row[key] = iso_utc(row[key])
        return row

    def _error_payload(self, job_name: str, exc: Exception) -> dict[str, str]:
        detail = str(exc)
        return {
            "job": job_name,
            "reason": type(exc).__name__,
            "detail": detail[:1000],
        }
