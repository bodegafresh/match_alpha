/**
 * Match Alpha — Google Apps Script Scheduler
 *
 * Setup:
 *   1. In GAS editor: File > Project Properties > Script Properties
 *      Add: API_INTERNAL_KEY = <your key>
 *   2. Create time-driven triggers:
 *      - keepalive()           → every 14 minutes  (prevents Render Free from sleeping)
 *      - dailyOrchestration()  → every hour        (internal guard ensures it runs once/day)
 *      - liveOrchestration()   → every 15 minutes  (backend decides if it should run)
 *
 * Render Free sleeps after 15 min of inactivity.
 * Keepalive at 14 min ensures the backend stays warm at all times.
 */

// ─── CONFIGURATION ───────────────────────────────────────────────────────────

var BACKEND_URL = 'https://match-alpha.onrender.com/api/v1';
var MAX_DAILY_FETCHES = 400;            // hard cap — real limit is 20,000 but stay conservative
var DAILY_ORCHESTRATION_HOUR_UTC = 6;  // run daily jobs at 06:00 UTC
var HTTP_TIMEOUT_MS = 25000;           // 25 s — GAS default is 30 s

// World Cup 2026 window — liveOrchestration only fires during this period
var WC_START_MS = new Date('2026-06-11T00:00:00Z').getTime();
var WC_END_MS   = new Date('2026-07-20T00:00:00Z').getTime();

// ─── PUBLIC FUNCTIONS (attach as triggers) ───────────────────────────────────

/**
 * keepalive — run every 14 minutes.
 * Pings the backend so Render Free never hits its 15-min sleep timeout.
 */
function keepalive() {
  if (!_canFetch('keepalive')) return;
  var resp = _post('/jobs/orchestrate/keepalive', {});
  _recordFetch();
  if (!resp) return;
  if (resp.code >= 500) {
    _logError('keepalive', resp.code, resp.body);
  } else {
    Logger.log('[keepalive] ok latency_ms=' + (JSON.parse(resp.body || '{}').database_latency_ms || '?'));
  }
}

/**
 * dailyOrchestration — run every hour.
 * Internal guard: only executes the actual daily job once per UTC day.
 */
function dailyOrchestration() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    Logger.log('[daily] skipped — lock held by another execution');
    return;
  }
  try {
    var now = new Date();
    var hourUtc = now.getUTCHours();

    // Only attempt during the designated hour (or within 59 min after it)
    if (hourUtc !== DAILY_ORCHESTRATION_HOUR_UTC) {
      Logger.log('[daily] skipped — not the right hour (UTC ' + hourUtc + ')');
      return;
    }

    var props = PropertiesService.getScriptProperties();
    var todayKey = 'daily_ran_' + _utcDateStr(0);
    if (props.getProperty(todayKey)) {
      Logger.log('[daily] skipped — already ran today (' + _utcDateStr(0) + ')');
      return;
    }

    if (!_canFetch('daily')) return;

    var resp = _post('/jobs/orchestrate/daily', {});
    _recordFetch();
    if (!resp) return;

    if (resp.code < 500) {
      props.setProperty(todayKey, 'true');
      // Evict yesterday's key to avoid unbounded growth
      props.deleteProperty('daily_ran_' + _utcDateStr(-1));
      Logger.log('[daily] dispatched status=' + resp.code);
    } else {
      _logError('daily', resp.code, resp.body);
    }
  } finally {
    lock.releaseLock();
  }
}

/**
 * liveOrchestration — run every 15 minutes.
 * Only fires during the WC 2026 window; backend further decides whether to run each job.
 */
function liveOrchestration() {
  if (!_isMatchWindow()) {
    Logger.log('[live] skipped — outside WC2026 window');
    return;
  }
  if (!_canFetch('live')) return;

  var resp = _post('/jobs/orchestrate/live', {});
  _recordFetch();
  if (!resp) return;

  if (resp.code >= 500) {
    _logError('live', resp.code, resp.body);
  } else {
    var body = JSON.parse(resp.body || '{}');
    Logger.log('[live] dispatched executed=' + JSON.stringify(body.executed || []) + ' skipped=' + JSON.stringify(body.skipped || []));
  }
}

/**
 * statusCheck — optional, run once a day to log the health endpoint.
 */
function statusCheck() {
  if (!_canFetch('status')) return;
  var resp = _get('/jobs/status/health');
  _recordFetch();
  if (!resp) return;
  Logger.log('[status] ' + resp.body);
}

// ─── PRIVATE HELPERS ─────────────────────────────────────────────────────────

function _post(path, data) {
  return _fetch(path, 'post', JSON.stringify(data || {}));
}

function _get(path) {
  return _fetch(path, 'get', null);
}

function _fetch(path, method, payload) {
  var apiKey = PropertiesService.getScriptProperties().getProperty('API_INTERNAL_KEY');
  if (!apiKey) {
    Logger.log('[match-alpha] API_INTERNAL_KEY not set in Script Properties');
    return null;
  }
  var options = {
    method: method,
    headers: {
      'Authorization': 'Bearer ' + apiKey,
      'Content-Type': 'application/json',
    },
    muteHttpExceptions: true,
    followRedirects: true,
  };
  if (payload !== null) {
    options.payload = payload;
  }
  try {
    var response = UrlFetchApp.fetch(BACKEND_URL + path, options);
    return { code: response.getResponseCode(), body: response.getContentText() };
  } catch (e) {
    _logError(path, 0, e.toString());
    return null;
  }
}

/** Returns true if the current time is within the World Cup 2026 match window. */
function _isMatchWindow() {
  var now = Date.now();
  return now >= WC_START_MS && now <= WC_END_MS;
}

/** Rate-limit guard: returns true if we're within daily fetch budget. */
function _canFetch(caller) {
  var props = PropertiesService.getScriptProperties();
  var key = 'fetch_count_' + _utcDateStr(0);
  var count = parseInt(props.getProperty(key) || '0', 10);
  if (count >= MAX_DAILY_FETCHES) {
    Logger.log('[' + caller + '] skipped — daily fetch limit reached (' + count + ')');
    return false;
  }
  return true;
}

/** Increments the daily fetch counter. */
function _recordFetch() {
  var props = PropertiesService.getScriptProperties();
  var key = 'fetch_count_' + _utcDateStr(0);
  var count = parseInt(props.getProperty(key) || '0', 10);
  props.setProperty(key, String(count + 1));
}

/** Returns UTC date string YYYY-MM-DD with optional day offset. */
function _utcDateStr(offsetDays) {
  var d = new Date();
  d.setDate(d.getDate() + (offsetDays || 0));
  return d.toISOString().slice(0, 10);
}

function _logError(fn, code, msg) {
  Logger.log('[match-alpha][' + fn + '] ERROR HTTP ' + code + ': ' + String(msg || '').slice(0, 300));
}
