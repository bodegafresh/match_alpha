-- TRUNCATE ALL DATA - clean data reset for the new project schema.
-- Destructive by design: removes all rows but preserves tables, views, types,
-- functions, indexes, constraints, and RPCs from 001_clean_schema.sql.
--
-- Use this when a bootstrap/import polluted canonical data and you want to
-- restart migration from an empty database without recreating the schema.
-- Reference catalogs such as countries are intentionally preserved.

begin;

truncate table
  -- Analytics / betting
  backtest_runs,
  bankroll_snapshots,
  bets,
  betting_decisions,
  drift_reports,
  model_metrics,
  calibration_bins,
  calibration_runs,
  model_predictions,
  model_runs,
  model_registry,
  rating_snapshots,
  feature_snapshots,
  feature_definitions,
  market_quality_snapshots,
  market_closing_odds,
  odds_snapshots,
  market_selections,
  markets,
  bookmaker_profiles,

  -- Observability
  supabase_heartbeats,
  data_quality_events,
  pipeline_runs,

  -- Canonical match/player/team dependencies
  player_match_stats,
  match_officials,
  match_events,
  match_lineups,
  match_participants,
  knockout_bracket_edges,
  tournament_slots,
  standings,
  matches,
  referees,
  venues,
  competition_rosters,
  team_memberships,
  player_aliases,
  players,
  entity_resolution_queue,
  entity_media_assets,
  entity_external_refs,
  competition_group_memberships,
  competition_team_entries,
  team_aliases,
  teams,
  competition_groups,
  competition_stages,
  competition_readiness_checks,
  competition_status,
  competition_seasons,
  competitions,

  -- Staging
  stg_events,
  stg_lineups,
  stg_rosters,
  stg_odds,
  stg_matches,
  stg_players,
  stg_teams,

  -- Raw
  raw_source_payloads,
  raw_api_calls,
  raw_source_files
restart identity cascade;

commit;
