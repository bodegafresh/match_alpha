-- Canonical quality diagnostics:
-- 1) possible duplicate teams
-- 2) possible duplicate players
-- 3) external ref collisions
-- 4) unresolved identity queue backlog

-- 1) Teams with same normalized identity.
SELECT
  t.normalized_name,
  t.team_type,
  COALESCE(t.country_code, '??') AS country_code,
  COUNT(*) AS dup_count,
  ARRAY_AGG(t.team_id ORDER BY t.updated_at DESC) AS team_ids
FROM teams t
GROUP BY t.normalized_name, t.team_type, COALESCE(t.country_code, '??')
HAVING COUNT(*) > 1
ORDER BY dup_count DESC, t.normalized_name;

-- 2) Players with same identity heuristics.
SELECT
  p.normalized_name,
  p.birth_date,
  COALESCE(p.nationality_country_code, '??') AS nationality_country_code,
  COUNT(*) AS dup_count,
  ARRAY_AGG(p.player_id ORDER BY p.updated_at DESC) AS player_ids
FROM players p
GROUP BY p.normalized_name, p.birth_date, COALESCE(p.nationality_country_code, '??')
HAVING COUNT(*) > 1
ORDER BY dup_count DESC, p.normalized_name;

-- 3) Any source external id pointing to multiple canonical entities (should be zero rows).
SELECT
  e.entity_type,
  e.source,
  e.source_entity_id,
  COUNT(DISTINCT e.entity_id) AS canonical_targets,
  ARRAY_AGG(DISTINCT e.entity_id) AS target_entity_ids
FROM entity_external_refs e
WHERE e.entity_type IN ('TEAM', 'PLAYER')
GROUP BY e.entity_type, e.source, e.source_entity_id
HAVING COUNT(DISTINCT e.entity_id) > 1
ORDER BY canonical_targets DESC, e.entity_type, e.source;

-- 4) Unresolved entity resolution queue grouped by type/source.
SELECT
  q.entity_type,
  q.source,
  q.status,
  COUNT(*) AS pending_count,
  MIN(q.created_at) AS oldest_created_at,
  MAX(q.created_at) AS newest_created_at
FROM entity_resolution_queue q
WHERE q.status IN ('PENDING', 'IN_REVIEW')
GROUP BY q.entity_type, q.source, q.status
ORDER BY pending_count DESC, q.entity_type, q.source;
