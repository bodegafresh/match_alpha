-- RESET TOTAL - Pool Team 2026 legacy Supabase objects
-- WARNING: destructive reset. Run only on the new Supabase project/base you want to wipe.
-- Covers objects created by migrations 001..007 in this repository.

begin;

-- Views / materialized views.
-- PostgreSQL errors if DROP VIEW is used against a materialized view, or vice versa,
-- so detect the relation kind before dropping.
do $$
declare
  rel record;
begin
  for rel in
    select c.relkind, format('%I.%I', n.nspname, c.relname) as qualified_name
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relname in (
        'published_today_matches',
        'published_match_schedule',
        'published_match_predictions',
        'published_ev_opportunities',
        'published_market_overpriced',
        'published_model_calibration',
        'published_bankroll_summary',
        'published_competition_health',
        'published_competition_participants',
        'published_team_identity',
        'competition_integrity_issues',
        'vw_sheet_partidos',
        'vw_sheet_odds_apuestas',
        'vw_sheet_poisson_odds',
        'vw_sheet_ev_opportunities',
        'vw_current_elo_ratings',
        'active_ev_plus',
        'model_calibration_daily'
      )
  loop
    if rel.relkind = 'v' then
      execute 'drop view if exists ' || rel.qualified_name || ' cascade';
    elsif rel.relkind = 'm' then
      execute 'drop materialized view if exists ' || rel.qualified_name || ' cascade';
    end if;
  end loop;
end $$;

-- RPC/functions
drop function if exists app_supabase_healthcheck(text, jsonb) cascade;
drop function if exists app_transaction_batch(jsonb) cascade;
drop function if exists app_transaction_insert_rows(text, jsonb) cascade;
drop function if exists app_transaction_upsert_rows(text, jsonb, text[]) cascade;
drop function if exists app_transaction_delete_rows(text, jsonb) cascade;

-- Tables, newest/highest dependency first
drop table if exists match_team_slots cascade;
drop table if exists tournament_slots cascade;
drop table if exists qualification_rules cascade;
drop table if exists competition_group_memberships cascade;
drop table if exists competition_groups cascade;
drop table if exists competition_participants cascade;
drop table if exists competition_stages cascade;
drop table if exists entity_media_assets cascade;
drop table if exists entity_resolution_queue cascade;
drop table if exists entity_external_refs cascade;
drop table if exists supabase_heartbeats cascade;

drop table if exists match_officials cascade;
drop table if exists referees cascade;
drop table if exists match_events cascade;
drop table if exists match_lineups cascade;
drop table if exists competition_rosters cascade;
drop table if exists team_memberships cascade;
drop table if exists source_player_mapping cascade;
drop table if exists player_aliases cascade;
drop table if exists venues cascade;

drop table if exists experiment_tracking cascade;
drop table if exists bankroll_snapshots cascade;
drop table if exists drift_reports cascade;
drop table if exists model_registry cascade;
drop table if exists cross_league_calibration cascade;
drop table if exists competition_market_profiles cascade;
drop table if exists league_strength_coefficients cascade;
drop table if exists rating_snapshots cascade;
drop table if exists feature_snapshots cascade;
drop table if exists feature_definitions cascade;
drop table if exists data_quality_events cascade;
drop table if exists model_metrics cascade;
drop table if exists market_closing_odds cascade;
drop table if exists betting_decisions cascade;
drop table if exists calibration_bins cascade;
drop table if exists calibration_runs cascade;
drop table if exists model_predictions cascade;
drop table if exists model_runs cascade;

drop table if exists domain_events cascade;
drop table if exists source_team_mapping cascade;
drop table if exists competition_team_mapping cascade;
drop table if exists team_aliases cascade;
drop table if exists competition_readiness_checks cascade;
drop table if exists competition_status cascade;
drop table if exists competition_seasons cascade;
drop table if exists competitions cascade;

drop table if exists news_items cascade;
drop table if exists weather_snapshots cascade;
drop table if exists data_quality_log cascade;
drop table if exists pipeline_runs cascade;
drop table if exists elo_ratings cascade;
drop table if exists group_simulations cascade;
drop table if exists model_calibration cascade;
drop table if exists bets cascade;
drop table if exists ev_picks cascade;
drop table if exists model_outputs cascade;
drop table if exists odds_snapshots cascade;
drop table if exists player_match_summary cascade;
drop table if exists player_match_stats cascade;
drop table if exists standings cascade;
drop table if exists source_fixtures cascade;
drop table if exists match_source_ids cascade;
drop table if exists matches cascade;
drop table if exists players cascade;
drop table if exists teams cascade;
drop table if exists countries cascade;

-- Types from the clean schema or possible previous attempts.
drop type if exists entity_type cascade;
drop type if exists team_type cascade;
drop type if exists gender_type cascade;
drop type if exists competition_type cascade;
drop type if exists season_status cascade;
drop type if exists competition_status_type cascade;
drop type if exists readiness_check_status cascade;
drop type if exists stage_type cascade;
drop type if exists group_membership_status cascade;
drop type if exists participant_side cascade;
drop type if exists match_status cascade;
drop type if exists match_participant_role cascade;
drop type if exists lineup_role cascade;
drop type if exists official_role cascade;
drop type if exists market_type cascade;
drop type if exists betting_decision_status cascade;
drop type if exists bet_mode cascade;
drop type if exists bet_status cascade;
drop type if exists betting_decision_settlement_status cascade;
drop type if exists risk_level cascade;
drop type if exists model_run_status cascade;
drop type if exists calibration_method cascade;
drop type if exists severity_level cascade;
drop type if exists raw_payload_status cascade;
drop type if exists entity_resolution_status cascade;
drop type if exists transaction_action cascade;

commit;
