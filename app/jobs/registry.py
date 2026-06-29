import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

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
from app.competitions.team_sync import (
    sync_players_for_all_leagues,
    sync_teams_for_all_leagues,
    validate_sync_coverage_for_all_leagues,
)
from app.competitions.finished_match_stats import sync_finished_match_stats
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
from app.normalization.core_entities_dedup import (
    reconcile_referees_identity,
    reconcile_teams_identity,
    reconcile_venues_identity,
)
from app.normalization.core_entities_identity import validate_core_entities_identity_consistency
from app.normalization.player_dedup import (
    reconcile_players_identity,
    validate_players_identity_consistency,
)
from app.models.ai_adjuster import adjust_predictions_with_ai
from app.models.drift_detector import detect_drift
from app.models.lgbm.retraining_pipeline import run_retraining
from app.services.notifications.telegram import notify_text
from app.news.context_pipeline import build_news_context

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
        "pending_candidates": result["pending_candidates"],
        "records_processed": result["settled"],
        "skipped_no_resolver": result["skipped_no_resolver"],
        "errors": result["errors"],
        "registered_resolvers": result["registered_resolvers"],
    }


async def model_recompute_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Run Poisson predictor for upcoming matches without predictions."""
    _ = payload
    settings = get_settings()

    try:
        model_id = await _get_or_create_model_registry(conn)
    except Exception as exc:
        return {"status": "ERROR", "job_name": "model_recompute", "records_processed": 0, "error": f"step=get_model_registry: {exc}"}

    try:
        row = await conn.execute(
            text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
            {"slug": settings.default_season_slug},
        )
        r = row.fetchone()
    except Exception as exc:
        return {"status": "ERROR", "job_name": "model_recompute", "records_processed": 0, "error": f"step=get_season: {exc}"}
    if not r:
        return {"status": "WARN", "job_name": "model_recompute", "records_processed": 0, "message": "season not found"}
    season_id = r[0]

    try:
        mkt_row = await conn.execute(
            text("SELECT market_id::text FROM markets WHERE market_code = '1X2' LIMIT 1")
        )
        mkt = mkt_row.fetchone()
    except Exception as exc:
        return {"status": "ERROR", "job_name": "model_recompute", "records_processed": 0, "error": f"step=get_market: {exc}"}
    if not mkt:
        return {"status": "WARN", "job_name": "model_recompute", "records_processed": 0, "message": "1X2 market not found"}
    market_id_1x2 = mkt[0]

    try:
        run_row = await conn.execute(
            text("""
                INSERT INTO model_runs (model_id, competition_season_id, market_id, run_status, prediction_as_of,
                                        feature_set_version, dataset_version, params)
                VALUES (cast(:model_id as uuid), cast(:season_id as uuid), cast(:market_id as uuid), 'STARTED', :as_of,
                        'v1', 'v1', cast(:params as jsonb))
                RETURNING model_run_id::text
            """),
            {
                "model_id": model_id,
                "season_id": season_id,
                "market_id": market_id_1x2,
                "as_of": utc_now(),
                "params": json.dumps({"model": "poisson_elo_v1"}),
            },
        )
        model_run_id = run_row.fetchone()[0]
    except Exception as exc:
        return {"status": "ERROR", "job_name": "model_recompute", "records_processed": 0, "error": f"step=insert_model_run: {exc}"}

    # Matches with feature snapshots but no predictions (next 14 days)
    matches = await conn.execute(
        text("""
            SELECT DISTINCT
              m.match_id::text,
              home_p.team_id::text  AS home_team_id,
              away_p.team_id::text  AS away_team_id,
              m.competition_season_id::text,
              m.kickoff_at
            FROM matches m
            JOIN match_participants home_p ON home_p.match_id = m.match_id AND home_p.side = 'HOME'
            JOIN match_participants away_p ON away_p.match_id = m.match_id AND away_p.side = 'AWAY'
            WHERE m.competition_season_id = cast(:season_id as uuid)
              AND m.status = 'SCHEDULED'
              AND m.kickoff_at BETWEEN now() AND now() + interval '14 days'
              AND EXISTS (
                SELECT 1 FROM feature_snapshots fs
                WHERE fs.match_id = m.match_id AND fs.team_side = 'HOME'
                  AND fs.feature_set_version = 'v1'
              )
              AND EXISTS (
                SELECT 1 FROM feature_snapshots fs
                WHERE fs.match_id = m.match_id AND fs.team_side = 'AWAY'
                  AND fs.feature_set_version = 'v1'
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
    ai_adjusted = 0
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
            continue

        predicted += 1

        # AI adjustment: enrich Poisson baseline with OpenAI context
        raw_probs = result.get("probabilities", {})
        if raw_probs and all(k in raw_probs for k in ("HOME", "DRAW", "AWAY")):
            # Use nested savepoint so a DB failure inside AI adjustment
            # doesn't abort the outer transaction and corrupt subsequent iterations.
            try:
                async with conn.begin_nested() as ai_sp:
                    try:
                        ai_result = await adjust_predictions_with_ai(
                            conn,
                            match_id=m["match_id"],
                            model_run_id=model_run_id,
                            raw_probs=raw_probs,
                        )
                        await ai_sp.commit()
                        if ai_result.get("status") == "ok":
                            ai_adjusted += 1
                    except Exception as ai_exc:
                        await ai_sp.rollback()
                        log.warning("AI adjustment failed for match %s: %s", m["match_id"], ai_exc)
            except Exception as ai_exc:
                log.warning("AI adjustment savepoint failed for match %s: %s", m["match_id"], ai_exc)

    # Update model run status
    db_run_status = "SUCCEEDED" if errors == 0 else "FAILED"
    api_status = "OK" if errors == 0 else "WARN"
    await conn.execute(
        text("""
            UPDATE model_runs SET run_status = :status, training_window_end_at = :now
            WHERE model_run_id = cast(:run_id as uuid)
        """),
        {
            "status": db_run_status,
            "now": utc_now(),
            "run_id": model_run_id,
        },
    )

    return {
        "status": api_status,
        "job_name": "model_recompute",
        "records_processed": predicted,
        "ai_adjusted": ai_adjusted,
        "model_run_id": model_run_id,
        "errors": errors,
    }


_ODDS_REFRESH_TTL_HOURS = 4  # minimum hours between calls to The Odds API (500 req/month budget)


async def odds_refresh_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Fetch live odds from The Odds API (bulk call) and store as append-only snapshots.

    Match linking is done by normalized team name + kickoff date (same strategy as the
    original world_cup_2026 GAS project). entity_external_refs is NOT required.

    Smart fetch policy (free tier — 500 req/month):
      - Finished/cancelled matches: skip
      - Kickoff > 7 days out: skip
      - Markets: h2h (1X2) + totals (Over/Under 2.5)
      - Regions: us,uk,eu
      - TTL 4h: skip if a successful run happened in the last 4 hours
    """
    _ = payload
    settings = get_settings()
    if not settings.the_odds_api_key:
        return {"status": "WARN", "job_name": "odds_refresh", "records_processed": 0, "message": "THE_ODDS_API_KEY not set"}

    # TTL gate — respect The Odds API 500 req/month free tier budget.
    # Skip if a successful run happened within the last _ODDS_REFRESH_TTL_HOURS hours.
    recent = await conn.execute(
        text("""
            SELECT started_at FROM pipeline_runs
            WHERE job_name = 'odds_refresh'
              AND status IN ('OK', 'WARN')
              AND started_at >= now() - make_interval(hours => :ttl_hours)
            ORDER BY started_at DESC
            LIMIT 1
        """),
        {"ttl_hours": _ODDS_REFRESH_TTL_HOURS},
    )
    last_run = recent.fetchone()
    if last_run:
        log.info("odds_refresh: skipping — last successful run was at %s (TTL %dh)", last_run[0], _ODDS_REFRESH_TTL_HOURS)
        return {
            "status": "WARN",
            "job_name": "odds_refresh",
            "records_processed": 0,
            "message": f"fresh_data_ttl_not_expired — last run at {last_run[0].isoformat()}",
        }

    from app.clients.odds_api_client import OddsApiClient
    import unicodedata as _ud
    import re as _re

    client = OddsApiClient()

    # Load DB reference tables
    bm_rows = await conn.execute(text("SELECT slug, bookmaker_id::text FROM bookmaker_profiles"))
    bookmaker_map: dict[str, str] = {r[0]: r[1] for r in bm_rows}

    mkt_rows = await conn.execute(text("SELECT market_code, market_id::text FROM markets"))
    market_map: dict[str, str] = {r[0]: r[1] for r in mkt_rows}

    sel_rows = await conn.execute(
        text("SELECT market_id::text, selection_code, selection_id::text FROM market_selections")
    )
    sel_map: dict[tuple[str, str], str] = {(r[0], r[1]): r[2] for r in sel_rows}

    # Build match lookup: {(kickoff_date, norm_home_key): (match_id, status, kickoff_at)}
    match_rows = await conn.execute(
        text("""
            SELECT m.match_id::text, m.status, m.kickoff_at,
                   ht.display_name AS home_name
            FROM matches m
            JOIN match_participants hp ON hp.match_id = m.match_id AND hp.side = 'HOME'
            JOIN teams ht ON ht.team_id = hp.team_id
        """)
    )
    match_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for r in match_rows:
        kickoff_date = str(r[2])[:10]
        home_key = _norm_team(r[3])
        match_lookup[(kickoff_date, home_key)] = {
            "match_id": r[0], "status": r[1], "kickoff_at": r[2]
        }

    inserted = 0
    skipped_policy = 0
    unmatched = 0
    captured_at = utc_now()

    # The Odds API changes sport key once the tournament is live.
    # Try both slugs; use whichever returns events.
    _SPORT_KEYS = ["soccer_fifa_world_cup", "soccer_fifa_world_cup_2026"]
    data: list[dict] = []
    sport_key_used = None
    for _sport_key in _SPORT_KEYS:
        try:
            _result = await client.odds(sport=_sport_key, regions="us,uk,eu", markets="h2h,totals")
            if _result:
                data = _result
                sport_key_used = _sport_key
                break
            log.info("odds_refresh: sport_key=%s returned 0 events, trying next", _sport_key)
        except Exception as exc:
            log.warning("odds_refresh: sport_key=%s error: %s", _sport_key, exc)
    if not data:
        return {
            "status": "WARN", "job_name": "odds_refresh", "records_processed": 0,
            "message": f"0 events from all sport keys: {_SPORT_KEYS}",
        }
    log.info("odds_refresh: sport_key=%s → %d events", sport_key_used, len(data))

    for event in (data or []):
        # Smart fetch policy: skip if no useful kickoff data
        commence_raw = event.get("commence_time")
        kickoff_date = str(commence_raw or "")[:10]
        if not kickoff_date:
            continue

        # Match by normalized home team name
        home_norm = _norm_team(event.get("home_team", ""))
        match_info = match_lookup.get((kickoff_date, home_norm))
        if not match_info:
            # Try away-as-home (some APIs swap them)
            away_norm = _norm_team(event.get("away_team", ""))
            match_info = match_lookup.get((kickoff_date, away_norm))
        if not match_info:
            unmatched += 1
            continue

        # Skip finished or cancelled matches (no live odds needed)
        if match_info["status"] in ("FINISHED", "CANCELLED", "POSTPONED", "ABANDONED"):
            skipped_policy += 1
            continue

        # Skip if kickoff > 7 days out
        kickoff_at = match_info["kickoff_at"]
        if kickoff_at:
            from datetime import timezone as _tz, timedelta as _td
            now_ts = utc_now()
            if hasattr(kickoff_at, "tzinfo") and kickoff_at.tzinfo:
                hours_until = (kickoff_at - now_ts).total_seconds() / 3600
            else:
                hours_until = 0
            if hours_until > 7 * 24:
                skipped_policy += 1
                continue

        match_id = match_info["match_id"]

        for bookmaker in event.get("bookmakers", []):
            bm_slug = bookmaker.get("key", "")
            bm_id = bookmaker_map.get(bm_slug)
            if not bm_id:
                ins = await conn.execute(
                    text("""
                        INSERT INTO bookmaker_profiles (slug, display_name, region)
                        VALUES (:slug, :name, :region)
                        ON CONFLICT (slug) DO UPDATE SET display_name = excluded.display_name
                        RETURNING bookmaker_id::text
                    """),
                    {"slug": bm_slug, "name": bookmaker.get("title", bm_slug), "region": "UNKNOWN"},
                )
                bm_id = ins.fetchone()[0]
                bookmaker_map[bm_slug] = bm_id

            for market in bookmaker.get("markets", []):
                market_key = market.get("key", "")

                if market_key == "h2h":
                    market_id = market_map.get("1X2")
                    if not market_id:
                        continue
                    for outcome in market.get("outcomes", []):
                        sel_code = _normalize_outcome(outcome.get("name", ""), event)
                        sel_id = sel_map.get((market_id, sel_code))
                        if not sel_id:
                            continue
                        decimal_odds = float(outcome.get("price", 0))
                        if decimal_odds <= 1.0:
                            continue
                        inserted += await _upsert_odds_snapshot(
                            conn, match_id, bm_id, market_id, sel_id, None,
                            decimal_odds, captured_at
                        )

                elif market_key == "totals":
                    market_id = market_map.get("OVER_UNDER")
                    if not market_id:
                        continue
                    for outcome in market.get("outcomes", []):
                        name = (outcome.get("name") or "").upper()
                        point = outcome.get("point")
                        # Only process 2.5 line
                        if point is not None and float(point) != 2.5:
                            continue
                        if name == "OVER":
                            sel_code = "OVER"
                        elif name == "UNDER":
                            sel_code = "UNDER"
                        else:
                            continue
                        sel_id = sel_map.get((market_id, sel_code))
                        if not sel_id:
                            continue
                        decimal_odds = float(outcome.get("price", 0))
                        if decimal_odds <= 1.0:
                            continue
                        inserted += await _upsert_odds_snapshot(
                            conn, match_id, bm_id, market_id, sel_id, 2.5,
                            decimal_odds, captured_at
                        )

    return {
        "status": "OK",
        "job_name": "odds_refresh",
        "records_processed": inserted,
        "skipped_policy": skipped_policy,
        "unmatched_events": unmatched,
    }


def _norm_team(name: Any) -> str:
    """Normalize team name for fuzzy matching (same logic as world_cup_2026 GAS project)."""
    import unicodedata as _ud, re as _re
    s = str(name or "")
    s = _ud.normalize("NFD", s)
    s = "".join(c for c in s if _ud.category(c) != "Mn")
    s = s.lower()
    s = _re.sub(r"[^a-z0-9]+", "", s)
    return s


async def _upsert_odds_snapshot(
    conn: AsyncConnection,
    match_id: str,
    bookmaker_id: str,
    market_id: str,
    selection_id: str,
    line: float | None,
    decimal_odds: float,
    captured_at: Any,
) -> int:
    """Insert one odds_snapshot row. Returns 1 if inserted, 0 if duplicate."""
    import hashlib as _hl
    minute = captured_at.replace(second=0, microsecond=0).isoformat()
    dedup_key = f"{match_id}:{bookmaker_id}:{market_id}:{selection_id}:{decimal_odds:.4f}:{minute}"
    source_snapshot_id = _hl.sha256(dedup_key.encode()).hexdigest()[:40]

    result = await conn.execute(
        text("""
            INSERT INTO odds_snapshots (
              match_id, bookmaker_id, market_id, selection_id,
              source, source_snapshot_id, line,
              decimal_odds, captured_at
            )
            VALUES (
              cast(:match_id as uuid), cast(:bm_id as uuid),
              cast(:mkt_id as uuid), cast(:sel_id as uuid),
              'THE_ODDS_API', :ssid, :line,
              :decimal_odds, :captured_at
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "match_id": match_id,
            "bm_id": bookmaker_id,
            "mkt_id": market_id,
            "sel_id": selection_id,
            "ssid": source_snapshot_id,
            "line": line,
            "decimal_odds": decimal_odds,
            "captured_at": captured_at,
        },
    )
    return result.rowcount if hasattr(result, "rowcount") and result.rowcount is not None else 1


def _normalize_outcome(name: str, event: dict) -> str:
    home = (event.get("home_team") or "").upper()
    away = (event.get("away_team") or "").upper()
    name_up = name.upper()
    if name_up == home or name_up == "HOME":
        return "HOME"
    if name_up == away or name_up == "AWAY":
        return "AWAY"
    if name_up in ("DRAW", "THE DRAW", "X"):
        return "DRAW"
    return name_up


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
            SELECT cs.competition_season_id::text, cs.slug
            FROM competition_seasons cs
            WHERE cs.status IN ('ACTIVE', 'SCHEDULED')
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
                WHERE entity_type = 'COMPETITION_SEASON'
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

        # FD standings response: list of group blocks, each with a "group" field
        # (e.g. "GROUP_A") and a "table" list of team rows.
        for group_standing in (data.get("standings") or []):
            fd_group_code = group_standing.get("group") or ""

            # Resolve our group_id once per FD group block (not per team row)
            group_id: str | None = None
            if fd_group_code:
                g_row = await conn.execute(
                    text("""
                        SELECT cg.group_id::text FROM competition_groups cg
                        JOIN competition_seasons cs ON cs.competition_season_id = cg.competition_season_id
                        WHERE cs.competition_season_id = cast(:season_id as uuid)
                          AND upper(cg.group_code) = upper(:group_code)
                        LIMIT 1
                    """),
                    {"season_id": season["competition_season_id"], "group_code": fd_group_code},
                )
                g = g_row.fetchone()
                if g:
                    group_id = g[0]

            if not group_id:
                # Cannot resolve group — skip entire block to avoid null-group rows
                continue

            for entry in (group_standing.get("table") or []):
                source_team_id = str(entry["team"]["id"])
                team_ref = await conn.execute(
                    text("""
                        SELECT entity_id::text FROM entity_external_refs
                        WHERE source = 'FOOTBALL_DATA' AND source_entity_id = :tid AND entity_type = 'TEAM'
                        LIMIT 1
                    """),
                    {"tid": source_team_id},
                )
                tr = team_ref.fetchone()
                if not tr:
                    continue
                team_id = tr[0]

                # The unique index includes as_of, so ON CONFLICT never fires when
                # as_of changes between runs — it would always INSERT a new row.
                # Fix: DELETE the existing row for this (season, group, team) first,
                # then INSERT fresh. No accumulation, no dependency on as_of index.
                await conn.execute(
                    text("""
                        DELETE FROM standings
                        WHERE competition_season_id = cast(:season_id as uuid)
                          AND group_id = cast(:group_id as uuid)
                          AND team_id = cast(:team_id as uuid)
                    """),
                    {"season_id": season["competition_season_id"], "group_id": group_id, "team_id": team_id},
                )
                await conn.execute(
                    text("""
                        INSERT INTO standings (
                          competition_season_id, group_id, team_id, position,
                          played, wins, draws, losses,
                          goals_for, goals_against, goal_difference, points, as_of,
                          source
                        )
                        VALUES (
                          cast(:season_id as uuid), cast(:group_id as uuid), cast(:team_id as uuid), :position,
                          :played, :wins, :draws, :losses,
                          :gf, :ga, :gd, :points, :as_of,
                          'FOOTBALL_DATA'
                        )
                    """),
                    {
                        "season_id": season["competition_season_id"],
                        "group_id": group_id,
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


async def drift_detection_full_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Detect distribution drift vs baseline for the champion model."""
    settings = get_settings()

    row = await conn.execute(
        text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
        {"slug": settings.default_season_slug},
    )
    r = row.fetchone()
    if not r:
        return {"status": "WARN", "job_name": "drift_detection_full", "records_processed": 0, "message": "season not found"}
    season_id = r[0]

    model_row = await conn.execute(
        text("SELECT model_id::text FROM model_registry WHERE champion_status = 'CHAMPION' ORDER BY created_at DESC LIMIT 1")
    )
    mr = model_row.fetchone()
    if not mr:
        return {"status": "WARN", "job_name": "drift_detection_full", "records_processed": 0, "message": "no champion model"}

    result = await detect_drift(conn, model_id=mr[0], competition_season_id=season_id)
    return {
        "status": result.get("status", "OK"),
        "job_name": "drift_detection_full",
        "records_processed": 1,
        "severity": result.get("severity"),
        "psi": result.get("psi"),
        "brier_delta": result.get("brier_delta"),
    }


async def model_promotion_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Run LightGBM retraining pipeline and optionally auto-promote.
    Triggered manually or by CI — NOT in the daily job plan by default.
    payload: {auto_promote: bool}
    """
    settings = get_settings()
    auto_promote = bool(payload.get("auto_promote", False))

    row = await conn.execute(
        text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
        {"slug": settings.default_season_slug},
    )
    r = row.fetchone()
    if not r:
        return {"status": "WARN", "job_name": "model_promotion", "records_processed": 0, "message": "season not found"}
    season_id = r[0]

    result = await run_retraining(conn, competition_season_id=season_id, auto_promote=auto_promote)
    return {
        "status": result.get("status", "OK"),
        "job_name": "model_promotion",
        "records_processed": result.get("n_total", 0),
        **result,
    }


async def backtest_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Run walk-forward backtesting on the champion model."""
    from app.training.walk_forward import run_walk_forward
    settings = get_settings()

    row = await conn.execute(
        text("SELECT competition_season_id::text FROM competition_seasons WHERE slug = :slug LIMIT 1"),
        {"slug": settings.default_season_slug},
    )
    r = row.fetchone()
    if not r:
        return {"status": "WARN", "job_name": "backtest_walk_forward", "records_processed": 0, "message": "season not found"}
    season_id = r[0]

    model_row = await conn.execute(
        text("SELECT model_id::text FROM model_registry WHERE champion_status = 'CHAMPION' ORDER BY created_at DESC LIMIT 1")
    )
    mr = model_row.fetchone()
    if not mr:
        return {"status": "WARN", "job_name": "backtest_walk_forward", "records_processed": 0, "message": "no champion model"}

    result = await run_walk_forward(
        conn,
        model_id=mr[0],
        competition_season_id=season_id,
        window_days=int(payload.get("window_days", 90)),
        test_days=int(payload.get("test_days", 30)),
    )
    return {
        "status": result.get("status", "OK"),
        "job_name": "backtest_walk_forward",
        "records_processed": len(result.get("windows", [])),
        "avg_brier": result.get("avg_brier"),
        "windows": len(result.get("windows", [])),
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


async def qualification_resolver_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.qualification.orchestrator import (
        get_active_season_id,
        run_qualification_resolver,
    )

    season_slug = payload.get("season_slug") or "wc2026"
    season_id = payload.get("competition_season_id")

    if not season_id:
        season_id = await get_active_season_id(conn, season_slug)
    if not season_id:
        return {"status": "SKIPPED", "reason": f"no active season for slug={season_slug}", "records_processed": 0}

    send_group_notifications = payload.get("send_group_notifications")
    if send_group_notifications is None:
        send_group_notifications = payload.get("orchestrator") != "live"

    result = await run_qualification_resolver(
        conn,
        season_id,
        send_group_notifications=bool(send_group_notifications),
    )
    return {
        "status": "ERROR" if result.errors else "OK",
        "records_processed": result.slots_resolved + result.groups_processed,
        "groups_processed": result.groups_processed,
        "slots_resolved": result.slots_resolved,
        "slots_pending": result.slots_pending,
        "thirds_qualified": result.thirds_qualified,
        "tiebreakers_pending": result.tiebreakers_pending,
        "events_written": result.events_written,
        "errors": result.errors,
    }


async def telegram_daily_summary_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Send 8AM CL summary to Telegram with last daily run + EV+ highlights."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {
            "status": "SKIPPED",
            "job_name": "telegram_daily_summary",
            "records_processed": 0,
            "reason": "TELEGRAM_NOT_CONFIGURED",
        }

    limit = int(payload.get("ev_limit", 8) or 8)
    run_row = await conn.execute(
        text(
            """
            select pipeline_run_id::text as pipeline_run_id,
                   started_at,
                   finished_at,
                   status,
                   records_processed,
                   payload
            from pipeline_runs
            where job_name = 'orchestrate_daily'
              and status in ('OK', 'WARN')
            order by started_at desc
            limit 1
            """
        )
    )
    daily_run = run_row.fetchone()
    if not daily_run:
        return {
            "status": "WARN",
            "job_name": "telegram_daily_summary",
            "records_processed": 0,
            "reason": "NO_DAILY_RUN_FOUND",
        }

    run_data = dict(daily_run._mapping)
    run_payload = run_data.get("payload") or {}
    started_at = run_data["started_at"]

    ev_rows_result = await conn.execute(
        text(
            """
            select
              bd.betting_decision_id::text as betting_decision_id,
              bd.ev,
              bd.edge,
              bd.decided_at,
              m.market_code,
              s.selection_code,
              home_team.display_name as home_team,
              away_team.display_name as away_team,
              coalesce(
                bd.payload->>'graph_url',
                bd.payload->>'chart_url',
                bd.payload->>'plot_url',
                bd.payload->>'image_url',
                bd.payload->>'dashboard_url'
              ) as graph_url
            from betting_decisions bd
            join model_predictions p on p.prediction_id = bd.prediction_id
            join markets m on m.market_id = p.market_id
            join market_selections s on s.selection_id = p.selection_id
            join matches mt on mt.match_id = bd.match_id
            left join match_participants home on home.match_id = mt.match_id and home.side = 'HOME'
            left join teams home_team on home_team.team_id = home.team_id
            left join match_participants away on away.match_id = mt.match_id and away.side = 'AWAY'
            left join teams away_team on away_team.team_id = away.team_id
            where bd.decision_status = 'BETTABLE'
              and bd.decided_at >= cast(:started_at as timestamptz)
            order by bd.ev desc nulls last, bd.decided_at desc
            limit :limit
            """
        ),
        {"started_at": started_at, "limit": limit},
    )
    ev_rows = [dict(r._mapping) for r in ev_rows_result]

    executed = run_payload.get("executed", []) if isinstance(run_payload, dict) else []
    skipped = run_payload.get("skipped", []) if isinstance(run_payload, dict) else []
    failed = run_payload.get("failed", []) if isinstance(run_payload, dict) else []

    lines = [
        "📊 <b>Resumen Daily Match Alpha (08:00 CL)</b>",
        f"Run: {run_data.get('status', 'OK')}",
        f"Inicio UTC: {iso_utc(started_at)}",
        f"Jobs ejecutados: {len(executed)} | skip: {len(skipped)} | fail: {len(failed)}",
        "",
        f"🎯 <b>EV+ detectados</b>: {len(ev_rows)}",
    ]

    if not ev_rows:
        lines.append("Sin EV+ nuevos desde el último daily.")
    else:
        for idx, row in enumerate(ev_rows, start=1):
            home = row.get("home_team") or "TBD"
            away = row.get("away_team") or "TBD"
            ev = row.get("ev")
            edge = row.get("edge")
            lines.append(
                f"{idx}. {home} vs {away} | {row.get('market_code')}/{row.get('selection_code')}"
            )
            lines.append(
                f"   EV={float(ev):.2%} | Edge={float(edge):.2%}" if ev is not None and edge is not None else "   EV/Edge no disponible"
            )
            if row.get("graph_url"):
                lines.append(f"   Grafico: {row['graph_url']}")

    message = "\n".join(lines)
    await notify_text(settings.telegram_bot_token, settings.telegram_chat_id, message)

    return {
        "status": "OK",
        "job_name": "telegram_daily_summary",
        "records_processed": len(ev_rows),
        "pipeline_run_id": run_data.get("pipeline_run_id"),
        "ev_sent": len(ev_rows),
    }


async def sync_all_leagues_teams_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Weekly: upsert teams for all catalog entries that have API_FOOTBALL external_ids."""
    _ = payload
    return await sync_teams_for_all_leagues(conn)


async def sync_all_leagues_players_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Weekly: upsert players + squad memberships for all catalog entries with API_FOOTBALL."""
    return await sync_players_for_all_leagues(conn, payload)


async def sync_all_leagues_match_entities_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Weekly: ingest stadiums/venues + referees from fixture providers across leagues."""
    return await sync_players_for_all_leagues(
        conn,
        {
            **payload,
            "only_match_entities": True,
            "ingest_referees": True,
            "ingest_venues": True,
        },
    )


async def sync_all_leagues_fixtures_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Daily: sync fixtures for all catalog entries (multi-league complement to worldcup_daily_refresh)."""
    competition = payload.get("competition")
    return await sync_competition_fixtures(conn, competition)


async def finished_match_stats_refresh_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Daily: refresh detailed stats for FINISHED matches (default: yesterday) using non-ESPN sources."""
    return await sync_finished_match_stats(conn, payload)


async def validate_sync_coverage_all_leagues_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Post-cron validation by league: teams coverage + min players + roster consistency."""
    min_players = int(payload.get("min_players_per_team", 11) or 11)
    return await validate_sync_coverage_for_all_leagues(conn, min_players_per_team=min_players)


async def validate_players_identity_all_leagues_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Post-cron validation for player identity consistency (no merge side effects)."""
    settings = get_settings()
    validation_payload = {
        **payload,
        "max_merge_candidates": int(
            payload.get(
                "max_merge_candidates",
                settings.identity_validation_max_merge_candidates,
            )
        ),
        "max_candidate_ratio": float(
            payload.get(
                "max_candidate_ratio",
                settings.identity_validation_max_candidate_ratio,
            )
        ),
        "max_ambiguous_signatures": int(
            payload.get(
                "max_ambiguous_signatures",
                settings.identity_validation_max_ambiguous_signatures,
            )
        ),
    }
    return await validate_players_identity_consistency(conn, validation_payload)


async def validate_core_entities_identity_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Post-cron validation for teams, referees and venues identity consistency."""
    settings = get_settings()
    validation_payload = {
        **payload,
        "max_team_duplicates": int(
            payload.get(
                "max_team_duplicates",
                settings.identity_validation_max_team_duplicates,
            )
        ),
        "max_referee_duplicates": int(
            payload.get(
                "max_referee_duplicates",
                settings.identity_validation_max_referee_duplicates,
            )
        ),
        "max_venue_duplicates": int(
            payload.get(
                "max_venue_duplicates",
                settings.identity_validation_max_venue_duplicates,
            )
        ),
    }
    return await validate_core_entities_identity_consistency(conn, validation_payload)


async def reconcile_players_identity_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate and merge players across sources while preserving relationships."""
    return await reconcile_players_identity(conn, payload)


async def reconcile_teams_identity_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate and merge teams across sources while preserving relationships."""
    return await reconcile_teams_identity(conn, payload)


async def reconcile_referees_identity_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate and merge referees across sources while preserving relationships."""
    return await reconcile_referees_identity(conn, payload)


async def reconcile_venues_identity_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate and merge venues across sources while preserving relationships."""
    return await reconcile_venues_identity(conn, payload)


async def news_context_extract_job(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract structured lineup/injury/suspension context from news bodies and resolve players to local IDs."""
    return await build_news_context(conn, payload)


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
        "qualification_resolver": qualification_resolver_job,
        "calibration_recompute": calibration_recompute_job,
        "clv_compute": clv_compute_job,
        # Phase 3 — ML + drift + champion/challenger
        "drift_detection_full": drift_detection_full_job,
        "model_promotion": model_promotion_job,
        "backtest_walk_forward": backtest_job,
        # Multi-league sync
        "sync_all_leagues_teams": sync_all_leagues_teams_job,
        "sync_all_leagues_match_entities": sync_all_leagues_match_entities_job,
        "sync_all_leagues_players": sync_all_leagues_players_job,
        "sync_all_leagues_fixtures": sync_all_leagues_fixtures_job,
        "finished_match_stats_refresh": finished_match_stats_refresh_job,
        "validate_sync_coverage_all_leagues": validate_sync_coverage_all_leagues_job,
        "validate_players_identity_all_leagues": validate_players_identity_all_leagues_job,
        "validate_core_entities_identity": validate_core_entities_identity_job,
        "reconcile_players_identity": reconcile_players_identity_job,
        "reconcile_teams_identity": reconcile_teams_identity_job,
        "reconcile_referees_identity": reconcile_referees_identity_job,
        "reconcile_venues_identity": reconcile_venues_identity_job,
        "news_context_extract": news_context_extract_job,
        "telegram_daily_summary": telegram_daily_summary_job,
    }
    scaffold_jobs = {
        "dataset_builder",
        "settlement",
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
