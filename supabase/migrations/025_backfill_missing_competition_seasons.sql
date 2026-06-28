-- 025_backfill_missing_competition_seasons.sql
-- Purpose:
--   Ensure competition_seasons exist for competitions already present in DB.
--   This migration is idempotent and safe to re-run.

with desired_seasons as (
  select * from (
    values
      (
        'fifa-world-cup',
        'wc2026',
        '2026',
        '2026-06-11T00:00:00Z'::timestamptz,
        '2026-07-19T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'ACTIVE'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"wc2026"}'::jsonb
      ),
      (
        'uefa-champions-league',
        'ucl-2026-2027',
        '2026/2027',
        null::timestamptz,
        null::timestamptz,
        'UTC',
        'LEAGUE_PHASE_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"ucl-2026-2027"}'::jsonb
      ),
      (
        'premier-league',
        'premier-league-2026-2027',
        '2026/2027',
        null::timestamptz,
        null::timestamptz,
        'Europe/London',
        'SINGLE_TABLE_LEAGUE',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"premier-league-2026-2027"}'::jsonb
      ),
      (
        'chile-primera',
        'chile-primera-2026',
        '2026',
        null::timestamptz,
        null::timestamptz,
        'America/Santiago',
        'SINGLE_TABLE_LEAGUE',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"chile-primera-2026"}'::jsonb
      ),
      (
        'copa-libertadores',
        'libertadores-2026',
        '2026',
        null::timestamptz,
        null::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"libertadores-2026"}'::jsonb
      ),
      (
        'conmebol-world-cup-qualifiers',
        'conmebol-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-20T23:59:59Z'::timestamptz,
        'UTC',
        'SINGLE_TABLE_LEAGUE',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"conmebol-qualifiers-wc2030"}'::jsonb
      ),
      (
        'uefa-world-cup-qualifiers',
        'uefa-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"uefa-qualifiers-wc2030"}'::jsonb
      ),
      (
        'concacaf-world-cup-qualifiers',
        'concacaf-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"concacaf-qualifiers-wc2030"}'::jsonb
      ),
      (
        'caf-world-cup-qualifiers',
        'caf-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"caf-qualifiers-wc2030"}'::jsonb
      ),
      (
        'afc-world-cup-qualifiers',
        'afc-qualifiers-wc2030',
        '2026-2029',
        '2027-10-07T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"afc-qualifiers-wc2030"}'::jsonb
      ),
      (
        'ofc-world-cup-qualifiers',
        'ofc-qualifiers-wc2030',
        '2026-2029',
        '2027-06-01T00:00:00Z'::timestamptz,
        '2029-09-30T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        'SCHEDULED'::season_status,
        '{"seed_source":"migration_025","catalog_slug":"ofc-qualifiers-wc2030"}'::jsonb
      )
  ) as v(
    competition_slug,
    season_slug,
    season_label,
    starts_at,
    ends_at,
    timezone_name,
    format_code,
    status,
    metadata
  )
),
upsert_seasons as (
  insert into competition_seasons (
    competition_id,
    slug,
    season_label,
    starts_at,
    ends_at,
    timezone_name,
    status,
    format_code,
    metadata
  )
  select
    c.competition_id,
    ds.season_slug,
    ds.season_label,
    ds.starts_at,
    ds.ends_at,
    ds.timezone_name,
    ds.status,
    ds.format_code,
    ds.metadata
  from desired_seasons ds
  join competitions c on c.slug = ds.competition_slug
  on conflict (slug) do update set
    competition_id = excluded.competition_id,
    season_label = excluded.season_label,
    starts_at = excluded.starts_at,
    ends_at = excluded.ends_at,
    timezone_name = excluded.timezone_name,
    status = excluded.status,
    format_code = excluded.format_code,
    metadata = competition_seasons.metadata || excluded.metadata,
    updated_at = now()
  returning competition_season_id
)
insert into competition_status (competition_season_id, status, status_reason, readiness_score)
select competition_season_id, 'OBSERVATION', 'Season backfilled by migration_025.', 0
from upsert_seasons
on conflict (competition_season_id) do nothing;
