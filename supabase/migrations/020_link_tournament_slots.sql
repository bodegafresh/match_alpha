-- Migration 020: Link tournament_slots to their source data
--
-- The migration script creates slots but doesn't populate:
--   1. source_rank  — rank within group (1=winner, 2=runner-up, 3=third)
--   2. source_match_id — knockout match whose winner/loser fills the slot
--
-- Without these, qualification_resolver_job can't resolve any slots.

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Set source_rank from slot_code patterns
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE tournament_slots
SET source_rank =
  CASE
    WHEN slot_code ~ '_winner$'                     THEN 1
    WHEN slot_code ~ '_(runner_up|2nd_place)$'      THEN 2
    WHEN slot_code ~ '^third_place_group_'           THEN 3
  END
WHERE source_rank IS NULL
  AND (
    slot_code ~ '_winner$'
    OR slot_code ~ '_(runner_up|2nd_place)$'
    OR slot_code ~ '^third_place_group_'
  )
  -- Only group-stage slots (slot_type not WINNER/LOSER = knockout)
  AND slot_type NOT IN ('WINNER', 'LOSER');

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Set source_group_id for third-place slots that lack it
--    (third-place slots are in ROUND_OF_32 stage, but need to reference a group)
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE tournament_slots ts
SET source_group_id = cg.group_id
FROM competition_groups cg
JOIN competition_seasons cs ON cs.competition_season_id = cg.competition_season_id
WHERE ts.competition_season_id = cg.competition_season_id
  AND ts.source_group_id IS NULL
  AND ts.slot_code ~ '^third_place_group_'
  -- Extract letter from slot_code e.g. 'third_place_group_c' → 'c'
  AND lower(trim(cg.group_code)) = (regexp_match(ts.slot_code, 'third_place_group_([a-z]+)'))[1];

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Set source_match_id for WINNER/LOSER knockout slots
--    Maps slot_code like 'round_of_32_3_winner' → 3rd match in ROUND_OF_32 stage
-- ─────────────────────────────────────────────────────────────────────────────

WITH slot_parse AS (
  SELECT
    ts.tournament_slot_id,
    ts.competition_season_id,
    -- Map slot_code prefix to stage_code
    CASE
      WHEN ts.slot_code ~ '^round_of_32_'    THEN 'ROUND_OF_32'
      WHEN ts.slot_code ~ '^round_of_16_'    THEN 'ROUND_OF_16'
      WHEN ts.slot_code ~ '^quarter_final_'   THEN 'QUARTER_FINAL'
      WHEN ts.slot_code ~ '^semi_?final_'     THEN 'SEMI_FINAL'
    END AS inferred_stage_code,
    -- Extract the match number within the round
    (regexp_match(ts.slot_code, '^(?:round_of_(?:32|16)|quarter_final|semi_?final)_(\d+)_'))[1]::int AS match_rank
  FROM tournament_slots ts
  WHERE ts.slot_type IN ('WINNER', 'LOSER')
    AND ts.source_match_id IS NULL
),
ranked_matches AS (
  SELECT
    m.match_id,
    m.competition_season_id,
    cs.stage_code,
    ROW_NUMBER() OVER (
      PARTITION BY m.competition_season_id, cs.stage_code
      ORDER BY m.match_number ASC NULLS LAST, m.kickoff_at ASC
    ) AS match_rank
  FROM matches m
  JOIN competition_stages cs ON cs.stage_id = m.stage_id
  WHERE cs.stage_code IN ('ROUND_OF_32', 'ROUND_OF_16', 'QUARTER_FINAL', 'SEMI_FINAL')
)
UPDATE tournament_slots ts
SET source_match_id = rm.match_id
FROM slot_parse sp
JOIN ranked_matches rm
  ON rm.competition_season_id = sp.competition_season_id
 AND rm.stage_code = sp.inferred_stage_code
 AND rm.match_rank = sp.match_rank
WHERE ts.tournament_slot_id = sp.tournament_slot_id
  AND sp.inferred_stage_code IS NOT NULL
  AND sp.match_rank IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Set metadata.outcome for WINNER/LOSER slots (resolver reads this field)
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE tournament_slots
SET metadata = metadata || jsonb_build_object('outcome', 'WINNER')
WHERE slot_type = 'WINNER'
  AND (metadata->>'outcome') IS NULL;

UPDATE tournament_slots
SET metadata = metadata || jsonb_build_object('outcome', 'LOSER')
WHERE slot_type = 'LOSER'
  AND (metadata->>'outcome') IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- Verification queries (run manually to check):
-- ─────────────────────────────────────────────────────────────────────────────
--
-- SELECT slot_type, source_rank, count(*) FROM tournament_slots GROUP BY 1,2 ORDER BY 1,2;
-- SELECT slot_code, source_match_id, source_group_id, source_rank
--   FROM tournament_slots WHERE slot_type IN ('WINNER','LOSER') LIMIT 20;
-- SELECT slot_code, source_match_id, source_group_id, source_rank
--   FROM tournament_slots WHERE slot_type = 'BEST_THIRD' LIMIT 20;
