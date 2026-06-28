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
```

## Operacion Semanal (Canonical)

- Trigger GAS semanal existente (`runWeeklyTeamsSync`) ejecuta una sola llamada al servicio:
  - `POST /api/v1/jobs/orchestrate/weekly`
- Flujo semanal backend:
  - `sync_all_leagues_teams` -> `sync_all_leagues_players` -> `validate_sync_coverage_all_leagues`

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
```

## Reglas Cuantitativas Implementadas En MVP

- `model_predictions` requiere `feature_snapshot_id`.
- `model_runs` guarda `git_sha`, `feature_set_version`, `dataset_version` y `config_hash` dentro de `params`.
- EV usa solo `calibrated_probability`.
- Odds capturadas después del kickoff quedan excluidas de EV pre-match.
- Competencia no `BETTABLE` bloquea con `BLOCKED_COMPETITION_NOT_BETTABLE`.

