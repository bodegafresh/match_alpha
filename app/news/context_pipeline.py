from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import iso_utc, utc_now
from app.normalization.player_identity import name_signature, normalize_identity_name

log = logging.getLogger(__name__)

_MAX_NEWS_PER_MATCH = 8
_MAX_BODY_CHARS = 18000


@dataclass
class RosterPlayer:
    player_id: str
    display_name: str
    normalized_name: str
    nationality_country_code: str | None
    aliases: list[str]


def _strip_html_to_text(raw_html: str) -> str:
    """Lightweight HTML to text extraction without external dependencies."""
    s = raw_html or ""
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:_MAX_BODY_CHARS]


def _safe_json_loads(payload: str) -> dict[str, Any] | None:
    try:
        out = json.loads(payload)
        return out if isinstance(out, dict) else None
    except Exception:
        return None


async def _fetch_article_body(url: str) -> tuple[str | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(
                url,
                headers={
                    "User-Agent": "match-alpha-news-context/1.0 (+https://match-alpha.onrender.com)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            if r.status_code >= 400:
                return None, f"http_{r.status_code}"
            return _strip_html_to_text(r.text), None
    except Exception as exc:
        return None, str(exc)


def _extraction_prompt(match: dict[str, Any], article: dict[str, Any]) -> list[dict[str, str]]:
    home = match.get("home_team", "")
    away = match.get("away_team", "")
    kickoff_at = str(match.get("kickoff_at") or "")

    system = (
        "You are a football match intelligence extractor. "
        "Extract ONLY concrete, explicit facts from the article text. "
        "Do not infer missing names. Return ONLY valid JSON object."
    )
    user = f"""
Match context:
- Home: {home}
- Away: {away}
- Kickoff: {kickoff_at}

Article source: {article.get("source")}
Article title: {article.get("title")}
Article url: {article.get("url")}
Article body:
{article.get("body_text")}

Return strict JSON with this schema:
{{
  "lineups": [
    {{"team_side":"HOME|AWAY|UNKNOWN","player_name":"string","position":"string|null","confidence":0.0}}
  ],
  "injuries": [
    {{"team_side":"HOME|AWAY|UNKNOWN","player_name":"string","status":"OUT|DOUBTFUL|QUESTIONABLE","reason":"string|null","confidence":0.0}}
  ],
  "suspensions": [
    {{"team_side":"HOME|AWAY|UNKNOWN","player_name":"string","reason":"string|null","confidence":0.0}}
  ],
  "notes": ["short strings"],
  "quality": {{"article_relevant": true, "signals_found": 0}}
}}

Rules:
- confidence range [0,1]
- If a signal is not explicit, do not include it.
- Keep player_name exactly as seen in article.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _extract_article_signals_with_llm(match: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        return {
            "lineups": [],
            "injuries": [],
            "suspensions": [],
            "notes": [],
            "quality": {"article_relevant": False, "signals_found": 0, "reason": "no_openai_key"},
        }

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.openai_model,
                    "messages": _extraction_prompt(match, article),
                    "temperature": 0,
                    "max_tokens": 900,
                    "response_format": {"type": "json_object"},
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            parsed = _safe_json_loads(content)
            if not parsed:
                return {
                    "lineups": [],
                    "injuries": [],
                    "suspensions": [],
                    "notes": [],
                    "quality": {"article_relevant": False, "signals_found": 0, "reason": "invalid_json"},
                }
            return {
                "lineups": parsed.get("lineups", []) or [],
                "injuries": parsed.get("injuries", []) or [],
                "suspensions": parsed.get("suspensions", []) or [],
                "notes": parsed.get("notes", []) or [],
                "quality": parsed.get("quality", {"article_relevant": False, "signals_found": 0}) or {},
            }
    except Exception as exc:
        log.warning("news context extraction failed url=%s err=%s", article.get("url"), exc)
        return {
            "lineups": [],
            "injuries": [],
            "suspensions": [],
            "notes": [],
            "quality": {"article_relevant": False, "signals_found": 0, "reason": str(exc)},
        }


async def _load_match_basic(conn: AsyncConnection, match_id: str) -> dict[str, Any] | None:
    row = await conn.execute(
        text(
            """
            SELECT
              m.match_id::text,
              m.kickoff_at,
              m.competition_season_id::text,
              ht.team_id::text AS home_team_id,
              ht.display_name AS home_team,
              away_t.team_id::text AS away_team_id,
              away_t.display_name AS away_team
            FROM matches m
            JOIN match_participants hp ON hp.match_id = m.match_id AND hp.side = 'HOME'
            JOIN teams ht ON ht.team_id = hp.team_id
            JOIN match_participants ap ON ap.match_id = m.match_id AND ap.side = 'AWAY'
            JOIN teams away_t ON away_t.team_id = ap.team_id
            WHERE m.match_id = cast(:mid as uuid)
            LIMIT 1
            """
        ),
        {"mid": match_id},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def _load_news_for_match(conn: AsyncConnection, match_id: str) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            SELECT id_hash, title, url, source, pub_date
            FROM news_items
            WHERE match_id = cast(:mid as uuid)
            ORDER BY pub_date DESC NULLS LAST, fetched_at DESC
            LIMIT :lim
            """
        ),
        {"mid": match_id, "lim": _MAX_NEWS_PER_MATCH},
    )
    return [dict(r._mapping) for r in rows]


async def _upsert_news_document(conn: AsyncConnection, id_hash: str, title: str, url: str, source: str, body_text: str | None, fetch_error: str | None) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO news_item_documents
              (id_hash, title, url, source, body_text, fetch_error, fetched_at)
            VALUES
              (:id_hash, :title, :url, :source, :body_text, :fetch_error, now())
            ON CONFLICT (id_hash) DO UPDATE SET
              title = excluded.title,
              url = excluded.url,
              source = excluded.source,
              body_text = COALESCE(excluded.body_text, news_item_documents.body_text),
              fetch_error = excluded.fetch_error,
              fetched_at = now()
            """
        ),
        {
            "id_hash": id_hash,
            "title": title,
            "url": url,
            "source": source,
            "body_text": body_text,
            "fetch_error": fetch_error,
        },
    )


async def _load_team_roster_players(conn: AsyncConnection, season_id: str, team_id: str) -> list[RosterPlayer]:
    rows = await conn.execute(
        text(
            """
            SELECT
              p.player_id::text,
              p.display_name,
              p.normalized_name,
              p.nationality_country_code,
              COALESCE(array_agg(pa.normalized_alias) FILTER (WHERE pa.normalized_alias IS NOT NULL), ARRAY[]::text[]) AS aliases
            FROM competition_rosters cr
            JOIN players p ON p.player_id = cr.player_id
            LEFT JOIN player_aliases pa ON pa.player_id = p.player_id
            WHERE cr.competition_season_id = cast(:season_id as uuid)
              AND cr.team_id = cast(:team_id as uuid)
            GROUP BY p.player_id, p.display_name, p.normalized_name, p.nationality_country_code
            """
        ),
        {"season_id": season_id, "team_id": team_id},
    )
    out: list[RosterPlayer] = []
    for r in rows:
        m = r._mapping
        out.append(
            RosterPlayer(
                player_id=m["player_id"],
                display_name=m["display_name"],
                normalized_name=m["normalized_name"],
                nationality_country_code=m.get("nationality_country_code"),
                aliases=list(m.get("aliases") or []),
            )
        )
    return out


def _resolve_player_from_roster(player_name: str, nationality_code: str | None, roster: list[RosterPlayer]) -> dict[str, Any] | None:
    if not player_name:
        return None

    normalized = normalize_identity_name(player_name)
    sig = name_signature(normalized)
    best: tuple[int, RosterPlayer] | None = None

    for p in roster:
        score = 0
        if p.normalized_name == normalized:
            score += 100
        elif normalized in p.aliases:
            score += 92
        else:
            if sig and name_signature(p.normalized_name) == sig:
                score += 72

        if nationality_code and p.nationality_country_code and nationality_code.upper() == p.nationality_country_code.upper():
            score += 14

        if score == 0:
            continue
        if best is None or score > best[0]:
            best = (score, p)

    if not best:
        return None

    score, p = best
    if score >= 96:
        confidence = "high"
    elif score >= 78:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "player_id": p.player_id,
        "display_name": p.display_name,
        "confidence": confidence,
        "match_score": score,
    }


async def _fetch_player_stats_summary(conn: AsyncConnection, player_id: str) -> dict[str, Any]:
    rows = await conn.execute(
        text(
            """
            SELECT stat_name, AVG(stat_value)::numeric(10,3) AS avg_value, MAX(captured_at) AS last_at
            FROM player_match_stats
            WHERE player_id = cast(:pid as uuid)
            GROUP BY stat_name
            ORDER BY MAX(captured_at) DESC
            LIMIT 8
            """
        ),
        {"pid": player_id},
    )
    stats = [
        {
            "stat_name": r[0],
            "avg_value": float(r[1]) if r[1] is not None else None,
            "last_captured_at": iso_utc(r[2]) if r[2] else None,
        }
        for r in rows
    ]

    matches_row = await conn.execute(
        text(
            """
            SELECT COUNT(DISTINCT match_id)::int
            FROM player_match_stats
            WHERE player_id = cast(:pid as uuid)
              AND captured_at >= now() - interval '180 days'
            """
        ),
        {"pid": player_id},
    )
    recent_matches = int(matches_row.scalar() or 0)

    aggregate_row = await conn.execute(
        text(
            """
            SELECT
              COUNT(DISTINCT match_id) FILTER (WHERE stat_name = 'minutes')::int AS appearances,
              COALESCE(SUM(stat_value) FILTER (WHERE stat_name = 'minutes'), 0)::numeric(10,2) AS minutes,
              COALESCE(SUM(stat_value) FILTER (WHERE stat_name = 'goals_scored'), 0)::numeric(10,2) AS goals,
              COALESCE(SUM(stat_value) FILTER (WHERE stat_name = 'assists'), 0)::numeric(10,2) AS assists,
              ROUND(AVG(stat_value) FILTER (WHERE stat_name = 'rating'), 2)::numeric(10,2) AS avg_rating
            FROM player_match_stats
            WHERE player_id = cast(:pid as uuid)
              AND captured_at >= now() - interval '365 days'
            """
        ),
        {"pid": player_id},
    )
    agg = aggregate_row.fetchone()
    aggregate = {
        "appearances_365d": int((agg[0] or 0) if agg else 0),
        "minutes_365d": float((agg[1] or 0) if agg else 0),
        "goals_365d": float((agg[2] or 0) if agg else 0),
        "assists_365d": float((agg[3] or 0) if agg else 0),
        "avg_rating_365d": float(agg[4]) if agg and agg[4] is not None else None,
    }

    impact_score = min(
        1.0,
        (
            min(1.0, aggregate["minutes_365d"] / 1800.0) * 0.5
            + min(1.0, (aggregate["goals_365d"] + aggregate["assists_365d"]) / 12.0) * 0.35
            + min(1.0, recent_matches / 20.0) * 0.15
        ),
    )

    return {
        "recent_matches_180d": recent_matches,
        "top_stats": stats,
        "aggregate": aggregate,
        "impact_score": round(float(impact_score), 3),
    }


def _normalize_side(side: str | None) -> str:
    s = str(side or "UNKNOWN").upper()
    return s if s in {"HOME", "AWAY"} else "UNKNOWN"


async def _resolve_signals(
    conn: AsyncConnection,
    match: dict[str, Any],
    extracted: dict[str, Any],
) -> dict[str, Any]:
    home_roster = await _load_team_roster_players(conn, match["competition_season_id"], match["home_team_id"])
    away_roster = await _load_team_roster_players(conn, match["competition_season_id"], match["away_team_id"])

    resolved = {
        "lineups": [],
        "injuries": [],
        "suspensions": [],
    }

    for key in ("lineups", "injuries", "suspensions"):
        for item in extracted.get(key, []) or []:
            team_side = _normalize_side(item.get("team_side"))
            roster = home_roster if team_side == "HOME" else away_roster if team_side == "AWAY" else (home_roster + away_roster)

            nationality_code = item.get("nationality_country_code")
            found = _resolve_player_from_roster(item.get("player_name") or "", nationality_code, roster)

            resolved_item = {
                "team_side": team_side,
                "player_name": item.get("player_name"),
                "confidence": float(item.get("confidence") or 0),
                "position": item.get("position"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "resolved_player": found,
            }
            if found and found.get("player_id"):
                resolved_item["player_stats"] = await _fetch_player_stats_summary(conn, found["player_id"])
            resolved[key].append(resolved_item)

    return resolved


def _aggregate_context(match: dict[str, Any], extracted_by_article: list[dict[str, Any]]) -> dict[str, Any]:
    lineups: list[dict[str, Any]] = []
    injuries: list[dict[str, Any]] = []
    suspensions: list[dict[str, Any]] = []
    notes: list[str] = []
    evidence: list[dict[str, Any]] = []

    for item in extracted_by_article:
        extracted = item["extracted"]
        resolved = item["resolved"]
        lineups.extend(resolved.get("lineups", []))
        injuries.extend(resolved.get("injuries", []))
        suspensions.extend(resolved.get("suspensions", []))
        notes.extend(extracted.get("notes", []) or [])
        evidence.append(
            {
                "id_hash": item.get("id_hash"),
                "url": item.get("url"),
                "source": item.get("source"),
                "pub_date": iso_utc(item.get("pub_date")) if item.get("pub_date") else None,
                "article_relevant": bool((extracted.get("quality") or {}).get("article_relevant")),
            }
        )

    resolved_mentions = sum(1 for section in (lineups, injuries, suspensions) for x in section if x.get("resolved_player"))

    def _summarize_team_impact(team_side: str) -> dict[str, Any]:
        injury_items = [x for x in injuries if x.get("team_side") == team_side]
        suspension_items = [x for x in suspensions if x.get("team_side") == team_side]

        key_absences: list[dict[str, Any]] = []
        for bucket, reason_label in ((injury_items, "injury"), (suspension_items, "suspension")):
            for item in bucket:
                resolved = item.get("resolved_player") or {}
                stats = item.get("player_stats") or {}
                agg = stats.get("aggregate") or {}
                key_absences.append(
                    {
                        "player_id": resolved.get("player_id"),
                        "player_name": resolved.get("display_name") or item.get("player_name"),
                        "reason_type": reason_label,
                        "status": item.get("status"),
                        "confidence": resolved.get("confidence") or "low",
                        "impact_score": float(stats.get("impact_score") or 0),
                        "appearances_365d": int(agg.get("appearances_365d") or 0),
                        "minutes_365d": float(agg.get("minutes_365d") or 0),
                        "goals_365d": float(agg.get("goals_365d") or 0),
                        "assists_365d": float(agg.get("assists_365d") or 0),
                        "avg_rating_365d": agg.get("avg_rating_365d"),
                    }
                )

        key_absences.sort(key=lambda x: x.get("impact_score", 0), reverse=True)
        total_impact = round(sum(float(x.get("impact_score") or 0) for x in key_absences[:8]), 3)
        return {
            "injury_count": len(injury_items),
            "suspension_count": len(suspension_items),
            "key_absence_impact_score": total_impact,
            "key_absences": key_absences[:8],
        }

    team_impact_summary = {
        "HOME": _summarize_team_impact("HOME"),
        "AWAY": _summarize_team_impact("AWAY"),
    }

    return {
        "match_id": match["match_id"],
        "snapshot_at": utc_now().isoformat(),
        "teams": {
            "HOME": {"team_id": match["home_team_id"], "team_name": match["home_team"]},
            "AWAY": {"team_id": match["away_team_id"], "team_name": match["away_team"]},
        },
        "signals": {
            "lineups": lineups,
            "injuries": injuries,
            "suspensions": suspensions,
            "notes": notes[:30],
        },
        "quality": {
            "articles_processed": len(extracted_by_article),
            "resolved_player_mentions": resolved_mentions,
        },
        "team_impact_summary": team_impact_summary,
        "source_evidence": evidence,
    }


async def _persist_match_context(conn: AsyncConnection, match_id: str, context_json: dict[str, Any]) -> None:
    context_date = utc_now().date()
    await conn.execute(
        text(
            """
            INSERT INTO match_news_context_snapshots
              (match_id, context_date, context_payload, source_count, high_confidence_signals, created_at)
            VALUES
              (cast(:mid as uuid), :context_date, cast(:payload as jsonb), :source_count, :high_conf, now())
            ON CONFLICT (match_id, context_date) DO UPDATE SET
              context_payload = excluded.context_payload,
              source_count = excluded.source_count,
              high_confidence_signals = excluded.high_confidence_signals,
              created_at = now()
            """
        ),
        {
            "mid": match_id,
            "context_date": context_date,
            "payload": json.dumps(context_json, ensure_ascii=False),
            "source_count": int(context_json.get("quality", {}).get("articles_processed") or 0),
            "high_conf": sum(
                1
                for section in ("lineups", "injuries", "suspensions")
                for item in context_json.get("signals", {}).get(section, [])
                if (item.get("resolved_player") or {}).get("confidence") == "high"
            ),
        },
    )


async def _build_context_for_match(conn: AsyncConnection, match_id: str) -> dict[str, Any]:
    match = await _load_match_basic(conn, match_id)
    if not match:
        return {"status": "WARN", "match_id": match_id, "message": "match_not_found", "articles_processed": 0}

    news_rows = await _load_news_for_match(conn, match_id)
    if not news_rows:
        return {"status": "OK", "match_id": match_id, "articles_processed": 0, "message": "no_news"}

    extracted_by_article: list[dict[str, Any]] = []
    for n in news_rows:
        body_text, fetch_error = await _fetch_article_body(n["url"])
        await _upsert_news_document(
            conn,
            id_hash=n["id_hash"],
            title=n["title"],
            url=n["url"],
            source=n["source"],
            body_text=body_text,
            fetch_error=fetch_error,
        )
        if not body_text:
            continue

        extracted = await _extract_article_signals_with_llm(
            match,
            {
                "title": n["title"],
                "url": n["url"],
                "source": n["source"],
                "body_text": body_text,
            },
        )
        resolved = await _resolve_signals(conn, match, extracted)

        extracted_by_article.append(
            {
                "id_hash": n["id_hash"],
                "url": n["url"],
                "source": n["source"],
                "pub_date": n.get("pub_date"),
                "extracted": extracted,
                "resolved": resolved,
            }
        )

    context_json = _aggregate_context(match, extracted_by_article)
    await _persist_match_context(conn, match_id, context_json)

    return {
        "status": "OK",
        "match_id": match_id,
        "articles_processed": len(news_rows),
        "articles_with_body": len(extracted_by_article),
        "resolved_player_mentions": int(context_json.get("quality", {}).get("resolved_player_mentions") or 0),
    }


async def _upcoming_match_ids(conn: AsyncConnection, limit: int = 24) -> list[str]:
    rows = await conn.execute(
        text(
            """
            SELECT DISTINCT n.match_id::text
            FROM news_items n
            JOIN matches m ON m.match_id = n.match_id
            WHERE n.match_id IS NOT NULL
              AND m.kickoff_at >= now() - interval '8 hours'
              AND m.kickoff_at <= now() + interval '72 hours'
              AND m.status IN ('SCHEDULED', 'LIVE')
            ORDER BY m.kickoff_at ASC
            LIMIT :lim
            """
        ),
        {"lim": limit},
    )
    return [r[0] for r in rows]


async def build_news_context(conn: AsyncConnection, payload: dict[str, Any]) -> dict[str, Any]:
    """Build structured news context for upcoming matches and resolve players to local IDs."""
    single_match_id = payload.get("match_id")
    limit = int(payload.get("limit", 12) or 12)

    match_ids = [single_match_id] if single_match_id else await _upcoming_match_ids(conn, limit=limit)
    if not match_ids:
        return {
            "status": "OK",
            "job_name": "news_context_extract",
            "records_processed": 0,
            "message": "no_matches_with_news",
            "generated_at": iso_utc(),
        }

    processed = 0
    warnings: list[str] = []
    total_mentions = 0
    for match_id in match_ids:
        result = await _build_context_for_match(conn, match_id)
        if result.get("status") == "WARN":
            warnings.append(f"{match_id}:{result.get('message')}")
        total_mentions += int(result.get("resolved_player_mentions") or 0)
        processed += 1

    return {
        "status": "WARN" if warnings else "OK",
        "job_name": "news_context_extract",
        "records_processed": processed,
        "matches_processed": processed,
        "resolved_player_mentions": total_mentions,
        "warnings": warnings[:40],
        "generated_at": iso_utc(),
    }
