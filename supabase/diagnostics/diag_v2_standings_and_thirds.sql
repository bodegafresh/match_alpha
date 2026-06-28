-- ══════════════════════════════════════════════════════════════════════════════
-- DIAG A: ¿Qué equipos están marcados como QUALIFIED_BEST_THIRD?
-- Necesitamos EXACTAMENTE 8 con ese estado.
-- Si hay más o menos → el resolver va a fallar o asignar mal.
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  cg.group_code,
  t.display_name        AS team_name,
  s.position,
  s.points,
  s.goal_difference     AS gd,
  s.goals_for           AS gf,
  s.qualification_status,
  s.as_of
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
JOIN teams t ON t.team_id = s.team_id
WHERE s.qualification_status IN ('QUALIFIED_BEST_THIRD', 'THIRD_PLACE_CANDIDATE')
  AND s.group_id IS NOT NULL
ORDER BY
  s.group_id,
  s.team_id,
  s.as_of DESC;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAG B: El DISTINCT ON que usa el resolver — ¿qué retorna exactamente?
-- Esta es la query real de _get_ranked_best_thirds en slot_resolver.py
-- Copia exacta de la query del resolver para simular lo que calcula
-- ══════════════════════════════════════════════════════════════════════════════
SELECT DISTINCT ON (s.group_id, s.team_id)
  s.team_id::text,
  cg.group_code,
  s.points,
  s.goal_difference,
  s.goals_for,
  s.qualification_status
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
WHERE s.qualification_status IN ('QUALIFIED_BEST_THIRD', 'THIRD_PLACE_CANDIDATE')
  AND s.group_id IS NOT NULL
ORDER BY s.group_id, s.team_id, s.as_of DESC NULLS LAST;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAG C: Terceros de cada grupo con el as_of más reciente (snapshot vigente)
-- Filtra solo position=3 para evitar falsos positivos
-- ══════════════════════════════════════════════════════════════════════════════
SELECT DISTINCT ON (s.group_id)
  cg.group_code,
  t.display_name        AS team_name,
  s.position,
  s.points,
  s.goal_difference     AS gd,
  s.goals_for           AS gf,
  s.qualification_status,
  s.as_of
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
JOIN teams t ON t.team_id = s.team_id
WHERE s.position = 3
  AND s.group_id IS NOT NULL
ORDER BY s.group_id, s.as_of DESC NULLS LAST;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAG D: Mapeo slots BEST_THIRD → partido → equipo actual
-- Muestra slot_code, allowed_groups, team asignado, y el partido al que apunta
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  ts.slot_code,
  ts.metadata->'allowed_groups'               AS allowed_groups,
  ts.resolved_team_id IS NOT NULL             AS is_resolved,
  t_resolved.display_name                     AS resolved_team,
  -- El partido en el que este slot aparece como participante
  home_t.display_name                         AS match_home_team,
  mp.side                                     AS slot_side,
  m.kickoff_at AT TIME ZONE 'America/Santiago' AS kickoff_chile
FROM tournament_slots ts
LEFT JOIN teams t_resolved ON t_resolved.team_id = ts.resolved_team_id
LEFT JOIN match_participants mp ON mp.tournament_slot_id = ts.tournament_slot_id
LEFT JOIN matches m ON m.match_id = mp.match_id
LEFT JOIN match_participants home_mp ON home_mp.match_id = m.match_id AND home_mp.side = 'HOME'
LEFT JOIN teams home_t ON home_t.team_id = home_mp.team_id
WHERE ts.slot_type = 'BEST_THIRD'
ORDER BY ts.slot_code;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAG E: ¿Cuántos snapshots de standings hay por equipo/grupo?
-- Si hay muchos snapshots, el DISTINCT ON puede estar tomando uno viejo
-- ══════════════════════════════════════════════════════════════════════════════
SELECT
  cg.group_code,
  t.display_name       AS team_name,
  s.position,
  count(*)             AS snapshot_count,
  min(s.as_of)         AS oldest_snapshot,
  max(s.as_of)         AS latest_snapshot,
  -- El qualification_status del snapshot MÁS RECIENTE
  (SELECT s2.qualification_status
   FROM standings s2
   WHERE s2.group_id = s.group_id AND s2.team_id = s.team_id
   ORDER BY s2.as_of DESC NULLS LAST LIMIT 1) AS latest_status
FROM standings s
JOIN competition_groups cg ON cg.group_id = s.group_id
JOIN teams t ON t.team_id = s.team_id
WHERE s.position = 3
  AND s.group_id IS NOT NULL
GROUP BY cg.group_code, t.display_name, s.group_id, s.team_id, s.position
ORDER BY cg.group_code;


-- ══════════════════════════════════════════════════════════════════════════════
-- DIAG F: De los 8 QUALIFIED_BEST_THIRD, ¿a qué slots son elegibles?
-- Muestra la matriz de compatibilidad equipo ↔ slot (la misma que calcula el resolver)
-- ══════════════════════════════════════════════════════════════════════════════
WITH best_thirds AS (
  SELECT DISTINCT ON (s.group_id)
    t.display_name   AS team_name,
    cg.group_code,
    s.qualification_status
  FROM standings s
  JOIN competition_groups cg ON cg.group_id = s.group_id
  JOIN teams t ON t.team_id = s.team_id
  WHERE s.qualification_status = 'QUALIFIED_BEST_THIRD'
    AND s.group_id IS NOT NULL
  ORDER BY s.group_id, s.as_of DESC NULLS LAST
),
slots AS (
  SELECT
    slot_code,
    jsonb_array_elements_text(metadata->'allowed_groups') AS allowed_group
  FROM tournament_slots
  WHERE slot_type = 'BEST_THIRD'
)
SELECT
  bt.team_name,
  bt.group_code,
  string_agg(s.slot_code, E'\n' ORDER BY s.slot_code) AS eligible_slots,
  count(*)                                              AS n_eligible_slots
FROM best_thirds bt
JOIN slots s ON upper(bt.group_code) = upper(s.allowed_group)
GROUP BY bt.team_name, bt.group_code
ORDER BY n_eligible_slots ASC, bt.group_code;  -- más restringidos primero
