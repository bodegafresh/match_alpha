-- Performance indexes for common query patterns in Match Alpha.
-- All use IF NOT EXISTS — safe to run multiple times.

-- matches: range scans by kickoff_at (used by every endpoint + orchestrator context)
create index if not exists idx_matches_kickoff_at
  on matches (kickoff_at asc);

-- matches: scoped range scan per competition season (used by BUG-04 fixed _build_context)
create index if not exists idx_matches_competition_season_kickoff
  on matches (competition_season_id, kickoff_at asc);

-- matches: status filter (used by has_finished_matches, has_live_matches)
create index if not exists idx_matches_status_kickoff
  on matches (status, kickoff_at desc)
  where status in ('FINISHED', 'LIVE', 'SCHEDULED');

-- match_participants: team lookups (used by BUG-02 match_schedule_for_team)
create index if not exists idx_match_participants_team_id
  on match_participants (team_id);

create index if not exists idx_match_participants_match_side
  on match_participants (match_id, side);

-- tournament_slots: join path from match_participants
create index if not exists idx_tournament_slots_stage_id
  on tournament_slots (stage_id);

-- competition_seasons: slug lookup (primary filter for all web endpoints)
create unique index if not exists idx_competition_seasons_slug
  on competition_seasons (slug);

-- entity_external_refs: source match lookups (used by _find_existing_match)
create index if not exists idx_entity_external_refs_source_entity
  on entity_external_refs (source, source_entity_id)
  where is_primary = true;

create index if not exists idx_entity_external_refs_entity_type_source
  on entity_external_refs (entity_type, source, source_entity_id);

-- odds_snapshots: partidos próximos con odds recientes
create index if not exists idx_odds_snapshots_match_captured
  on odds_snapshots (match_id, captured_at desc);

-- model_predictions: lookup por match
create index if not exists idx_model_predictions_match
  on model_predictions (match_id, as_of desc);

-- data_quality_events: monitoreo por severidad y fecha
create index if not exists idx_data_quality_events_severity_created
  on data_quality_events (severity, created_at desc);

-- pipeline_runs: cleanup job needs this for the date range delete
create index if not exists idx_pipeline_runs_job_name
  on pipeline_runs (job_name, started_at desc);
