/**
 * scheduler.js — Match Alpha GAS Scheduler
 *
 * Mismo patrón que BackendCronOrchestration.gs del proyecto original.
 * GAS solo despierta y orquesta el backend — no ingesta datos ni toca Supabase.
 *
 * Fixes del audit aplicados:
 *   - Keepalive cada 10 min (Render Free duerme a los 15 min; GAS permite 1/5/10/15/30)
 *   - Live orchestration cada 15 min (antes era 30)
 *   - Guard de idempotencia diario en GAS (además del guard del backend)
 *   - Live orchestration solo corre dentro de la ventana WC2026
 *   - checkBackendHealth llama al nuevo endpoint /jobs/status/health
 *   - MAX_FETCH_PER_DAY subido a 400 (el real es 20.000, 80 era muy bajo)
 *
 * Setup:
 *   1. Script Properties (Archivo > Propiedades del proyecto):
 *        BACKEND_BASE_URL   = https://tu-backend.onrender.com
 *        API_INTERNAL_KEY   = <tu clave interna>
 *   2. Correr installMatchAlphaTriggers() una sola vez para crear los triggers.
 */

// ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────

var MATCH_ALPHA_CRON_CONFIG = {
  BACKEND_BASE_URL:         'https://YOUR_RENDER_SERVICE.onrender.com',
  API_INTERNAL_KEY:         'SET_IN_SCRIPT_PROPERTIES',
  MAX_FETCH_PER_DAY:        500,
  KEEPALIVE_ENABLED:        true,
  DAILY_JOB_ENABLED:        true,
  LIVE_JOB_ENABLED:         true,
  MORNING_SUMMARY_ENABLED:  true,
  FETCH_TIMEOUT_MS:         25000,
  DAILY_HOUR_UTC:           10,   // hora UTC en que corre el daily (06:00 Chile = 10:00 UTC, UTC-4 invierno)
  MORNING_SUMMARY_HOUR_UTC: 12,   // 08:00 Chile (UTC-4 invierno)
  // Ventana WC2026 para live orchestration
  WC_START_ISO:             '2026-06-11T00:00:00Z',
  WC_END_ISO:               '2026-07-20T00:00:00Z'
};

var MATCH_ALPHA_CRON_PROPS = {
  FETCH_COUNT:              'MATCH_ALPHA_FETCH_COUNT',
  FETCH_COUNT_DATE:         'MATCH_ALPHA_FETCH_COUNT_DATE',
  LAST_STATUS:              'MATCH_ALPHA_LAST_BACKEND_STATUS',
  DAILY_RAN_DATE:           'MATCH_ALPHA_DAILY_RAN_DATE',
  TRIGGER_PREFIX:           'MATCH_ALPHA_BACKEND_TRIGGER_'
};

// ─── FUNCIONES PÚBLICAS (adjuntar como triggers) ──────────────────────────────

/**
 * pingBackendKeepAlive — configurar cada 10 minutos.
 * Mantiene el backend de Render Free despierto (se duerme a los 15 min de inactividad).
 * GAS solo acepta 1, 5, 10, 15 o 30 — usamos 10 para tener margen seguro.
 */
function pingBackendKeepAlive() {
  var config = getBackendCronConfig_();
  if (!config.KEEPALIVE_ENABLED) {
    return logBackendCronResult_('pingBackendKeepAlive', { ok: false, skipped: true, reason: 'KEEPALIVE_DISABLED' });
  }
  return logBackendCronResult_(
    'pingBackendKeepAlive',
    backendFetch_('/api/v1/jobs/orchestrate/keepalive', { method: 'post', payload: { source: 'gas_keepalive' } })
  );
}

/**
 * runDailyBackendOrchestration — configurar cada 1 hora.
 * Guard interno: solo ejecuta el job diario una vez por día UTC.
 * El backend también tiene su propio guard de idempotencia por ventana.
 */
function runDailyBackendOrchestration() {
  var config = getBackendCronConfig_();
  if (!config.DAILY_JOB_ENABLED) {
    return logBackendCronResult_('runDailyBackendOrchestration', { ok: false, skipped: true, reason: 'DAILY_JOB_DISABLED' });
  }

  // Guard de hora: corre en ventana de 3 horas desde DAILY_HOUR_UTC
  // (permite que Render despierte si estaba dormido a la hora exacta)
  var nowUtcHour = new Date().getUTCHours();
  var startHour = config.DAILY_HOUR_UTC;
  var inWindow = (nowUtcHour >= startHour && nowUtcHour < startHour + 3);
  if (!inWindow) {
    return logBackendCronResult_('runDailyBackendOrchestration', {
      ok: false, skipped: true, reason: 'WRONG_HOUR', utc_hour: nowUtcHour
    });
  }

  // Guard de idempotencia: solo correr una vez por día UTC
  var props = PropertiesService.getScriptProperties();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var ranDate = props.getProperty(MATCH_ALPHA_CRON_PROPS.DAILY_RAN_DATE);
  if (ranDate === today) {
    return logBackendCronResult_('runDailyBackendOrchestration', {
      ok: false, skipped: true, reason: 'ALREADY_RAN_TODAY', date: today
    });
  }

  var result = backendFetch_('/api/v1/jobs/orchestrate/daily', { method: 'post', payload: { source: 'gas_daily' } });

  // Marcar como ejecutado solo si el backend respondió sin error 5xx
  if (result.ok || (result.status_code && result.status_code < 500)) {
    props.setProperty(MATCH_ALPHA_CRON_PROPS.DAILY_RAN_DATE, today);
    // Limpiar registro del día anterior para no acumular
    var yesterday = Utilities.formatDate(new Date(Date.now() - 86400000), 'UTC', 'yyyy-MM-dd');
    if (ranDate === yesterday) {
      props.deleteProperty(MATCH_ALPHA_CRON_PROPS.DAILY_RAN_DATE);
      props.setProperty(MATCH_ALPHA_CRON_PROPS.DAILY_RAN_DATE, today);
    }
  }

  return logBackendCronResult_('runDailyBackendOrchestration', result);
}

/**
 * runLiveBackendOrchestration — configurar cada 5 minutos.
 * Solo corre durante la ventana de partidos (WC2026).
 * El backend decide internamente si hay partidos activos y cuáles jobs ejecutar.
 */
function runLiveBackendOrchestration() {
  var config = getBackendCronConfig_();
  if (!config.LIVE_JOB_ENABLED) {
    return logBackendCronResult_('runLiveBackendOrchestration', { ok: false, skipped: true, reason: 'LIVE_JOB_DISABLED' });
  }

  if (!isInMatchWindow_(config)) {
    return logBackendCronResult_('runLiveBackendOrchestration', {
      ok: false, skipped: true, reason: 'OUTSIDE_MATCH_WINDOW'
    });
  }

  return logBackendCronResult_(
    'runLiveBackendOrchestration',
    backendFetch_('/api/v1/jobs/orchestrate/live', { method: 'post', payload: { source: 'gas_live' } })
  );
}

/**
 * runWeeklyTeamsSync — corre domingos a las 2 AM UTC.
 * Mantiene el nombre legacy del handler para reutilizar el trigger semanal existente,
 * pero ejecuta la nueva orquestación de servicio única en backend (solo equipos).
 */
function runWeeklyTeamsSync() {
  return logBackendCronResult_(
    'runWeeklyTeamsSync',
    backendFetch_('/api/v1/jobs/orchestrate/weekly', { method: 'post', payload: { source: 'gas_weekly' } })
  );
}

/**
 * runMorningTelegramSummary — trigger diario a las 8:00 Chile.
 * Envia a Telegram un resumen de la corrida daily previa, priorizando EV+.
 */
function runMorningTelegramSummary() {
  var config = getBackendCronConfig_();
  if (!config.MORNING_SUMMARY_ENABLED) {
    return logBackendCronResult_('runMorningTelegramSummary', {
      ok: false,
      skipped: true,
      reason: 'MORNING_SUMMARY_DISABLED'
    });
  }

  var nowUtcHour = new Date().getUTCHours();
  if (nowUtcHour !== config.MORNING_SUMMARY_HOUR_UTC) {
    return logBackendCronResult_('runMorningTelegramSummary', {
      ok: false,
      skipped: true,
      reason: 'WRONG_HOUR',
      utc_hour: nowUtcHour
    });
  }

  return logBackendCronResult_(
    'runMorningTelegramSummary',
    backendFetch_('/api/v1/jobs/telegram_daily_summary/run', {
      method: 'post',
      payload: { source: 'gas_morning_summary', ev_limit: 8 }
    })
  );
}

/**
 * checkBackendHealth — opcional, correr 1 vez al día para monitoreo.
 * Llama al nuevo endpoint /jobs/status/health introducido en el audit.
 */
function checkBackendHealth() {
  return logBackendCronResult_(
    'checkBackendHealth',
    backendFetch_('/api/v1/jobs/status/health', { method: 'get' })
  );
}

/**
 * forceDailyOrchestration — uso manual desde el editor GAS.
 * Omite los guards de hora UTC e idempotencia diaria.
 * Úsalo para forzar la actualización de hoy/ayer fuera del horario configurado.
 */
function forceDailyOrchestration() {
  var result = backendFetch_('/api/v1/jobs/orchestrate/daily', {
    method: 'post',
    payload: { source: 'gas_force_daily' }
  });

  // Resetear el guard de idempotencia para que el trigger automático
  // también pueda correr en su próxima ventana horaria
  var props = PropertiesService.getScriptProperties();
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.DAILY_RAN_DATE);

  return logBackendCronResult_('forceDailyOrchestration', result);
}

/**
 * checkBackendLatestStatus — mantener por compatibilidad con el script original.
 */
function checkBackendLatestStatus() {
  return logBackendCronResult_(
    'checkBackendLatestStatus',
    backendFetch_('/api/v1/jobs/status/latest', { method: 'get' })
  );
}

// ─── GESTIÓN DE TRIGGERS ──────────────────────────────────────────────────────

/**
 * runNewsSyncJob — trigger independiente para fetchear RSS de Google News.
 * Corre 1 hora antes del daily (9 AM UTC = 5 AM Chile, UTC-4 invierno) para que
 * las noticias estén disponibles cuando el pipeline diario genere predicciones.
 * Se puede forzar manualmente desde el editor GAS.
 */
function runNewsSyncJob() {
  return logBackendCronResult_('runNewsSyncJob', runNewsSync());
}

/**
 * installMatchAlphaTriggers — correr UNA SOLA VEZ para configurar los triggers.
 *
 * Intervalos:
 *   - Keepalive: 10 min  (Render Free duerme a los 15 min de inactividad)
 *   - News:       1 hora (9 AM UTC — 1h antes del daily, para que AI las lea)
 *   - Daily:      1 hora (10 AM UTC = 6 AM Chile, con guard interno de hora+idempotencia)
 *   - Live:       5 min  (actualización frecuente durante partidos WC2026)
 *   - Weekly orchestration: domingo 2 AM UTC (teams only)
 */
function installMatchAlphaTriggers() {
  removeMatchAlphaTriggers();

  // GAS solo acepta: 1, 5, 10, 15, 30 minutos.
  // Render Free duerme a los 15 min de inactividad → usamos 10 min con margen.
  var keepalive = ScriptApp.newTrigger('pingBackendKeepAlive')
    .timeBased()
    .everyMinutes(10)
    .create();

  // News sync: 9 AM UTC (hora antes del daily a las 10 AM UTC = 6 AM Chile, UTC-4 invierno)
  var news = ScriptApp.newTrigger('runNewsSyncJob')
    .timeBased()
    .atHour(9)
    .everyDays(1)
    .inTimezone('UTC')
    .create();

  var daily = ScriptApp.newTrigger('runDailyBackendOrchestration')
    .timeBased()
    .everyHours(1)
    .create();

  var live = ScriptApp.newTrigger('runLiveBackendOrchestration')
    .timeBased()
    .everyMinutes(5)
    .create();

  // Weekly orchestration — domingos de madrugada UTC para evitar interferir
  // con el daily (10 AM UTC) y el live (durante partidos).
  var weekly = ScriptApp.newTrigger('runWeeklyTeamsSync')
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY)
    .atHour(2)
    .inTimezone('UTC')
    .create();

  var morningSummary = ScriptApp.newTrigger('runMorningTelegramSummary')
    .timeBased()
    .everyHours(1)
    .create();

  var props = PropertiesService.getScriptProperties();
  props.setProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'KEEPALIVE', keepalive.getUniqueId());
  props.setProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'NEWS', news.getUniqueId());
  props.setProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'DAILY', daily.getUniqueId());
  props.setProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'LIVE', live.getUniqueId());
  props.setProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'TEAMS_SYNC', weekly.getUniqueId());
  props.setProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'MORNING_SUMMARY', morningSummary.getUniqueId());

  Logger.log('Triggers instalados: keepalive=10min news=9AM-UTC daily=1h live=5min teams=domingo-2AM(orchestrated) morning_summary=1h@12UTC');
  return {
    ok: true,
    keepalive_trigger_id: keepalive.getUniqueId(),
    news_trigger_id: news.getUniqueId(),
    daily_trigger_id: daily.getUniqueId(),
    live_trigger_id: live.getUniqueId(),
    teams_sync_trigger_id: weekly.getUniqueId(),
    morning_summary_trigger_id: morningSummary.getUniqueId()
  };
}

function removeMatchAlphaTriggers() {
  var handlers = {
    pingBackendKeepAlive: true,
    runNewsSyncJob: true,
    runDailyBackendOrchestration: true,
    runLiveBackendOrchestration: true,
    runWeeklyTeamsSync: true,
    runMorningTelegramSummary: true,
    checkBackendLatestStatus: true,
    checkBackendHealth: true
  };

  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (handlers[trigger.getHandlerFunction()]) {
      ScriptApp.deleteTrigger(trigger);
    }
  });

  var props = PropertiesService.getScriptProperties();
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'KEEPALIVE');
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'NEWS');
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'DAILY');
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'LIVE');
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'TEAMS_SYNC');
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'MORNING_SUMMARY');
  props.deleteProperty(MATCH_ALPHA_CRON_PROPS.TRIGGER_PREFIX + 'PLAYERS_SYNC');

  return { ok: true, removed: true };
}

// ─── HELPERS INTERNOS ─────────────────────────────────────────────────────────

function backendFetch_(path, options) {
  options = options || {};
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    return { ok: false, skipped: true, reason: 'LOCK_BUSY', timestamp: new Date().toISOString() };
  }

  try {
    if (!canUseFetch_()) {
      return { ok: false, skipped: true, reason: 'FETCH_DAILY_LIMIT_REACHED', timestamp: new Date().toISOString() };
    }

    var config = getBackendCronConfig_();
    var url = config.BACKEND_BASE_URL.replace(/\/+$/, '') + path;
    var params = {
      method: options.method || 'get',
      muteHttpExceptions: true,
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + config.API_INTERNAL_KEY }
    };
    if (options.payload) {
      params.payload = JSON.stringify(options.payload);
    }

    incrementFetchCounter_();
    var response;
    try {
      response = UrlFetchApp.fetch(url, params);
    } catch (firstError) {
      Utilities.sleep(1000);
      if (!canUseFetch_()) throw firstError;
      incrementFetchCounter_();
      response = UrlFetchApp.fetch(url, params);
    }

    var statusCode = response.getResponseCode();
    var text = response.getContentText();
    var body = {};
    try {
      body = text ? JSON.parse(text) : {};
    } catch (parseError) {
      body = { raw: text };
    }

    var result = {
      ok: statusCode >= 200 && statusCode < 300,
      status_code: statusCode,
      data: body,
      timestamp: new Date().toISOString()
    };
    PropertiesService.getScriptProperties().setProperty(
      MATCH_ALPHA_CRON_PROPS.LAST_STATUS, JSON.stringify(result)
    );
    Logger.log('backendFetch_ %s %s -> %s', params.method.toUpperCase(), path, statusCode);
    return result;
  } finally {
    lock.releaseLock();
  }
}

function canUseFetch_() {
  resetDailyFetchCounterIfNeeded_();
  var config = getBackendCronConfig_();
  var props = PropertiesService.getScriptProperties();
  var count = Number(props.getProperty(MATCH_ALPHA_CRON_PROPS.FETCH_COUNT) || 0);
  return count < Number(config.MAX_FETCH_PER_DAY || 400);
}

function incrementFetchCounter_() {
  resetDailyFetchCounterIfNeeded_();
  var props = PropertiesService.getScriptProperties();
  var count = Number(props.getProperty(MATCH_ALPHA_CRON_PROPS.FETCH_COUNT) || 0) + 1;
  props.setProperty(MATCH_ALPHA_CRON_PROPS.FETCH_COUNT, String(count));
  return count;
}

function resetDailyFetchCounterIfNeeded_() {
  var props = PropertiesService.getScriptProperties();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var storedDate = props.getProperty(MATCH_ALPHA_CRON_PROPS.FETCH_COUNT_DATE);
  if (storedDate !== today) {
    props.setProperty(MATCH_ALPHA_CRON_PROPS.FETCH_COUNT_DATE, today);
    props.setProperty(MATCH_ALPHA_CRON_PROPS.FETCH_COUNT, '0');
  }
}

function isInMatchWindow_(config) {
  var now = Date.now();
  var start = new Date(config.WC_START_ISO).getTime();
  var end = new Date(config.WC_END_ISO).getTime();
  return now >= start && now <= end;
}

function getBackendCronConfig_() {
  var props = PropertiesService.getScriptProperties();
  return Object.assign({}, MATCH_ALPHA_CRON_CONFIG, {
    BACKEND_BASE_URL: props.getProperty('BACKEND_BASE_URL') || MATCH_ALPHA_CRON_CONFIG.BACKEND_BASE_URL,
    API_INTERNAL_KEY: props.getProperty('API_INTERNAL_KEY') || MATCH_ALPHA_CRON_CONFIG.API_INTERNAL_KEY,
    MAX_FETCH_PER_DAY: Number(props.getProperty('MAX_FETCH_PER_DAY') || MATCH_ALPHA_CRON_CONFIG.MAX_FETCH_PER_DAY),
    DAILY_HOUR_UTC: Number(props.getProperty('DAILY_HOUR_UTC') || MATCH_ALPHA_CRON_CONFIG.DAILY_HOUR_UTC),
    KEEPALIVE_ENABLED: readBooleanProperty_('KEEPALIVE_ENABLED', MATCH_ALPHA_CRON_CONFIG.KEEPALIVE_ENABLED),
    DAILY_JOB_ENABLED: readBooleanProperty_('DAILY_JOB_ENABLED', MATCH_ALPHA_CRON_CONFIG.DAILY_JOB_ENABLED),
    LIVE_JOB_ENABLED: readBooleanProperty_('LIVE_JOB_ENABLED', MATCH_ALPHA_CRON_CONFIG.LIVE_JOB_ENABLED)
  });
}

function readBooleanProperty_(key, fallback) {
  var value = PropertiesService.getScriptProperties().getProperty(key);
  if (value === null || value === undefined || value === '') return fallback;
  return String(value).toLowerCase() === 'true';
}

function logBackendCronResult_(label, result) {
  Logger.log('%s result: %s', label, JSON.stringify(result));
  return result;
}
