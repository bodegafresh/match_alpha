-- Phase 1: Add prediction_status, confidence_score, and explanation to model_predictions.
--
-- prediction_status: lifecycle state of the prediction
--   RAW_ONLY    = only raw_probability; no calibration applied
--   CALIBRATED  = calibrated_probability filled from calibration job
--   STALE       = calibration was applied but is now outdated
--   BLOCKED     = prediction could not be generated (missing features, etc.)
--
-- confidence_score: operational confidence 0.0-1.0 (NOT probability of outcome).
--   Factors: feature completeness, calibration quality, market liquidity, odds freshness.
--
-- explanation: fixed-contract JSON (see plan) for auditability and explainability.

do $$
begin
  if not exists (select 1 from pg_type where typname = 'prediction_status') then
    create type prediction_status as enum ('RAW_ONLY', 'CALIBRATED', 'STALE', 'BLOCKED');
  end if;
end $$;

alter table model_predictions
  add column if not exists prediction_status  prediction_status not null default 'RAW_ONLY',
  add column if not exists confidence_score   numeric
    check (confidence_score >= 0 and confidence_score <= 1),
  add column if not exists explanation        jsonb;

create index if not exists idx_model_predictions_status
  on model_predictions (prediction_status, as_of desc);
