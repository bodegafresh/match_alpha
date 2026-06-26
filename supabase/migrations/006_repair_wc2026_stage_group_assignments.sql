begin;

with season as (
  select competition_season_id
  from competition_seasons
  where slug = 'wc2026'
),
group_stage as (
  select st.stage_id
  from competition_stages st
  join season s on s.competition_season_id = st.competition_season_id
  where st.stage_code = 'GROUP_STAGE'
),
team_groups as (
  select
    cte.team_id,
    cgm.group_id
  from competition_team_entries cte
  join competition_group_memberships cgm on cgm.competition_team_entry_id = cte.competition_team_entry_id
  join competition_groups cg on cg.group_id = cgm.group_id
  join season s on s.competition_season_id = cte.competition_season_id
  where cgm.membership_status = 'ACTIVE'
),
same_group_team_matches as (
  select
    m.match_id,
    tg_home.group_id
  from matches m
  join season s on s.competition_season_id = m.competition_season_id
  join match_participants home on home.match_id = m.match_id and home.side = 'HOME' and home.participant_role = 'TEAM'
  join match_participants away on away.match_id = m.match_id and away.side = 'AWAY' and away.participant_role = 'TEAM'
  join team_groups tg_home on tg_home.team_id = home.team_id
  join team_groups tg_away on tg_away.team_id = away.team_id and tg_away.group_id = tg_home.group_id
)
update matches m
set
  stage_id = group_stage.stage_id,
  group_id = same_group_team_matches.group_id,
  metadata = coalesce(m.metadata, '{}'::jsonb)
    || jsonb_build_object(
      'stage_group_repair', jsonb_build_object(
        'reason', 'both_real_teams_belong_to_same_competition_group',
        'repaired_at', now()
      )
    ),
  updated_at = now()
from same_group_team_matches, group_stage
where m.match_id = same_group_team_matches.match_id
  and (
    m.stage_id is distinct from group_stage.stage_id
    or m.group_id is distinct from same_group_team_matches.group_id
  );

with season as (
  select competition_season_id
  from competition_seasons
  where slug = 'wc2026'
),
slot_matches as (
  select
    m.match_id,
    lower(string_agg(coalesce(ts.slot_label, ts.slot_code, ''), ' ' order by mp.side)) as slot_text
  from matches m
  join season s on s.competition_season_id = m.competition_season_id
  join match_participants mp on mp.match_id = m.match_id and mp.participant_role = 'SLOT'
  join tournament_slots ts on ts.tournament_slot_id = mp.tournament_slot_id
  group by m.match_id
),
slot_target_stage as (
  select
    sm.match_id,
    case
      when sm.slot_text like '%semifinal%loser%' then 'THIRD_PLACE'
      when sm.slot_text like '%semifinal%winner%' then 'FINAL'
      when sm.slot_text like '%quarter%winner%' then 'SEMI_FINAL'
      when sm.slot_text like '%round of 16%winner%' or sm.slot_text like '%octav%winner%' then 'QUARTER_FINAL'
      when sm.slot_text like '%round of 32%winner%' or sm.slot_text like '%dieciseis%winner%' then 'ROUND_OF_16'
      when sm.slot_text like '%group%' or sm.slot_text like '%grupo%' or sm.slot_text like '%third place group%' then 'ROUND_OF_32'
      else null
    end as target_stage_code
  from slot_matches sm
),
resolved_slot_stages as (
  select
    sts.match_id,
    st.stage_id
  from slot_target_stage sts
  join season s on true
  join competition_stages st
    on st.competition_season_id = s.competition_season_id
   and st.stage_code = sts.target_stage_code
  where sts.target_stage_code is not null
)
update matches m
set
  stage_id = resolved_slot_stages.stage_id,
  group_id = null,
  metadata = coalesce(m.metadata, '{}'::jsonb)
    || jsonb_build_object(
      'stage_group_repair', jsonb_build_object(
        'reason', 'slot_participant_implies_knockout_stage',
        'repaired_at', now()
      )
    ),
  updated_at = now()
from resolved_slot_stages
where m.match_id = resolved_slot_stages.match_id
  and (
    m.stage_id is distinct from resolved_slot_stages.stage_id
    or m.group_id is not null
  );

with season as (
  select competition_season_id
  from competition_seasons
  where slug = 'wc2026'
),
slot_target_stage as (
  select
    ts.tournament_slot_id,
    case
      when lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%semifinal%loser%' then 'THIRD_PLACE'
      when lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%semifinal%winner%' then 'FINAL'
      when lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%quarter%winner%' then 'SEMI_FINAL'
      when lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%round of 16%winner%'
        or lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%octav%winner%' then 'QUARTER_FINAL'
      when lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%round of 32%winner%'
        or lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%dieciseis%winner%' then 'ROUND_OF_16'
      when lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%group%'
        or lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%grupo%'
        or lower(coalesce(ts.slot_label, '') || ' ' || coalesce(ts.slot_code, '')) like '%third place group%' then 'ROUND_OF_32'
      else null
    end as target_stage_code
  from tournament_slots ts
  join season s on s.competition_season_id = ts.competition_season_id
),
resolved_slot_stages as (
  select
    sts.tournament_slot_id,
    st.stage_id,
    st.stage_code
  from slot_target_stage sts
  join season s on true
  join competition_stages st
    on st.competition_season_id = s.competition_season_id
   and st.stage_code = sts.target_stage_code
  where sts.target_stage_code is not null
)
update tournament_slots ts
set
  stage_id = resolved_slot_stages.stage_id,
  metadata = coalesce(ts.metadata, '{}'::jsonb)
    || jsonb_build_object(
      'stage_group_repair', jsonb_build_object(
        'reason', 'slot_label_implies_knockout_stage',
        'stage_code', resolved_slot_stages.stage_code,
        'repaired_at', now()
      )
    ),
  updated_at = now()
from resolved_slot_stages
where ts.tournament_slot_id = resolved_slot_stages.tournament_slot_id
  and ts.stage_id is distinct from resolved_slot_stages.stage_id;

commit;

select
  st.stage_code,
  st.stage_name,
  count(*)::int as match_count,
  (st.rules->>'expected_matches')::int as expected_matches
from matches m
join competition_seasons cs on cs.competition_season_id = m.competition_season_id
left join competition_stages st on st.stage_id = m.stage_id
where cs.slug = 'wc2026'
group by st.stage_code, st.stage_name, st.rules
order by min(st.stage_order);
