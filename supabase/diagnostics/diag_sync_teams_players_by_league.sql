-- Phase 3 diagnostics: teams and players sync coverage by league (canonical schema)
-- Adjust min_players_per_team as needed.

with params as (
  select 11::int as min_players_per_team
),
api_leagues as (
  select
    cs.competition_season_id,
    cs.slug as league_slug
  from competition_seasons cs
),
team_entries as (
  select
    cte.competition_season_id,
    cte.team_id,
    coalesce(cte.metadata->>'external_id', '') as external_id
  from competition_team_entries cte
),
roster_counts as (
  select
    cr.competition_season_id,
    cr.team_id,
    count(*)::int as players_count
  from competition_rosters cr
  group by cr.competition_season_id, cr.team_id
),
league_coverage as (
  select
    l.league_slug,
    count(te.team_id)::int as teams_total,
    count(te.team_id) filter (where te.external_id <> '')::int as teams_with_external_id,
    count(te.team_id) filter (where coalesce(rc.players_count, 0) >= p.min_players_per_team)::int as teams_with_min_players,
    count(te.team_id) filter (where coalesce(rc.players_count, 0) < p.min_players_per_team)::int as teams_below_min_players,
    p.min_players_per_team
  from api_leagues l
  cross join params p
  left join team_entries te on te.competition_season_id = l.competition_season_id
  left join roster_counts rc
    on rc.competition_season_id = te.competition_season_id
   and rc.team_id = te.team_id
  group by l.league_slug, p.min_players_per_team
),
orphan_rosters as (
  select
    cs.slug as league_slug,
    count(*)::int as orphan_rosters
  from competition_rosters cr
  join competition_seasons cs on cs.competition_season_id = cr.competition_season_id
  left join competition_team_entries cte
    on cte.competition_season_id = cr.competition_season_id
   and cte.team_id = cr.team_id
  where cte.competition_team_entry_id is null
  group by cs.slug
)

-- Result 1: Executive summary by league
select
  lc.league_slug,
  case
    when lc.teams_total = 0 then 'WARN'
    when lc.teams_with_external_id < lc.teams_total then 'WARN'
    when lc.teams_below_min_players > 0 then 'WARN'
    when coalesce(o.orphan_rosters, 0) > 0 then 'WARN'
    else 'OK'
  end as status,
  lc.teams_total,
  lc.teams_with_external_id,
  lc.teams_with_min_players,
  lc.teams_below_min_players,
  coalesce(o.orphan_rosters, 0) as orphan_rosters,
  lc.min_players_per_team
from league_coverage lc
left join orphan_rosters o on o.league_slug = lc.league_slug
order by lc.league_slug;

-- Result 2: Teams below threshold (actionable list)
with params as (
  select 11::int as min_players_per_team
)
select
  cs.slug as league_slug,
  t.display_name as team_name,
  coalesce(count(cr.player_id), 0)::int as players_count,
  p.min_players_per_team
from competition_team_entries cte
join competition_seasons cs on cs.competition_season_id = cte.competition_season_id
join teams t on t.team_id = cte.team_id
cross join params p
left join competition_rosters cr
  on cr.competition_season_id = cte.competition_season_id
 and cr.team_id = cte.team_id
group by cs.slug, t.display_name, p.min_players_per_team
having coalesce(count(cr.player_id), 0)::int < p.min_players_per_team
order by cs.slug, players_count asc, t.display_name;

-- Result 3: Team entries missing external_id in metadata
select
  cs.slug as league_slug,
  t.display_name as team_name,
  cte.metadata
from competition_team_entries cte
join competition_seasons cs on cs.competition_season_id = cte.competition_season_id
join teams t on t.team_id = cte.team_id
where coalesce(cte.metadata->>'external_id', '') = ''
order by cs.slug, t.display_name;
