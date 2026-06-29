from app.competitions.service import _normalize_espn_event, _normalize_espn_status


def test_normalize_espn_status_marks_finished_when_completed_true() -> None:
    status = {
        "completed": True,
        "type": {"name": "STATUS_IN_PROGRESS"},
    }
    assert _normalize_espn_status(status) == "FINISHED"


def test_normalize_espn_status_marks_live_for_in_progress_variants() -> None:
    status = {
        "type": {
            "name": "STATUS_IN_PROGRESS",
            "description": "In Progress",
            "shortDetail": "45'",
        }
    }
    assert _normalize_espn_status(status) == "LIVE"


def test_normalize_espn_event_uses_linescores_when_score_is_missing() -> None:
    event = {
        "id": "event-1",
        "name": "Home vs Away",
        "date": "2026-06-29T19:00:00Z",
        "competitions": [
            {
                "date": "2026-06-29T19:00:00Z",
                "status": {
                    "type": {
                        "name": "STATUS_IN_PROGRESS",
                        "description": "In Progress",
                    }
                },
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "",
                        "linescores": [{"value": "1"}, {"value": "2"}],
                        "team": {"id": "home-id", "displayName": "Home"},
                    },
                    {
                        "homeAway": "away",
                        "score": None,
                        "linescores": [{"displayValue": "1"}],
                        "team": {"id": "away-id", "displayName": "Away"},
                    },
                ],
            }
        ],
    }

    normalized = _normalize_espn_event(event)

    assert normalized["status"] == "LIVE"
    assert normalized["home_score"] == 3
    assert normalized["away_score"] == 1
