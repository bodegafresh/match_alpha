-- Phase 1: Competition context layer.
-- Eliminates hardcoded competition logic from models and features.
-- Describes the competitive context of a stage: format rules, knockout, neutral venue, etc.

create table if not exists competition_context_snapshots (
  context_snapshot_id   uuid        primary key default gen_random_uuid(),
  competition_season_id uuid        not null references competition_seasons(competition_season_id),
  stage_id              uuid        references competition_stages(stage_id),
  competition_type      text        not null,   -- 'INTERNATIONAL' | 'DOMESTIC' | 'CLUB_INTERNATIONAL'
  stage_type            text        not null,   -- 'GROUP_STAGE' | 'ROUND_OF_16' | 'FINAL' | ...
  is_knockout           boolean     not null default false,
  neutral_venue_policy  text,                   -- 'ALWAYS' | 'SOMETIMES' | 'NEVER'
  home_advantage_scale  numeric     default 1.0,
  is_aggregate_series   boolean     default false,
  is_second_leg         boolean     default false,
  away_goals_rule       boolean     default false,
  points_per_win        integer     default 3,
  created_at            timestamptz not null default now()
);

create index if not exists idx_competition_context_season_stage
  on competition_context_snapshots (competition_season_id, stage_id);
