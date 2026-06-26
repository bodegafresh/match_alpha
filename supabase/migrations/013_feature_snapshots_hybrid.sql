-- Phase 1: Add real columns to feature_snapshots for critical features (indexable, type-safe).
-- Secondary/future features continue in the existing features JSONB column.
--
-- team_side: distinguishes HOME vs AWAY perspective (elo_diff, is_home, etc. differ per side).
-- NO DEFAULT on team_side — missing side must fail, not silently assume HOME.
-- Workflow: add column nullable → application backfills → set NOT NULL.

alter table feature_snapshots
  add column if not exists team_side          text
    check (team_side in ('HOME', 'AWAY')),
  add column if not exists elo_global         numeric,
  add column if not exists elo_international  numeric,
  add column if not exists elo_domestic       numeric,
  add column if not exists elo_diff           numeric,
  add column if not exists attack_strength    numeric,
  add column if not exists defense_strength   numeric,
  add column if not exists form_points        numeric,
  add column if not exists form_gd            numeric,
  add column if not exists rest_days          integer,
  add column if not exists is_home            boolean,
  add column if not exists is_neutral         boolean,
  add column if not exists stage_pressure     numeric,
  add column if not exists feature_completeness numeric;

-- Drop old unique index (did not include team_side) and replace with correct one.
drop index if exists uq_feature_snapshots_match_team_version;

create unique index if not exists uq_feature_snapshots_key
  on feature_snapshots (match_id, team_id, team_side, feature_set_version, as_of)
  where team_side is not null;

-- Supporting index for feature lookup in prediction pipeline
create index if not exists idx_feature_snapshots_match_side
  on feature_snapshots (match_id, team_side);
