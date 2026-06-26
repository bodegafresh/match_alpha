-- BUG-05: Add generated column + index to pipeline_runs for fast idempotency lookups.
-- Without this, should_run_job does a full-table JSONB scan on every job check.

alter table pipeline_runs
  add column if not exists idempotency_key text
    generated always as (payload ->> 'idempotency_key') stored;

create index if not exists idx_pipeline_runs_idempotency
  on pipeline_runs (job_name, status, idempotency_key)
  where status in ('OK', 'WARN');

-- Supporting index for the latest_status and health queries
create index if not exists idx_pipeline_runs_started_at
  on pipeline_runs (started_at desc);
