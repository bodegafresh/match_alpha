-- Migration 023: Reset BEST_THIRD slots y aplicar asignación oficial
--
-- Problema: el resolver greedy asignó mal (Suecia a Alemania, Paraguay a Francia, etc.)
-- Solución: resetear todos los BEST_THIRD, luego aplicar el sorteo oficial directamente.
--
-- NOTA: después de correr esta migración, el qualification_resolver re-confirmará
-- las asignaciones usando la draw matrix (022). Esta migración fuerza el estado
-- correcto para los 6 equipos ya clasificados; Ecuador y Ghana quedan PENDING.

-- ─────────────────────────────────────────────────────────────────────────────
-- PASO 1: Reset ALL BEST_THIRD slots y sus match_participants
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE match_participants mp
SET team_id         = NULL,
    participant_role = 'SLOT',
    updated_at      = now()
FROM tournament_slots ts
WHERE mp.tournament_slot_id = ts.tournament_slot_id
  AND ts.slot_type = 'BEST_THIRD';

UPDATE tournament_slots
SET resolved_team_id = NULL,
    resolved_at      = NULL,
    updated_at       = now()
WHERE slot_type = 'BEST_THIRD';

-- ─────────────────────────────────────────────────────────────────────────────
-- PASO 2: Aplicar asignación correcta según sorteo oficial
-- (los 6 equipos ya confirmados; Ecuador y Ghana se asignarán al resolver)
-- ─────────────────────────────────────────────────────────────────────────────

-- third_place_group_a_b_c_d_f → Paraguay (grupo D) → Alemania
UPDATE tournament_slots
SET resolved_team_id = (SELECT team_id FROM teams WHERE display_name ILIKE '%Paraguay%' LIMIT 1),
    resolved_at      = now(),
    updated_at       = now()
WHERE slot_code = 'third_place_group_a_b_c_d_f';

-- third_place_group_a_e_h_i_j → Senegal (grupo I) → Bélgica
UPDATE tournament_slots
SET resolved_team_id = (SELECT team_id FROM teams WHERE display_name ILIKE '%Senegal%' LIMIT 1),
    resolved_at      = now(),
    updated_at       = now()
WHERE slot_code = 'third_place_group_a_e_h_i_j';

-- third_place_group_b_e_f_i_j → Bosnia y Herzegovina (grupo B) → EE.UU.
UPDATE tournament_slots
SET resolved_team_id = (SELECT team_id FROM teams WHERE display_name ILIKE '%Bosnia%' LIMIT 1),
    resolved_at      = now(),
    updated_at       = now()
WHERE slot_code = 'third_place_group_b_e_f_i_j';

-- third_place_group_c_d_f_g_h → Suecia (grupo F) → Francia
UPDATE tournament_slots
SET resolved_team_id = (SELECT team_id FROM teams WHERE display_name ILIKE '%Suecia%' LIMIT 1),
    resolved_at      = now(),
    updated_at       = now()
WHERE slot_code = 'third_place_group_c_d_f_g_h';

-- third_place_group_c_e_f_h_i → Ecuador (grupo E) → México — PENDING hasta que resuelva tiebreaker
-- (dejar NULL — se asignará cuando Ecuador sea QUALIFIED_BEST_THIRD)

-- third_place_group_d_e_i_j_l → Ghana (grupo L) → Colombia — PENDING
-- (dejar NULL — se asignará cuando Ghana sea QUALIFIED_BEST_THIRD)

-- third_place_group_e_f_g_i_j → Argelia (grupo J) → Suiza
UPDATE tournament_slots
SET resolved_team_id = (SELECT team_id FROM teams WHERE display_name ILIKE '%Argelia%' LIMIT 1),
    resolved_at      = now(),
    updated_at       = now()
WHERE slot_code = 'third_place_group_e_f_g_i_j';

-- third_place_group_e_h_i_j_k → Congo DR (grupo K) → Inglaterra
UPDATE tournament_slots
SET resolved_team_id = (SELECT team_id FROM teams WHERE display_name ILIKE '%Congo%' LIMIT 1),
    resolved_at      = now(),
    updated_at       = now()
WHERE slot_code = 'third_place_group_e_h_i_j_k';

-- ─────────────────────────────────────────────────────────────────────────────
-- PASO 3: Actualizar match_participants con los equipos resueltos
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE match_participants mp
SET team_id         = ts.resolved_team_id,
    participant_role = 'TEAM',
    updated_at      = now()
FROM tournament_slots ts
WHERE mp.tournament_slot_id = ts.tournament_slot_id
  AND ts.slot_type = 'BEST_THIRD'
  AND ts.resolved_team_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Verificar resultado
-- ─────────────────────────────────────────────────────────────────────────────
-- SELECT
--   ts.slot_code,
--   t.display_name AS team,
--   home_t.display_name AS match_home,
--   mp.participant_role
-- FROM tournament_slots ts
-- LEFT JOIN teams t ON t.team_id = ts.resolved_team_id
-- LEFT JOIN match_participants mp ON mp.tournament_slot_id = ts.tournament_slot_id
-- LEFT JOIN matches m ON m.match_id = mp.match_id
-- LEFT JOIN match_participants home_mp ON home_mp.match_id = m.match_id AND home_mp.side = 'HOME'
-- LEFT JOIN teams home_t ON home_t.team_id = home_mp.team_id
-- WHERE ts.slot_type = 'BEST_THIRD'
-- ORDER BY ts.slot_code;
