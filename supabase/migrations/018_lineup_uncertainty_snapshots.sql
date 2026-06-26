-- Phase 1 (structure only): Lineup uncertainty tracking.
-- Initially empty — no lineup ingestion yet. Schema created to:
--   1. Reserve the structure for Phase 2 integration.
--   2. Allow feature_completeness to reference lineup_available.
--   3. Enable confidence_score to factor lineup certainty when data arrives.

create table if not exists lineup_uncertainty_snapshots (
  snapshot_id            uuid        primary key default gen_random_uuid(),
  match_id               uuid        not null references matches(match_id),
  team_id                uuid        not null references teams(team_id),
  lineup_status          text        default 'UNKNOWN'
    check (lineup_status in ('CONFIRMED', 'PROBABLE', 'UNKNOWN')),
  lineup_confirmed_at    timestamptz,
  lineup_confidence      numeric
    check (lineup_confidence >= 0 and lineup_confidence <= 1),
  missing_key_players    integer     default 0,
  missing_players_impact numeric      default 0.0,
  source                 text,
  as_of                  timestamptz not null default now(),
  payload                jsonb,
  created_at             timestamptz not null default now()
);

create index if not exists idx_lineup_uncertainty_match_team
  on lineup_uncertainty_snapshots (match_id, team_id, as_of desc);
