from __future__ import annotations

import asyncio
from typing import Any

import app.competitions.team_sync as team_sync


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None, scalar_value: Any = None) -> None:
        self._rows = rows or []
        self._scalar_value = scalar_value
        self.rowcount = 0

    def scalar_one(self) -> Any:
        return self._scalar_value

    def scalar_one_or_none(self) -> Any:
        return self._scalar_value

    def fetchone(self) -> Any:
        if not self._rows:
            return None
        first = self._rows[0]
        return tuple(first.values())

    def first(self) -> Any:
        if not self._rows:
            return None

        class _Row:
            def __init__(self, mapping: dict[str, Any]) -> None:
                self._mapping = mapping

        return _Row(self._rows[0])


class _FakeConn:
    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).lower()
        params = params or {}

        if "information_schema.tables" in sql:
            return _FakeResult(scalar_value=True)
        if "count(*)::int" in sql and "raw_api_calls" in sql:
            return _FakeResult(scalar_value=0)
        if "from competition_seasons" in sql and "where slug" in sql:
            return _FakeResult(rows=[{"competition_season_id": "season-id"}])
        if "insert into raw_api_calls" in sql:
            return _FakeResult(scalar_value=None)
        if "from entity_external_refs" in sql:
            return _FakeResult(scalar_value=None)
        if "insert into competition_team_entries" in sql:
            return _FakeResult(scalar_value=None)
        if "insert into competition_rosters" in sql:
            return _FakeResult(scalar_value=None)
        if "insert into data_quality_events" in sql:
            return _FakeResult(scalar_value=None)

        # Generic fallback for updates/inserts not asserted here.
        return _FakeResult(scalar_value=None)


class _FakeApiFootballClient:
    async def teams(self, league: int, season: int) -> dict[str, Any]:
        _ = league, season
        return {
            "response": [
                {
                    "team": {
                        "id": 1,
                        "name": "Test Team",
                        "country": "Chile",
                        "national": False,
                        "founded": 1900,
                        "logo": "logo.png",
                    },
                    "venue": {"name": "Test Stadium", "capacity": 10000},
                }
            ],
            "results": 1,
        }


class _FakeFootballDataClient:
    async def competition_teams(self, code: str, season: int) -> dict[str, Any]:
        _ = code, season
        return {"teams": []}


class _FakeSportmonksClient:
    async def fixtures(self, include: str, page: int, per_page: int) -> dict[str, Any]:
        _ = include, page, per_page
        return {"data": []}


def test_sync_teams_seeds_each_catalog_entry(monkeypatch: Any) -> None:
    seed_calls: list[str] = []

    async def _fake_seed(conn: Any, competition: str | None = None) -> dict[str, Any]:
        _ = conn
        if competition:
            seed_calls.append(competition)
        return {"status": "OK"}

    async def _fake_resolve_country_code(conn: Any, country_value: str | None, cache: dict[str, str | None]) -> str | None:
        _ = conn, cache
        return "CL" if country_value else None

    async def _fake_resolve_or_create_team(
        conn: Any,
        *,
        source: str,
        source_team_id: str,
        display_name: str,
        team_type: str,
        country_code: str | None,
        metadata: dict[str, Any],
    ) -> str:
        _ = conn, source, source_team_id, display_name, team_type, country_code, metadata
        return "team-id"

    monkeypatch.setattr(team_sync, "seed_competition_catalog", _fake_seed)
    monkeypatch.setattr(team_sync, "ApiFootballClient", lambda: _FakeApiFootballClient())
    monkeypatch.setattr(team_sync, "FootballDataClient", lambda: _FakeFootballDataClient())
    monkeypatch.setattr(team_sync, "SportmonksClient", lambda: _FakeSportmonksClient())
    monkeypatch.setattr(team_sync, "_resolve_country_code", _fake_resolve_country_code)
    monkeypatch.setattr(team_sync, "_resolve_or_create_team", _fake_resolve_or_create_team)

    conn = _FakeConn()
    result = asyncio.run(team_sync.sync_teams_for_all_leagues(conn))

    expected = {entry.slug for entry in team_sync.supported_competitions()}
    assert expected.issubset(set(seed_calls))
    assert result["job_name"] == "sync_all_leagues_teams"


def test_sync_players_seeds_each_catalog_entry_before_time_budget_skip(monkeypatch: Any) -> None:
    seed_calls: list[str] = []

    async def _fake_seed(conn: Any, competition: str | None = None) -> dict[str, Any]:
        _ = conn
        if competition:
            seed_calls.append(competition)
        return {"status": "OK"}

    monkeypatch.setattr(team_sync, "seed_competition_catalog", _fake_seed)

    conn = _FakeConn()
    result = asyncio.run(
        team_sync.sync_players_for_all_leagues(
            conn,
            {
                "max_runtime_seconds": 0,
            },
        )
    )

    expected = {entry.slug for entry in team_sync.supported_competitions()}
    assert expected.issubset(set(seed_calls))
    assert result["job_name"] == "sync_all_leagues_players"
