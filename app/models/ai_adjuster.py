"""
AI probability adjuster — wraps Poisson baseline with OpenAI corrections.

Mirrors the world_cup_2026 AiAnalysis.gs approach:
  - Poisson probabilities are the baseline (not the final answer)
  - OpenAI adjusts ±8pp max with CONCRETE evidence
  - Output: calibrated_probability stored in model_predictions
  - Cache: skip if calibrated_probability already set for today

Sources used (best-effort, graceful degradation):
  - Odds snapshot (best available from odds_snapshots)
  - Group standings (standings table)
  - Team form (from feature_snapshots: form_points, form_gd, rest_days)
  - ELO ratings (from feature_snapshots)
  - Weather (WeatherAPI if key configured)
  - News headlines (Google News RSS always + NewsAPI if NEWS_API_KEY configured)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import utc_now

log = logging.getLogger(__name__)

_MAX_ADJUSTMENT_PP = 0.08   # max probability adjustment in percentage points


# ---------------------------------------------------------------------------
# Context fetchers
# ---------------------------------------------------------------------------

async def _fetch_match_context(conn: AsyncConnection, match_id: str) -> dict:
    row = await conn.execute(
        text("""
            SELECT
              m.match_id::text,
              m.kickoff_at,
              ht.display_name AS home_team,
              at.display_name AS away_team,
              v.name          AS venue_name,
              v.city          AS venue_city,
              v.country       AS venue_country,
              cst.stage_code  AS stage_type,
              m.status
            FROM matches m
            JOIN match_participants hp ON hp.match_id = m.match_id AND hp.side = 'HOME'
            JOIN match_participants ap ON ap.match_id = m.match_id AND ap.side = 'AWAY'
            JOIN teams ht ON ht.team_id = hp.team_id
            JOIN teams at ON at.team_id = ap.team_id
            LEFT JOIN venues v ON v.venue_id = m.venue_id
            LEFT JOIN competition_stages cst ON cst.stage_id = m.stage_id
            WHERE m.match_id = cast(:mid as uuid)
        """),
        {"mid": match_id},
    )
    r = row.fetchone()
    return dict(r._mapping) if r else {}


async def _fetch_best_odds(conn: AsyncConnection, match_id: str) -> dict:
    """Return best available odds per selection from odds_snapshots."""
    rows = await conn.execute(
        text("""
            SELECT
              ms.selection_code,
              max(os.decimal_odds) AS best_odds,
              count(DISTINCT os.bookmaker_id) AS n_bookmakers,
              max(os.captured_at) AS latest_at
            FROM odds_snapshots os
            JOIN market_selections ms ON ms.selection_id = os.selection_id
            JOIN markets mk ON mk.market_id = os.market_id AND mk.market_code = '1X2'
            WHERE os.match_id = cast(:mid as uuid)
              AND os.captured_at > now() - interval '24 hours'
            GROUP BY ms.selection_code
        """),
        {"mid": match_id},
    )
    result: dict[str, Any] = {}
    for r in rows:
        result[r[0]] = {"odds": round(float(r[1]), 3), "bookmakers": r[2]}
    return result


async def _fetch_standings(conn: AsyncConnection, match_id: str) -> list[dict]:
    """Return group standings for the teams in this match (if group stage)."""
    rows = await conn.execute(
        text("""
            SELECT
              t.display_name AS team,
              s.position,
              s.played,
              s.won,
              s.drawn,
              s.lost,
              s.goals_for,
              s.goals_against,
              s.points
            FROM standings s
            JOIN teams t ON t.team_id = s.team_id
            WHERE s.competition_season_id = (
              SELECT competition_season_id FROM matches WHERE match_id = cast(:mid as uuid)
            )
              AND s.group_id IS NOT NULL
            ORDER BY s.group_id, s.position
            LIMIT 32
        """),
        {"mid": match_id},
    )
    return [dict(r._mapping) for r in rows]


async def _fetch_feature_context(conn: AsyncConnection, match_id: str) -> dict:
    """Return form + ELO from feature_snapshots for both teams."""
    rows = await conn.execute(
        text("""
            SELECT
              t.display_name   AS team,
              fs.team_side,
              fs.elo_global,
              fs.elo_diff,
              fs.attack_strength,
              fs.defense_strength,
              fs.form_points,
              fs.form_gd,
              fs.rest_days,
              fs.feature_completeness
            FROM feature_snapshots fs
            JOIN teams t ON t.team_id = fs.team_id
            WHERE fs.match_id = cast(:mid as uuid)
            ORDER BY fs.team_side
        """),
        {"mid": match_id},
    )
    result: dict[str, dict] = {}
    for r in rows:
        d = dict(r._mapping)
        result[d["team_side"]] = d
    return result


async def _fetch_weather(
    conn: AsyncConnection,
    match_id: str,
    venue_city: str | None,
    venue_country: str | None,
    kickoff_at: Any,
) -> dict:
    """Return forecast weather at kickoff time.

    Priority order:
    1. Read from matches.metadata.weather (persisted by the web layer).
    2. If stale or missing, call WeatherAPI forecast.json for the kickoff date/hour.

    This avoids duplicate API calls and always returns conditions for kickoff time,
    not current conditions.
    """
    from datetime import UTC, datetime

    # --- 1. Try reading from persisted match metadata ---
    try:
        row = await conn.execute(
            text("SELECT metadata FROM matches WHERE match_id = cast(:mid as uuid)"),
            {"mid": match_id},
        )
        r = row.fetchone()
        if r:
            meta = r[0] or {}
            cached = meta.get("weather") if isinstance(meta, dict) else None
            if cached and isinstance(cached, dict) and cached.get("fetched_at"):
                try:
                    fetched_at = datetime.fromisoformat(cached["fetched_at"].replace("Z", "+00:00"))
                    age_h = (datetime.now(UTC) - fetched_at).total_seconds() / 3600
                    if age_h < 3:
                        # Already fresh — normalize keys for AI prompt compatibility
                        return {
                            "temp_c": cached.get("temperature_c") or cached.get("temp_c"),
                            "condition": cached.get("condition"),
                            "wind_kph": cached.get("wind_kph"),
                            "precip_mm": cached.get("precip_mm"),
                            "humidity": cached.get("humidity_pct") or cached.get("humidity"),
                            "chance_of_rain": cached.get("chance_of_rain"),
                            "forecast_type": cached.get("forecast_type", "cached"),
                        }
                except Exception:
                    pass
    except Exception as exc:
        log.debug("weather db read failed: %s", exc)

    # --- 2. Fetch from WeatherAPI using forecast.json for the kickoff hour ---
    settings = get_settings()
    if not settings.weather_api_key or not venue_city:
        return {}

    kickoff_dt: datetime | None = None
    if kickoff_at:
        try:
            kickoff_dt = datetime.fromisoformat(str(kickoff_at).replace("Z", "+00:00"))
        except Exception:
            pass

    location = f"{venue_city},{venue_country or ''}".strip(",")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if kickoff_dt and kickoff_dt > datetime.now(UTC):
                r = await client.get(
                    "https://api.weatherapi.com/v1/forecast.json",
                    params={
                        "key": settings.weather_api_key,
                        "q": location,
                        "dt": kickoff_dt.strftime("%Y-%m-%d"),
                        "aqi": "no",
                        "alerts": "no",
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    forecast_day = (data.get("forecast", {}).get("forecastday") or [{}])[0]
                    hours = forecast_day.get("hour") or []
                    kickoff_hour = kickoff_dt.hour
                    hour_data = next(
                        (h for h in hours if datetime.fromisoformat(h["time"]).hour == kickoff_hour),
                        hours[kickoff_hour] if hours and kickoff_hour < len(hours) else (hours[-1] if hours else {}),
                    )
                    return {
                        "temp_c": hour_data.get("temp_c"),
                        "condition": hour_data.get("condition", {}).get("text"),
                        "wind_kph": hour_data.get("wind_kph"),
                        "precip_mm": hour_data.get("precip_mm"),
                        "humidity": hour_data.get("humidity"),
                        "chance_of_rain": hour_data.get("chance_of_rain"),
                        "forecast_type": "kickoff_hour",
                    }
            # Fallback: current conditions (match in progress or today)
            r = await client.get(
                "https://api.weatherapi.com/v1/current.json",
                params={"key": settings.weather_api_key, "q": location, "aqi": "no"},
            )
            if r.status_code == 200:
                current = r.json().get("current", {})
                return {
                    "temp_c": current.get("temp_c"),
                    "condition": current.get("condition", {}).get("text"),
                    "wind_kph": current.get("wind_kph"),
                    "precip_mm": current.get("precip_mm"),
                    "humidity": current.get("humidity"),
                    "forecast_type": "current",
                }
    except Exception as exc:
        log.debug("weather fetch failed: %s", exc)
    return {}


async def _fetch_news(conn: AsyncConnection, match_id: str) -> list[str]:
    """Read pre-fetched news from news_items table (populated by GAS via /web/news/ingest)."""
    try:
        rows = await conn.execute(
            text("""
                SELECT title, source
                FROM news_items
                WHERE match_id = cast(:mid as uuid)
                ORDER BY pub_date DESC NULLS LAST
                LIMIT 12
            """),
            {"mid": match_id},
        )
        return [f"- {r.title} ({r.source})" for r in rows]
    except Exception as exc:
        log.debug("news_items fetch failed: %s", exc)
        return []


async def _fetch_structured_news_context(conn: AsyncConnection, match_id: str) -> dict[str, Any] | None:
    """Read latest structured news context extracted from full article bodies."""
    try:
        row = await conn.execute(
            text(
                """
                SELECT context_payload
                FROM match_news_context_snapshots
                WHERE match_id = cast(:mid as uuid)
                ORDER BY context_date DESC, created_at DESC
                LIMIT 1
                """
            ),
            {"mid": match_id},
        )
        r = row.fetchone()
        if not r:
            return None
        payload = r[0] or {}
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        log.debug("structured news context fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    match: dict,
    raw_probs: dict[str, float],
    odds: dict,
    standings: list[dict],
    features: dict,
    weather: dict,
    news: list[str],
    structured_news_context: dict[str, Any] | None,
) -> list[dict]:
    home = match.get("home_team", "?")
    away = match.get("away_team", "?")
    kickoff = str(match.get("kickoff_at", ""))[:16].replace("T", " ")
    venue = f"{match.get('venue_name', 'Unknown')} ({match.get('venue_city', '')}, {match.get('venue_country', '')})"
    stage = match.get("stage_type", "GROUP_STAGE")

    # Format Poisson baseline
    p_home = round(raw_probs.get("HOME", 0.33) * 100, 1)
    p_draw = round(raw_probs.get("DRAW", 0.33) * 100, 1)
    p_away = round(raw_probs.get("AWAY", 0.34) * 100, 1)

    # Format odds
    odds_str = "No odds available"
    if odds:
        parts = []
        for sel, label in [("HOME", home), ("DRAW", "Draw"), ("AWAY", away)]:
            if sel in odds:
                parts.append(f"{label}: {odds[sel]['odds']} ({odds[sel]['bookmakers']} books)")
        if parts:
            odds_str = " | ".join(parts)

    # Format standings (top 8 rows)
    standings_str = "Not available"
    if standings:
        lines = [f"  {r['team']}: P{r['played']} W{r['won']} D{r['drawn']} L{r['lost']} GF{r['goals_for']} GA{r['goals_against']} Pts{r['points']}" for r in standings[:8]]
        standings_str = "\n".join(lines)

    # Format form
    home_fs = features.get("HOME", {})
    away_fs = features.get("AWAY", {})

    def fmt_team_form(fs: dict, name: str) -> str:
        if not fs:
            return f"{name}: no data"
        return (
            f"{name}: ELO={fs.get('elo_global', 1500):.0f} "
            f"(diff={fs.get('elo_diff', 0):+.0f}) | "
            f"ATK={fs.get('attack_strength', 1.0):.2f} DEF={fs.get('defense_strength', 1.0):.2f} | "
            f"Form pts={fs.get('form_points', 0):.1f} GD={fs.get('form_gd', 0):+.1f} | "
            f"Rest={fs.get('rest_days', 7)}d"
        )

    # Format weather
    weather_str = "No weather data"
    if weather:
        weather_str = (
            f"{weather.get('condition', 'Unknown')}, "
            f"{weather.get('temp_c', '?')}°C, "
            f"Wind {weather.get('wind_kph', '?')} km/h, "
            f"Rain {weather.get('precip_mm', 0)} mm"
        )

    # Format headline news fallback
    news_str = "\n".join(news) if news else "No recent news available"

    # Format structured context extracted from full article bodies.
    structured_ctx_str = "No structured news context available"
    if structured_news_context:
        try:
            sig = structured_news_context.get("signals", {})
            quality = structured_news_context.get("quality", {})
            impact = structured_news_context.get("team_impact_summary", {})
            lineup_n = len(sig.get("lineups", []) or [])
            injury_n = len(sig.get("injuries", []) or [])
            susp_n = len(sig.get("suspensions", []) or [])
            resolved_n = int(quality.get("resolved_player_mentions") or 0)

            def _fmt_team_impact(side: str) -> str:
                t = impact.get(side, {}) or {}
                top = t.get("key_absences", []) or []
                top_lines = []
                for item in top[:4]:
                    top_lines.append(
                        (
                            f"{item.get('player_name')}"
                            f" ({item.get('reason_type')}, impact={item.get('impact_score', 0):.2f}, "
                            f"min365={item.get('minutes_365d', 0):.0f}, "
                            f"g+a365={(item.get('goals_365d', 0) + item.get('assists_365d', 0)):.0f})"
                        )
                    )
                top_str = "; ".join(top_lines) if top_lines else "none"
                return (
                    f"{side}: injury_count={t.get('injury_count', 0)}, "
                    f"suspension_count={t.get('suspension_count', 0)}, "
                    f"key_absence_impact_score={t.get('key_absence_impact_score', 0):.3f}, "
                    f"top_absences={top_str}"
                )

            structured_ctx_str = (
                f"articles_processed={quality.get('articles_processed', 0)}, "
                f"resolved_player_mentions={resolved_n}, "
                f"lineups={lineup_n}, injuries={injury_n}, suspensions={susp_n}\n"
                f"team_impact_summary:\n{_fmt_team_impact('HOME')}\n{_fmt_team_impact('AWAY')}\n"
                f"context_json={json.dumps(structured_news_context, ensure_ascii=False)[:9000]}"
            )
        except Exception:
            structured_ctx_str = "Structured context exists but could not be serialized"

    system = (
        "You are an expert FIFA World Cup 2026 football analyst and betting model adjuster. "
        "Your role is to review a statistical model's probability estimates and make small, justified adjustments "
        "based on contextual factors not fully captured by the model. "
        "You must be conservative: only adjust when you have CONCRETE evidence. "
        "Return ONLY a valid JSON object — no markdown, no explanation outside the JSON."
    )

    user = f"""## Match
{home} vs {away}
Date: {kickoff} UTC | Stage: {stage} | Venue: {venue}

## Statistical Model Probabilities (Poisson + ELO)
Home ({home}): {p_home}%
Draw: {p_draw}%
Away ({away}): {p_away}%

## Market Odds
{odds_str}

## Group Standings
{standings_str}

## Team Form & Ratings
{fmt_team_form(home_fs, home)}
{fmt_team_form(away_fs, away)}

## Weather at Venue
{weather_str}

## Recent News (last 8 headlines)
{news_str}

## Structured News Context (from full article bodies)
{structured_ctx_str}

---
## Your Task

Review the model probabilities and decide if any adjustment is warranted.

ADJUSTMENT RULES (strict):
- Maximum adjustment: ±8 percentage points per outcome
- Only adjust if you have CONCRETE evidence such as:
  * Confirmed key starter injury or suspension
  * Extreme weather (>35°C, heavy rain affecting play style)
  * Clear form imbalance not captured by ELO (e.g., 5W vs 5L in last 5)
  * Significant home advantage not captured (partisan crowd in semi-final)
  * Critical standings pressure (must-win vs already qualified)
- Without such concrete evidence: keep adjustments within ±2 percentage points
- Probabilities MUST sum to exactly 1.000 (renormalize if needed)
- Use model values as baseline, not as a starting point to contradict

Return this JSON schema exactly:
{{
  "prob_home": <float 0-1, 6 decimal places>,
  "prob_draw": <float 0-1, 6 decimal places>,
  "prob_away": <float 0-1, 6 decimal places>,
  "confidence": <"high"|"medium"|"low">,
  "source": <"poisson"|"ia_ajustada"|"estimado">,
  "adjustment_home_pp": <float, actual change in percentage points vs model>,
  "adjustment_draw_pp": <float>,
  "adjustment_away_pp": <float>,
  "key_factors": [<list of 2-4 concrete factors considered>],
  "warnings": [<list of data quality issues, empty if none>]
}}"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------------------------

async def _call_openai(messages: list[dict], raw_probs: dict[str, float]) -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        return {}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.openai_model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 512,
                    "response_format": {"type": "json_object"},
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed
    except Exception as exc:
        log.warning("OpenAI call failed: %s", exc)
        return {}


def _validate_and_normalize(ai_result: dict, raw_probs: dict[str, float]) -> dict[str, float] | None:
    """Validate AI output, enforce ±8pp limit, renormalize to 1.0."""
    try:
        ph = float(ai_result["prob_home"])
        pd = float(ai_result["prob_draw"])
        pa = float(ai_result["prob_away"])
    except (KeyError, TypeError, ValueError):
        return None

    # Clamp adjustments to ±8pp
    for ai_val, raw_key in [(ph, "HOME"), (pd, "DRAW"), (pa, "AWAY")]:
        diff = ai_val - raw_probs[raw_key]
        if abs(diff) > _MAX_ADJUSTMENT_PP + 0.001:
            log.warning("AI adjustment exceeds ±8pp for %s: %.4f → %.4f, clamping", raw_key, raw_probs[raw_key], ai_val)
            if raw_key == "HOME":
                ph = raw_probs[raw_key] + max(-_MAX_ADJUSTMENT_PP, min(_MAX_ADJUSTMENT_PP, diff))
            elif raw_key == "DRAW":
                pd = raw_probs[raw_key] + max(-_MAX_ADJUSTMENT_PP, min(_MAX_ADJUSTMENT_PP, diff))
            else:
                pa = raw_probs[raw_key] + max(-_MAX_ADJUSTMENT_PP, min(_MAX_ADJUSTMENT_PP, diff))

    # Clamp to valid range
    ph = max(0.01, min(0.95, ph))
    pd = max(0.01, min(0.95, pd))
    pa = max(0.01, min(0.95, pa))

    # Renormalize to exactly 1.0
    total = ph + pd + pa
    return {
        "HOME": round(ph / total, 6),
        "DRAW": round(pd / total, 6),
        "AWAY": round(pa / total, 6),
    }


# ---------------------------------------------------------------------------
# DB updater
# ---------------------------------------------------------------------------

async def _update_calibrated_probabilities(
    conn: AsyncConnection,
    match_id: str,
    model_run_id: str,
    adjusted_probs: dict[str, float],
    ai_meta: dict,
    as_of: Any,
) -> int:
    """Update model_predictions with calibrated probabilities. Returns rows updated."""
    updated = 0
    sel_map = {"HOME": "HOME", "DRAW": "DRAW", "AWAY": "AWAY"}
    for sel_code, cal_prob in adjusted_probs.items():
        result = await conn.execute(
            text("""
                UPDATE model_predictions mp
                SET
                  calibrated_probability = :cal_prob,
                  prediction_status      = 'CALIBRATED',
                  explanation            = jsonb_set(
                    COALESCE(explanation, '{}'),
                    '{ai_adjustment}',
                    cast(:ai_meta as jsonb)
                  ),
                  as_of = :as_of
                FROM market_selections ms
                JOIN markets mk ON mk.market_id = ms.market_id AND mk.market_code = '1X2'
                WHERE mp.model_run_id  = cast(:run_id as uuid)
                  AND mp.match_id      = cast(:mid as uuid)
                  AND mp.market_id     = mk.market_id
                  AND ms.selection_id  = mp.selection_id
                  AND ms.selection_code = :sel_code
            """),
            {
                "cal_prob": round(cal_prob, 6),
                "run_id": model_run_id,
                "mid": match_id,
                "sel_code": sel_map[sel_code],
                "ai_meta": json.dumps(ai_meta),
                "as_of": as_of,
            },
        )
        updated += result.rowcount
    return updated


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def adjust_predictions_with_ai(
    conn: AsyncConnection,
    match_id: str,
    model_run_id: str,
    raw_probs: dict[str, float],
) -> dict[str, Any]:
    """
    Enrich Poisson predictions with OpenAI context adjustment.

    Returns dict with status and adjusted probabilities.
    If OpenAI is unavailable or key not set, returns raw_probs unchanged (RAW_ONLY status kept).
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return {"status": "skipped", "reason": "no_openai_key"}

    as_of = utc_now()

    # Fetch enriched context (all best-effort)
    match = await _fetch_match_context(conn, match_id)
    if not match:
        return {"status": "error", "reason": "match_not_found"}

    odds, standings, features = await _fetch_best_odds(conn, match_id), [], {}
    try:
        standings = await _fetch_standings(conn, match_id)
        features = await _fetch_feature_context(conn, match_id)
    except Exception as exc:
        log.debug("context fetch partial failure: %s", exc)

    weather = await _fetch_weather(conn, match_id, match.get("venue_city"), match.get("venue_country"), match.get("kickoff_at"))
    news = await _fetch_news(conn, match_id)
    structured_news_context = await _fetch_structured_news_context(conn, match_id)

    messages = _build_prompt(match, raw_probs, odds, standings, features, weather, news, structured_news_context)
    ai_result = await _call_openai(messages, raw_probs)

    if not ai_result:
        return {"status": "skipped", "reason": "openai_call_failed"}

    adjusted = _validate_and_normalize(ai_result, raw_probs)
    if not adjusted:
        log.warning("AI response invalid for match %s, keeping raw probs", match_id)
        return {"status": "skipped", "reason": "invalid_ai_response"}

    ai_meta = {
        "source": ai_result.get("source", "ia_ajustada"),
        "confidence": ai_result.get("confidence", "medium"),
        "key_factors": ai_result.get("key_factors", []),
        "warnings": ai_result.get("warnings", []),
        "adjustments_pp": {
            "HOME": ai_result.get("adjustment_home_pp", 0),
            "DRAW": ai_result.get("adjustment_draw_pp", 0),
            "AWAY": ai_result.get("adjustment_away_pp", 0),
        },
        "model": settings.openai_model,
        "adjusted_at": as_of.isoformat(),
        "structured_news_context_used": bool(structured_news_context),
    }

    rows_updated = await _update_calibrated_probabilities(
        conn, match_id, model_run_id, adjusted, ai_meta, as_of
    )

    return {
        "status": "ok",
        "raw_probs": raw_probs,
        "adjusted_probs": adjusted,
        "rows_updated": rows_updated,
        "confidence": ai_result.get("confidence"),
        "key_factors": ai_result.get("key_factors", []),
    }
