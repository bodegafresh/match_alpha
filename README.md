# Match Alpha Backend

Backend FastAPI para migrar el runtime principal desde Google Apps Script hacia Python + Supabase/PostgreSQL.

## Local

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

## Jobs

```bash
python -m app.cli.run_job worldcup_daily_refresh
python -m app.cli.run_job worldcup_live_refresh
python -m app.cli.run_job odds_refresh
python -m app.cli.run_job feature_snapshot_build
python -m app.cli.run_job dataset_builder
python -m app.cli.run_job model_recompute
python -m app.cli.run_job ev_decision
python -m app.cli.run_job settlement
python -m app.cli.run_job calibration_recompute
python -m app.cli.run_job backtest_walk_forward
python -m app.cli.run_job drift_detection
python -m app.cli.run_job model_promotion
python -m app.cli.run_job sync_all_leagues_teams
python -m app.cli.run_job sync_all_leagues_players
python -m app.cli.run_job validate_sync_coverage_all_leagues
python -m app.cli.run_job telegram_daily_summary
```

Los jobs HTTP usan:

```bash
curl -X POST "$API_URL/api/v1/jobs/ev_decision/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY"
```

Orquestaciones por servicio:

```bash
curl -X POST "$API_URL/api/v1/jobs/orchestrate/daily" \
  -H "Authorization: Bearer $API_INTERNAL_KEY"

curl -X POST "$API_URL/api/v1/jobs/orchestrate/live" \
  -H "Authorization: Bearer $API_INTERNAL_KEY"

curl -X POST "$API_URL/api/v1/jobs/orchestrate/weekly" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"manual_ops"}'

curl -X POST "$API_URL/api/v1/jobs/orchestrate/weekly-players" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"manual_ops_players"}'

curl -X POST "$API_URL/api/v1/jobs/telegram_daily_summary/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"manual_ops","ev_limit":8}'

curl -X POST "$API_URL/api/v1/jobs/reconcile_players_identity/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true,"limit_merges":1000}'

curl -X POST "$API_URL/api/v1/jobs/reconcile_players_identity/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run":false,"limit_merges":1000}'

curl -X POST "$API_URL/api/v1/jobs/reconcile_teams_identity/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true,"limit_merges":500}'

curl -X POST "$API_URL/api/v1/jobs/reconcile_referees_identity/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true,"limit_merges":300}'

curl -X POST "$API_URL/api/v1/jobs/reconcile_venues_identity/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true,"limit_merges":300}'
```

Telegram:
- `qualification_resolver` no envia mensajes cuando corre dentro de `orchestrate/live`.
- El resumen matinal (8:00 Chile) se envia con `telegram_daily_summary` y prioriza EV+ del ultimo daily.

## Operacion Semanal (Canonical)

Prioridad de fuentes para sync canonical:
- Equipos: API_FOOTBALL primero; luego FOOTBALL_DATA; luego SPORTMONKS (participants desde fixtures).
- Jugadores: API_FOOTBALL primero; luego FOOTBALL_DATA (squad); luego SPORTMONKS (lineups desde fixtures).
- Resolucion de identidad: `entity_external_refs` + aliases + normalized name (sin modelo legacy).
- Seguridad de reconciliacion: rosters solo se desactivan cuando hubo una fuente seleccionada con datos para esa liga.
- Trazabilidad: `league_stats` incluye `source_attempts` (fuente/razon) para diagnostico de fallback.

Validacion de cobertura (`validate_sync_coverage_all_leagues`):
- Ya no depende solo de `external_ids.API_FOOTBALL`.
- Evalua toda liga con capacidad `teams` en alguna fuente declarada del catalogo.

- Trigger GAS semanal existente (`runWeeklyTeamsSync`) ejecuta una sola llamada al servicio:
  - `POST /api/v1/jobs/orchestrate/weekly`
- Flujo semanal backend:
  - `sync_all_leagues_teams`
- Trigger GAS semanal de estadios/referees (`runWeeklyMatchEntitiesSync`) ejecuta:
  - `POST /api/v1/jobs/orchestrate/weekly-match-entities`
- Flujo semanal de entidades backend:
  - `sync_all_leagues_match_entities` -> `validate_core_entities_identity` -> `reconcile_referees_identity` -> `reconcile_venues_identity`
- Trigger GAS semanal de jugadores (`runWeeklyPlayersSync`) ejecuta:
  - `POST /api/v1/jobs/orchestrate/weekly-players`
- Flujo semanal de jugadores backend:
  - `sync_all_leagues_players` (sin ingesta de venues/referees en esta fase) ->
  - `validate_players_identity_all_leagues` -> `validate_core_entities_identity` -> `validate_sync_coverage_all_leagues` ->
  - `reconcile_teams_identity` -> `reconcile_referees_identity` -> `reconcile_venues_identity`
  - El orquestador aplica presupuesto de tiempo (< 5 min) y limites de merge para evitar timeout de GAS.

Diagnostico canonical (duplicados/ambiguedades):
- `supabase/diagnostics/diag_canonical_duplicates_and_resolution.sql`

Recuperacion manual rapida (si falla una corrida semanal):

```bash
curl -X POST "$API_URL/api/v1/jobs/sync_all_leagues_teams/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY"

curl -X POST "$API_URL/api/v1/jobs/sync_all_leagues_players/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY"

curl -X POST "$API_URL/api/v1/jobs/validate_sync_coverage_all_leagues/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"manual_recovery","min_players_per_team":11}'

Normalizacion/merge de jugadores duplicados:
- Job: `reconcile_players_identity`
- Objetivo: unificar jugadores cargados desde multiples fuentes y mantener consistencia referencial en:
  - `player_aliases`, `entity_external_refs`, `team_memberships`, `competition_rosters`,
  - `match_lineups`, `player_match_stats`, `match_events`, `entity_resolution_queue`.
- Recomendacion operativa:
  - 1) ejecutar `dry_run=true`,
  - 2) revisar `merge_plan`,
  - 3) ejecutar merge real (`dry_run=false`) en ventana controlada.

Normalizacion/merge de entidades core duplicadas:
- Jobs:
  - `reconcile_teams_identity`
  - `reconcile_referees_identity`
  - `reconcile_venues_identity`
- Operacion recomendada:
  - ejecutar primero `dry_run=true` y revisar `merge_plan`,
  - luego ejecutar merge real (`dry_run=false`) por lotes acotados.
```

## Reglas Cuantitativas Implementadas En MVP

- `model_predictions` requiere `feature_snapshot_id`.
- `model_runs` guarda `git_sha`, `feature_set_version`, `dataset_version` y `config_hash` dentro de `params`.
- EV usa solo `calibrated_probability`.
- Odds capturadas después del kickoff quedan excluidas de EV pre-match.
- Competencia no `BETTABLE` bloquea con `BLOCKED_COMPETITION_NOT_BETTABLE`.

