begin;

create unique index if not exists ux_standings_current_source on standings (
  competition_season_id,
  stage_id,
  group_id,
  team_id,
  source
);

commit;
