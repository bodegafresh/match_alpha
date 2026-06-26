-- Canonical competition catalog seeds.
-- Run after 001_clean_schema.sql and the countries seed.
-- This defines format/rules before any fixture/result ingestion.

begin;

insert into countries (
  code_alpha2, code_alpha3, numeric_code, default_name, names, continent, is_fifa_member, is_sovereign
) values
  ('GB', 'GBR', '826', 'United Kingdom', '{"en":"United Kingdom","es":"Reino Unido"}'::jsonb, 'EU', true, true),
  ('CL', 'CHL', '152', 'Chile', '{"en":"Chile","es":"Chile"}'::jsonb, 'SA', true, true)
on conflict (code_alpha2) do nothing;

with catalog as (
  select *
  from (values
    (
      'fifa-world-cup',
      'FIFA World Cup',
      'TOURNAMENT',
      null,
      'Global',
      1,
      true,
      '{"domain_type":"INTERNATIONAL_CUP","confederation":"FIFA","catalog_slug":"wc2026","supported_sources":["ESPN","SPORTMONKS","API_FOOTBALL","FOOTBALL_DATA"]}'::jsonb,
      'wc2026',
      '2026',
      '2026-06-11T00:00:00Z'::timestamptz,
      '2026-07-19T23:59:59Z'::timestamptz,
      'UTC',
      'GROUPS_THEN_KNOCKOUT',
      '{"ui":{"navigation":["matches","standings","teams","bracket"],"default_view":"matches"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_best_third_places":true},"sources":{"primary":"ESPN","secondary":["SPORTMONKS","API_FOOTBALL","FOOTBALL_DATA"],"external_ids":{"ESPN":"fifa.world","FOOTBALL_DATA":"WC"}}}'::jsonb
    ),
    (
      'uefa-champions-league',
      'UEFA Champions League',
      'CUP',
      null,
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
      '{"ui":{"navigation":["matches","league_phase","teams","bracket"],"default_view":"matches"},"format":{"type":"LEAGUE_PHASE_THEN_KNOCKOUT","has_groups":false,"has_league_table":true,"has_knockout":true,"has_playoffs":true,"has_two_leg_ties":true},"sources":{"primary":"SPORTMONKS","secondary":["FOOTBALL_DATA","API_FOOTBALL","ESPN"],"external_ids":{"FOOTBALL_DATA":"CL"}}}'::jsonb
    ),
    (
      'premier-league',
      'Premier League',
      'LEAGUE',
      'GB',
      'Europe',
      1,
      false,
      '{"domain_type":"DOMESTIC_LEAGUE","confederation":"UEFA","catalog_slug":"premier-league-2026-2027","supported_sources":["FOOTBALL_DATA","SPORTMONKS","API_FOOTBALL","ESPN"]}'::jsonb,
      'premier-league-2026-2027',
      '2026/2027',
      null::timestamptz,
      null::timestamptz,
      'Europe/London',
      'SINGLE_TABLE_LEAGUE',
      '{"ui":{"navigation":["matches","standings","teams"],"default_view":"matches"},"format":{"type":"SINGLE_TABLE_LEAGUE","has_groups":false,"has_league_table":true,"has_knockout":false,"has_playoffs":false},"sources":{"primary":"FOOTBALL_DATA","secondary":["SPORTMONKS","API_FOOTBALL","ESPN"],"external_ids":{"FOOTBALL_DATA":"PL"}}}'::jsonb
    ),
    (
      'chile-primera',
      'Chile Primera División',
      'LEAGUE',
      'CL',
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
      '{"ui":{"navigation":["matches","standings","teams"],"default_view":"matches"},"format":{"type":"SINGLE_TABLE_LEAGUE","has_groups":false,"has_league_table":true,"has_knockout":false,"has_playoffs":false},"sources":{"primary":"API_FOOTBALL","secondary":["SPORTMONKS","ESPN"],"external_ids":{}}}'::jsonb
    ),
    (
      'copa-libertadores',
      'Copa Libertadores',
      'CUP',
      null,
      'South America',
      1,
      true,
      '{"domain_type":"CONTINENTAL_CLUB","confederation":"CONMEBOL","catalog_slug":"libertadores-2026","supported_sources":["API_FOOTBALL","SPORTMONKS","ESPN"]}'::jsonb,
      'libertadores-2026',
      '2026',
      null::timestamptz,
      null::timestamptz,
      'UTC',
      'GROUPS_THEN_KNOCKOUT',
      '{"ui":{"navigation":["matches","standings","teams","bracket"],"default_view":"matches"},"format":{"type":"GROUPS_THEN_KNOCKOUT","has_groups":true,"has_league_table":false,"has_knockout":true,"has_playoffs":false},"sources":{"primary":"API_FOOTBALL","secondary":["SPORTMONKS","ESPN"],"external_ids":{}}}'::jsonb
    )
  ) as v (
    competition_slug, display_name, competition_type, country_code, region, tier, is_international,
    competition_metadata, season_slug, season_label, starts_at, ends_at, timezone_name, format_code, season_metadata
  )
),
upsert_competitions as (
  insert into competitions (
    slug, display_name, competition_type, country_code, region, tier, is_international, metadata
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
    competition_id, slug, season_label, starts_at, ends_at, timezone_name, status, format_code, metadata
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
select competition_season_id, 'OBSERVATION', 'Catalog seeded; readiness checks pending.', 0
from upsert_seasons
on conflict (competition_season_id) do nothing;

with stage_seed as (
  select cs.competition_season_id, s.*
  from competition_seasons cs
  cross join lateral (
    values
      ('wc2026','GROUP_STAGE','Fase de grupos',1,'GROUP_STAGE','{"view_type":"GROUP_TABLES","expected_matches":72,"teams_per_group":4,"group_count":12,"qualifies":{"top_n_per_group":2,"best_third_places":8},"tie_breakers":["points","goal_difference","goals_for","head_to_head","fair_play","draw"]}'::jsonb),
      ('wc2026','ROUND_OF_32','Dieciseisavos de final',2,'KNOCKOUT','{"view_type":"BRACKET_ROUND","expected_matches":16,"legs":1,"single_leg":true,"aggregate_score":false,"extra_time":true,"penalties":true}'::jsonb),
      ('wc2026','ROUND_OF_16','Octavos de final',3,'KNOCKOUT','{"view_type":"BRACKET_ROUND","expected_matches":8,"legs":1,"single_leg":true,"aggregate_score":false,"extra_time":true,"penalties":true}'::jsonb),
      ('wc2026','QUARTER_FINAL','Cuartos de final',4,'KNOCKOUT','{"view_type":"BRACKET_ROUND","expected_matches":4,"legs":1,"single_leg":true,"aggregate_score":false,"extra_time":true,"penalties":true}'::jsonb),
      ('wc2026','SEMI_FINAL','Semifinal',5,'KNOCKOUT','{"view_type":"BRACKET_ROUND","expected_matches":2,"legs":1,"single_leg":true,"aggregate_score":false,"extra_time":true,"penalties":true}'::jsonb),
      ('wc2026','THIRD_PLACE','Tercer lugar',6,'THIRD_PLACE','{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true,"aggregate_score":false,"extra_time":true,"penalties":true}'::jsonb),
      ('wc2026','FINAL','Final',7,'FINAL','{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true,"aggregate_score":false,"extra_time":true,"penalties":true}'::jsonb),
      ('ucl-2026-2027','LEAGUE_PHASE','Fase liga',1,'LEAGUE_PHASE','{"view_type":"LEAGUE_PHASE_TABLE","format":"SWISS_OR_LEAGUE_PHASE","qualification":{"top_8":"ROUND_OF_16","positions_9_to_24":"KNOCKOUT_PLAYOFF","positions_25_plus":"ELIMINATED"}}'::jsonb),
      ('ucl-2026-2027','KNOCKOUT_PLAYOFF','Playoffs eliminatorios',2,'PLAYOFF','{"view_type":"TWO_LEG_TIE","expected_matches":16,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('ucl-2026-2027','ROUND_OF_16','Octavos de final',3,'KNOCKOUT','{"view_type":"TWO_LEG_TIE","expected_matches":8,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('ucl-2026-2027','QUARTER_FINAL','Cuartos de final',4,'KNOCKOUT','{"view_type":"TWO_LEG_TIE","expected_matches":4,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('ucl-2026-2027','SEMI_FINAL','Semifinal',5,'KNOCKOUT','{"view_type":"TWO_LEG_TIE","expected_matches":2,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('ucl-2026-2027','FINAL','Final',6,'FINAL','{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true}'::jsonb),
      ('premier-league-2026-2027','LEAGUE_REGULAR','Temporada regular',1,'LEAGUE_PHASE','{"view_type":"LEAGUE_TABLE","rounds":"DOUBLE_ROUND_ROBIN","tie_breakers":["points","goal_difference","goals_for","wins"]}'::jsonb),
      ('chile-primera-2026','LEAGUE_REGULAR','Temporada regular',1,'LEAGUE_PHASE','{"view_type":"LEAGUE_TABLE","rounds":"DOUBLE_ROUND_ROBIN","tie_breakers":["points","goal_difference","goals_for","wins"]}'::jsonb),
      ('libertadores-2026','GROUP_STAGE','Fase de grupos',1,'GROUP_STAGE','{"view_type":"GROUP_TABLES","teams_per_group":4,"qualifies":{"top_n_per_group":2},"tie_breakers":["points","goal_difference","goals_for","away_goals","fair_play","draw"]}'::jsonb),
      ('libertadores-2026','ROUND_OF_16','Octavos de final',2,'KNOCKOUT','{"view_type":"TWO_LEG_TIE","expected_matches":8,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('libertadores-2026','QUARTER_FINAL','Cuartos de final',3,'KNOCKOUT','{"view_type":"TWO_LEG_TIE","expected_matches":4,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('libertadores-2026','SEMI_FINAL','Semifinal',4,'KNOCKOUT','{"view_type":"TWO_LEG_TIE","expected_matches":2,"legs":2,"aggregate_score":true,"away_goals_rule":false}'::jsonb),
      ('libertadores-2026','FINAL','Final',5,'FINAL','{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true}'::jsonb)
  ) as s(season_slug, stage_code, stage_name, stage_order, stage_type, rules)
  where cs.slug = s.season_slug
)
insert into competition_stages (
  competition_season_id, stage_code, stage_name, stage_order, stage_type, rules
)
select competition_season_id, stage_code, stage_name, stage_order, stage_type::stage_type, rules
from stage_seed
on conflict (competition_season_id, stage_code) do update set
  stage_name = excluded.stage_name,
  stage_order = excluded.stage_order,
  stage_type = excluded.stage_type,
  rules = competition_stages.rules || excluded.rules,
  updated_at = now();

with wc_group_seed as (
  select cs.competition_season_id, st.stage_id, ('Grupo ' || chr(64 + g.n)) as group_code, g.n as group_order
  from generate_series(1, 12) as g(n)
  join competition_seasons cs on cs.slug = 'wc2026'
  join competition_stages st on st.competition_season_id = cs.competition_season_id and st.stage_code = 'GROUP_STAGE'
),
lib_group_seed as (
  select cs.competition_season_id, st.stage_id, ('Grupo ' || chr(64 + g.n)) as group_code, g.n as group_order
  from generate_series(1, 8) as g(n)
  join competition_seasons cs on cs.slug = 'libertadores-2026'
  join competition_stages st on st.competition_season_id = cs.competition_season_id and st.stage_code = 'GROUP_STAGE'
),
all_groups as (
  select * from wc_group_seed
  union all
  select * from lib_group_seed
)
insert into competition_groups (
  competition_season_id, stage_id, group_code, group_name, group_order, metadata
)
select competition_season_id, stage_id, group_code, group_code, group_order, '{"source":"competition_catalog"}'::jsonb
from all_groups
on conflict (competition_season_id, stage_id, group_code) do update set
  group_name = excluded.group_name,
  group_order = excluded.group_order,
  metadata = competition_groups.metadata || excluded.metadata,
  updated_at = now();

commit;
