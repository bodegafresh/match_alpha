begin;

with season as (
  select competition_season_id
  from competition_seasons
  where slug = 'wc2026'
)
update competition_seasons cs
set
  format_code = 'GROUPS_THEN_KNOCKOUT',
  metadata = coalesce(cs.metadata, '{}'::jsonb)
    || jsonb_build_object(
      'format', jsonb_build_object(
        'type', 'GROUPS_THEN_KNOCKOUT',
        'group_count', 12,
        'teams_per_group', 4,
        'group_stage_matches', 72,
        'knockout_teams', 32,
        'qualified_per_group', 2,
        'best_third_places', 8,
        'tie_breakers', jsonb_build_array('points', 'goal_difference', 'goals_for', 'head_to_head')
      ),
      'ui', jsonb_build_object(
        'default_view', 'matches',
        'navigation', jsonb_build_array(
          jsonb_build_object('key', 'matches', 'label', 'Partidos', 'enabled', true, 'order', 10),
          jsonb_build_object('key', 'standings', 'label', 'Posiciones', 'enabled', true, 'order', 20),
          jsonb_build_object('key', 'teams', 'label', 'Equipos', 'enabled', true, 'order', 30),
          jsonb_build_object('key', 'bracket', 'label', 'Eliminatorias', 'enabled', true, 'order', 40)
        )
      )
    )
from season
where cs.competition_season_id = season.competition_season_id;

with season as (
  select competition_season_id
  from competition_seasons
  where slug = 'wc2026'
),
rules(stage_code, patch) as (
  values
    ('GROUP_STAGE', jsonb_build_object(
      'view_type', 'GROUP_TABLES',
      'expected_matches', 72,
      'group_count', 12,
      'teams_per_group', 4,
      'qualifies', jsonb_build_object('top_n_per_group', 2, 'best_third_places', 8),
      'tie_breakers', jsonb_build_array('points', 'goal_difference', 'goals_for', 'head_to_head')
    )),
    ('ROUND_OF_32', jsonb_build_object('view_type', 'BRACKET_ROUND', 'expected_matches', 16, 'single_leg', true, 'extra_time', true, 'penalties', true)),
    ('ROUND_OF_16', jsonb_build_object('view_type', 'BRACKET_ROUND', 'expected_matches', 8, 'single_leg', true, 'extra_time', true, 'penalties', true)),
    ('QUARTER_FINAL', jsonb_build_object('view_type', 'BRACKET_ROUND', 'expected_matches', 4, 'single_leg', true, 'extra_time', true, 'penalties', true)),
    ('SEMI_FINAL', jsonb_build_object('view_type', 'BRACKET_ROUND', 'expected_matches', 2, 'single_leg', true, 'extra_time', true, 'penalties', true)),
    ('THIRD_PLACE', jsonb_build_object('view_type', 'BRACKET_ROUND', 'expected_matches', 1, 'single_leg', true, 'extra_time', true, 'penalties', true)),
    ('FINAL', jsonb_build_object('view_type', 'BRACKET_ROUND', 'expected_matches', 1, 'single_leg', true, 'extra_time', true, 'penalties', true))
)
update competition_stages st
set rules = coalesce(st.rules, '{}'::jsonb) || rules.patch
from season, rules
where st.competition_season_id = season.competition_season_id
  and st.stage_code = rules.stage_code;

commit;

