-- Phase 1: Track which matches have had ELO ratings computed.
-- Runtime daily job processes ONLY unprocessed matches (incremental, scalable).
-- Bootstrap/repair uses rebuild_all_elo_history() which resets this flag.

alter table matches
  add column if not exists elo_processed boolean not null default false;

create index if not exists idx_matches_elo_unprocessed
  on matches (kickoff_at asc)
  where elo_processed = false and status = 'FINISHED';
