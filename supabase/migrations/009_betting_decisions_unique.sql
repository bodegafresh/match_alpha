-- BUG-01: Prevent duplicate betting decisions for the same (prediction, odds snapshot) pair.
-- ADD CONSTRAINT IF NOT EXISTS is not valid PostgreSQL — use DO block instead.

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'uq_betting_decisions_prediction_odds'
      and conrelid = 'betting_decisions'::regclass
  ) then
    alter table betting_decisions
      add constraint uq_betting_decisions_prediction_odds
      unique (prediction_id, odds_snapshot_id);
  end if;
end
$$;
