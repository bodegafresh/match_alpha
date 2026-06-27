-- Migration 019: add qualification_status to standings
-- Tracks the computed qualification outcome per team per group,
-- derived by the qualification resolver from finished match results.

alter table standings
  add column if not exists qualification_status text
    check (qualification_status in (
      'QUALIFIED_GROUP_WINNER',
      'QUALIFIED_GROUP_RUNNER_UP',
      'THIRD_PLACE_CANDIDATE',
      'QUALIFIED_BEST_THIRD',
      'ELIMINATED',
      'PENDING',
      'PENDING_TIEBREAKER'
    ));

-- Index for resolver queries filtering by status
create index if not exists idx_standings_qualification_status
  on standings (competition_season_id, qualification_status)
  where qualification_status is not null;

-- Index for quickly finding third-place candidates across all groups
create index if not exists idx_standings_third_candidates
  on standings (competition_season_id, position, points, goal_difference, goals_for)
  where position = 3;
