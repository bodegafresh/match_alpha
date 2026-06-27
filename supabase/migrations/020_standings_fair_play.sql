-- Migration 020: add fair_play_score to standings
-- fair_play_score: sum of card penalties per team in group phase
-- yellow = -1, direct red = -3, yellow+red = -4  (FIFA standard)
-- Less negative = better tiebreaker position

ALTER TABLE standings
  ADD COLUMN IF NOT EXISTS fair_play_score integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN standings.fair_play_score IS
  'Sum of FIFA fair play card penalties: yellow=-1, direct_red=-3, yellow+red=-4. Less negative = better.';
