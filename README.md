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
```

Los jobs HTTP usan:

```bash
curl -X POST "$API_URL/api/v1/jobs/ev_decision/run" \
  -H "Authorization: Bearer $API_INTERNAL_KEY"
```

## Reglas Cuantitativas Implementadas En MVP

- `model_predictions` requiere `feature_snapshot_id`.
- `model_runs` guarda `git_sha`, `feature_set_version`, `dataset_version` y `config_hash` dentro de `params`.
- EV usa solo `calibrated_probability`.
- Odds capturadas después del kickoff quedan excluidas de EV pre-match.
- Competencia no `BETTABLE` bloquea con `BLOCKED_COMPETITION_NOT_BETTABLE`.

