-- Migration 020c: Reset incorrectly assigned BEST_THIRD slots
--
-- The previous greedy resolver assigned wrong teams to BEST_THIRD slots
-- (e.g. Sweden to Germany's slot, Paraguay to France's slot).
-- Reset them so the new constraint-propagation resolver can re-assign correctly.
--
-- Also resets the match_participants that were incorrectly updated so they
-- go back to SLOT role (waiting for correct team assignment).

-- 1. Reset tournament_slots for BEST_THIRD type
UPDATE tournament_slots
SET resolved_team_id = NULL,
    resolved_at      = NULL,
    updated_at       = now()
WHERE slot_type = 'BEST_THIRD'
  AND resolved_team_id IS NOT NULL;

-- 2. Reset match_participants that were promoted by the wrong BEST_THIRD resolution
--    (revert to SLOT role, clear team_id)
UPDATE match_participants mp
SET team_id          = NULL,
    participant_role  = 'SLOT',
    updated_at        = now()
FROM tournament_slots ts
WHERE mp.tournament_slot_id = ts.tournament_slot_id
  AND ts.slot_type = 'BEST_THIRD'
  AND mp.participant_role = 'TEAM'
  AND mp.team_id IS NOT NULL;

-- Verify:
-- SELECT slot_code, resolved_team_id FROM tournament_slots WHERE slot_type = 'BEST_THIRD';
-- Should all be NULL now.
