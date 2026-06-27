import json
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import iso_utc, utc_now
from app.competitions.service import (
    discover_competition_sources,
    seed_competition_catalog,
    sync_competition_fixtures,
    worldcup_daily_refresh,
    worldcup_live_refresh,
)
from app.db.repositories.betting import BettingRepository
from app.db.repositories.observability import ObservabilityRepository
from app.decision.decision_engine import evaluate_decision
from app.features.calculators.ratings import update_elo_from_recent_matches
from app.features.snapshot_builder import (
    build_match_feature_snapshots,
    get_matches_needing_snapshots,
)
from app.calibration.evaluator import run_calibration
from app.feedback.clv_calculator import compute_pending_clv
from app.feedback.settlement_service import settle_pending_decisions
from app.models.poisson_predictor import _get_or_create_model_registry, run_poisson_prediction

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


async def worldcup_daily_refresh_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    return await worldcup_daily_refresh(conn, payload.get("competition"))


async def worldcup_live_refresh_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    return await worldcup_live_refresh(conn, payload.get("competition"))


async def elo_ratings_update_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Incremental ELO update — processes only unprocessed FINISHED matches."""
    _ = payload
    result = await update_elo_from_recent_matches(conn)
    return {
        "status": "OK",
        "job_name": "elo_ratings_update",
        "records_processed": result["processed"],
        "rating_types": result["rating_types"],
    }


async def feature_snapshot_build_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Materialize feature snapshots for upcoming matches."""
    _ = payload
    match_ids = await get_matches_needing_snapshots(conn, days_ahead=14, days_behind=1)
    built = 0
    errors = 0
    for match_id in match_ids:
        result = await build_match_feature_snapshots(conn, match_id)
        if "error" in result:
            errors += 1
        else:
            built += 1
    status = "OK" if errors == 0 else "WARN"
    return {
        "status": status,
        "job_name": "feature_snapshot_build",
        "records_processed": built,
        "errors": errors,
    }


async def results_settlement_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Settle pending betting decisions against finished match results."""
    _ = payload
    result = await settle_pending_decisions(conn)
    return {
        "status": result["status"],
        "job_name": "results_settlement",
        "records_processed": result["settled"],
        "skipped_no_resolver": result["skipped_no_resolver"],
        "errors": result["errors"],
        "registered_resolvers": result["registered_resolvers"],
    }


async def model_recompute_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Run Poisson predictor for upcoming matches without predictions."""
    _ = payload
    settings = get_settings()

    # Get champion model or create it
    model_id = await _get_or_create_model_registry(conn)

    # Get season
    row = await conn.execute(
        text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
        {"slug": settings.default_season_slug},
    )
    r = row.fetchone()
    if not r:
        return {"status": "WARN", "job_name": "model_recompute", "records_processed": 0, "message": "season not found"}
    season_id = r[0]

    # Create model run
    run_row = await conn.execute(
        text("""
            INSERT INTO model_runs (model_id, competition_season_id, run_status, prediction_as_of,
                                    feature_set_version, dataset_version, params)
            VALUES (cast(:model_id as uuid), cast(:season_id as uuid), 'STARTED', :as_of,
                    'v1', 'v1', cast(:params as jsonb))
            RETURNING model_run_id::text
        """),
        {
            "model_id": model_id,
            "season_id": season_id,
            "as_of": utc_now(),
            "params": json.dumps({"model": "poisson_elo_v1"}),
        },
    )
    model_run_id = run_row.fetchone()[0]

    # Matches with feature snapshots but no predictions (next 14 days)
    matches = await conn.execute(
        text("""
            SELECT DISTINCT
              m.match_id::text,
              home_p.team_id::text  AS home_team_id,
              away_p.team_id::text  AS away_team_id,
              m.competition_season_id::text
            FROM matches m
            JOIN match_participants home_p ON home_p.match_id = m.match_id AND home_p.side = 'HOME'
            JOIN match_participants away_p ON away_p.match_id = m.match_id AND away_p.side = 'AWAY'
            WHERE m.competition_season_id = cast(:season_id as uuid)
              AND m.status = 'SCHEDULED'
              AND m.kickoff_at BETWEEN now() AND now() + interval '14 days'
              AND EXISTS (
                SELECT 1 FROM feature_snapshots fs
                WHERE fs.match_id = m.match_id AND fs.team_side IS NOT NULL
              )
              AND NOT EXISTS (
                SELECT 1 FROM model_predictions mp
                WHERE mp.match_id = m.match_id
                  AND mp.model_run_id = cast(:run_id as uuid)
              )
            ORDER BY m.kickoff_at ASC
        """),
        {"season_id": season_id, "run_id": model_run_id},
    )
    match_rows = [dict(r._mapping) for r in matches]

    predicted = 0
    errors = 0
    for m in match_rows:
        result = await run_poisson_prediction(
            conn,
            match_id=m["match_id"],
            home_team_id=m["home_team_id"],
            away_team_id=m["away_team_id"],
            competition_season_id=m["competition_season_id"],
            model_run_id=model_run_id,
        )
        if "error" in result:
            errors += 1
        else:
            predicted += 1

    # Update model run status
    await conn.execute(
        text("""
            UPDATE model_runs SET run_status = :status, training_window_end_at = :now
            WHERE model_run_id = cast(:run_id as uuid)
        """),
        {
            "status": "OK" if errors == 0 else "WARN",
            "now": utc_now(),
            "run_id": model_run_id,
        },
    )

    return {
        "status": "OK" if errors == 0 else "WARN",
        "job_name": "model_recompute",
        "records_processed": predicted,
        "model_run_id": model_run_id,
        "errors": errors,
    }


async def odds_refresh_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch live odds from The Odds API and store as append-only snapshots."""
    _ = payload
    settings = get_settings()
    if not settings.the_odds_api_key:
        return {"status": "WARN", "job_name": "odds_refresh", "records_processed": 0, "message": "THE_ODDS_API_KEY not set"}

    from app.clients.odds_api_client import OddsApiClient
    from app.core.hashing import sha256_json

    client = OddsApiClient()

    # Get bookmaker and market IDs
    bm_rows = await conn.execute(text("SELECT slug, bookmaker_id::text FROM bookmaker_profiles"))
    bookmaker_map = {r[0]: r[1] for r in bm_rows}

    mkt_rows = await conn.execute(text("SELECT market_code, market_id::text FROM markets"))
    market_map = {r[0]: r[1] for r in mkt_rows}

    sel_rows = await conn.execute(
        text("SELECT market_id::text, selection_code, selection_id::text FROM market_selections")
    )
    sel_map: dict[tuple, str] = {(r[0], r[1]): r[2] for r in sel_rows}

    inserted = 0
    captured_at = utc_now()

    try:
        data = await client.odds(sport="soccer_fifa_world_cup", regions="eu", markets="h2h")
    except Exception as exc:
        return {"status": "WARN", "job_name": "odds_refresh", "records_processed": 0, "error": str(exc)}

    for event in (data or []):
        source_event_id = event.get("id")
        if not source_event_id:
            continue

        # Resolve match_id from entity_external_refs
        ref_row = await conn.execute(
            text("""
                SELECT entity_id::text FROM entity_external_refs
                WHERE source = 'THE_ODDS_API' AND source_entity_id = :eid AND is_primary = true
                LIMIT 1
            """),
            {"eid": source_event_id},
        )
        ref = ref_row.fetchone()
        if not ref:
            continue
        match_id = ref[0]

        market_id = market_map.get("1X2")
        if not market_id:
            continue

        for bookmaker in event.get("bookmakers", []):
            bm_slug = bookmaker.get("key", "")
            bm_id = bookmaker_map.get(bm_slug)
            if not bm_id:
                # Auto-insert unknown bookmaker
                ins = await conn.execute(
                    text("""
                        INSERT INTO bookmaker_profiles (slug, display_name)
                        VALUES (:slug, :name)
                        ON CONFLICT (slug) DO UPDATE SET display_name = excluded.display_name
                        RETURNING bookmaker_id::text
                    """),
                    {"slug": bm_slug, "name": bookmaker.get("title", bm_slug)},
                )
                bm_id = ins.fetchone()[0]
                bookmaker_map[bm_slug] = bm_id

            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    sel_name = outcome.get("name", "").upper()
                    # Normalize: home team name → HOME, etc.
                    sel_code = _normalize_outcome(sel_name, event)
                    sel_id = sel_map.get((market_id, sel_code))
                    if not sel_id:
                        continue

                    decimal_odds = float(outcome.get("price", 0))
                    if decimal_odds <= 1.0:
                        continue
                    implied_prob = round(1.0 / decimal_odds, 6)

                    content_hash = sha256_json({
                        "match_id": match_id,
                        "bookmaker_id": bm_id,
                        "market_id": market_id,
                        "selection_id": sel_id,
                        "decimal_odds": decimal_odds,
                        "minute": captured_at.replace(second=0, microsecond=0).isoformat(),
                    })

                    await conn.execute(
                        text("""
                            INSERT INTO odds_snapshots (
                              match_id, bookmaker_id, market_id, selection_id,
                              decimal_odds, implied_probability, captured_at
                            )
                            VALUES (
                              cast(:match_id as uuid), cast(:bm_id as uuid),
                              cast(:mkt_id as uuid), cast(:sel_id as uuid),
                              :decimal_odds, :implied_prob, :captured_at
                            )
                            ON CONFLICT DO NOTHING
                        """),
                        {
                            "match_id": match_id,
                            "bm_id": bm_id,
                            "mkt_id": market_id,
                            "sel_id": sel_id,
                            "decimal_odds": decimal_odds,
                            "implied_prob": implied_prob,
                            "captured_at": captured_at,
                        },
                    )
                    inserted += 1

    return {"status": "OK", "job_name": "odds_refresh", "records_processed": inserted}


def _normalize_outcome(name: str, event: dict) -> str:
    home = (event.get("home_team") or "").upper()
    away = (event.get("away_team") or "").upper()
    if name == home or name == "HOME":
        return "HOME"
    if name == away or name == "AWAY":
        return "AWAY"
    if name in ("DRAW", "THE DRAW", "X"):
        return "DRAW"
    return name


async def standings_refresh_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh standings from FootballData."""
    _ = payload
    settings = get_settings()
    if not settings.football_data_token:
        return {"status": "WARN", "job_name": "standings_refresh", "records_processed": 0, "message": "FOOTBALL_DATA_TOKEN not set"}

    from app.clients.football_data_client import FootballDataClient
    from app.core.time import utc_now as _now

    client = FootballDataClient()

    # Get active seasons with football_data source
    season_rows = await conn.execute(
        text("""
            SELECT cs.competition_season_id::text, cs.slug, cs.stage_id::text AS default_stage_id
            FROM competition_seasons cs
            WHERE cs.status = 'ACTIVE' OR cs.status = 'BETTABLE'
            LIMIT 5
        """)
    )
    seasons = [dict(r._mapping) for r in season_rows]

    inserted = 0
    as_of = utc_now()

    for season in seasons:
        # Look up football_data external ref for this season's competition
        ref_row = await conn.execute(
            text("""
                SELECT source_entity_id FROM entity_external_refs
                WHERE entity_type = 'competition_season'
                  AND entity_id = cast(:season_id as uuid)
                  AND source = 'FOOTBALL_DATA'
                LIMIT 1
            """),
            {"season_id": season["competition_season_id"]},
        )
        ref = ref_row.fetchone()
        if not ref:
            continue

        fd_code = ref[0]
        try:
            data = await client.competition_standings(fd_code)
        except Exception:
            continue

        for group_standing in (data.get("standings") or []):
            for entry in (group_standing.get("table") or []):
                source_team_id = str(entry["team"]["id"])
                team_ref = await conn.execute(
                    text("""
                        SELECT entity_id::text FROM entity_external_refs
                        WHERE source = 'FOOTBALL_DATA' AND source_entity_id = :tid AND entity_type = 'team'
                        LIMIT 1
                    """),
                    {"tid": source_team_id},
                )
                tr = team_ref.fetchone()
                if not tr:
                    continue
                team_id = tr[0]

                await conn.execute(
                    text("""
                        INSERT INTO standings (
                          competition_season_id, team_id, position,
                          played, wins, draws, losses,
                          goals_for, goals_against, goal_difference, points, as_of
                        )
                        VALUES (
                          cast(:season_id as uuid), cast(:team_id as uuid), :position,
                          :played, :wins, :draws, :losses,
                          :gf, :ga, :gd, :points, :as_of
                        )
                        ON CONFLICT (competition_season_id, team_id, as_of)
                        DO UPDATE SET
                          position = excluded.position,
                          played = excluded.played, wins = excluded.wins,
                          draws = excluded.draws, losses = excluded.losses,
                          goals_for = excluded.goals_for, goals_against = excluded.goals_against,
                          goal_difference = excluded.goal_difference, points = excluded.points
                    """),
                    {
                        "season_id": season["competition_season_id"],
                        "team_id": team_id,
                        "position": entry.get("position"),
                        "played": entry.get("playedGames", 0),
                        "wins": entry.get("won", 0),
                        "draws": entry.get("draw", 0),
                        "losses": entry.get("lost", 0),
                        "gf": entry.get("goalsFor", 0),
                        "ga": entry.get("goalsAgainst", 0),
                        "gd": entry.get("goalDifference", 0),
                        "points": entry.get("points", 0),
                        "as_of": as_of,
                    },
                )
                inserted += 1

    return {"status": "OK", "job_name": "standings_refresh", "records_processed": inserted}


async def calibration_recompute_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Fit calibration model on settled predictions and update calibrated_probability."""
    settings = get_settings()

    row = await conn.execute(
        text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
        {"slug": settings.default_season_slug},
    )
    r = row.fetchone()
    if not r:
        return {"status": "WARN", "job_name": "calibration_recompute", "records_processed": 0, "message": "season not found"}
    season_id = r[0]

    model_row = await conn.execute(
        text("SELECT model_id::text FROM model_registry WHERE champion_status = 'CHAMPION' ORDER BY created_at DESC LIMIT 1")
    )
    mr = model_row.fetchone()
    if not mr:
        return {"status": "WARN", "job_name": "calibration_recompute", "records_processed": 0, "message": "no champion model"}
    model_id = mr[0]

    method = payload.get("method", "ISOTONIC")
    result = await run_calibration(conn, model_id=model_id, competition_season_id=season_id, method=method)
    return {
        "status": result.get("status", "OK"),
        "job_name": "calibration_recompute",
        "records_processed": result.get("n", 0),
        **result,
    }


async def clv_compute_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Compute CLV for settled decisions that don't have it yet."""
    _ = payload
    result = await compute_pending_clv(conn)
    return {
        "status": result["status"],
        "job_name": "clv_compute",
        "records_processed": result["computed"],
        **result,
    }


async def pipeline_cleanup_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Deletes pipeline_runs older than 30 days to keep the table lean."""
    _ = payload
    result = await conn.execute(
        text(
            """
            delete from pipeline_runs
            where started_at < now() - interval '30 days'
            """
        )
    )
    deleted = result.rowcount if result.rowcount is not None else 0
    return {"status": "OK", "job_name": "pipeline_cleanup", "records_processed": deleted}


async def run_registered_job(job_name: str, conn: AsyncConnection, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    jobs: dict[str, JobFn] = {
        "ev_decision": ev_decision_job,
        "drift_detection": drift_detection_job,
        "seed_competition_catalog": seed_competition_catalog_job,
        "discover_competition_sources": discover_competition_sources_job,
        "sync_competition_fixtures": sync_competition_fixtures_job,
        "worldcup_daily_refresh": worldcup_daily_refresh_job,
        "worldcup_live_refresh": worldcup_live_refresh_job,
        "pipeline_cleanup": pipeline_cleanup_job,
        # Phase 1 — quantitative engine
        "elo_ratings_update": elo_ratings_update_job,
        "feature_snapshot_build": feature_snapshot_build_job,
        "results_settlement": results_settlement_job,
        "model_recompute": model_recompute_job,
        "odds_refresh": odds_refresh_job,
        "standings_refresh": standings_refresh_job,
        "calibration_recompute": calibration_recompute_job,
        "clv_compute": clv_compute_job,
    }
    scaffold_jobs = {
        "dataset_builder",
        "settlement",
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
