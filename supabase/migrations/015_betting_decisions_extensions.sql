-- Phase 1: Extend betting_decisions with CLV tracking, multiple block reasons,
-- and PAPER_ONLY decision status.
--
-- clv_value: Closing Line Value = ln(odds_taken / reference_odds). Positive = good timing.
-- clv_source: which reference odds were used ('CLOSING' | 'LAST_AVAILABLE' | NULL if unavailable).
-- block_reasons: JSON array of reason codes e.g. ["LOW_LIQUIDITY", "NO_CALIBRATION", "ODDS_STALE"].
-- PAPER_ONLY: EV positive but real betting blocked (no calibration, low confidence, etc.)

alter table betting_decisions
  add column if not exists clv_value     numeric,
  add column if not exists clv_source    text
    check (clv_source in ('CLOSING', 'LAST_AVAILABLE') or clv_source is null),
  add column if not exists block_reasons jsonb;

-- Add PAPER_ONLY to decision_status enum (idempotent via DO block)
do $$
begin
  if not exists (
    select 1 from pg_enum
    where enumtypid = 'decision_status'::regtype and enumlabel = 'PAPER_ONLY'
  ) then
    alter type decision_status add value 'PAPER_ONLY';
  end if;
end $$;
