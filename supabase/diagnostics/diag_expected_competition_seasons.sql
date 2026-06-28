-- Verify expected competition_seasons are present for all configured competitions.
with expected(slug) as (
  values
    ('wc2026'),
    ('ucl-2026-2027'),
    ('premier-league-2026-2027'),
    ('chile-primera-2026'),
    ('libertadores-2026'),
    ('conmebol-qualifiers-wc2030'),
    ('uefa-qualifiers-wc2030'),
    ('concacaf-qualifiers-wc2030'),
    ('caf-qualifiers-wc2030'),
    ('afc-qualifiers-wc2030'),
    ('ofc-qualifiers-wc2030')
)
select
  e.slug as expected_slug,
  cs.competition_season_id,
  cs.competition_id,
  cs.status,
  cs.starts_at,
  cs.ends_at,
  case when cs.competition_season_id is null then 'MISSING' else 'OK' end as check_status
from expected e
left join competition_seasons cs on cs.slug = e.slug
order by e.slug;
