-- =============================================================================
-- match_alpha — Validación de integridad y consistencia de datos
-- Ejecutar en Supabase SQL Editor
-- Cada check devuelve filas solo si hay un problema.
-- =============================================================================

-- ─── HELPERS ─────────────────────────────────────────────────────────────────
-- Convención: cada bloque tiene un título y devuelve:
--   ok=true  → sin problemas
--   ok=false → hay filas con problemas (ver detalle)

-- =============================================================================
-- 1. COMPETITION GROUPS
-- =============================================================================

-- 1.1 Grupos duplicados (mismo season + stage + group_name con distinto group_code)
SELECT 'groups_duplicates' AS check, competition_season_id, group_name,
       count(*) AS duplicates, string_agg(group_code, ', ') AS codes
FROM competition_groups
GROUP BY competition_season_id, stage_id, group_name
HAVING count(*) > 1;

-- 1.2 Grupos sin group_order (no se pueden ordenar en el frontend)
SELECT 'groups_missing_order' AS check, group_id, group_code, group_name
FROM competition_groups
WHERE group_order IS NULL;

-- 1.3 Grupos sin stage_id válido
SELECT 'groups_orphan_stage' AS check, cg.group_id, cg.group_code
FROM competition_groups cg
LEFT JOIN competition_stages cs ON cs.stage_id = cg.stage_id
WHERE cs.stage_id IS NULL;

-- =============================================================================
-- 2. MATCHES
-- =============================================================================

-- 2.1 Partidos sin participantes (HOME + AWAY)
SELECT 'matches_missing_participants' AS check, m.match_id, m.status,
       count(mp.side) AS sides_found
FROM matches m
LEFT JOIN match_participants mp ON mp.match_id = m.match_id
GROUP BY m.match_id, m.status
HAVING count(mp.side) < 2;

-- 2.2 Partidos FINISHED sin marcador
SELECT 'finished_without_score' AS check, m.match_id, m.kickoff_at
FROM matches m
WHERE m.status = 'FINISHED'
  AND (m.home_score IS NULL OR m.away_score IS NULL);

-- 2.3 Partidos con mismo equipo en ambos lados
SELECT 'matches_same_team_both_sides' AS check, m.match_id
FROM matches m
JOIN match_participants h ON h.match_id = m.match_id AND h.side = 'HOME'
JOIN match_participants a ON a.match_id = m.match_id AND a.side = 'AWAY'
WHERE h.team_id = a.team_id;

-- 2.4 Partidos de grupos sin group_id
SELECT 'group_matches_without_group_id' AS check, m.match_id, m.kickoff_at
FROM matches m
JOIN competition_stages cs ON cs.stage_id = m.stage_id
WHERE cs.stage_code = 'GROUP_STAGE'
  AND m.group_id IS NULL;

-- =============================================================================
-- 3. TOURNAMENT SLOTS
-- =============================================================================

-- 3.1 Slots resueltos a equipos que no participan en la competencia
SELECT 'slots_team_not_in_season' AS check, ts.slot_code, ts.resolved_team_id
FROM tournament_slots ts
LEFT JOIN competition_team_entries cte
       ON cte.team_id = ts.resolved_team_id
      AND cte.competition_season_id = ts.competition_season_id
WHERE ts.resolved_team_id IS NOT NULL
  AND cte.competition_team_entry_id IS NULL;

-- 3.2 Slots con source_rank <= 2 (winner/runner-up) sin source_group_id
--     Estos deben resolverse via slot_code derivation
SELECT 'slots_group_rank_no_source_group' AS check,
       ts.slot_code, ts.source_rank, ts.resolved_team_id
FROM tournament_slots ts
WHERE ts.source_rank IN (1, 2)
  AND ts.source_group_id IS NULL
  AND ts.source_match_id IS NULL;

-- 3.3 Partidos de eliminatorias con slots resueltos en conflicto con match_participants
SELECT 'slots_mismatch_match_participants' AS check,
       ts.slot_code, ts.resolved_team_id AS slot_team,
       mp.team_id AS participant_team, mp.match_id
FROM tournament_slots ts
JOIN match_participants mp ON mp.tournament_slot_id = ts.tournament_slot_id
WHERE ts.resolved_team_id IS NOT NULL
  AND mp.team_id != ts.resolved_team_id;

-- =============================================================================
-- 4. STANDINGS
-- =============================================================================

-- 4.1 Standings con group_id que no existe en competition_groups
SELECT 'standings_orphan_group' AS check, s.group_id, count(*) AS rows
FROM standings s
LEFT JOIN competition_groups cg ON cg.group_id = s.group_id
WHERE s.group_id IS NOT NULL
  AND cg.group_id IS NULL
GROUP BY s.group_id;

-- 4.2 Grupos con != 4 equipos en standings (WC2026 tiene 4 por grupo)
SELECT 'standings_wrong_team_count' AS check,
       cg.group_code, cg.group_name, count(DISTINCT s.team_id) AS teams
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
WHERE s.competition_season_id = (
  SELECT competition_season_id FROM competition_seasons WHERE slug = 'wc2026' LIMIT 1
)
GROUP BY cg.group_id, cg.group_code, cg.group_name
HAVING count(DISTINCT s.team_id) != 4;

-- 4.3 Equipos con posición duplicada dentro del mismo grupo
SELECT 'standings_duplicate_position' AS check,
       cg.group_code, s.position, count(*) AS duplicates
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
WHERE s.competition_season_id = (
  SELECT competition_season_id FROM competition_seasons WHERE slug = 'wc2026' LIMIT 1
)
GROUP BY cg.group_id, cg.group_code, s.position
HAVING count(*) > 1;

-- 4.4 Standings con puntos negativos o played=0 pero puntos > 0
SELECT 'standings_invalid_stats' AS check,
       s.team_id, cg.group_code, s.points, s.played
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
WHERE s.points < 0
   OR (s.played = 0 AND s.points > 0);

-- =============================================================================
-- 5. TEAMS
-- =============================================================================

-- 5.1 Equipos en partidos que no están registrados en la competencia
SELECT 'teams_in_matches_not_in_season' AS check,
       mp.team_id, m.competition_season_id
FROM match_participants mp
JOIN matches m ON m.match_id = mp.match_id
LEFT JOIN competition_team_entries cte
       ON cte.team_id = mp.team_id
      AND cte.competition_season_id = m.competition_season_id
WHERE cte.competition_team_entry_id IS NULL;

-- 5.2 Equipos en standings sin entrada en competition_team_entries
SELECT 'standings_team_not_in_entries' AS check,
       s.team_id, s.competition_season_id
FROM standings s
LEFT JOIN competition_team_entries cte
       ON cte.team_id = s.team_id
      AND cte.competition_season_id = s.competition_season_id
WHERE cte.competition_team_entry_id IS NULL;

-- =============================================================================
-- 6. ODDS & PREDICTIONS
-- =============================================================================

-- 6.1 Odds capturadas DESPUÉS del kickoff (leakage potencial)
SELECT 'odds_captured_after_kickoff' AS check,
       os.odds_snapshot_id, m.match_id,
       m.kickoff_at, os.captured_at
FROM odds_snapshots os
JOIN matches m ON m.match_id = os.match_id
WHERE os.captured_at > m.kickoff_at + interval '2 hours';

-- 6.2 Predicciones con probabilidad fuera de rango [0,1]
SELECT 'predictions_invalid_probability' AS check,
       mp.prediction_id, mp.raw_probability, mp.calibrated_probability
FROM model_predictions mp
WHERE mp.raw_probability < 0 OR mp.raw_probability > 1
   OR mp.calibrated_probability < 0 OR mp.calibrated_probability > 1;

-- 6.3 Predicciones 1X2 de un mismo modelo/partido que no suman ~1.0
SELECT 'predictions_probs_dont_sum_to_one' AS check,
       mp.model_run_id, mp.match_id,
       round(sum(mp.raw_probability)::numeric, 4) AS prob_sum
FROM model_predictions mp
JOIN markets mk ON mk.market_id = mp.market_id
WHERE mk.market_code = '1X2'
GROUP BY mp.model_run_id, mp.match_id
HAVING abs(sum(mp.raw_probability) - 1.0) > 0.01;

-- =============================================================================
-- 7. BETTING DECISIONS
-- =============================================================================

-- 7.1 Decisions referenciando predicciones de otro partido
SELECT 'decisions_prediction_match_mismatch' AS check,
       bd.betting_decision_id, bd.match_id AS decision_match,
       mp.match_id AS prediction_match
FROM betting_decisions bd
JOIN model_predictions mp ON mp.prediction_id = bd.prediction_id
WHERE bd.match_id != mp.match_id;

-- 7.2 Decisions SETTLED sin settlement_result
SELECT 'settled_without_result' AS check, count(*) AS count
FROM betting_decisions
WHERE settlement_status = 'SETTLED'
  AND settlement_result IS NULL;

-- 7.3 Decisions BETTABLE con odds > 7 días de antigüedad (stale)
SELECT 'bettable_stale_odds' AS check,
       bd.betting_decision_id, m.kickoff_at, os.captured_at,
       round(extract(epoch FROM (m.kickoff_at - os.captured_at))/3600, 1) AS hours_before_kickoff
FROM betting_decisions bd
JOIN matches m ON m.match_id = bd.match_id
JOIN odds_snapshots os ON os.odds_snapshot_id = bd.odds_snapshot_id
WHERE bd.decision_status = 'BETTABLE'
  AND m.status = 'SCHEDULED'
  AND os.captured_at < now() - interval '2 hours';

-- =============================================================================
-- 8. PIPELINE RUNS
-- =============================================================================

-- 8.1 Jobs que fallaron en las últimas 24 horas
SELECT 'failed_jobs_24h' AS check,
       job_name, status, started_at, error_message
FROM pipeline_runs
WHERE status = 'ERROR'
  AND started_at >= now() - interval '24 hours'
ORDER BY started_at DESC;

-- 8.2 Jobs críticos sin correr en las últimas 48 horas
SELECT 'critical_jobs_not_run' AS check, job_name,
       max(started_at) AS last_run
FROM pipeline_runs
WHERE job_name IN (
  'worldcup_daily_refresh', 'standings_refresh', 'qualification_resolver'
)
  AND status IN ('OK', 'WARN')
GROUP BY job_name
HAVING max(started_at) < now() - interval '48 hours'
    OR max(started_at) IS NULL;

-- =============================================================================
-- 9. RESUMEN GENERAL (siempre devuelve 1 fila con conteos clave)
-- =============================================================================
SELECT
  (SELECT count(*) FROM matches WHERE status = 'FINISHED')         AS matches_finished,
  (SELECT count(*) FROM matches WHERE status = 'SCHEDULED')        AS matches_scheduled,
  (SELECT count(*) FROM matches WHERE status = 'LIVE')             AS matches_live,
  (SELECT count(*) FROM standings)                                  AS standings_rows,
  (SELECT count(DISTINCT team_id) FROM standings)                   AS teams_with_standings,
  (SELECT count(*) FROM tournament_slots WHERE resolved_team_id IS NOT NULL) AS slots_resolved,
  (SELECT count(*) FROM tournament_slots WHERE resolved_team_id IS NULL)     AS slots_pending,
  (SELECT count(*) FROM odds_snapshots)                             AS odds_snapshots,
  (SELECT count(*) FROM model_predictions)                          AS predictions,
  (SELECT count(*) FROM betting_decisions)                          AS decisions,
  (SELECT count(*) FROM competition_groups)                         AS groups_total,
  (SELECT count(*) FROM pipeline_runs WHERE status = 'ERROR'
     AND started_at >= now() - interval '24 hours')                AS errors_24h;
