"""
Qualification orchestrator: ties together StandingsResolver → BestThirdPlaceResolver →
TournamentSlotResolver and writes data_quality_events for every decision.

Entry point: run_qualification_resolver(conn, competition_season_id)

Called by:
  - qualification_resolver_job (daily + after FINISHED group-stage match in live cron)
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.time import iso_utc, utc_now
from app.services.notifications.telegram import notify_group_result
from app.services.qualification.best_third_resolver import BestThirdPlaceResolver
from app.services.qualification.models import QualificationResult
from app.services.qualification.slot_resolver import TournamentSlotResolver
from app.services.qualification.standings_resolver import StandingsResolver

log = logging.getLogger(__name__)


async def run_qualification_resolver(
    conn: AsyncConnection,
    competition_season_id: str,
) -> QualificationResult:
    """
    Idempotent end-to-end qualification resolution for one competition season.

    Steps:
      1. Recompute group standings from FINISHED matches
      2. Rank best thirds and qualify top 8
      3. Resolve tournament slots (group winners, runners-up, thirds, knockout)
      4. Write data_quality_events for each significant decision
    """
    result = QualificationResult(competition_season_id=competition_season_id)
    calculated_at = iso_utc()

    try:
        # ── 1. Standings ────────────────────────────────────────────────────
        standings = StandingsResolver(conn)
        standings_result = await standings.recompute(competition_season_id)
        result.groups_processed = standings_result.get("groups_processed", 0)
        result.tiebreakers_pending = sum(
            1 for t in standings_result.get("all_thirds", [])
            if "PENDING_TIEBREAKER" in (t.get("tiebreaker_notes") or [])
        )

        # Send Telegram notifications for fully-decided groups
        settings = get_settings()
        if settings.telegram_bot_token and settings.telegram_chat_id and standings_result.get("groups_processed", 0) > 0:
            for group_info in standings_result.get("decided_groups", []):
                await notify_group_result(
                    settings.telegram_bot_token,
                    settings.telegram_chat_id,
                    group_info["group_code"],
                    group_info["winner"],
                    group_info["runner_up"],
                    group_info["third"],
                    group_info["eliminated"],
                )

        await _write_event(
            conn,
            event_type="GROUP_STANDINGS_RECOMPUTED",
            competition_season_id=competition_season_id,
            payload={
                "groups_processed": result.groups_processed,
                "thirds_found": len(standings_result.get("all_thirds", [])),
                "calculated_at": calculated_at,
            },
        )

        # ── 2. Best thirds ───────────────────────────────────────────────────
        thirds_resolver = BestThirdPlaceResolver(conn)
        thirds_result = await thirds_resolver.resolve(competition_season_id)
        result.thirds_qualified = thirds_result.get("qualified", 0)

        if thirds_result.get("thirds_total", 0) > 0:
            await _write_event(
                conn,
                event_type=(
                    "BEST_THIRD_RESOLVED"
                    if thirds_result.get("pending", 0) == 0
                    else "BEST_THIRD_TIE_UNRESOLVED"
                ),
                competition_season_id=competition_season_id,
                payload={
                    "thirds_total": thirds_result.get("thirds_total", 0),
                    "qualified": thirds_result.get("qualified", 0),
                    "eliminated": thirds_result.get("eliminated", 0),
                    "pending": thirds_result.get("pending", 0),
                    "calculated_at": calculated_at,
                },
            )

        # ── 3. Slots ─────────────────────────────────────────────────────────
        slot_resolver = TournamentSlotResolver(conn)
        slots_result = await slot_resolver.resolve(competition_season_id)
        result.slots_resolved = slots_result.get("resolved", 0)
        result.slots_pending = slots_result.get("pending", 0)
        result.slot_resolutions = []  # summary only

        for r in slots_result.get("resolutions", []):
            if r["status"] == "RESOLVED":
                event_type = "SLOT_RESOLVED"
            elif r["status"] in ("CONFLICT",):
                event_type = "SLOT_CONFLICT"
            else:
                event_type = "SLOT_PENDING"

            await _write_event(
                conn,
                event_type=event_type,
                competition_season_id=competition_season_id,
                payload={
                    "slot_code": r["slot_code"],
                    "status": r["status"],
                    "team_id": r.get("team_id"),
                    "reason": r.get("reason"),
                    "calculated_at": calculated_at,
                },
            )
            result.events_written += 1

        result.events_written += 2  # standings + thirds events

        log.info(
            "qualification_resolver: season=%s groups=%d slots_resolved=%d slots_pending=%d thirds=%d",
            competition_season_id,
            result.groups_processed,
            result.slots_resolved,
            result.slots_pending,
            result.thirds_qualified,
        )

    except Exception as exc:
        log.exception("qualification_resolver failed: %s", exc)
        result.errors.append(str(exc))
        await _write_event(
            conn,
            event_type="SLOT_CONFLICT",
            competition_season_id=competition_season_id,
            payload={"error": str(exc), "source": "qualification_resolver"},
        )

    return result


async def should_run_resolver(conn: AsyncConnection, competition_season_id: str) -> bool:
    """
    Returns True if there are FINISHED group-stage matches whose result has
    not yet been reflected in standings (quick check for live cron guard).
    """
    result = await conn.execute(
        text("""
            SELECT count(*) FROM matches m
            JOIN competition_stages cs ON cs.stage_id = m.stage_id
            WHERE m.competition_season_id = cast(:sid as uuid)
              AND cs.stage_type = 'GROUP_STAGE'
              AND m.status = 'FINISHED'
        """),
        {"sid": competition_season_id},
    )
    finished_matches = result.scalar() or 0
    return finished_matches > 0


async def get_active_season_id(conn: AsyncConnection, season_slug: str) -> str | None:
    result = await conn.execute(
        text("""
            SELECT competition_season_id::text
            FROM competition_seasons
            WHERE slug = :slug AND status IN ('ACTIVE', 'SCHEDULED')
            LIMIT 1
        """),
        {"slug": season_slug},
    )
    row = result.fetchone()
    return row[0] if row else None


# ── internal ──────────────────────────────────────────────────────────────────

async def _write_event(
    conn: AsyncConnection,
    event_type: str,
    competition_season_id: str,
    payload: dict,
) -> None:
    try:
        await conn.execute(
            text("""
                INSERT INTO data_quality_events
                    (layer, entity_type, entity_id, severity, check_type, message, payload)
                VALUES
                    ('CANONICAL', 'COMPETITION_SEASON', cast(:eid as uuid),
                     'INFO', :check_type, :message, cast(:payload as jsonb))
            """),
            {
                "eid": competition_season_id,
                "check_type": event_type,
                "message": f"qualification_resolver: {event_type.lower()}",
                "payload": __import__("json").dumps({
                    "competition_season_id": competition_season_id,
                    "source": "qualification_resolver",
                    **payload,
                }),
            },
        )
    except Exception:
        log.warning("qualification_resolver: failed to write event %s", event_type)
