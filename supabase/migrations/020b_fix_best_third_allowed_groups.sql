-- Migration 020b: Fix BEST_THIRD slots and quarterfinal pattern
--
-- Run this after 020_link_tournament_slots.sql.
--
-- Fixes:
--   1. BEST_THIRD slots need metadata.allowed_groups parsed from slot_code
--      e.g. 'third_place_group_b_e_f_i_j' → allowed_groups: ["B","E","F","I","J"]
--   2. source_match_id missing for 'quarterfinal_N_winner' (different spelling)

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Set metadata.allowed_groups for BEST_THIRD slots
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE tournament_slots
SET metadata = metadata || jsonb_build_object(
  'allowed_groups',
  (
    SELECT jsonb_agg(upper(m[1]) ORDER BY upper(m[1]))
    FROM regexp_matches(
      regexp_replace(slot_code, '^third_place_group_', ''),
      '([a-z])',
      'g'
    ) AS r(m)
  )
)
WHERE slot_type = 'BEST_THIRD'
  AND (metadata->'allowed_groups') IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Link 'quarterfinal_N_winner/loser' slots to QUARTER_FINAL matches
--    (different spelling than 'quarter_final_' handled in migration 020)
-- ─────────────────────────────────────────────────────────────────────────────

WITH slot_parse AS (
  SELECT
    ts.tournament_slot_id,
    ts.competition_season_id,
    (regexp_match(ts.slot_code, '^quarterfinal_(\d+)_'))[1]::int AS match_rank
  FROM tournament_slots ts
  WHERE ts.slot_type IN ('WINNER', 'LOSER')
    AND ts.source_match_id IS NULL
    AND ts.slot_code ~ '^quarterfinal_\d+_'
),
ranked_matches AS (
  SELECT
    m.match_id,
    m.competition_season_id,
    ROW_NUMBER() OVER (
      PARTITION BY m.competition_season_id
      ORDER BY m.match_number ASC NULLS LAST, m.kickoff_at ASC
    ) AS match_rank
  FROM matches m
  JOIN competition_stages cs ON cs.stage_id = m.stage_id
  WHERE cs.stage_code = 'QUARTER_FINAL'
)
UPDATE tournament_slots ts
SET source_match_id = rm.match_id
FROM slot_parse sp
JOIN ranked_matches rm
  ON rm.competition_season_id = sp.competition_season_id
 AND rm.match_rank = sp.match_rank
WHERE ts.tournament_slot_id = sp.tournament_slot_id
  AND sp.match_rank IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Verify
-- ─────────────────────────────────────────────────────────────────────────────
-- SELECT slot_code, metadata->'allowed_groups' AS allowed_groups
--   FROM tournament_slots WHERE slot_type = 'BEST_THIRD';
--
-- SELECT slot_code, source_match_id
--   FROM tournament_slots WHERE slot_code ~ '^quarterfinal_';
