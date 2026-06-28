-- 024_seed_competitions_and_seasons_catalog_expansion.sql
-- Purpose:
--   - Keep competition and season catalog data migration-driven.
--   - Upsert new WC2030 qualifier competitions/seasons.
--   - Update season metadata for existing entries where API_FOOTBALL external_ids were added.

with catalog as (
  select * from (
    values
      (
        'uefa-champions-league',
        'UEFA Champions League',
        'CUP',
        null::char(2),
        'Europe',
        1,
        true,
        '{"domain_type":"CONTINENTAL_CLUB","confederation":"UEFA","catalog_slug":"ucl-2026-2027","supported_sources":["SPORTMONKS","FOOTBALL_DATA","API_FOOTBALL","ESPN"]}'::jsonb,
        'ucl-2026-2027',
        '2026/2027',
        null::timestamptz,
        null::timestamptz,
        'UTC',
        'LEAGUE_PHASE_THEN_KNOCKOUT',
        '{"ui":{"navigation":["matches","league_phase","teams","bracket"],"default_view":"matches"},"format":{"type":"LEAGUE_PHASE_THEN_KNOCKOUT","has_groups":false,"has_league_table":true,"has_knockout":true,"has_playoffs":true,"has_two_leg_ties":true},"sources":{"primary":"SPORTMONKS","secondary":["FOOTBALL_DATA","API_FOOTBALL","ESPN"],"priority":["SPORTMONKS","FOOTBALL_DATA","API_FOOTBALL","ESPN"],"external_ids":{"FOOTBALL_DATA":"CL","API_FOOTBALL":"2"},"capabilities":{"SPORTMONKS":["fixtures","standings","teams","venues","players","lineups","events","stats"],"FOOTBALL_DATA":["fixtures","standings","teams"],"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'chile-primera',
        'Chile Primera Division',
        'LEAGUE',
        'CL'::char(2),
        'South America',
        1,
        false,
        '{"domain_type":"DOMESTIC_LEAGUE","confederation":"CONMEBOL","catalog_slug":"chile-primera-2026","supported_sources":["API_FOOTBALL","SPORTMONKS","ESPN"]}'::jsonb,
        'chile-primera-2026',
        '2026',
        null::timestamptz,
        null::timestamptz,
        'America/Santiago',
        'SINGLE_TABLE_LEAGUE',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"matches"},"format":{"type":"SINGLE_TABLE_LEAGUE","has_groups":false,"has_league_table":true,"has_knockout":false,"has_playoffs":false},"sources":{"primary":"API_FOOTBALL","secondary":["SPORTMONKS","ESPN"],"priority":["API_FOOTBALL","SPORTMONKS","ESPN"],"external_ids":{"API_FOOTBALL":"265"},"capabilities":{"API_FOOTBALL":["fixtures","standings","teams","venues","players","events","stats","odds"],"SPORTMONKS":["fixtures","standings","teams","venues","players","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'conmebol-world-cup-qualifiers',
        'Eliminatorias Sudamericanas 2030',
        'TOURNAMENT',
        null::char(2),
        'South America',
        1,
        true,
        '{"domain_type":"INTERNATIONAL_CUP","confederation":"CONMEBOL","catalog_slug":"conmebol-qualifiers-wc2030","supported_sources":["API_FOOTBALL","SPORTMONKS","ESPN"]}'::jsonb,
        'conmebol-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-20T23:59:59Z'::timestamptz,
        'UTC',
        'SINGLE_TABLE_LEAGUE',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"matches"},"format":{"type":"SINGLE_TABLE_LEAGUE","has_groups":false,"has_league_table":true,"has_knockout":false,"has_playoffs":false},"sources":{"primary":"API_FOOTBALL","secondary":["SPORTMONKS","ESPN"],"priority":["API_FOOTBALL","SPORTMONKS","ESPN"],"external_ids":{"API_FOOTBALL":"31"},"capabilities":{"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events","stats"],"SPORTMONKS":["fixtures","standings","teams","venues","players","lineups","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'uefa-world-cup-qualifiers',
        'Clasificacion Europea 2030',
        'TOURNAMENT',
        null::char(2),
        'Europe',
        1,
        true,
        '{"domain_type":"INTERNATIONAL_CUP","confederation":"UEFA","catalog_slug":"uefa-qualifiers-wc2030","supported_sources":["FOOTBALL_DATA","API_FOOTBALL","SPORTMONKS","ESPN"]}'::jsonb,
        'uefa-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"standings"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_playoffs":false,"has_best_third_places":true},"sources":{"primary":"FOOTBALL_DATA","secondary":["API_FOOTBALL","SPORTMONKS","ESPN"],"priority":["FOOTBALL_DATA","API_FOOTBALL","SPORTMONKS","ESPN"],"external_ids":{"FOOTBALL_DATA":"EC"},"capabilities":{"FOOTBALL_DATA":["fixtures","standings","teams","results"],"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events","stats"],"SPORTMONKS":["fixtures","standings","teams","venues","players","lineups","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'concacaf-world-cup-qualifiers',
        'Clasificacion CONCACAF 2030',
        'TOURNAMENT',
        null::char(2),
        'North & Central America',
        1,
        true,
        '{"domain_type":"INTERNATIONAL_CUP","confederation":"CONCACAF","catalog_slug":"concacaf-qualifiers-wc2030","supported_sources":["API_FOOTBALL","ESPN","SPORTMONKS"]}'::jsonb,
        'concacaf-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"standings"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_playoffs":false,"has_best_third_places":true},"sources":{"primary":"API_FOOTBALL","secondary":["ESPN","SPORTMONKS"],"priority":["API_FOOTBALL","ESPN","SPORTMONKS"],"external_ids":{"API_FOOTBALL":"32"},"capabilities":{"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events","stats"],"SPORTMONKS":["fixtures","standings","teams","venues","players","lineups","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'caf-world-cup-qualifiers',
        'Clasificacion Africana 2030',
        'TOURNAMENT',
        null::char(2),
        'Africa',
        1,
        true,
        '{"domain_type":"INTERNATIONAL_CUP","confederation":"CAF","catalog_slug":"caf-qualifiers-wc2030","supported_sources":["API_FOOTBALL","SPORTMONKS","ESPN"]}'::jsonb,
        'caf-qualifiers-wc2030',
        '2026-2029',
        '2026-09-04T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"standings"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_playoffs":false,"has_best_third_places":true},"sources":{"primary":"API_FOOTBALL","secondary":["SPORTMONKS","ESPN"],"priority":["API_FOOTBALL","SPORTMONKS","ESPN"],"external_ids":{"API_FOOTBALL":"33"},"capabilities":{"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events","stats"],"SPORTMONKS":["fixtures","standings","teams","venues","players","lineups","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'afc-world-cup-qualifiers',
        'Clasificacion Asiatica 2030',
        'TOURNAMENT',
        null::char(2),
        'Asia',
        1,
        true,
        '{"domain_type":"INTERNATIONAL_CUP","confederation":"AFC","catalog_slug":"afc-qualifiers-wc2030","supported_sources":["API_FOOTBALL","SPORTMONKS","ESPN"]}'::jsonb,
        'afc-qualifiers-wc2030',
        '2026-2029',
        '2027-10-07T00:00:00Z'::timestamptz,
        '2029-11-18T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"standings"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_playoffs":false,"has_best_third_places":true},"sources":{"primary":"API_FOOTBALL","secondary":["SPORTMONKS","ESPN"],"priority":["API_FOOTBALL","SPORTMONKS","ESPN"],"external_ids":{"API_FOOTBALL":"34"},"capabilities":{"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events","stats"],"SPORTMONKS":["fixtures","standings","teams","venues","players","lineups","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      ),
      (
        'ofc-world-cup-qualifiers',
        'Clasificacion OFC 2030',
        'TOURNAMENT',
        null::char(2),
        'Oceania',
        1,
        true,
        '{"domain_type":"INTERNATIONAL_CUP","confederation":"OFC","catalog_slug":"ofc-qualifiers-wc2030","supported_sources":["API_FOOTBALL","ESPN"]}'::jsonb,
        'ofc-qualifiers-wc2030',
        '2026-2029',
        '2027-06-01T00:00:00Z'::timestamptz,
        '2029-09-30T23:59:59Z'::timestamptz,
        'UTC',
        'GROUPS_THEN_KNOCKOUT',
        '{"ui":{"navigation":["matches","standings","teams"],"default_view":"standings"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_playoffs":false,"has_best_third_places":true},"sources":{"primary":"API_FOOTBALL","secondary":["ESPN"],"priority":["API_FOOTBALL","ESPN"],"external_ids":{"API_FOOTBALL":"35"},"capabilities":{"API_FOOTBALL":["fixtures","standings","teams","venues","players","lineups","events","stats"],"ESPN":["fixtures","scores"]},"conflict_resolution":{"identity":"canonical_internal_id_wins","fixtures":"primary_source_wins_unless_manual_override","results":"official_result_source_wins","stats":"source_specific_stats_do_not_merge_without_mapping","odds":"append_only_snapshots_no_overwrite"}}}'::jsonb
      )
  ) as v (
    competition_slug,
    display_name,
    competition_type,
    country_code,
    region,
    tier,
    is_international,
    competition_metadata,
    season_slug,
    season_label,
    starts_at,
    ends_at,
    timezone_name,
    format_code,
    season_metadata
  )
),
upsert_competitions as (
  insert into competitions (
    slug,
    display_name,
    competition_type,
    country_code,
    region,
    tier,
    is_international,
    metadata
  )
  select
    competition_slug,
    display_name,
    competition_type::competition_type,
    country_code,
    region,
    tier,
    is_international,
    competition_metadata
  from catalog
  on conflict (slug) do update set
    display_name = excluded.display_name,
    competition_type = excluded.competition_type,
    country_code = excluded.country_code,
    region = excluded.region,
    tier = excluded.tier,
    is_international = excluded.is_international,
    metadata = competitions.metadata || excluded.metadata,
    updated_at = now()
  returning competition_id, slug
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
    catalog.season_slug,
    catalog.season_label,
    catalog.starts_at,
    catalog.ends_at,
    catalog.timezone_name,
    case when catalog.season_slug = 'wc2026' then 'ACTIVE'::season_status else 'SCHEDULED'::season_status end,
    catalog.format_code,
    catalog.season_metadata
  from catalog
  join competitions c on c.slug = catalog.competition_slug
  on conflict (slug) do update set
    competition_id = excluded.competition_id,
    season_label = excluded.season_label,
    starts_at = excluded.starts_at,
    ends_at = excluded.ends_at,
    timezone_name = excluded.timezone_name,
    format_code = excluded.format_code,
    metadata = competition_seasons.metadata || excluded.metadata,
    updated_at = now()
  returning competition_season_id, slug
)
insert into competition_status (competition_season_id, status, status_reason, readiness_score)
select competition_season_id, 'OBSERVATION', 'Catalog seeded via migration-driven expansion.', 0
from upsert_seasons
on conflict (competition_season_id) do nothing;
