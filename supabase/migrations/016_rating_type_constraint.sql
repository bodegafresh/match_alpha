-- Phase 1: Enforce valid rating_type values on rating_snapshots.
-- Valid types for Phase 1:
--   ELO_GLOBAL        — computed from all competitions
--   ELO_INTERNATIONAL — computed from international competitions only
--   ELO_DOMESTIC      — computed from domestic competitions only
--   ATTACK_STRENGTH   — normalized rolling attack strength (Phase 1)
--   DEFENSE_STRENGTH  — normalized rolling defense strength (Phase 1)

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'chk_rating_type_values'
      and conrelid = 'rating_snapshots'::regclass
  ) then
    alter table rating_snapshots
      add constraint chk_rating_type_values
      check (rating_type in (
        'ELO_GLOBAL', 'ELO_INTERNATIONAL', 'ELO_DOMESTIC',
        'ATTACK_STRENGTH', 'DEFENSE_STRENGTH'
      ));
  end if;
end $$;

-- Index for feature builder: get latest rating for team before kickoff
create index if not exists idx_rating_snapshots_team_type_as_of
  on rating_snapshots (team_id, rating_type, as_of desc);
