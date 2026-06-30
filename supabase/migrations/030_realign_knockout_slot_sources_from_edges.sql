-- Migration 030: Realign knockout WINNER/LOSER slots with bracket edges
--
-- Why:
--   Historical migrations linked source_match_id using slot_code + ordinal rank.
--   If match numbering/order drifted, slots can point to the wrong source match,
--   causing swapped teams in downstream rounds (e.g. Octavos placeholders).
--
-- What this does:
--   1) Recomputes expected source_match_id from knockout_bracket_edges + to_side.
--   2) Updates tournament_slots.source_match_id for WINNER/LOSER slots when mismatched.
--   3) Clears stale resolved_team_id for changed slots.
--   4) Reverts affected match_participants back to SLOT so qualification_resolver
--      can repopulate teams with the corrected source mapping.

begin;

with edge_mapping as (
  select
    ts.tournament_slot_id,
    ts.competition_season_id,
    ts.slot_code,
    ts.source_match_id as old_source_match_id,
    kbe.from_match_id as expected_source_match_id
  from tournament_slots ts
  join match_participants mp
    on mp.tournament_slot_id = ts.tournament_slot_id
  join matches to_match
    on to_match.match_id = mp.match_id
   and to_match.competition_season_id = ts.competition_season_id
  join knockout_bracket_edges kbe
    on kbe.competition_season_id = ts.competition_season_id
   and kbe.to_match_id = mp.match_id
   and kbe.to_side = mp.side
   and kbe.outcome = coalesce(ts.metadata->>'outcome', ts.slot_type)
  where ts.slot_type in ('WINNER', 'LOSER')
),
changed_slots as (
  update tournament_slots ts
  set source_match_id = em.expected_source_match_id,
      resolved_team_id = null,
      resolved_at = null,
      metadata = coalesce(ts.metadata, '{}'::jsonb)
        || jsonb_build_object(
          'slot_source_repair',
          jsonb_build_object(
            'old_source_match_id', em.old_source_match_id,
            'new_source_match_id', em.expected_source_match_id,
            'repaired_at', now()
          )
        ),
      updated_at = now()
  from edge_mapping em
  where ts.tournament_slot_id = em.tournament_slot_id
    and ts.source_match_id is distinct from em.expected_source_match_id
  returning ts.tournament_slot_id
)
update match_participants mp
set team_id = null,
    participant_role = 'SLOT',
    updated_at = now()
where mp.tournament_slot_id in (select tournament_slot_id from changed_slots)
  and mp.participant_role = 'TEAM';

commit;

-- Optional checks:
-- 1) Slots should now align with edges:
-- select
--   ts.slot_code,
--   ts.source_match_id,
--   kbe.from_match_id as edge_source_match_id,
--   ts.resolved_team_id
-- from tournament_slots ts
-- join match_participants mp on mp.tournament_slot_id = ts.tournament_slot_id
-- join knockout_bracket_edges kbe
--   on kbe.competition_season_id = ts.competition_season_id
--  and kbe.to_match_id = mp.match_id
--  and kbe.to_side = mp.side
--  and kbe.outcome = coalesce(ts.metadata->>'outcome', ts.slot_type)
-- where ts.slot_type in ('WINNER', 'LOSER')
-- order by ts.slot_code;
--
-- 2) Re-run job: qualification_resolver
--    to fill cleared SLOT participants with corrected winners/losers.
