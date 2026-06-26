-- BUG-01: Prevent duplicate betting decisions for the same (prediction, odds snapshot) pair.
-- The ev_decision_job must be idempotent when called multiple times for the same window.

alter table betting_decisions
  add constraint if not exists uq_betting_decisions_prediction_odds
  unique (prediction_id, odds_snapshot_id);
