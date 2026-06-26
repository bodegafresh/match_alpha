-- CLEAN SCHEMA - Quantitative multi-competition football platform
-- New project only. No legacy columns.
-- All timestamps are timestamptz and must be written as UTC.

begin;

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------------

create type entity_type as enum (
  'COMPETITION',
  'COMPETITION_SEASON',
  'TEAM',
  'PLAYER',
  'VENUE',
  'REFEREE',
  'MATCH',
  'BOOKMAKER',
  'MODEL',
  'OTHER'
);

create type team_type as enum ('CLUB', 'NATIONAL_TEAM', 'OTHER');
create type gender_type as enum ('MEN', 'WOMEN', 'MIXED', 'UNKNOWN');
create type competition_type as enum ('LEAGUE', 'CUP', 'TOURNAMENT', 'FRIENDLY', 'QUALIFIER', 'OTHER');
create type season_status as enum ('SCHEDULED', 'ACTIVE', 'COMPLETED', 'CANCELLED');
create type competition_status_type as enum ('OBSERVATION', 'PAPER_TRADING', 'BETTABLE', 'DISABLED');
create type readiness_check_status as enum ('PASS', 'WARN', 'FAIL', 'NOT_APPLICABLE');
create type stage_type as enum ('GROUP_STAGE', 'LEAGUE_PHASE', 'KNOCKOUT', 'PLAYOFF', 'QUALIFIER', 'FINAL', 'THIRD_PLACE', 'OTHER');
create type group_membership_status as enum ('ACTIVE', 'QUALIFIED', 'ELIMINATED', 'WITHDRAWN', 'UNKNOWN');
create type participant_side as enum ('HOME', 'AWAY');
create type match_status as enum ('SCHEDULED', 'LIVE', 'POSTPONED', 'CANCELLED', 'FINISHED', 'ABANDONED');
create type match_participant_role as enum ('TEAM', 'SLOT');
create type lineup_role as enum ('STARTER', 'SUBSTITUTE', 'RESERVE', 'UNKNOWN');
create type official_role as enum ('REFEREE', 'ASSISTANT_REFEREE', 'VAR', 'FOURTH_OFFICIAL', 'OTHER');
create type market_category as enum ('1X2', 'OVER_UNDER', 'BTTS', 'HANDICAP', 'CARDS', 'CORNERS', 'OTHER');
create type betting_decision_status as enum ('BETTABLE', 'PAPER_ONLY', 'BLOCKED', 'NO_EDGE', 'WATCHLIST');
create type bet_mode as enum ('REAL', 'PAPER');
create type bet_status as enum ('OPEN', 'WON', 'LOST', 'VOID', 'CASHED_OUT', 'CANCELLED');
create type betting_decision_settlement_status as enum ('PENDING', 'SETTLED', 'VOID', 'NOT_APPLICABLE');
create type risk_level as enum ('LOW', 'MEDIUM', 'HIGH', 'EXTREME');
create type model_run_status as enum ('STARTED', 'SUCCEEDED', 'FAILED', 'CANCELLED');
create type calibration_method as enum ('NONE', 'PLATT', 'ISOTONIC', 'BETA', 'ENSEMBLE');
create type severity_level as enum ('INFO', 'WARN', 'ERROR', 'CRITICAL');
create type raw_payload_status as enum ('RECEIVED', 'PARSED', 'REJECTED', 'PROMOTED');
create type entity_resolution_status as enum ('OPEN', 'IN_REVIEW', 'RESOLVED', 'REJECTED', 'IGNORED');

-- ---------------------------------------------------------------------------
-- Utility
-- ---------------------------------------------------------------------------

create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- RAW layer
-- ---------------------------------------------------------------------------

create table raw_source_files (
  raw_file_id uuid primary key default gen_random_uuid(),
  source text not null,
  source_file_name text,
  source_uri text,
  content_hash text not null,
  content_type text,
  received_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb,
  unique (source, content_hash)
);

create table raw_api_calls (
  raw_api_call_id uuid primary key default gen_random_uuid(),
  source text not null,
  endpoint text not null,
  request_hash text not null,
  request_payload jsonb not null default '{}'::jsonb,
  response_status integer,
  response_hash text,
  called_at timestamptz not null default now(),
  latency_ms integer,
  payload jsonb not null default '{}'::jsonb,
  unique (source, request_hash, called_at)
);

create table raw_source_payloads (
  raw_payload_id uuid primary key default gen_random_uuid(),
  source text not null,
  source_entity_type text not null,
  source_entity_id text,
  raw_file_id uuid references raw_source_files(raw_file_id) on delete set null,
  raw_api_call_id uuid references raw_api_calls(raw_api_call_id) on delete set null,
  payload_hash text not null,
  payload jsonb not null,
  status raw_payload_status not null default 'RECEIVED',
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  unique (source, source_entity_type, payload_hash)
);

-- ---------------------------------------------------------------------------
-- STAGING layer
-- ---------------------------------------------------------------------------

create table stg_teams (
  stg_team_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_team_id text,
  source_team_name text not null,
  normalized_name text,
  team_type team_type,
  country_code text,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

create table stg_players (
  stg_player_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_player_id text,
  source_player_name text not null,
  normalized_name text,
  birth_date date,
  nationality_country_code text,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

create table stg_matches (
  stg_match_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_match_id text,
  source_competition_id text,
  source_season text,
  kickoff_at timestamptz,
  home_team_name text,
  away_team_name text,
  venue_name text,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

create table stg_odds (
  stg_odds_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_match_id text,
  bookmaker_name text,
  market text,
  selection text,
  line numeric,
  decimal_odds numeric,
  captured_at timestamptz,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

create table stg_rosters (
  stg_roster_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_team_id text,
  source_player_id text,
  source_player_name text,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

create table stg_lineups (
  stg_lineup_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_match_id text,
  source_team_id text,
  source_player_id text,
  lineup_role lineup_role,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

create table stg_events (
  stg_event_id uuid primary key default gen_random_uuid(),
  raw_payload_id uuid references raw_source_payloads(raw_payload_id) on delete set null,
  source text not null,
  source_match_id text,
  source_event_id text,
  event_type text,
  minute integer,
  parsed_payload jsonb not null default '{}'::jsonb,
  validation_errors jsonb not null default '[]'::jsonb,
  parsed_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- CANONICAL layer
-- ---------------------------------------------------------------------------

create table countries (
  code_alpha2 text primary key,
  code_alpha3 text not null unique,
  numeric_code text not null unique,
  fifa_code text,
  ioc_code text,
  default_name text not null,
  names jsonb not null,
  official_names jsonb,
  region text,
  subregion text,
  continent text,
  timezone_default text,
  timezones text[],
  currency_code text,
  currency_name text,
  flag_emoji text,
  is_fifa_member boolean,
  is_sovereign boolean,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (code_alpha2 = upper(code_alpha2) and code_alpha2 ~ '^[A-Z]{2}$'),
  check (code_alpha3 = upper(code_alpha3) and code_alpha3 ~ '^[A-Z]{3}$'),
  check (numeric_code ~ '^[0-9]{3}$'),
  check (fifa_code is null or (fifa_code = upper(fifa_code) and fifa_code ~ '^[A-Z0-9]{3}$')),
  check (ioc_code is null or (ioc_code = upper(ioc_code) and ioc_code ~ '^[A-Z0-9]{3}$')),
  check (jsonb_typeof(names) = 'object'),
  check (official_names is null or jsonb_typeof(official_names) = 'object'),
  check (timezone_default is null or timezones is null or timezone_default = any(timezones)),
  check (currency_code is null or (currency_code = upper(currency_code) and currency_code ~ '^[A-Z]{3}$'))
);

create table competitions (
  competition_id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  display_name text not null,
  competition_type competition_type not null,
  country_code text references countries(code_alpha2) on update cascade on delete restrict,
  region text,
  tier integer,
  is_international boolean not null default false,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table competition_seasons (
  competition_season_id uuid primary key default gen_random_uuid(),
  competition_id uuid not null references competitions(competition_id) on delete cascade,
  slug text not null unique,
  season_label text not null,
  starts_at timestamptz,
  ends_at timestamptz,
  timezone_name text,
  status season_status not null default 'SCHEDULED',
  format_code text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table competition_status (
  competition_season_id uuid primary key references competition_seasons(competition_season_id) on delete cascade,
  status competition_status_type not null default 'OBSERVATION',
  status_reason text,
  readiness_score numeric check (readiness_score is null or readiness_score between 0 and 1),
  min_data_at timestamptz,
  approved_at timestamptz,
  approved_by text,
  updated_at timestamptz not null default now()
);

create table competition_readiness_checks (
  readiness_check_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  check_name text not null,
  status readiness_check_status not null,
  score numeric check (score is null or score between 0 and 1),
  details jsonb not null default '{}'::jsonb,
  checked_at timestamptz not null default now(),
  unique (competition_season_id, check_name)
);

create table competition_stages (
  stage_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  stage_code text not null,
  stage_name text not null,
  stage_order integer not null,
  stage_type stage_type not null,
  starts_at timestamptz,
  ends_at timestamptz,
  rules jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (competition_season_id, stage_code)
);

create table competition_groups (
  group_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  stage_id uuid not null references competition_stages(stage_id) on delete cascade,
  group_code text not null,
  group_name text not null,
  group_order integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (competition_season_id, stage_id, group_code)
);

create table teams (
  team_id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  team_type team_type not null,
  display_name text not null,
  normalized_name text not null,
  country_code text references countries(code_alpha2) on update cascade on delete restrict,
  gender gender_type not null default 'UNKNOWN',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table team_aliases (
  team_alias_id uuid primary key default gen_random_uuid(),
  team_id uuid not null references teams(team_id) on delete cascade,
  alias text not null,
  normalized_alias text not null,
  language_code text,
  source text not null default 'manual',
  confidence numeric not null default 1 check (confidence between 0 and 1),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (normalized_alias, source)
);

create table competition_team_entries (
  competition_team_entry_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  entry_status group_membership_status not null default 'ACTIVE',
  seed_rating numeric,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (competition_season_id, team_id)
);

create table competition_group_memberships (
  group_membership_id uuid primary key default gen_random_uuid(),
  group_id uuid not null references competition_groups(group_id) on delete cascade,
  competition_team_entry_id uuid not null references competition_team_entries(competition_team_entry_id) on delete cascade,
  membership_status group_membership_status not null default 'ACTIVE',
  seed_position integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (group_id, competition_team_entry_id)
);

create table tournament_slots (
  tournament_slot_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  stage_id uuid references competition_stages(stage_id) on delete set null,
  slot_code text not null,
  slot_label text not null,
  slot_type text not null,
  source_stage_id uuid references competition_stages(stage_id) on delete set null,
  source_group_id uuid references competition_groups(group_id) on delete set null,
  source_match_id uuid,
  source_rank integer,
  resolved_team_id uuid references teams(team_id) on delete set null,
  resolved_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (competition_season_id, slot_code)
);

create table knockout_bracket_edges (
  bracket_edge_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  from_match_id uuid not null,
  to_match_id uuid not null,
  outcome text not null check (outcome in ('WINNER', 'LOSER')),
  to_side participant_side not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (from_match_id <> to_match_id),
  unique (from_match_id, outcome),
  unique (to_match_id, to_side)
);

create table players (
  player_id uuid primary key default gen_random_uuid(),
  slug text unique,
  display_name text not null,
  normalized_name text not null,
  birth_date date,
  nationality_country_code text references countries(code_alpha2) on update cascade on delete restrict,
  gender gender_type not null default 'UNKNOWN',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table player_aliases (
  player_alias_id uuid primary key default gen_random_uuid(),
  player_id uuid not null references players(player_id) on delete cascade,
  alias text not null,
  normalized_alias text not null,
  language_code text,
  source text not null default 'manual',
  confidence numeric not null default 1 check (confidence between 0 and 1),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (normalized_alias, source)
);

create table team_memberships (
  team_membership_id uuid primary key default gen_random_uuid(),
  player_id uuid not null references players(player_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  membership_type text not null check (membership_type in ('CLUB', 'NATIONAL_TEAM', 'LOAN', 'OTHER')),
  valid_from_at timestamptz,
  valid_to_at timestamptz,
  source text,
  confidence numeric not null default 1 check (confidence between 0 and 1),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (player_id, team_id, membership_type, source),
  check (valid_to_at is null or valid_from_at is null or valid_to_at >= valid_from_at)
);

create table competition_rosters (
  competition_roster_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  player_id uuid not null references players(player_id) on delete cascade,
  shirt_number integer,
  position text,
  roster_status text not null default 'UNKNOWN' check (roster_status in ('CALLED_UP', 'ACTIVE', 'INJURED', 'CUT', 'UNKNOWN')),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (competition_season_id, team_id, player_id)
);

create table venues (
  venue_id uuid primary key default gen_random_uuid(),
  slug text unique,
  display_name text not null,
  city text,
  country_code text references countries(code_alpha2) on update cascade on delete restrict,
  timezone_name text,
  latitude numeric,
  longitude numeric,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table referees (
  referee_id uuid primary key default gen_random_uuid(),
  slug text unique,
  display_name text not null,
  normalized_name text not null,
  nationality_country_code text references countries(code_alpha2) on update cascade on delete restrict,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table matches (
  match_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  stage_id uuid references competition_stages(stage_id) on delete set null,
  group_id uuid references competition_groups(group_id) on delete set null,
  venue_id uuid references venues(venue_id) on delete set null,
  slug text unique,
  match_number integer,
  kickoff_at timestamptz not null,
  status match_status not null default 'SCHEDULED',
  is_neutral boolean not null default false,
  home_score integer,
  away_score integer,
  winner_team_id uuid references teams(team_id) on delete set null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (competition_season_id, match_id)
);

alter table tournament_slots
  add constraint tournament_slots_source_match_fk
  foreign key (source_match_id) references matches(match_id) on delete set null;

alter table knockout_bracket_edges
  add constraint knockout_bracket_edges_from_match_fk
  foreign key (competition_season_id, from_match_id) references matches(competition_season_id, match_id) on delete cascade,
  add constraint knockout_bracket_edges_to_match_fk
  foreign key (competition_season_id, to_match_id) references matches(competition_season_id, match_id) on delete cascade;

create table match_participants (
  match_participant_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  side participant_side not null,
  participant_role match_participant_role not null,
  team_id uuid references teams(team_id) on delete set null,
  tournament_slot_id uuid references tournament_slots(tournament_slot_id) on delete set null,
  is_home_designation boolean not null default false,
  score integer,
  penalty_score integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (match_id, side),
  check (
    (participant_role = 'TEAM' and team_id is not null)
    or
    (participant_role = 'SLOT' and tournament_slot_id is not null)
  )
);

create table match_lineups (
  match_lineup_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  player_id uuid not null references players(player_id) on delete cascade,
  lineup_role lineup_role not null default 'UNKNOWN',
  position text,
  shirt_number integer,
  is_captain boolean not null default false,
  source text not null default 'unknown',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (match_id, team_id, player_id, source)
);

create table match_events (
  match_event_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  team_id uuid references teams(team_id) on delete set null,
  player_id uuid references players(player_id) on delete set null,
  related_player_id uuid references players(player_id) on delete set null,
  event_type text not null,
  event_detail text,
  minute integer,
  stoppage_minute integer,
  occurred_at timestamptz,
  source text,
  source_event_id text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (source, source_event_id)
);

create table match_officials (
  match_official_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  referee_id uuid not null references referees(referee_id) on delete cascade,
  role official_role not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (match_id, referee_id, role)
);

create table player_match_stats (
  player_match_stat_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  player_id uuid not null references players(player_id) on delete cascade,
  stat_name text not null,
  stat_value numeric,
  source text not null default 'unknown',
  captured_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb,
  unique (match_id, player_id, stat_name, source)
);

create table standings (
  standing_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  stage_id uuid references competition_stages(stage_id) on delete cascade,
  group_id uuid references competition_groups(group_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  position integer,
  played integer,
  wins integer,
  draws integer,
  losses integer,
  goals_for integer,
  goals_against integer,
  goal_difference integer,
  points numeric,
  as_of timestamptz not null,
  source text,
  payload jsonb not null default '{}'::jsonb
);

create table entity_external_refs (
  entity_external_ref_id uuid primary key default gen_random_uuid(),
  entity_type entity_type not null,
  entity_id uuid not null,
  source text not null,
  source_entity_type text,
  source_entity_id text not null,
  source_entity_name text,
  source_url text,
  confidence numeric not null default 1 check (confidence between 0 and 1),
  is_primary boolean not null default false,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (entity_type, source, source_entity_id)
);

create table entity_media_assets (
  entity_media_asset_id uuid primary key default gen_random_uuid(),
  entity_type entity_type not null,
  entity_id uuid not null,
  media_type text not null check (media_type in ('FLAG', 'LOGO', 'CREST', 'PHOTO', 'ICON', 'VENUE_IMAGE', 'OTHER')),
  source text not null,
  url text not null,
  is_primary boolean not null default false,
  width integer,
  height integer,
  mime_type text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (entity_type, entity_id, media_type, source)
);

create table entity_resolution_queue (
  entity_resolution_id uuid primary key default gen_random_uuid(),
  entity_type entity_type not null,
  source text not null,
  source_entity_type text,
  source_entity_id text not null default '',
  source_entity_name text not null,
  normalized_name text not null,
  resolution_status entity_resolution_status not null default 'OPEN',
  candidate_entities jsonb not null default '[]'::jsonb,
  resolved_entity_id uuid,
  resolved_external_ref_id uuid references entity_external_refs(entity_external_ref_id) on delete set null,
  resolution_reason text,
  assigned_to text,
  opened_at timestamptz not null default now(),
  resolved_at timestamptz,
  payload jsonb not null default '{}'::jsonb,
  unique (entity_type, source, source_entity_id, normalized_name)
);

-- ---------------------------------------------------------------------------
-- ANALYTICS layer: market, features, models, calibration, EV, bankroll
-- ---------------------------------------------------------------------------

create table bookmaker_profiles (
  bookmaker_id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  display_name text not null,
  region text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table markets (
  market_id uuid primary key default gen_random_uuid(),
  market_code text not null unique,
  display_name text not null,
  category market_category not null,
  is_active boolean not null default true,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (market_code = upper(market_code))
);

create table market_selections (
  selection_id uuid primary key default gen_random_uuid(),
  market_id uuid not null references markets(market_id) on delete cascade,
  selection_code text not null,
  display_name text not null,
  sort_order integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (market_id, selection_code),
  unique (market_id, selection_id),
  check (selection_code = upper(selection_code))
);

create table odds_snapshots (
  odds_snapshot_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  bookmaker_id uuid references bookmaker_profiles(bookmaker_id) on delete set null,
  source text not null,
  source_snapshot_id text,
  market_id uuid not null references markets(market_id) on delete restrict,
  selection_id uuid not null,
  line numeric,
  decimal_odds numeric not null check (decimal_odds > 1),
  implied_probability numeric generated always as (1 / decimal_odds) stored,
  captured_at timestamptz not null,
  payload jsonb not null default '{}'::jsonb,
  foreign key (market_id, selection_id) references market_selections(market_id, selection_id) on delete restrict
);

create table market_closing_odds (
  market_closing_odds_id uuid primary key default gen_random_uuid(),
  match_id uuid not null references matches(match_id) on delete cascade,
  market_id uuid not null references markets(market_id) on delete restrict,
  selection_id uuid not null,
  line numeric,
  closing_decimal_odds numeric not null check (closing_decimal_odds > 1),
  closing_snapshot_id uuid references odds_snapshots(odds_snapshot_id) on delete set null,
  closed_at timestamptz not null,
  method text,
  payload jsonb not null default '{}'::jsonb,
  foreign key (market_id, selection_id) references market_selections(market_id, selection_id) on delete restrict
);

create table market_quality_snapshots (
  market_quality_snapshot_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  match_id uuid references matches(match_id) on delete cascade,
  market_id uuid not null references markets(market_id) on delete restrict,
  bookmaker_count integer,
  liquidity_tier text check (liquidity_tier in ('HIGH', 'MEDIUM', 'LOW', 'UNUSABLE')),
  market_quality_score numeric check (market_quality_score is null or market_quality_score between 0 and 1),
  odds_volatility numeric,
  captured_at timestamptz not null,
  payload jsonb not null default '{}'::jsonb
);

create table feature_definitions (
  feature_definition_id uuid primary key default gen_random_uuid(),
  feature_name text not null unique,
  feature_set_version text not null,
  description text,
  valid_contexts text[] not null default '{}',
  owner text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table feature_snapshots (
  feature_snapshot_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  match_id uuid references matches(match_id) on delete cascade,
  team_id uuid references teams(team_id) on delete cascade,
  feature_set_version text not null,
  as_of timestamptz not null,
  features jsonb not null,
  source_hash text
);

create table rating_snapshots (
  rating_snapshot_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid references competition_seasons(competition_season_id) on delete cascade,
  team_id uuid not null references teams(team_id) on delete cascade,
  rating_type text not null,
  rating_value numeric not null,
  as_of timestamptz not null,
  payload jsonb not null default '{}'::jsonb
);

create table model_registry (
  model_id uuid primary key default gen_random_uuid(),
  model_name text not null,
  model_version text not null,
  model_family text not null,
  champion_status text check (champion_status in ('CHAMPION', 'CHALLENGER', 'ARCHIVED', 'EXPERIMENTAL')),
  training_code_ref text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (model_name, model_version)
);

create table model_runs (
  model_run_id uuid primary key default gen_random_uuid(),
  model_id uuid not null references model_registry(model_id) on delete restrict,
  competition_season_id uuid references competition_seasons(competition_season_id) on delete cascade,
  market_id uuid not null references markets(market_id) on delete restrict,
  run_status model_run_status not null default 'STARTED',
  training_window_start_at timestamptz,
  training_window_end_at timestamptz,
  prediction_as_of timestamptz not null,
  feature_set_version text,
  dataset_version text,
  git_sha text,
  params jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  error_message text,
  unique (model_id, competition_season_id, market_id, prediction_as_of)
);

create table model_predictions (
  prediction_id uuid primary key default gen_random_uuid(),
  model_run_id uuid not null references model_runs(model_run_id) on delete cascade,
  feature_snapshot_id uuid references feature_snapshots(feature_snapshot_id) on delete set null,
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  match_id uuid not null references matches(match_id) on delete cascade,
  market_id uuid not null references markets(market_id) on delete restrict,
  selection_id uuid not null,
  line numeric,
  raw_probability numeric not null check (raw_probability >= 0 and raw_probability <= 1),
  calibrated_probability numeric check (calibrated_probability is null or calibrated_probability between 0 and 1),
  fair_odds numeric,
  as_of timestamptz not null,
  flags text[] not null default '{}',
  payload jsonb not null default '{}'::jsonb,
  foreign key (market_id, selection_id) references market_selections(market_id, selection_id) on delete restrict
);

create table calibration_runs (
  calibration_run_id uuid primary key default gen_random_uuid(),
  model_id uuid references model_registry(model_id) on delete set null,
  competition_season_id uuid references competition_seasons(competition_season_id) on delete cascade,
  market_id uuid not null references markets(market_id) on delete restrict,
  stage_type stage_type,
  method calibration_method not null,
  sample_size integer not null default 0,
  train_start_at timestamptz,
  train_end_at timestamptz,
  brier_score numeric,
  log_loss numeric,
  ece numeric,
  sharpness numeric,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table calibration_bins (
  calibration_bin_id uuid primary key default gen_random_uuid(),
  calibration_run_id uuid not null references calibration_runs(calibration_run_id) on delete cascade,
  bin_lower numeric not null,
  bin_upper numeric not null,
  predicted_mean numeric,
  observed_rate numeric,
  sample_size integer not null default 0,
  ci_lower numeric,
  ci_upper numeric,
  check (bin_lower >= 0 and bin_upper <= 1 and bin_upper >= bin_lower)
);

create table model_metrics (
  model_metric_id uuid primary key default gen_random_uuid(),
  model_id uuid references model_registry(model_id) on delete set null,
  competition_season_id uuid references competition_seasons(competition_season_id) on delete cascade,
  market_id uuid references markets(market_id) on delete set null,
  metric_name text not null,
  metric_value numeric,
  sample_size integer,
  calculated_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb
);

create table drift_reports (
  drift_report_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid references competition_seasons(competition_season_id) on delete cascade,
  model_id uuid references model_registry(model_id) on delete set null,
  feature_set_version text,
  drift_score numeric,
  severity severity_level not null,
  detected_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb
);

create table betting_decisions (
  betting_decision_id uuid primary key default gen_random_uuid(),
  competition_season_id uuid not null references competition_seasons(competition_season_id) on delete cascade,
  match_id uuid not null references matches(match_id) on delete cascade,
  prediction_id uuid not null references model_predictions(prediction_id) on delete cascade,
  odds_snapshot_id uuid not null references odds_snapshots(odds_snapshot_id) on delete restrict,
  decision_status betting_decision_status not null,
  risk_level risk_level not null,
  block_reason text,
  calibrated_probability_used numeric not null check (calibrated_probability_used between 0 and 1),
  market_probability numeric,
  edge numeric,
  ev numeric,
  kelly_fraction numeric,
  stake_fraction numeric,
  settlement_status betting_decision_settlement_status not null default 'PENDING',
  settlement_result text check (settlement_result is null or settlement_result in ('WIN', 'LOSS', 'PUSH', 'HALF_WIN', 'HALF_LOSS', 'VOID')),
  settlement_profit_units numeric,
  settlement_closing_odds_id uuid references market_closing_odds(market_closing_odds_id) on delete set null,
  settled_at timestamptz,
  decided_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb,
  check (decision_status <> 'BLOCKED' or block_reason is not null),
  check (decision_status <> 'BETTABLE' or (ev is not null and ev > 0)),
  check (settled_at is null or settlement_status <> 'PENDING'),
  check (settlement_status <> 'SETTLED' or settlement_result is not null)
);

create table bets (
  bet_id uuid primary key default gen_random_uuid(),
  betting_decision_id uuid references betting_decisions(betting_decision_id) on delete set null,
  bet_mode bet_mode not null,
  bet_status bet_status not null default 'OPEN',
  bookmaker_id uuid references bookmaker_profiles(bookmaker_id) on delete set null,
  stake numeric not null check (stake >= 0),
  decimal_odds_taken numeric not null check (decimal_odds_taken > 1),
  placed_at timestamptz not null,
  settled_at timestamptz,
  profit_loss numeric,
  payload jsonb not null default '{}'::jsonb
);

create table bankroll_snapshots (
  bankroll_snapshot_id uuid primary key default gen_random_uuid(),
  bankroll_name text not null,
  bet_mode bet_mode not null,
  balance numeric not null,
  exposure numeric not null default 0,
  snapshot_at timestamptz not null,
  payload jsonb not null default '{}'::jsonb,
  unique (bankroll_name, bet_mode, snapshot_at)
);

create table backtest_runs (
  backtest_run_id uuid primary key default gen_random_uuid(),
  model_id uuid references model_registry(model_id) on delete set null,
  competition_season_id uuid references competition_seasons(competition_season_id) on delete cascade,
  market_id uuid references markets(market_id) on delete set null,
  validation_method text not null check (validation_method in ('WALK_FORWARD', 'ROLLING_WINDOW', 'HOLDOUT', 'OTHER')),
  window_start_at timestamptz,
  window_end_at timestamptz,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Observability
-- ---------------------------------------------------------------------------

create table pipeline_runs (
  pipeline_run_id uuid primary key default gen_random_uuid(),
  job_name text not null,
  status text not null check (status in ('STARTED', 'OK', 'WARN', 'ERROR')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  records_processed integer,
  error_message text,
  payload jsonb not null default '{}'::jsonb
);

create table data_quality_events (
  data_quality_event_id uuid primary key default gen_random_uuid(),
  layer text not null check (layer in ('RAW', 'STAGING', 'CANONICAL', 'ANALYTICS', 'PUBLISHED')),
  entity_type entity_type,
  entity_id uuid,
  severity severity_level not null,
  check_type text not null,
  message text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table supabase_heartbeats (
  heartbeat_key text primary key,
  service_name text not null,
  status text not null check (status in ('OK', 'WARN', 'ERROR')),
  checked_at timestamptz not null default now(),
  latency_ms integer,
  details jsonb not null default '{}'::jsonb
);

create or replace function validate_entity_external_ref()
returns trigger
language plpgsql
as $$
declare
  v_exists boolean;
begin
  case new.entity_type
    when 'COMPETITION' then
      select exists(select 1 from competitions where competition_id = new.entity_id) into v_exists;
    when 'COMPETITION_SEASON' then
      select exists(select 1 from competition_seasons where competition_season_id = new.entity_id) into v_exists;
    when 'TEAM' then
      select exists(select 1 from teams where team_id = new.entity_id) into v_exists;
    when 'PLAYER' then
      select exists(select 1 from players where player_id = new.entity_id) into v_exists;
    when 'VENUE' then
      select exists(select 1 from venues where venue_id = new.entity_id) into v_exists;
    when 'REFEREE' then
      select exists(select 1 from referees where referee_id = new.entity_id) into v_exists;
    when 'MATCH' then
      select exists(select 1 from matches where match_id = new.entity_id) into v_exists;
    when 'BOOKMAKER' then
      select exists(select 1 from bookmaker_profiles where bookmaker_id = new.entity_id) into v_exists;
    when 'MODEL' then
      select exists(select 1 from model_registry where model_id = new.entity_id) into v_exists;
    else
      v_exists := true;
  end case;

  if not v_exists then
    raise exception 'Invalid entity_external_refs target: entity_type %, entity_id %', new.entity_type, new.entity_id
      using errcode = '23503';
  end if;

  return new;
end;
$$;

create or replace function app_transaction_batch(p_operations jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_op jsonb;
  v_table text;
  v_action text;
  v_payload jsonb;
  v_filters jsonb;
  v_conflict_columns text[];
  v_result jsonb := '[]'::jsonb;
  v_count integer;
begin
  if jsonb_typeof(p_operations) <> 'array' then
    raise exception 'p_operations must be a jsonb array';
  end if;

  for v_op in select * from jsonb_array_elements(p_operations)
  loop
    v_action := lower(coalesce(v_op->>'action', ''));
    v_table := coalesce(v_op->>'table', '');
    v_payload := coalesce(v_op->'rows', v_op->'payload', '[]'::jsonb);
    v_filters := coalesce(v_op->'filters', '{}'::jsonb);

    if v_table = '' then
      raise exception 'transaction operation missing table';
    end if;

    if to_regclass(format('public.%I', v_table)) is null then
      raise exception 'table does not exist: %', v_table;
    end if;

    if v_action in ('insert', 'upsert') then
      if jsonb_typeof(v_payload) <> 'array' then
        raise exception 'insert/upsert rows must be an array for table %', v_table;
      end if;

      if jsonb_array_length(v_payload) = 0 then
        v_count := 0;
      elsif v_action = 'insert' then
        v_count := app_transaction_insert_rows(v_table, v_payload);
      else
        select array_agg(value::text)
        into v_conflict_columns
        from jsonb_array_elements_text(coalesce(v_op->'conflict_columns', '[]'::jsonb)) as t(value);

        if v_conflict_columns is null or array_length(v_conflict_columns, 1) is null then
          raise exception 'upsert requires conflict_columns for table %', v_table;
        end if;

        v_count := app_transaction_upsert_rows(v_table, v_payload, v_conflict_columns);
      end if;
    elsif v_action = 'delete' then
      v_count := app_transaction_delete_rows(v_table, v_filters);
    else
      raise exception 'unsupported transaction action: %', v_action;
    end if;

    v_result := v_result || jsonb_build_array(jsonb_build_object(
      'action', v_action,
      'table', v_table,
      'count', v_count
    ));
  end loop;

  return jsonb_build_object('ok', true, 'operations', jsonb_array_length(p_operations), 'results', v_result);
end;
$$;

create or replace function app_transaction_insert_rows(
  p_table text,
  p_rows jsonb
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_sql text;
  v_count integer;
  v_cols text[];
  v_insert_cols text;
begin
  select array_agg(distinct key order by key)
  into v_cols
  from jsonb_array_elements(p_rows) as row_obj(value),
       jsonb_object_keys(row_obj.value) as key
  where exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = p_table
      and c.column_name = key
  );

  if v_cols is null or array_length(v_cols, 1) is null then
    raise exception 'no valid table columns found for %', p_table;
  end if;

  v_insert_cols := (
    select string_agg(format('%I', col), ', ')
    from unnest(v_cols) col
  );

  v_sql := format(
    'insert into %1$I (%2$s)
     select %2$s from jsonb_populate_recordset(null::%1$I, $1)',
    p_table,
    v_insert_cols
  );

  execute v_sql using p_rows;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

create or replace function app_transaction_upsert_rows(
  p_table text,
  p_rows jsonb,
  p_conflict_columns text[]
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_sql text;
  v_count integer;
  v_cols text[];
  v_insert_cols text;
  v_conflict_cols text;
  v_update_cols text;
begin
  select array_agg(distinct key order by key)
  into v_cols
  from jsonb_array_elements(p_rows) as row_obj(value),
       jsonb_object_keys(row_obj.value) as key
  where exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = p_table
      and c.column_name = key
  );

  if v_cols is null or array_length(v_cols, 1) is null then
    raise exception 'no valid table columns found for %', p_table;
  end if;

  v_insert_cols := (
    select string_agg(format('%I', col), ', ')
    from unnest(v_cols) col
  );

  v_conflict_cols := (
    select string_agg(format('%I', col), ', ')
    from unnest(p_conflict_columns) col
  );

  v_update_cols := (
    select string_agg(format('%1$I = excluded.%1$I', col), ', ')
    from unnest(v_cols) col
    where not (col = any(p_conflict_columns))
  );

  if v_update_cols is null or v_update_cols = '' then
    v_update_cols := format('%1$I = excluded.%1$I', v_cols[1]);
  end if;

  v_sql := format(
    'insert into %1$I (%2$s)
     select %2$s from jsonb_populate_recordset(null::%1$I, $1)
     on conflict (%3$s) do update set %4$s',
    p_table,
    v_insert_cols,
    v_conflict_cols,
    v_update_cols
  );

  execute v_sql using p_rows;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

create or replace function app_transaction_delete_rows(
  p_table text,
  p_filters jsonb
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_sql text;
  v_where text;
  v_count integer;
begin
  if p_filters is null or p_filters = '{}'::jsonb then
    raise exception 'delete requires filters';
  end if;

  select string_agg(format('%I = %L', key, value), ' and ')
  into v_where
  from jsonb_each_text(p_filters)
  where exists (
    select 1
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = p_table
      and c.column_name = key
  );

  if v_where is null or v_where = '' then
    raise exception 'delete filters do not match columns for %', p_table;
  end if;

  v_sql := format('delete from %I where %s', p_table, v_where);
  execute v_sql;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

create index idx_raw_payloads_source_time on raw_source_payloads(source, received_at desc);
create index idx_stg_matches_source_match on stg_matches(source, source_match_id);
create unique index idx_countries_fifa_code_unique on countries(fifa_code) where fifa_code is not null;
create unique index idx_countries_ioc_code_unique on countries(ioc_code) where ioc_code is not null;
create index idx_countries_region on countries(region, subregion);
create index idx_countries_payload_gin on countries using gin(payload);
create index idx_competition_seasons_competition on competition_seasons(competition_id, starts_at);
create index idx_competition_status_status on competition_status(status);
create index idx_competition_stages_order on competition_stages(competition_season_id, stage_order);
create index idx_group_memberships_entry on competition_group_memberships(competition_team_entry_id);
create index idx_teams_country on teams(country_code);
create index idx_team_aliases_normalized on team_aliases(normalized_alias);
create index idx_players_nationality_country on players(nationality_country_code);
create index idx_player_aliases_normalized on player_aliases(normalized_alias);
create index idx_entity_refs_entity on entity_external_refs(entity_type, entity_id);
create index idx_entity_refs_source on entity_external_refs(source, source_entity_id);
create index idx_entity_resolution_queue_status on entity_resolution_queue(resolution_status, opened_at desc);
create index idx_markets_code on markets(market_code);
create index idx_market_selections_code on market_selections(market_id, selection_code);
create index idx_matches_competition_kickoff on matches(competition_season_id, kickoff_at);
create index idx_matches_status_kickoff on matches(status, kickoff_at);
create index idx_match_participants_team on match_participants(team_id, match_id);
create index idx_odds_snapshots_match_market_time on odds_snapshots(match_id, market_id, captured_at desc);
create index idx_model_predictions_context on model_predictions(competition_season_id, match_id, market_id, as_of desc);
create index idx_betting_decisions_context on betting_decisions(competition_season_id, decision_status, decided_at desc);
create index idx_betting_decisions_settlement on betting_decisions(settlement_status, settled_at desc);
create unique index if not exists ux_betting_decisions_prediction_odds on betting_decisions(prediction_id, odds_snapshot_id);
create index idx_bets_mode_status on bets(bet_mode, bet_status, placed_at desc);
create index idx_feature_snapshots_match on feature_snapshots(match_id, feature_set_version, as_of desc);
create index idx_rating_snapshots_team on rating_snapshots(team_id, rating_type, as_of desc);

create unique index ux_standings_context on standings (
  competition_season_id,
  coalesce(stage_id, '00000000-0000-0000-0000-000000000000'::uuid),
  coalesce(group_id, '00000000-0000-0000-0000-000000000000'::uuid),
  team_id,
  as_of
);

create unique index ux_standings_current_source on standings (
  competition_season_id,
  stage_id,
  group_id,
  team_id,
  source
);

create unique index ux_odds_snapshots_capture on odds_snapshots (
  match_id,
  bookmaker_id,
  source,
  market_id,
  selection_id,
  line,
  captured_at
) nulls not distinct;

create unique index ux_odds_snapshots_source_snapshot on odds_snapshots (
  source,
  source_snapshot_id
)
where source_snapshot_id is not null;

create unique index ux_market_closing_odds_context on market_closing_odds (
  match_id,
  market_id,
  selection_id,
  line
) nulls not distinct;

create unique index ux_feature_snapshots_context on feature_snapshots (
  competition_season_id,
  coalesce(match_id, '00000000-0000-0000-0000-000000000000'::uuid),
  coalesce(team_id, '00000000-0000-0000-0000-000000000000'::uuid),
  feature_set_version,
  as_of
);

create unique index ux_rating_snapshots_context on rating_snapshots (
  competition_season_id,
  team_id,
  rating_type,
  as_of
) nulls not distinct;

create unique index ux_model_predictions_context on model_predictions (
  model_run_id,
  match_id,
  market_id,
  selection_id,
  line,
  as_of
) nulls not distinct;

create unique index ux_model_predictions_feature_context on model_predictions (
  model_run_id,
  feature_snapshot_id,
  market_id,
  selection_id,
  line
) nulls not distinct
where feature_snapshot_id is not null;

insert into markets (market_code, display_name, category)
values
  ('1X2', 'Match Result', '1X2'),
  ('OVER_UNDER', 'Totals', 'OVER_UNDER'),
  ('BTTS', 'Both Teams To Score', 'BTTS'),
  ('HANDICAP', 'Handicap', 'HANDICAP'),
  ('CARDS', 'Cards', 'CARDS'),
  ('CORNERS', 'Corners', 'CORNERS')
on conflict (market_code) do nothing;

insert into market_selections (market_id, selection_code, display_name, sort_order)
select m.market_id, s.selection_code, s.display_name, s.sort_order
from markets m
join (
  values
    ('1X2', 'HOME', 'Home', 1),
    ('1X2', 'DRAW', 'Draw', 2),
    ('1X2', 'AWAY', 'Away', 3),
    ('OVER_UNDER', 'OVER', 'Over', 1),
    ('OVER_UNDER', 'UNDER', 'Under', 2),
    ('BTTS', 'YES', 'Yes', 1),
    ('BTTS', 'NO', 'No', 2),
    ('HANDICAP', 'HOME', 'Home', 1),
    ('HANDICAP', 'AWAY', 'Away', 2),
    ('CARDS', 'OVER', 'Over', 1),
    ('CARDS', 'UNDER', 'Under', 2),
    ('CORNERS', 'OVER', 'Over', 1),
    ('CORNERS', 'UNDER', 'Under', 2)
) as s(market_code, selection_code, display_name, sort_order)
  on s.market_code = m.market_code
on conflict (market_id, selection_code) do nothing;

-- ---------------------------------------------------------------------------
-- Updated_at triggers
-- ---------------------------------------------------------------------------

create trigger trg_countries_updated_at before update on countries for each row execute function set_updated_at();
create trigger trg_competitions_updated_at before update on competitions for each row execute function set_updated_at();
create trigger trg_competition_seasons_updated_at before update on competition_seasons for each row execute function set_updated_at();
create trigger trg_competition_stages_updated_at before update on competition_stages for each row execute function set_updated_at();
create trigger trg_competition_groups_updated_at before update on competition_groups for each row execute function set_updated_at();
create trigger trg_teams_updated_at before update on teams for each row execute function set_updated_at();
create trigger trg_team_aliases_updated_at before update on team_aliases for each row execute function set_updated_at();
create trigger trg_competition_team_entries_updated_at before update on competition_team_entries for each row execute function set_updated_at();
create trigger trg_players_updated_at before update on players for each row execute function set_updated_at();
create trigger trg_player_aliases_updated_at before update on player_aliases for each row execute function set_updated_at();
create trigger trg_team_memberships_updated_at before update on team_memberships for each row execute function set_updated_at();
create trigger trg_venues_updated_at before update on venues for each row execute function set_updated_at();
create trigger trg_referees_updated_at before update on referees for each row execute function set_updated_at();
create trigger trg_matches_updated_at before update on matches for each row execute function set_updated_at();
create trigger trg_bookmaker_profiles_updated_at before update on bookmaker_profiles for each row execute function set_updated_at();
create trigger trg_markets_updated_at before update on markets for each row execute function set_updated_at();
create trigger trg_market_selections_updated_at before update on market_selections for each row execute function set_updated_at();
create trigger trg_feature_definitions_updated_at before update on feature_definitions for each row execute function set_updated_at();
create trigger trg_entity_external_refs_validate before insert or update on entity_external_refs for each row execute function validate_entity_external_ref();

-- ---------------------------------------------------------------------------
-- RPC healthcheck
-- ---------------------------------------------------------------------------

create or replace function app_supabase_healthcheck(
  p_service_name text default 'quant-platform',
  p_details jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_started_at timestamptz := clock_timestamp();
  v_latency_ms integer;
  v_counts jsonb;
begin
  select jsonb_build_object(
    'competitions', (select count(*) from competitions),
    'competition_seasons', (select count(*) from competition_seasons),
    'teams', (select count(*) from teams),
    'matches', (select count(*) from matches),
    'odds_snapshots', (select count(*) from odds_snapshots),
    'model_predictions', (select count(*) from model_predictions)
  ) into v_counts;

  v_latency_ms := greatest(0, floor(extract(epoch from (clock_timestamp() - v_started_at)) * 1000)::integer);

  insert into supabase_heartbeats (heartbeat_key, service_name, status, checked_at, latency_ms, details)
  values ('default', p_service_name, 'OK', now(), v_latency_ms, coalesce(p_details, '{}'::jsonb) || jsonb_build_object('counts', v_counts))
  on conflict (heartbeat_key) do update set
    service_name = excluded.service_name,
    status = excluded.status,
    checked_at = excluded.checked_at,
    latency_ms = excluded.latency_ms,
    details = excluded.details;

  return jsonb_build_object('ok', true, 'status', 'OK', 'checked_at', now(), 'latency_ms', v_latency_ms, 'counts', v_counts);
end;
$$;

-- ---------------------------------------------------------------------------
-- Published views
-- ---------------------------------------------------------------------------

create view published_match_schedule as
select
  m.match_id,
  cs.slug as competition_season_slug,
  c.display_name as competition_name,
  m.kickoff_at,
  v.timezone_name as venue_timezone,
  m.status,
  m.is_neutral,
  home.team_id as home_team_id,
  home_team.display_name as home_team_name,
  away.team_id as away_team_id,
  away_team.display_name as away_team_name,
  v.display_name as venue_name
from matches m
join competition_seasons cs on cs.competition_season_id = m.competition_season_id
join competitions c on c.competition_id = cs.competition_id
left join venues v on v.venue_id = m.venue_id
left join match_participants home on home.match_id = m.match_id and home.side = 'HOME'
left join teams home_team on home_team.team_id = home.team_id
left join match_participants away on away.match_id = m.match_id and away.side = 'AWAY'
left join teams away_team on away_team.team_id = away.team_id;

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
  p.as_of,
  mr.model_run_id,
  reg.model_name,
  reg.model_version,
  p.flags
from model_predictions p
join model_runs mr on mr.model_run_id = p.model_run_id
join model_registry reg on reg.model_id = mr.model_id
join markets m on m.market_id = p.market_id
join market_selections s on s.selection_id = p.selection_id;

create view published_ev_opportunities as
select
  bd.betting_decision_id,
  bd.competition_season_id,
  bd.match_id,
  bd.decision_status,
  bd.risk_level,
  bd.block_reason,
  bd.edge,
  bd.ev,
  bd.kelly_fraction,
  bd.stake_fraction,
  bd.decided_at,
  m.market_code,
  s.selection_code,
  p.calibrated_probability,
  os.decimal_odds,
  os.captured_at as odds_captured_at
from betting_decisions bd
join model_predictions p on p.prediction_id = bd.prediction_id
join odds_snapshots os on os.odds_snapshot_id = bd.odds_snapshot_id
join markets m on m.market_id = p.market_id
join market_selections s on s.selection_id = p.selection_id
where bd.decision_status = 'BETTABLE';

create view published_market_value_comparison as
select
  os.match_id,
  m.market_code,
  s.selection_code,
  os.line,
  os.decimal_odds,
  os.implied_probability,
  p.calibrated_probability,
  (p.calibrated_probability - os.implied_probability) as edge,
  os.captured_at
from odds_snapshots os
join model_predictions p
  on p.match_id = os.match_id
 and p.market_id = os.market_id
 and p.selection_id = os.selection_id
 and coalesce(p.line, -999999) = coalesce(os.line, -999999)
join markets m on m.market_id = os.market_id
join market_selections s on s.selection_id = os.selection_id;

create view published_model_calibration as
select
  cr.calibration_run_id,
  reg.model_name,
  reg.model_version,
  cr.competition_season_id,
  m.market_code,
  cr.stage_type,
  cr.method,
  cr.sample_size,
  cr.brier_score,
  cr.log_loss,
  cr.ece,
  cr.sharpness,
  cr.created_at
from calibration_runs cr
left join model_registry reg on reg.model_id = cr.model_id
join markets m on m.market_id = cr.market_id;

create view published_bankroll_summary as
select distinct on (bankroll_name, bet_mode)
  bankroll_name,
  bet_mode,
  balance,
  exposure,
  snapshot_at
from bankroll_snapshots
order by bankroll_name, bet_mode, snapshot_at desc;

create view published_competition_health as
select
  cs.competition_season_id,
  cs.slug,
  c.display_name as competition_name,
  coalesce(st.status::text, 'OBSERVATION') as status,
  st.readiness_score,
  count(rc.readiness_check_id) filter (where rc.status = 'FAIL') as failed_checks,
  count(rc.readiness_check_id) filter (where rc.status = 'WARN') as warning_checks
from competition_seasons cs
join competitions c on c.competition_id = cs.competition_id
left join competition_status st on st.competition_season_id = cs.competition_season_id
left join competition_readiness_checks rc on rc.competition_season_id = cs.competition_season_id
group by cs.competition_season_id, cs.slug, c.display_name, st.status, st.readiness_score;

create view published_country_catalog as
select
  code_alpha2,
  code_alpha3,
  numeric_code,
  fifa_code,
  ioc_code,
  default_name,
  names,
  region,
  subregion,
  continent,
  timezone_default,
  timezones,
  currency_code,
  flag_emoji,
  is_fifa_member,
  is_sovereign,
  payload->'sports' as sports_metadata
from countries;

create view published_blocked_decisions as
select
  bd.betting_decision_id,
  bd.competition_season_id,
  bd.match_id,
  bd.decision_status,
  bd.risk_level,
  bd.block_reason,
  bd.edge,
  bd.ev,
  bd.decided_at,
  m.market_code,
  s.selection_code
from betting_decisions bd
join model_predictions p on p.prediction_id = bd.prediction_id
join markets m on m.market_id = p.market_id
join market_selections s on s.selection_id = p.selection_id
where bd.decision_status = 'BLOCKED';

create view published_model_diagnostics as
select
  reg.model_id,
  reg.model_name,
  reg.model_version,
  reg.model_family,
  reg.champion_status,
  count(distinct mr.model_run_id) as run_count,
  count(distinct p.prediction_id) as prediction_count,
  max(mr.finished_at) as last_finished_at,
  max(mm.calculated_at) as last_metric_at,
  max(dr.detected_at) as last_drift_detected_at,
  count(dr.drift_report_id) filter (where dr.severity in ('ERROR', 'CRITICAL')) as severe_drift_reports
from model_registry reg
left join model_runs mr on mr.model_id = reg.model_id
left join model_predictions p on p.model_run_id = mr.model_run_id
left join model_metrics mm on mm.model_id = reg.model_id
left join drift_reports dr on dr.model_id = reg.model_id
group by reg.model_id, reg.model_name, reg.model_version, reg.model_family, reg.champion_status;

create view published_data_quality_health as
select
  'DUPLICATE_TEAM_NORMALIZED_NAME'::text as check_name,
  'WARN'::text as severity,
  count(*)::bigint as issue_count,
  coalesce(jsonb_agg(jsonb_build_object('normalized_name', normalized_name, 'rows', rows_count)) filter (where rows_count > 1), '[]'::jsonb) as sample
from (
  select normalized_name, count(*) as rows_count
  from teams
  group by normalized_name
  having count(*) > 1
) x
union all
select
  'DUPLICATE_PLAYER_NORMALIZED_NAME',
  'WARN',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('normalized_name', normalized_name, 'rows', rows_count)) filter (where rows_count > 1), '[]'::jsonb)
from (
  select normalized_name, count(*) as rows_count
  from players
  group by normalized_name
  having count(*) > 1
) x
union all
select
  'AMBIGUOUS_ALIASES',
  'ERROR',
  count(*)::bigint,
  coalesce(jsonb_agg(payload), '[]'::jsonb)
from (
  select jsonb_build_object('entity', 'TEAM', 'normalized_alias', normalized_alias, 'entities', count(distinct team_id)) as payload
  from team_aliases
  group by normalized_alias
  having count(distinct team_id) > 1
  union all
  select jsonb_build_object('entity', 'PLAYER', 'normalized_alias', normalized_alias, 'entities', count(distinct player_id)) as payload
  from player_aliases
  group by normalized_alias
  having count(distinct player_id) > 1
) x
union all
select
  'OPEN_ENTITY_RESOLUTION_QUEUE',
  'WARN',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('entity_type', entity_type, 'source', source, 'name', source_entity_name)) filter (where resolution_status in ('OPEN', 'IN_REVIEW')), '[]'::jsonb)
from entity_resolution_queue
where resolution_status in ('OPEN', 'IN_REVIEW')
union all
select
  'DUPLICATE_EXTERNAL_REFS',
  'ERROR',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('entity_type', entity_type, 'source', source, 'source_entity_id', source_entity_id, 'rows', rows_count)), '[]'::jsonb)
from (
  select entity_type, source, source_entity_id, count(*) as rows_count
  from entity_external_refs
  group by entity_type, source, source_entity_id
  having count(*) > 1
) x
union all
select
  'MATCHES_WITHOUT_HOME_AWAY',
  'ERROR',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('match_id', match_id, 'home_count', home_count, 'away_count', away_count)), '[]'::jsonb)
from (
  select
    m.match_id,
    count(mp.match_participant_id) filter (where mp.side = 'HOME') as home_count,
    count(mp.match_participant_id) filter (where mp.side = 'AWAY') as away_count
  from matches m
  left join match_participants mp on mp.match_id = m.match_id
  group by m.match_id
  having count(mp.match_participant_id) filter (where mp.side = 'HOME') <> 1
      or count(mp.match_participant_id) filter (where mp.side = 'AWAY') <> 1
) x
union all
select
  'ODDS_CAPTURED_AFTER_KICKOFF',
  'WARN',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('odds_snapshot_id', os.odds_snapshot_id, 'match_id', os.match_id, 'captured_at', os.captured_at, 'kickoff_at', m.kickoff_at)), '[]'::jsonb)
from odds_snapshots os
join matches m on m.match_id = os.match_id
where os.captured_at > m.kickoff_at
union all
select
  'ODDS_WITHOUT_MATCH',
  'CRITICAL',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('odds_snapshot_id', os.odds_snapshot_id, 'match_id', os.match_id)), '[]'::jsonb)
from odds_snapshots os
left join matches m on m.match_id = os.match_id
where m.match_id is null
union all
select
  'PREDICTIONS_WITHOUT_COMPARABLE_ODDS',
  'WARN',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('prediction_id', p.prediction_id, 'match_id', p.match_id)), '[]'::jsonb)
from model_predictions p
left join odds_snapshots os
  on os.match_id = p.match_id
 and os.market_id = p.market_id
 and os.selection_id = p.selection_id
 and coalesce(os.line, -999999) = coalesce(p.line, -999999)
where os.odds_snapshot_id is null
union all
select
  'BETTING_DECISIONS_MISSING_BLOCK_REASON',
  'ERROR',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('betting_decision_id', betting_decision_id, 'match_id', match_id)), '[]'::jsonb)
from betting_decisions
where decision_status = 'BLOCKED'
  and block_reason is null
union all
select
  'BETTABLE_WITHOUT_CALIBRATED_PROBABILITY',
  'ERROR',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('betting_decision_id', bd.betting_decision_id, 'prediction_id', p.prediction_id)), '[]'::jsonb)
from betting_decisions bd
join model_predictions p on p.prediction_id = bd.prediction_id
where bd.decision_status = 'BETTABLE'
  and p.calibrated_probability is null
union all
select
  'BETTABLE_COMPETITION_NOT_BETTABLE',
  'CRITICAL',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('betting_decision_id', bd.betting_decision_id, 'competition_season_id', bd.competition_season_id, 'status', coalesce(cs.status::text, 'MISSING'))), '[]'::jsonb)
from betting_decisions bd
left join competition_status cs on cs.competition_season_id = bd.competition_season_id
where bd.decision_status = 'BETTABLE'
  and coalesce(cs.status::text, 'OBSERVATION') <> 'BETTABLE'
union all
select
  'NATIONAL_TEAMS_WITHOUT_ISO_COUNTRY',
  'ERROR',
  count(*)::bigint,
  coalesce(jsonb_agg(jsonb_build_object('team_id', team_id, 'display_name', display_name, 'slug', slug)), '[]'::jsonb)
from teams
where team_type = 'NATIONAL_TEAM'
  and country_code is null;

-- ---------------------------------------------------------------------------
-- RLS recommendation baseline
-- Keep canonical/analytics private. Expose read access through API or published views.
-- Enable RLS once auth roles are defined.
-- ---------------------------------------------------------------------------

-- alter table raw_source_payloads enable row level security;
-- alter table stg_matches enable row level security;
-- alter table teams enable row level security;
-- alter table matches enable row level security;
-- alter table odds_snapshots enable row level security;
-- alter table model_predictions enable row level security;
-- alter table betting_decisions enable row level security;

commit;
