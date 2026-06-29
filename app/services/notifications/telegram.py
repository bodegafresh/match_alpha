"""Telegram Bot API notifications for match events."""
from __future__ import annotations
import logging
import httpx

log = logging.getLogger(__name__)

CARD_EMOJI = {
    "yellow": "🟨",
    "red": "🟥",
    "yellow_red": "🟨🟥",
}

EVENT_TYPE_MAP = {
    "yellow card": ("yellow", "Tarjeta Amarilla"),
    "yellowcard": ("yellow", "Tarjeta Amarilla"),
    "yellow": ("yellow", "Tarjeta Amarilla"),
    "red card": ("red", "Tarjeta Roja"),
    "redcard": ("red", "Tarjeta Roja"),
    "red": ("red", "Tarjeta Roja"),
    "yellow red card": ("yellow_red", "Doble Amarilla"),
    "yellowred": ("yellow_red", "Doble Amarilla"),
    "second yellow": ("yellow_red", "Doble Amarilla"),
}


def _classify_event(event_type: str) -> tuple[str, str] | None:
    """Returns (card_type, label) or None if not a card event."""
    key = event_type.lower().strip()
    for pattern, result in EVENT_TYPE_MAP.items():
        if pattern in key:
            return result
    return None


async def _send(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            if resp.status_code != 200:
                log.warning("telegram: sendMessage returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("telegram: notification failed: %s", exc)


async def notify_card_event(
    bot_token: str,
    chat_id: str,
    match_label: str,
    team_name: str,
    player_name: str | None,
    event_type: str,
    minute: int | None,
) -> None:
    """Send a Telegram notification for a card event."""
    classified = _classify_event(event_type)
    if not classified:
        return
    card_type, label = classified
    emoji = CARD_EMOJI[card_type]
    minute_str = f"min. {minute}" if minute else ""
    player_str = player_name or "Jugador desconocido"
    text = f"{emoji} <b>{label}</b> — {match_label} {minute_str}\n{player_str} ({team_name})"
    await _send(bot_token, chat_id, text)


async def notify_group_result(
    bot_token: str,
    chat_id: str,
    group_code: str,
    winner: str,
    runner_up: str,
    third: str,
    eliminated: str,
) -> None:
    """Send a Telegram summary when a group is fully decided."""
    text = (
        f"🏆 <b>{group_code} definido</b>\n"
        f"1° {winner} → Clasifica\n"
        f"2° {runner_up} → Clasifica\n"
        f"3° {third} → Candidato mejor tercero\n"
        f"4° {eliminated} → Eliminado"
    )
    await _send(bot_token, chat_id, text)


async def notify_text(
    bot_token: str,
    chat_id: str,
    text: str,
) -> None:
    """Send a plain Telegram message (HTML parse mode enabled)."""
    await _send(bot_token, chat_id, text)
