-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 1: feature_completeness — ¿en qué escala está guardado?
-- Esperado: valores entre 0.0 y 1.0
-- Si ves valores > 1 → está guardado como porcentaje (bug)
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  min(feature_completeness)  AS min_val,
  max(feature_completeness)  AS max_val,
  avg(feature_completeness)  AS avg_val,
  count(*)                   AS total_rows,
  count(*) FILTER (WHERE feature_completeness > 1) AS rows_gt_1
FROM feature_snapshots;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 2: confidence_score en model_predictions — ¿cuántos son NULL?
-- Si todos son NULL → el COALESCE siempre cae al feature_completeness bugueado
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  prediction_status,
  count(*)                                              AS total,
  count(*) FILTER (WHERE confidence_score IS NULL)      AS conf_null,
  count(*) FILTER (WHERE confidence_score IS NOT NULL)  AS conf_not_null,
  round(min(confidence_score)::numeric, 4)              AS min_conf,
  round(max(confidence_score)::numeric, 4)              AS max_conf
FROM model_predictions
GROUP BY prediction_status;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 3: ¿Qué ve la view published_ev_opportunities para confidence?
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  home_team_name || ' vs ' || away_team_name AS partido,
  market_code,
  selection_code,
  confidence_score,
  prediction_status,
  match_status,
  kickoff_at
FROM published_ev_opportunities
ORDER BY kickoff_at ASC;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 4: Estado de BEST_THIRD slots — ¿siguen con resolved_team_id?
-- Esperado después de 020c: todos NULL
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  slot_code,
  slot_type,
  resolved_team_id,
  metadata->'allowed_groups' AS allowed_groups
FROM tournament_slots
WHERE slot_type = 'BEST_THIRD'
ORDER BY slot_code;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 5: Estado del ranking de mejores terceros
-- ¿Cuántos grupos tienen QUALIFIED_BEST_THIRD?
-- Necesitamos ver exactamente qué grupos clasificaron y en qué posición
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  cg.group_code,
  t.display_name        AS team_name,
  s.points,
  s.goals_for,
  s.goals_against,
  s.goals_diff,
  s.wins,
  s.ranking_position,
  s.qualification_status,
  s.tiebreaker_notes
FROM competition_group_standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
JOIN teams t ON t.team_id = s.team_id
WHERE s.ranking_position = 3
ORDER BY s.points DESC, s.goals_diff DESC, s.goals_for DESC;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 6: ¿Cuántos grupos terceros tienen QUALIFIED_BEST_THIRD?
-- Necesitamos exactamente 8 para que el resolver pueda asignar todos los slots
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  s.qualification_status,
  count(*) AS cnt,
  string_agg(cg.group_code, ', ' ORDER BY cg.group_code) AS groups
FROM competition_group_standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
WHERE s.ranking_position = 3
GROUP BY s.qualification_status;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 7: ¿Los match_participants de los slots BEST_THIRD siguen con SLOT?
-- Esperado después de 020c: participant_role = 'SLOT', team_id NULL
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  ts.slot_code,
  mp.side,
  mp.participant_role,
  mp.team_id,
  t.display_name AS team_name
FROM tournament_slots ts
JOIN match_participants mp ON mp.tournament_slot_id = ts.tournament_slot_id
LEFT JOIN teams t ON t.team_id = mp.team_id
WHERE ts.slot_type = 'BEST_THIRD'
ORDER BY ts.slot_code;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 8: Partidos de eliminatoria (R32) — ¿quién ya tiene teams asignados?
-- Muestra el estado actual del bracket de dieciseisavos
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  m.match_number,
  m.kickoff_at AT TIME ZONE 'America/Santiago' AS kickoff_chile,
  coalesce(home_t.display_name, home_ts.slot_label, home_ts.slot_code) AS home,
  coalesce(away_t.display_name, away_ts.slot_label, away_ts.slot_code) AS away,
  home_mp.participant_role AS home_role,
  away_mp.participant_role AS away_role
FROM matches m
JOIN competition_stages cs ON cs.stage_id = m.stage_id
LEFT JOIN match_participants home_mp ON home_mp.match_id = m.match_id AND home_mp.side = 'HOME'
LEFT JOIN teams home_t ON home_t.team_id = home_mp.team_id
LEFT JOIN tournament_slots home_ts ON home_ts.tournament_slot_id = home_mp.tournament_slot_id
LEFT JOIN match_participants away_mp ON away_mp.match_id = m.match_id AND away_mp.side = 'AWAY'
LEFT JOIN teams away_t ON away_t.team_id = away_mp.team_id
LEFT JOIN tournament_slots away_ts ON away_ts.tournament_slot_id = away_mp.tournament_slot_id
WHERE cs.stage_code = 'ROUND_OF_32'
ORDER BY m.kickoff_at;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAGNÓSTICO 9: ¿Partidos de fase de grupos están todos FINISHED?
-- Todos deberían ser FINISHED para que el resolver calcule los terceros
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  m.status,
  count(*) AS count
FROM matches m
JOIN competition_stages cs ON cs.stage_id = m.stage_id
WHERE cs.stage_code = 'GROUP_STAGE'
GROUP BY m.status;
