-- Migration 019: Update published views to expose quant fields added in migrations 014-015.
--
-- published_ev_opportunities: adds match context, model probability, raw fields,
--   confidence_score, prediction_status, block_reasons, odds_age_minutes.
--   Also surfaces PAPER_ONLY decisions (not just BETTABLE).
--
-- published_blocked_decisions: adds block_reasons JSONB and match context.
--
-- published_match_predictions: adds prediction_status, confidence_score, explanation.

-- Drop and recreate views (idempotent: always reflects current schema)

drop view if exists published_ev_opportunities;
create view published_ev_opportunities as
select
  bd.betting_decision_id,
  bd.competition_season_id,
  bd.match_id,
  bd.decision_status,
  bd.risk_level,
  bd.block_reason,
  coalesce(bd.block_reasons, '[]'::jsonb) as block_reasons,
  bd.edge,
  bd.ev,
  bd.kelly_fraction,
  bd.stake_fraction,
  bd.calibrated_probability_used  as model_probability,
  bd.market_probability,
  bd.decided_at,
  m.market_code,
  s.selection_code,
  p.raw_probability,
  p.calibrated_probability,
  p.prediction_status,
  p.confidence_score,
  p.explanation,
  os.decimal_odds,
  os.implied_probability          as market_implied_probability,
  os.captured_at                  as odds_captured_at,
  extract(epoch from (now() - os.captured_at)) / 60 as odds_age_minutes,
  match.kickoff_at,
  match.status                    as match_status,
  home_team.display_name          as home_team_name,
  home_team.country_code          as home_country_code,
  home_country.flag_emoji         as home_flag_emoji,
  away_team.display_name          as away_team_name,
  away_team.country_code          as away_country_code,
  away_country.flag_emoji         as away_flag_emoji,
  reg.model_name,
  reg.model_version,
  reg.model_family
from betting_decisions bd
join model_predictions p on p.prediction_id = bd.prediction_id
join odds_snapshots os on os.odds_snapshot_id = bd.odds_snapshot_id
join markets m on m.market_id = p.market_id
join market_selections s on s.selection_id = p.selection_id
join matches match on match.match_id = bd.match_id
left join match_participants home_mp on home_mp.match_id = bd.match_id and home_mp.side = 'HOME'
left join teams home_team on home_team.team_id = home_mp.team_id
left join countries home_country on home_country.code_alpha2 = home_team.country_code
left join match_participants away_mp on away_mp.match_id = bd.match_id and away_mp.side = 'AWAY'
left join teams away_team on away_team.team_id = away_mp.team_id
left join countries away_country on away_country.code_alpha2 = away_team.country_code
join model_runs mr on mr.model_run_id = p.model_run_id
join model_registry reg on reg.model_id = mr.model_id
where bd.decision_status in ('BETTABLE', 'PAPER_ONLY')
  and bd.ev > 0;


drop view if exists published_blocked_decisions;
create view published_blocked_decisions as
select
  bd.betting_decision_id,
  bd.competition_season_id,
  bd.match_id,
  bd.decision_status,
  bd.risk_level,
  bd.block_reason,
  coalesce(bd.block_reasons, '[]'::jsonb) as block_reasons,
  bd.edge,
  bd.ev,
  bd.decided_at,
  m.market_code,
  s.selection_code,
  p.raw_probability,
  p.calibrated_probability,
  p.prediction_status,
  p.confidence_score,
  match.kickoff_at,
  home_team.display_name          as home_team_name,
  home_country.flag_emoji         as home_flag_emoji,
  away_team.display_name          as away_team_name,
  away_country.flag_emoji         as away_flag_emoji
from betting_decisions bd
join model_predictions p on p.prediction_id = bd.prediction_id
join markets m on m.market_id = p.market_id
join market_selections s on s.selection_id = p.selection_id
join matches match on match.match_id = bd.match_id
left join match_participants home_mp on home_mp.match_id = bd.match_id and home_mp.side = 'HOME'
left join teams home_team on home_team.team_id = home_mp.team_id
left join countries home_country on home_country.code_alpha2 = home_team.country_code
left join match_participants away_mp on away_mp.match_id = bd.match_id and away_mp.side = 'AWAY'
left join teams away_team on away_team.team_id = away_mp.team_id
left join countries away_country on away_country.code_alpha2 = away_team.country_code
where bd.decision_status = 'BLOCKED';


drop view if exists published_match_predictions;
create view published_match_predictions as
select
  p.prediction_id,
  p.match_id,
  m.market_code,
  s.selection_code,
  p.line,
  p.raw_probability,
  p.calibrated_probability,
  p.fair_odds,
  p.prediction_status,
  p.confidence_score,
  p.explanation,
  p.as_of,
  mr.model_run_id,
  reg.model_name,
  reg.model_version,
  reg.model_family,
  p.flags
from model_predictions p
join model_runs mr on mr.model_run_id = p.model_run_id
join model_registry reg on reg.model_id = mr.model_id
join markets m on m.market_id = p.market_id
join market_selections s on s.selection_id = p.selection_id;
