-- Verify expected stage count per competition season.
with expected as (
  select * from (
    values
      ('wc2026', 7),
      ('ucl-2026-2027', 6),
      ('premier-league-2026-2027', 1),
      ('chile-primera-2026', 1),
      ('libertadores-2026', 5),
      ('conmebol-qualifiers-wc2030', 1),
      ('uefa-qualifiers-wc2030', 2),
      ('concacaf-qualifiers-wc2030', 2),
      ('caf-qualifiers-wc2030', 2),
      ('afc-qualifiers-wc2030', 2),
      ('ofc-qualifiers-wc2030', 2)
  ) as v(season_slug, expected_stages)
),
actual as (
  select
    cs.slug as season_slug,
    count(st.stage_id)::int as actual_stages
  from competition_seasons cs
  left join competition_stages st on st.competition_season_id = cs.competition_season_id
  group by cs.slug
)
select
  e.season_slug,
  e.expected_stages,
  coalesce(a.actual_stages, 0) as actual_stages,
  (e.expected_stages - coalesce(a.actual_stages, 0)) as missing_stages,
  case
    when coalesce(a.actual_stages, 0) = e.expected_stages then 'OK'
    when coalesce(a.actual_stages, 0) = 0 then 'MISSING_ALL'
    else 'PARTIAL'
  end as check_status
from expected e
left join actual a on a.season_slug = e.season_slug
order by e.season_slug;
