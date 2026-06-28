-- 026_backfill_competition_stages_rules.sql
-- Purpose:
--   Backfill and keep competition_stages fully populated (with rules) for all
--   seeded competition_seasons. Idempotent and safe to re-run.

with stage_catalog as (
  select * from (
    values
      (
        'wc2026',
        'GROUP_STAGE',
        'Fase de grupos',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","expected_matches":72,"teams_per_group":4,"group_count":12,"qualifies":{"top_n_per_group":2,"best_third_places":8},"tie_breakers":["points","goal_difference","goals_for","head_to_head","fair_play","draw"]}'::jsonb
      ),
      (
        'wc2026',
        'ROUND_OF_32',
        'Dieciseisavos de final',
        2,
        'KNOCKOUT',
        '{"view_type":"BRACKET_ROUND","expected_matches":16,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true,"best_third_assignment_matrix":{"B,D,F,I,J,K":{"third_place_group_a_b_c_d_f":"D","third_place_group_a_e_h_i_j":"I","third_place_group_b_e_f_i_j":"B","third_place_group_c_d_f_g_h":"F","third_place_group_e_f_g_i_j":"J","third_place_group_e_h_i_j_k":"K"},"B,D,E,F,I,J,K,L":{"third_place_group_a_b_c_d_f":"D","third_place_group_a_e_h_i_j":"I","third_place_group_b_e_f_i_j":"B","third_place_group_c_d_f_g_h":"F","third_place_group_c_e_f_h_i":"E","third_place_group_d_e_i_j_l":"L","third_place_group_e_f_g_i_j":"J","third_place_group_e_h_i_j_k":"K"}}}'::jsonb
      ),
      (
        'wc2026',
        'ROUND_OF_16',
        'Octavos de final',
        3,
        'KNOCKOUT',
        '{"view_type":"BRACKET_ROUND","expected_matches":8,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'wc2026',
        'QUARTER_FINAL',
        'Cuartos de final',
        4,
        'KNOCKOUT',
        '{"view_type":"BRACKET_ROUND","expected_matches":4,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'wc2026',
        'SEMI_FINAL',
        'Semifinal',
        5,
        'KNOCKOUT',
        '{"view_type":"BRACKET_ROUND","expected_matches":2,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'wc2026',
        'THIRD_PLACE',
        'Tercer lugar',
        6,
        'THIRD_PLACE',
        '{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'wc2026',
        'FINAL',
        'Final',
        7,
        'FINAL',
        '{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),

      (
        'ucl-2026-2027',
        'LEAGUE_PHASE',
        'Fase liga',
        1,
        'LEAGUE_PHASE',
        '{"view_type":"LEAGUE_PHASE_TABLE","format":"SWISS_OR_LEAGUE_PHASE","tie_breakers":["points","goal_difference","goals_for","away_goals","wins"],"qualification":{"top_8":"ROUND_OF_16","positions_9_to_24":"KNOCKOUT_PLAYOFF","positions_25_plus":"ELIMINATED"}}'::jsonb
      ),
      (
        'ucl-2026-2027',
        'KNOCKOUT_PLAYOFF',
        'Playoffs eliminatorios',
        2,
        'PLAYOFF',
        '{"view_type":"TWO_LEG_TIE","expected_matches":16,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'ucl-2026-2027',
        'ROUND_OF_16',
        'Octavos de final',
        3,
        'KNOCKOUT',
        '{"view_type":"TWO_LEG_TIE","expected_matches":8,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'ucl-2026-2027',
        'QUARTER_FINAL',
        'Cuartos de final',
        4,
        'KNOCKOUT',
        '{"view_type":"TWO_LEG_TIE","expected_matches":4,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'ucl-2026-2027',
        'SEMI_FINAL',
        'Semifinal',
        5,
        'KNOCKOUT',
        '{"view_type":"TWO_LEG_TIE","expected_matches":2,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'ucl-2026-2027',
        'FINAL',
        'Final',
        6,
        'FINAL',
        '{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),

      (
        'premier-league-2026-2027',
        'LEAGUE_REGULAR',
        'Temporada regular',
        1,
        'LEAGUE_PHASE',
        '{"view_type":"LEAGUE_TABLE","rounds":"DOUBLE_ROUND_ROBIN","tie_breakers":["points","goal_difference","goals_for","wins"]}'::jsonb
      ),
      (
        'chile-primera-2026',
        'LEAGUE_REGULAR',
        'Temporada regular',
        1,
        'LEAGUE_PHASE',
        '{"view_type":"LEAGUE_TABLE","rounds":"DOUBLE_ROUND_ROBIN","tie_breakers":["points","goal_difference","goals_for","wins"]}'::jsonb
      ),

      (
        'libertadores-2026',
        'GROUP_STAGE',
        'Fase de grupos',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","teams_per_group":4,"qualifies":{"top_n_per_group":2},"tie_breakers":["points","goal_difference","goals_for","away_goals","fair_play","draw"]}'::jsonb
      ),
      (
        'libertadores-2026',
        'ROUND_OF_16',
        'Octavos de final',
        2,
        'KNOCKOUT',
        '{"view_type":"TWO_LEG_TIE","expected_matches":8,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'libertadores-2026',
        'QUARTER_FINAL',
        'Cuartos de final',
        3,
        'KNOCKOUT',
        '{"view_type":"TWO_LEG_TIE","expected_matches":4,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'libertadores-2026',
        'SEMI_FINAL',
        'Semifinal',
        4,
        'KNOCKOUT',
        '{"view_type":"TWO_LEG_TIE","expected_matches":2,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),
      (
        'libertadores-2026',
        'FINAL',
        'Final',
        5,
        'FINAL',
        '{"view_type":"BRACKET_ROUND","expected_matches":1,"legs":1,"single_leg":true,"aggregate_score":false,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),

      (
        'conmebol-qualifiers-wc2030',
        'LEAGUE_REGULAR',
        'Eliminatorias',
        1,
        'LEAGUE_PHASE',
        '{"view_type":"LEAGUE_TABLE","rounds":"DOUBLE_ROUND_ROBIN","teams":10,"tie_breakers":["points","goal_difference","goals_for","wins","head_to_head"],"qualification":{"direct_wc":6,"intercontinental_playoff":2,"eliminated":2}}'::jsonb
      ),

      (
        'uefa-qualifiers-wc2030',
        'GROUP_STAGE',
        'Fase de grupos',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","teams_per_group":5,"group_count":12,"qualifies":{"top_n_per_group":1,"runners_up_to_playoff":12},"tie_breakers":["points","goal_difference","goals_for","away_goals","wins","fair_play"],"qualification":{"direct_wc":12,"playoff_round":12}}'::jsonb
      ),
      (
        'uefa-qualifiers-wc2030',
        'PLAYOFF',
        'Playoffs',
        2,
        'PLAYOFF',
        '{"view_type":"TWO_LEG_TIE","expected_matches":6,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),

      (
        'concacaf-qualifiers-wc2030',
        'GROUP_STAGE',
        'Ronda de grupos',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","teams_per_group":4,"group_count":3,"tie_breakers":["points","goal_difference","goals_for","wins","head_to_head"],"qualification":{"direct_wc":3,"runners_up_to_playoff":3}}'::jsonb
      ),
      (
        'concacaf-qualifiers-wc2030',
        'FINAL_ROUND',
        'Ronda final',
        2,
        'LEAGUE_PHASE',
        '{"view_type":"LEAGUE_TABLE","rounds":"DOUBLE_ROUND_ROBIN","teams":6,"tie_breakers":["points","goal_difference","goals_for","wins","head_to_head"],"qualification":{"direct_wc":3,"intercontinental_playoff":2,"eliminated":1}}'::jsonb
      ),

      (
        'caf-qualifiers-wc2030',
        'GROUP_STAGE',
        'Fase de grupos',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","teams_per_group":5,"group_count":9,"tie_breakers":["points","goal_difference","goals_for","away_goals","wins","fair_play"],"qualification":{"top_n_per_group":1}}'::jsonb
      ),
      (
        'caf-qualifiers-wc2030',
        'PLAYOFF',
        'Playoffs finales',
        2,
        'PLAYOFF',
        '{"view_type":"TWO_LEG_TIE","expected_matches":4,"legs":2,"single_leg":false,"aggregate_score":true,"away_goals_rule":false,"extra_time":true,"penalties":true}'::jsonb
      ),

      (
        'afc-qualifiers-wc2030',
        'GROUP_STAGE',
        'Ronda 3',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","teams_per_group":6,"group_count":3,"tie_breakers":["points","goal_difference","goals_for","wins","head_to_head","fair_play"],"qualification":{"top_n_per_group":2,"runners_up_to_playoff":6}}'::jsonb
      ),
      (
        'afc-qualifiers-wc2030',
        'PLAYOFF',
        'Ronda 4',
        2,
        'PLAYOFF',
        '{"view_type":"GROUP_TABLES","teams_per_group":3,"group_count":2,"tie_breakers":["points","goal_difference","goals_for"],"qualification":{"top_n_per_group":1,"intercontinental_playoff":2}}'::jsonb
      ),

      (
        'ofc-qualifiers-wc2030',
        'GROUP_STAGE',
        'Fase de grupos',
        1,
        'GROUP_STAGE',
        '{"view_type":"GROUP_TABLES","teams_per_group":4,"group_count":2,"tie_breakers":["points","goal_difference","goals_for"],"qualification":{"top_n_per_group":2}}'::jsonb
      ),
      (
        'ofc-qualifiers-wc2030',
        'FINAL_ROUND',
        'Ronda final',
        2,
        'LEAGUE_PHASE',
        '{"view_type":"LEAGUE_TABLE","rounds":"SINGLE_ROUND_ROBIN","teams":4,"tie_breakers":["points","goal_difference","goals_for"],"qualification":{"direct_wc":1,"intercontinental_playoff":1}}'::jsonb
      )
  ) as v(
    season_slug,
    stage_code,
    stage_name,
    stage_order,
    stage_type,
    rules
  )
),
upsert_stages as (
  insert into competition_stages (
    competition_season_id,
    stage_code,
    stage_name,
    stage_order,
    stage_type,
    rules
  )
  select
    cs.competition_season_id,
    sc.stage_code,
    sc.stage_name,
    sc.stage_order,
    sc.stage_type::stage_type,
    sc.rules
  from stage_catalog sc
  join competition_seasons cs on cs.slug = sc.season_slug
  on conflict (competition_season_id, stage_code) do update set
    stage_name = excluded.stage_name,
    stage_order = excluded.stage_order,
    stage_type = excluded.stage_type,
    rules = competition_stages.rules || excluded.rules,
    updated_at = now()
  returning stage_id
)
select count(*) as upserted_rows
from upsert_stages;
