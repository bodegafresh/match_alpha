-- Consolidate duplicated Cape Verde / Cabo Verde team identities into a single canonical team.
-- This migration is idempotent and only applies when both source and target rows are present.

DO $$
DECLARE
  v_target_team_id uuid;
  v_source_team_id uuid;
BEGIN
  -- Serialize this hotfix merge if multiple deploys attempt to run it.
  PERFORM pg_advisory_xact_lock(74500127);

  -- Canonical target preference: slug/display/name for Cabo Verde.
  SELECT t.team_id
    INTO v_target_team_id
  FROM teams t
  WHERE t.slug = 'cabo-verde'
     OR lower(t.display_name) = 'cabo verde'
     OR t.normalized_name = 'cabo verde'
  ORDER BY
    CASE WHEN t.slug = 'cabo-verde' THEN 0 ELSE 1 END,
    t.updated_at DESC
  LIMIT 1;

  -- Source duplicate preference: Cape Verde Islands variant.
  SELECT t.team_id
    INTO v_source_team_id
  FROM teams t
  WHERE (
      t.slug = 'cape-verde-islands'
      OR lower(t.display_name) = 'cape verde islands'
      OR t.normalized_name = 'cape verde islands'
    )
    AND (v_target_team_id IS NULL OR t.team_id <> v_target_team_id)
  ORDER BY t.updated_at DESC
  LIMIT 1;

  IF v_target_team_id IS NULL THEN
    RAISE NOTICE 'Cabo Verde hotfix: canonical target team was not found. No changes applied.';
    RETURN;
  END IF;

  IF v_source_team_id IS NULL THEN
    -- Ensure aliases are still canonical even if no duplicate source row remains.
    INSERT INTO team_aliases (team_id, alias, normalized_alias, source, confidence)
    VALUES
      (v_target_team_id, 'Cabo Verde', 'cabo-verde', 'manual', 1),
      (v_target_team_id, 'Cape Verde', 'cape-verde', 'manual', 1),
      (v_target_team_id, 'Cape Verde Islands', 'cape-verde-islands', 'manual', 1)
    ON CONFLICT (normalized_alias, source) DO UPDATE
      SET team_id = EXCLUDED.team_id,
          alias = EXCLUDED.alias,
          confidence = GREATEST(team_aliases.confidence, EXCLUDED.confidence),
          updated_at = now();

    UPDATE teams
    SET display_name = 'Cabo Verde',
        slug = COALESCE(NULLIF(slug, ''), 'cabo-verde'),
        normalized_name = 'cabo verde',
        updated_at = now()
    WHERE team_id = v_target_team_id;

    RAISE NOTICE 'Cabo Verde hotfix: no duplicate source team found. Canonical aliases refreshed only.';
    RETURN;
  END IF;

  -- Merge alias rows first so any source aliases survive the merge.
  INSERT INTO team_aliases (team_id, alias, normalized_alias, language_code, source, confidence, created_at, updated_at)
  SELECT
    v_target_team_id,
    ta.alias,
    ta.normalized_alias,
    ta.language_code,
    ta.source,
    ta.confidence,
    ta.created_at,
    ta.updated_at
  FROM team_aliases ta
  WHERE ta.team_id = v_source_team_id
  ON CONFLICT (normalized_alias, source) DO UPDATE
    SET team_id = EXCLUDED.team_id,
        alias = EXCLUDED.alias,
        confidence = GREATEST(team_aliases.confidence, EXCLUDED.confidence),
        updated_at = now();

  -- Ensure canonical aliases are present.
  INSERT INTO team_aliases (team_id, alias, normalized_alias, source, confidence)
  VALUES
    (v_target_team_id, 'Cabo Verde', 'cabo-verde', 'manual', 1),
    (v_target_team_id, 'Cape Verde', 'cape-verde', 'manual', 1),
    (v_target_team_id, 'Cape Verde Islands', 'cape-verde-islands', 'manual', 1)
  ON CONFLICT (normalized_alias, source) DO UPDATE
    SET team_id = EXCLUDED.team_id,
        alias = EXCLUDED.alias,
        confidence = GREATEST(team_aliases.confidence, EXCLUDED.confidence),
        updated_at = now();

  -- Rewire external refs.
  UPDATE entity_external_refs
  SET entity_id = v_target_team_id,
      updated_at = now(),
      payload = payload || jsonb_build_object('merge_reason', 'CABO_VERDE_DUPLICATE_HOTFIX')
  WHERE entity_type = 'TEAM'
    AND entity_id = v_source_team_id;

  -- Deduplicate rows that would violate unique constraints after FK rewiring.
  DELETE FROM competition_team_entries source_row
  USING competition_team_entries target_row
  WHERE source_row.team_id = v_source_team_id
    AND target_row.team_id = v_target_team_id
    AND source_row.competition_season_id = target_row.competition_season_id;

  DELETE FROM competition_rosters source_row
  USING competition_rosters target_row
  WHERE source_row.team_id = v_source_team_id
    AND target_row.team_id = v_target_team_id
    AND source_row.competition_season_id = target_row.competition_season_id
    AND source_row.player_id = target_row.player_id;

  DELETE FROM team_memberships source_row
  USING team_memberships target_row
  WHERE source_row.team_id = v_source_team_id
    AND target_row.team_id = v_target_team_id
    AND source_row.player_id = target_row.player_id
    AND source_row.membership_type = target_row.membership_type
    AND source_row.source IS NOT DISTINCT FROM target_row.source;

  DELETE FROM match_lineups source_row
  USING match_lineups target_row
  WHERE source_row.team_id = v_source_team_id
    AND target_row.team_id = v_target_team_id
    AND source_row.match_id = target_row.match_id
    AND source_row.player_id = target_row.player_id
    AND source_row.source = target_row.source;

  -- Rewire all known team_id foreign keys.
  UPDATE competition_team_entries SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE competition_rosters SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE team_memberships SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE standings SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE match_participants SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE match_lineups SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE match_events SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE player_match_stats SET team_id = v_target_team_id WHERE team_id = v_source_team_id;
  UPDATE matches SET winner_team_id = v_target_team_id WHERE winner_team_id = v_source_team_id;
  UPDATE tournament_slots SET resolved_team_id = v_target_team_id WHERE resolved_team_id = v_source_team_id;

  -- Preserve useful metadata and canonical naming on the target.
  UPDATE teams
  SET display_name = 'Cabo Verde',
      slug = COALESCE(NULLIF(slug, ''), 'cabo-verde'),
      normalized_name = 'cabo verde',
      country_code = COALESCE(country_code, (SELECT country_code FROM teams WHERE team_id = v_source_team_id)),
      metadata = metadata || COALESCE((SELECT metadata FROM teams WHERE team_id = v_source_team_id), '{}'::jsonb),
      updated_at = now()
  WHERE team_id = v_target_team_id;

  DELETE FROM teams WHERE team_id = v_source_team_id;

  RAISE NOTICE 'Cabo Verde hotfix applied. source=% target=%', v_source_team_id, v_target_team_id;
END $$;
