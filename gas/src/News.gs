/**
 * News.gs — Match Alpha GAS News Fetcher
 *
 * GAS puede acceder a Google News RSS sin bloqueo de IPs (infraestructura Google).
 * El backend de Render sí es bloqueado — por eso el fetch vive acá.
 *
 * Flujo:
 *   1. fetchTodayMatchTeams_()  →  GET /web/news  (partidos de hoy + equipos)
 *   2. fetchNewsForTeams_()     →  Google News RSS por equipo
 *   3. pushNewsToBackend_()     →  POST /web/news/ingest (upsert en news_items)
 *
 * Llamada principal: runNewsSync()
 *   - Se ejecuta dentro de runDailyBackendOrchestration() después del daily job
 *   - También puede configurarse como trigger independiente (Time-based, 6–7 AM)
 */

// ─── CONSTANTES ───────────────────────────────────────────────────────────────

var NEWS_MAX_PER_TEAM = 6;
var NEWS_QUERIES_PER_TEAM = 2; // "world cup 2026 {team}" + "mundial 2026 {team}"

// ─── FUNCIÓN PRINCIPAL ────────────────────────────────────────────────────────

/**
 * runNewsSync — fetchea RSS de Google News para los equipos que juegan hoy
 * y pushea los artículos al endpoint /web/news/ingest del backend.
 * Llamar desde runDailyBackendOrchestration() o como trigger independiente.
 */
function runNewsSync() {
  var config = getBackendCronConfig_();

  // 1. Obtener equipos de los partidos de hoy desde el backend
  var matches = fetchTodayMatchTeams_(config);
  if (!matches || !matches.length) {
    Logger.log('runNewsSync: no hay partidos hoy, skip.');
    return { ok: true, skipped: true, reason: 'NO_MATCHES_TODAY' };
  }
  Logger.log('runNewsSync: ' + matches.length + ' partidos hoy.');

  // 2. Recolectar equipos únicos con contexto del partido
  var teamSet = {};
  matches.forEach(function(m) {
    if (m.home_team) teamSet[m.home_team] = { match_id: m.match_id, home_team: m.home_team, away_team: m.away_team };
    if (m.away_team) teamSet[m.away_team] = { match_id: m.match_id, home_team: m.home_team, away_team: m.away_team };
  });

  // 3. Fetchear RSS por equipo y acumular artículos
  var allItems = [];
  Object.keys(teamSet).forEach(function(team) {
    var matchInfo = teamSet[team];
    var articles = fetchNewsForTeam_(team);
    articles.forEach(function(a) {
      a.match_id = matchInfo.match_id;
      a.home_team = matchInfo.home_team;
      a.away_team = matchInfo.away_team;
    });
    allItems = allItems.concat(articles);
    Utilities.sleep(300); // cortesía entre requests
  });

  if (!allItems.length) {
    Logger.log('runNewsSync: no se encontraron artículos.');
    return { ok: true, inserted: 0 };
  }

  // 4. Deduplicar por id_hash antes de pushear
  var seen = {};
  var deduped = allItems.filter(function(a) {
    if (seen[a.id_hash]) return false;
    seen[a.id_hash] = true;
    return true;
  });

  // 5. Pushear al backend en lotes de 50
  var totalInserted = 0;
  var batchSize = 50;
  for (var i = 0; i < deduped.length; i += batchSize) {
    var batch = deduped.slice(i, i + batchSize);
    var result = pushNewsToBackend_(config, batch);
    if (result && result.inserted) totalInserted += result.inserted;
  }

  Logger.log('runNewsSync: ' + deduped.length + ' artículos enviados, ' + totalInserted + ' insertados.');
  return { ok: true, total: deduped.length, inserted: totalInserted };
}

// ─── HELPERS ─────────────────────────────────────────────────────────────────

/**
 * Obtiene los partidos de hoy desde GET /api/v1/web/news.
 * Retorna array de {match_id, home_team, away_team} o [].
 */
function fetchTodayMatchTeams_(config) {
  try {
    var url = config.BACKEND_BASE_URL.replace(/\/+$/, '') + '/api/v1/web/news';
    var response = UrlFetchApp.fetch(url, {
      method: 'get',
      muteHttpExceptions: true,
      headers: { 'Authorization': 'Bearer ' + config.API_INTERNAL_KEY }
    });
    var code = response.getResponseCode();
    var text = response.getContentText();
    Logger.log('fetchTodayMatchTeams_ HTTP ' + code + ' body=' + text.slice(0, 500));
    if (code !== 200) return [];
    var body = JSON.parse(text);
    return (body.data && body.data.matches_news) ? body.data.matches_news : [];
  } catch (e) {
    Logger.log('fetchTodayMatchTeams_ error: ' + e.message);
    return [];
  }
}

/**
 * Fetchea Google News RSS para un equipo. Retorna artículos deduplicados.
 */
function fetchNewsForTeam_(teamName) {
  var queries = [
    'FIFA World Cup 2026 ' + teamName,
    'Mundial 2026 ' + teamName
  ];
  var seen = {};
  var articles = [];

  queries.forEach(function(q) {
    var url = 'https://news.google.com/rss/search?q=' +
      encodeURIComponent(q) + '&hl=es&gl=US&ceid=US:es';
    try {
      var resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
      if (resp.getResponseCode() !== 200) return;
      var doc = XmlService.parse(resp.getContentText());
      var channel = doc.getRootElement().getChild('channel');
      if (!channel) return;
      var items = channel.getChildren('item').slice(0, NEWS_MAX_PER_TEAM);
      items.forEach(function(item) {
        var title   = item.getChildText('title') || '';
        var link    = item.getChildText('link')  || '';
        var pubDate = item.getChildText('pubDate') || '';
        var source  = '';
        try { source = item.getChild('source').getText(); } catch(e_) { source = 'Google News'; }
        if (!title || !link) return;
        var hash = computeHash_(title + link);
        if (seen[hash]) return;
        seen[hash] = true;
        articles.push({
          id_hash:   hash,
          home_team: teamName,  // se sobreescribe con el equipo correcto en el caller
          away_team: '',
          title:     title,
          url:       link,
          source:    source || 'Google News RSS',
          pub_date:  pubDate
        });
      });
    } catch (e) {
      Logger.log('fetchNewsForTeam_ RSS error (' + q + '): ' + e.message);
    }
  });

  return articles;
}

/**
 * POST /api/v1/web/news/ingest con X-Internal-Key.
 */
function pushNewsToBackend_(config, items) {
  try {
    var url = config.BACKEND_BASE_URL.replace(/\/+$/, '') + '/api/v1/web/news/ingest';
    var resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      headers: { 'X-Internal-Key': config.API_INTERNAL_KEY },
      payload: JSON.stringify(items)
    });
    var code = resp.getResponseCode();
    if (code !== 200) {
      Logger.log('pushNewsToBackend_ HTTP ' + code + ': ' + resp.getContentText().slice(0, 200));
      return { ok: false, inserted: 0 };
    }
    return JSON.parse(resp.getContentText());
  } catch (e) {
    Logger.log('pushNewsToBackend_ error: ' + e.message);
    return { ok: false, inserted: 0 };
  }
}

/**
 * Hash simple usando Utilities.computeDigest (SHA-1 → hex string).
 */
function computeHash_(str) {
  var bytes = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_1,
    str,
    Utilities.Charset.UTF_8
  );
  return bytes.map(function(b) {
    return ('0' + (b < 0 ? b + 256 : b).toString(16)).slice(-2);
  }).join('');
}
