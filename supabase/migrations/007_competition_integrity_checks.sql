create or replace view competition_integrity_issues as
with stage_counts as (
  select
    cs.slug as competition_season_slug,
    st.stage_code,
    st.stage_name,
    count(m.match_id)::int as actual_matches,
    nullif(st.rules->>'expected_matches', '')::int as expected_matches
  from competition_stages st
  join competition_seasons cs on cs.competition_season_id = st.competition_season_id
  left join matches m on m.stage_id = st.stage_id
  group by cs.slug, st.stage_code, st.stage_name, st.rules
),
same_group_misclassified as (
  select
    cs.slug as competition_season_slug,
    m.match_id,
    m.slug as match_slug,
    'SAME_GROUP_TEAM_MATCH_NOT_GROUP_STAGE'::text as issue_code,
    jsonb_build_object(
      'stage_code', st.stage_code,
      'home_team_id', home.team_id,
      'away_team_id', away.team_id,
      'group_id', tg_home.group_id
    ) as details
  from matches m
  join competition_seasons cs on cs.competition_season_id = m.competition_season_id
  left join competition_stages st on st.stage_id = m.stage_id
  join match_participants home on home.match_id = m.match_id and home.side = 'HOME' and home.participant_role = 'TEAM'
  join match_participants away on away.match_id = m.match_id and away.side = 'AWAY' and away.participant_role = 'TEAM'
  join competition_team_entries home_entry on home_entry.competition_season_id = m.competition_season_id and home_entry.team_id = home.team_id
  join competition_group_memberships tg_home on tg_home.competition_team_entry_id = home_entry.competition_team_entry_id
  join competition_team_entries away_entry on away_entry.competition_season_id = m.competition_season_id and away_entry.team_id = away.team_id
  join competition_group_memberships tg_away on tg_away.competition_team_entry_id = away_entry.competition_team_entry_id and tg_away.group_id = tg_home.group_id
  where coalesce(st.stage_code, '') <> 'GROUP_STAGE'
),
group_missing_group_id as (
  select
    cs.slug as competition_season_slug,
    m.match_id,
    m.slug as match_slug,
    'GROUP_STAGE_MATCH_WITHOUT_GROUP_ID'::text as issue_code,
    jsonb_build_object('stage_code', st.stage_code) as details
  from matches m
  join competition_seasons cs on cs.competition_season_id = m.competition_season_id
  join competition_stages st on st.stage_id = m.stage_id
  where st.stage_code = 'GROUP_STAGE'
    and m.group_id is null
),
stage_count_mismatch as (
  select
    competition_season_slug,
    null::uuid as match_id,
    null::text as match_slug,
    'STAGE_MATCH_COUNT_MISMATCH'::text as issue_code,
    jsonb_build_object(
      'stage_code', stage_code,
      'stage_name', stage_name,
      'actual_matches', actual_matches,
      'expected_matches', expected_matches
    ) as details
  from stage_counts
  where expected_matches is not null
    and actual_matches <> expected_matches
)
select * from same_group_misclassified
union all
select * from group_missing_group_id
union all
select * from stage_count_mismatch;

