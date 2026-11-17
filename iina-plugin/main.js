/* IINA 1.3.5+ bridge: real external subtitle track, no custom video overlay. */
const { console, core, event, file, http, menu, mpv, preferences, sidebar } = iina;

const SERVICE_URL = "http://127.0.0.1:18743/state";
const HEALTH_URL = SERVICE_URL.replace(/\/state$/, "/health");
const CONTROL_URL = SERVICE_URL.replace(/\/state$/, "/control");
const TOKEN_PATH = "~/Library/Application Support/LiveTranslate/iina-token";
const TRACK_TITLE = "Русский · Live Translation (локально)";
const CHUNK_SECONDS = 30;
const BUFFER_TIMEOUT_MS = 45000;
const REQUEST_TIMEOUT_MS = 10000;

let enabled = preferences.get("enabled") !== false;
let mediaPath = "";
let epoch = 0;
let busy = false;
let closed = false;
let token = "";
let pendingStops = [];
let subtitlePath = "";
let revision = -1;
let wantSelect = true;
let previousSid = "no";
let state = null;
let lastProblem = "";
let lastUnsupportedPath = "";
let retryAfter = 0;
let seekGeneration = 0;
let urgentTick = false;
let bufferWait = null;
let bufferBypass = null;
let internalRefresh = null;
let changingSubtitles = false;
let subtitleIntent = null;
let followRussian = true;
let serviceReady = false;
let serviceRuntime = "";
let serviceFailures = 0;
const clientId = "iina-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
let requestSequence = 0;
let sourceSubtitleId = null;
let sourceSelectionKey = "";
let backendSourceRevision = null;
let selectedCacheKey = "";
let originalPath = "";
let originalRevision = -1;
let secondaryHiddenByUser = false;
let secondaryReplacedByUser = false;
let sidebarLoaded = false;
let sidebarReady = false;
let sidebarDescription = { settings: {}, models: [], cache: [], languages: {}, model_error: "" };
let sidebarOperation = { busy: false, error: "", requestId: "", action: "" };
let sidebarQueue = [];

function settings() {
  return sidebarDescription.settings || {};
}

function generatedTrack(track) {
  const path = normalizePath(track && track["external-filename"]);
  return Boolean(track && track.type === "sub" && (
    (path && (path === subtitlePath || path === originalPath || path.indexOf("/LiveTranslate/subtitles/") >= 0)) ||
    String(track.title || "").indexOf("Live Translation") >= 0
  ));
}

function trackId(value) {
  const number = Number(value);
  return value !== "" && Number.isInteger(number) && number > 0 ? number : null;
}

function selectedSource() {
  const list = tracks().filter(track => !generatedTrack(track));
  const subs = list.filter(track => track.type === "sub");
  const selected = subs.find(track => String(track.id) === currentSid());
  if (selected) sourceSubtitleId = trackId(selected.id);
  if (!subs.some(track => track.id === sourceSubtitleId)) {
    // With no explicit native choice, the backend chooses an auto source using
    // the configured language. Never silently nominate an English fallback.
    const preferred = settings().source_mode === "subtitles" ? subs[0] : null;
    sourceSubtitleId = preferred ? trackId(preferred.id) : null;
  }
  const audio = list.filter(track => track.type === "audio");
  const audioId = trackId(mpv.getString("aid")) ||
    trackId((audio.find(track => track.selected) || audio[0] || {}).id);
  return {
    mode: settings().source_mode || "auto",
    subtitle_track_id: sourceSubtitleId,
    audio_track_id: audioId,
    tracks: list,
    subtitle_delay: Number(mpv.getNumber("sub-delay")) || 0,
    audio_delay: Number(mpv.getNumber("audio-delay")) || 0
  };
}

function selectionKey(selection) {
  return JSON.stringify([selection.mode, selection.subtitle_track_id, selection.audio_track_id,
    selection.subtitle_delay, selection.audio_delay]);
}

function clearGeneratedTracks() {
  const primary = subtitlePath ? ownTrack(subtitlePath) : null;
  const original = originalPath ? ownTrack(originalPath) : null;
  changeSubtitles(() => {
    if (primary && currentSid() === String(primary.id)) {
      mpv.set("sid", sidStillExists(previousSid) ? previousSid : "no");
    }
    if (original && String(mpv.getString("secondary-sid")) === String(original.id)) {
      mpv.set("secondary-sid", "no");
    }
    if (primary) mpv.command("sub-remove", [String(primary.id)]);
    if (original && (!primary || original.id !== primary.id)) {
      mpv.command("sub-remove", [String(original.id)]);
    }
  });
  subtitlePath = "";
  originalPath = "";
  revision = -1;
  originalRevision = -1;
}

function invalidateSource() {
  const current = subtitlePath ? ownTrack(subtitlePath) : null;
  if (current && (currentSid() !== String(current.id) || !mpv.getFlag("sub-visibility"))) {
    followRussian = false;
    wantSelect = false;
  }
  releaseBuffer(true);
  clearGeneratedTracks();
  epoch += 1;
  seekGeneration += 1;
  requestSequence += 1;
  busy = false;
  selectedCacheKey = "";
  backendSourceRevision = null;
  state = null;
  bufferBypass = null;
  internalRefresh = null;
  if (followRussian) wantSelect = true;
  urgentTick = true;
  retryAfter = 0;
}

function onSourceChanged() {
  if (closed || !mediaPath || changingSubtitles) return;
  const next = selectionKey(selectedSource());
  if (sourceSelectionKey && next !== sourceSelectionKey) invalidateSource();
  sourceSelectionKey = next;
  publishSidebar();
  tick();
}

function languageName(code) {
  const languages = sidebarDescription.languages || {};
  if (!Array.isArray(languages) && typeof languages[code] === "string") return languages[code];
  const item = Array.isArray(languages)
    ? languages.find(value => value && (value.code === code || value.id === code)) : null;
  return item ? String(item.label || item.name || code) : ({ ru: "Русский", en: "Английский" }[code] || code);
}

function applyOriginal(result) {
  const path = normalizePath(result.original_path);
  const previous = originalPath ? ownTrack(originalPath) : null;
  if (!settings().bilingual) {
    if (previous) changeSubtitles(() => {
      if (String(mpv.getString("secondary-sid")) === String(previous.id)) mpv.set("secondary-sid", "no");
      mpv.command("sub-remove", [String(previous.id)]);
    });
    originalPath = "";
    originalRevision = -1;
    return;
  }
  if (!path || secondaryHiddenByUser || secondaryReplacedByUser) return;
  const secondary = String(mpv.getString("secondary-sid") || "no");
  if (secondary !== "no" && secondary !== "0" && secondary !== "" &&
      (!previous || secondary !== String(previous.id))) return;
  changeSubtitles(() => {
    const primarySid = currentSid();
    let track = ownTrack(path);
    let changed = false;
    if (!track) {
      mpv.command("sub-add", [path, "auto", "Оригинал · Live Translation (локально)",
        result.source_language && result.source_language !== "auto" ? result.source_language : "und"]);
      track = ownTrack(path);
      changed = true;
    } else if (originalPath !== path || originalRevision !== Number(result.revision)) {
      mpv.command("sub-reload", [String(track.id)]);
      track = ownTrack(path);
      changed = true;
    }
    if (!track) return;
    mpv.set("secondary-sid", String(track.id));
    if (!previous) mpv.set("secondary-sub-visibility", true);
    if (sidStillExists(primarySid)) mpv.set("sid", primarySid);
    if (previous && previous.id !== track.id) mpv.command("sub-remove", [String(previous.id)]);
    originalPath = path;
    originalRevision = Number(result.revision);
    if (changed && mpv.getFlag("pause")) {
      internalRefresh = { file: mediaPath, position: currentPosition(), until: Date.now() + 1200 };
      mpv.command("seek", ["0", "relative+exact"]);
    }
  });
}

function onSecondaryChanged(property) {
  if (changingSubtitles || !originalPath) return;
  const track = ownTrack(originalPath);
  if (!track) return;
  if (property === "secondary-sub-visibility") secondaryHiddenByUser = !mpv.getFlag(property);
  else secondaryReplacedByUser = String(mpv.getString("secondary-sid")) !== String(track.id);
  publishSidebar();
}

// IINA 1.3–1.5 interpolate WebView messages into a template literal. Keep every
// untrusted string inside a second JSON envelope without $, backticks or Unicode.
function safeSidebarEnvelope(value) {
  return { json: JSON.stringify(value).replace(/[^\x20-\x7e]|[$`]/g, character =>
    "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0")) };
}

function publishSidebar() {
  if (!sidebarReady || !sidebar) return;
  try {
    const secondId = String(mpv.getString("secondary-sid") || "no");
    const second = originalPath ? ownTrack(originalPath) : null;
    const occupied = secondId !== "no" && secondId !== "0" && secondId !== "" &&
      (!second || secondId !== String(second.id));
    sidebar.postMessage("lt:state", safeSidebarEnvelope({
      enabled, connected: serviceReady, status: statusText(), state: state || {},
      media: mediaPath ? mediaPath.split("/").pop() : "", position: currentPosition(),
      duration: Number(mpv.getNumber("duration")) || 0,
      settings: settings(), models: sidebarDescription.models || [],
      cache: sidebarDescription.cache || [], languages: sidebarDescription.languages || {},
      modelError: sidebarDescription.model_error || "",
      operation: sidebarOperation, secondaryBusy: occupied || secondaryReplacedByUser
    }));
  } catch (error) { console.log("Не удалось обновить панель перевода"); }
}

function acceptDescription(result) {
  const description = result.sidebar || result;
  for (const key of ["settings", "models", "cache", "languages"]) {
    if (description[key] !== undefined) sidebarDescription[key] = description[key];
  }
  if (description.models !== undefined) sidebarDescription.model_error = description.model_error || "";
}

function controlRequest(action, data, requestId) {
  if (closed) return;
  if (action === "enabled") {
    setEnabled(Boolean(data.enabled));
    return;
  }
  if (["describe", "settings", "cache_use", "cache_delete", "retry"].indexOf(action) < 0) return;
  if (sidebarOperation.busy) {
    if (sidebarQueue.length < 8) sidebarQueue.push({ action, data, requestId });
    return;
  }
  if (action === "settings") {
    const filtered = {};
    for (const key of ["translation_model", "source_language", "target_language", "source_mode",
      "bilingual", "wait_for_translation", "vad_mode", "batch_size", "glossary"]) {
      if (Object.prototype.hasOwnProperty.call(data, key)) filtered[key] = data[key];
    }
    data = filtered;
  }
  sidebarOperation = { busy: true, error: "", requestId: String(requestId || ""), action };
  const oldBilingual = Boolean(settings().bilingual);
  const generationChanged = action === "settings" && ["translation_model", "source_language",
    "target_language", "source_mode", "vad_mode", "batch_size", "glossary"].some(key =>
    Object.prototype.hasOwnProperty.call(data, key) && JSON.stringify(data[key]) !== JSON.stringify(settings()[key]));
  if (generationChanged || action === "cache_use") invalidateSource();
  publishSidebar();
  let auth;
  try { auth = authToken(); } catch (error) {
    sidebarOperation.busy = false;
    sidebarOperation.error = String(error.message || error);
    publishSidebar();
    return;
  }
  serviceRequest(() => http.post(CONTROL_URL, {
    headers: { "Authorization": "Bearer " + auth, "Content-Type": "application/x-www-form-urlencoded" },
    data: { action, data: JSON.stringify(data || {}), client_id: clientId,
      session_id: clientId + ":" + epoch, path: mediaPath, position: String(currentPosition()) }
  })).then(response => {
    const result = responseData(response);
    if (result.error) throw new Error(String(result.error));
    acceptDescription(result);
    if (settings().wait_for_translation === false) releaseBuffer(true);
    if (action === "cache_use") selectedCacheKey = String(data.key || "");
    if (action === "cache_delete" && selectedCacheKey === String(data.key || "")) selectedCacheKey = "";
    if (Boolean(settings().bilingual) && !oldBilingual) {
      secondaryHiddenByUser = false;
      secondaryReplacedByUser = false;
    }
    sourceSelectionKey = selectionKey(selectedSource());
    retryAfter = 0;
    urgentTick = true;
  }).catch(error => {
    sidebarOperation.error = String(error && error.message || "Не удалось выполнить действие. Попробуйте ещё раз.");
  }).finally(() => {
    sidebarOperation.busy = false;
    publishSidebar();
    if (sidebarQueue.length) {
      const next = sidebarQueue.shift();
      controlRequest(next.action, next.data, next.requestId);
    } else tick();
  });
}

function loadSidebar() {
  if (!sidebar || sidebarLoaded || !core.window || !core.window.loaded) return;
  try {
    sidebar.loadFile("sidebar.html");
    sidebarLoaded = true;
    sidebar.onMessage("lt:ready", () => {
      sidebarReady = true;
      publishSidebar();
      controlRequest("describe", {}, "initial");
    });
    sidebar.onMessage("lt:action", message => {
      if (!message || typeof message !== "object" || Array.isArray(message)) return;
      controlRequest(message.action, message.data || {}, message.requestId);
    });
  } catch (error) { console.log("Не удалось открыть панель перевода"); }
}

function osd(message) {
  try {
    core.osd(message);
  } catch (error) {
    console.log(message);
  }
}

function normalizePath(value) {
  if (typeof value !== "string") return "";
  if (value.indexOf("file://") === 0) {
    const encoded = value.slice(7);
    // Only local file URLs; a file://host URL is not a local video path.
    if (encoded.charAt(0) !== "/") return "";
    try {
      value = decodeURIComponent(encoded);
    } catch (error) {
      return "";
    }
  }
  return value.charAt(0) === "/" ? value : "";
}

function tracks() {
  return mpv.getNative("track-list") || [];
}

function ownTrack(path) {
  return tracks().find((track) => (
    track.type === "sub" &&
    normalizePath(track["external-filename"]) === path
  ));
}

function currentSid() {
  return mpv.getString("sid") || "no";
}

function currentPosition() {
  return Math.max(0, mpv.getNumber("time-pos") || 0);
}

function chunkAt(position) {
  return Math.floor(Math.max(0, position) / CHUNK_SECONDS);
}

function expectsRussian() {
  if (wantSelect) return true;
  const track = subtitlePath ? ownTrack(subtitlePath) : null;
  return Boolean(track && currentSid() === String(track.id) && mpv.getFlag("sub-visibility"));
}

function bypassCurrentChunk() {
  bufferBypass = { file: mediaPath, generation: seekGeneration, chunk: chunkAt(currentPosition()) };
}

function isBypassed(chunk) {
  return bufferBypass && bufferBypass.file === mediaPath &&
    bufferBypass.generation === seekGeneration && bufferBypass.chunk === chunk;
}

function releaseBuffer(resume) {
  const wait = bufferWait;
  bufferWait = null;
  if (resume && wait && wait.ownsPause && wait.file === mediaPath &&
      wait.file === normalizePath(mpv.getString("path") || "") && mpv.getFlag("pause")) {
    // Clear ownership before setting pause: its native change event may run now.
    mpv.set("pause", false);
  }
}

function bufferFailure(message) {
  if (!bufferWait) return;
  bypassCurrentChunk();
  releaseBuffer(true);
  osd(message);
}

function checkBufferTimeout() {
  if (bufferWait && Date.now() - bufferWait.startedAt >= BUFFER_TIMEOUT_MS) {
    bufferFailure("Перевод готовится дольше 45 секунд. Перевод продолжится в фоне.");
  }
}

function handleBuffering(result, requestPosition, requestSeekGeneration, applied) {
  if (settings().wait_for_translation === false) {
    releaseBuffer(true);
    return;
  }
  if (requestSeekGeneration !== seekGeneration || chunkAt(currentPosition()) !== chunkAt(requestPosition)) return;
  if (result.read_only && result.current_ready === false) {
    releaseBuffer(true);
    if (!isBypassed(chunkAt(requestPosition))) {
      bypassCurrentChunk();
      osd("В этой сохранённой версии нет перевода для этого участка.");
    }
    return;
  }
  if (!expectsRussian()) {
    releaseBuffer(true);
    return;
  }
  if (result.error) {
    bufferFailure("Не удалось подготовить перевод: " + result.error);
    return;
  }
  const chunk = chunkAt(requestPosition);
  if (result.current_ready === true) {
    // A null path is a successfully processed silent chunk, with no filler SRT.
    if (bufferWait && bufferWait.file === mediaPath &&
        bufferWait.generation === seekGeneration && (applied || !result.path)) {
      releaseBuffer(true);
    }
    return;
  }
  if (result.current_ready !== false || isBypassed(chunk)) return;
  if (!bufferWait || bufferWait.file !== mediaPath || bufferWait.generation !== seekGeneration ||
      bufferWait.chunk !== chunk) {
    const inheritedOwnership = Boolean(bufferWait && bufferWait.file === mediaPath &&
      bufferWait.ownsPause && mpv.getFlag("pause"));
    const wasPlaying = !mpv.getFlag("pause");
    bufferWait = {
      file: mediaPath, generation: seekGeneration, chunk,
      ownsPause: inheritedOwnership || wasPlaying, startedAt: Date.now()
    };
    if (wasPlaying) mpv.set("pause", true);
    osd("Готовлю перевод этого участка…");
  }
}

function onSeek() {
  const position = currentPosition();
  if (internalRefresh && internalRefresh.file === mediaPath &&
      Date.now() <= internalRefresh.until && Math.abs(position - internalRefresh.position) < 0.35) {
    internalRefresh = null;
    return;
  }
  internalRefresh = null;
  seekGeneration += 1;
  bufferBypass = null;
  if (bufferWait) {
    if (bufferWait.file === mediaPath && bufferWait.ownsPause && mpv.getFlag("pause")) {
      // A new seek owns a new target, while retaining the user's original play intent.
      bufferWait.generation = seekGeneration;
      bufferWait.chunk = chunkAt(position);
      bufferWait.startedAt = Date.now();
    } else {
      releaseBuffer(false);
    }
  }
  urgentTick = true;
  retryAfter = 0;
  updateMenu();
  tick();
}

function onPauseChanged() {
  if (bufferWait && !mpv.getFlag("pause")) {
    // An explicit user resume is authoritative; do not pause again on each poll.
    bypassCurrentChunk();
    releaseBuffer(false);
    updateMenu();
  }
}

function rememberSubtitleIntent() {
  subtitleIntent = { sid: currentSid(), visible: mpv.getFlag("sub-visibility") };
}

function changeSubtitles(action) {
  const previous = changingSubtitles;
  changingSubtitles = true;
  try {
    return action();
  } finally {
    changingSubtitles = previous;
    rememberSubtitleIntent();
  }
}

function onSubtitleIntentChanged(property) {
  const previous = subtitleIntent;
  rememberSubtitleIntent();
  if (changingSubtitles) return;
  const changed = previous && (
    (property === "sid" && previous.sid !== subtitleIntent.sid) ||
    (property === "sub-visibility" && previous.visible !== subtitleIntent.visible)
  );
  const track = subtitlePath ? ownTrack(subtitlePath) : null;
  // A disappearing generated track can reset sid during a native reload.
  // Keep the last intent so the next response can attach that track again.
  if (changed && property === "sid" && (track || subtitleIntent.sid !== "no")) {
    followRussian = Boolean(track && subtitleIntent.sid === String(track.id) && subtitleIntent.visible);
    if (!followRussian) wantSelect = false;
  }
  if (changed && property === "sub-visibility") {
    if (!subtitleIntent.visible) {
      followRussian = false;
      wantSelect = false;
    } else if (track && subtitleIntent.sid === String(track.id)) followRussian = true;
  }
  onSourceChanged();
  if (!bufferWait) return;
  // Initial native notifications may repeat the default "no subtitles" state.
  // Only a real user change cancels our first automatic track selection.
  if (wantSelect && previous && (
    (property === "sid" && previous.sid !== subtitleIntent.sid) ||
    (property === "sub-visibility" && previous.visible !== subtitleIntent.visible && !subtitleIntent.visible)
  )) wantSelect = false;
  if (bufferWait && !expectsRussian()) {
    bypassCurrentChunk();
    releaseBuffer(true);
    updateMenu();
  }
}

function sidStillExists(sid) {
  return sid === "no" || tracks().some((track) => (
    track.type === "sub" && String(track.id) === sid
  ));
}

function selectOwnTrack(track) {
  changeSubtitles(() => {
    const sid = currentSid();
    if (sid !== String(track.id)) previousSid = sid;
    mpv.set("sid", String(track.id));
    mpv.set("sub-visibility", true);
    wantSelect = false;
    followRussian = true;
  });
}

function restoreUserSelection() {
  const track = subtitlePath ? ownTrack(subtitlePath) : null;
  if (track && currentSid() === String(track.id)) {
    changeSubtitles(() => mpv.set("sid", sidStillExists(previousSid) ? previousSid : "no"));
  }
}

function applySubtitles(result) {
  return changeSubtitles(() => applySubtitlesNow(result));
}

function applySubtitlesNow(result) {
  const path = normalizePath(result.path);
  if (!path || !file.exists(path)) return false;
  const nextRevision = Number(result.revision);
  const sidBefore = currentSid();
  const previousTrack = subtitlePath && subtitlePath !== path ? ownTrack(subtitlePath) : null;
  let track = ownTrack(path);
  if (!track && followRussian) wantSelect = true;
  const previousWasSelected = previousTrack && sidBefore === String(previousTrack.id);
  const currentWasSelected = track && sidBefore === String(track.id);
  const shouldSelect = wantSelect || previousWasSelected || currentWasSelected;
  let changed = false;

  // Replacing our cache filename must not make the old generated track the
  // "user's previous subtitle". Stop should still restore the original choice.
  if (wantSelect && !previousWasSelected && !currentWasSelected) previousSid = sidBefore;

  if (!track) {
    mpv.command("sub-add", [
      path, shouldSelect ? "select" : "auto",
      settings().target_language && settings().target_language !== "ru"
        ? languageName(settings().target_language) + " · Live Translation (локально)" : TRACK_TITLE,
      settings().target_language || "rus"
    ]);
    track = ownTrack(path);
    changed = true;
  } else if (subtitlePath !== path || revision !== nextRevision) {
    // sub-reload unloads/re-adds the track. Never assume its ID survives.
    mpv.command("sub-reload", [String(track.id)]);
    track = ownTrack(path);
    changed = true;
  }

  if (!track) return false;
  if (shouldSelect && (changed || wantSelect)) {
    mpv.set("sid", String(track.id));
    if (wantSelect) mpv.set("sub-visibility", true);
    wantSelect = false;
  } else if (changed && sidStillExists(sidBefore)) {
    mpv.set("sid", sidBefore);
  }
  // Remove only the exact track we previously owned, after the replacement is
  // usable and selected if appropriate. Other subtitle tracks stay untouched.
  if (previousTrack && previousTrack.id !== track.id) {
    mpv.command("sub-remove", [String(previousTrack.id)]);
  }
  subtitlePath = path;
  revision = nextRevision;
  // A newly loaded/reloaded external track needs a video refresh when paused.
  if (changed && mpv.getFlag("pause")) {
    internalRefresh = { file: mediaPath, position: currentPosition(), until: Date.now() + 1200 };
    mpv.command("seek", ["0", "relative+exact"]);
  }
  return true;
}

function responseData(response) {
  const data = response.data;
  const result = data && typeof data === "object" ? data : JSON.parse(response.text || data);
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new Error("Invalid local service response");
  }
  return result;
}

function hasRuntime(result) {
  return result.protocol === 1 && typeof result.runtime_id === "string" &&
    result.runtime_id.length > 0 && Number.isInteger(result.process_id) && result.process_id > 0;
}

function useRuntime(identity) {
  const runtime = identity.runtime_id + ":" + identity.process_id;
  if (serviceRuntime && serviceRuntime !== runtime) {
    const track = subtitlePath ? ownTrack(subtitlePath) : null;
    // Poll the current native choice too: native change notifications can arrive
    // after an HTTP response, and an update must never undo the user's choice.
    if (track && (currentSid() !== String(track.id) || !mpv.getFlag("sub-visibility"))) {
      followRussian = false;
    }
    revision = -1;
    originalRevision = -1;
    if (followRussian) wantSelect = true;
  }
  serviceRuntime = runtime;
}

function retryService(message) {
  serviceReady = false;
  serviceFailures += 1;
  const delays = [3000, 5000, 10000, 15000];
  retryAfter = Date.now() + delays[Math.min(serviceFailures - 1, delays.length - 1)];
  lastProblem = message;
  bufferFailure(message);
}

function serviceRequest(request) {
  let timer;
  const timeout = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error("Local service request timed out")), REQUEST_TIMEOUT_MS);
  });
  // A late response belongs to the timed-out request and cannot change tracks
  // or overwrite the recovered connection's state.
  return Promise.race([Promise.resolve().then(request), timeout]).finally(() => clearTimeout(timer));
}

function checkService() {
  const requestSerial = ++requestSequence;
  busy = true;
  serviceRequest(() => http.get(HEALTH_URL, {})).then((response) => {
    const health = responseData(response);
    if (health.status !== "ok" || health.service !== "iina-local-translation" ||
        !hasRuntime(health) || typeof health.version !== "string" || !health.version ||
        !Array.isArray(health.capabilities) ||
        !["state_form", "native_srt"].every(name => health.capabilities.indexOf(name) >= 0)) {
      retryService("Служба обновляется… Подключусь автоматически.");
      return;
    }
    useRuntime(health);
    serviceReady = true;
    serviceFailures = 0;
    retryAfter = 0;
    lastProblem = "";
  }).catch(() => {
    retryService("Локальный сервис перевода пока недоступен. Подключусь автоматически…");
  }).finally(() => {
    if (requestSerial !== requestSequence) return;
    busy = false;
    if (!closed) {
      updateMenu();
      if (serviceReady && (pendingStops.length || (enabled && mediaPath))) setTimeout(tick, 0);
    }
  });
}

function statusText() {
  if (!enabled) return "Локальный перевод выключен";
  if (!mediaPath) return "Откройте локальное видео в IINA";
  if (lastProblem) return lastProblem;
  if (bufferWait) return "Готовлю перевод этого участка…";
  if (!state) return "Подключаю локальный перевод…";
  if (state.error) return "Ошибка локального перевода: " + state.error;
  if (state.read_only) return state.current_ready === false
    ? "В этой версии нет перевода для этого участка"
    : "Сохранённая версия · только просмотр";
  if (state.done) return "Кэш всего видео готов · 100%";
  const percent = Math.max(0, Math.min(100, Math.floor(Number(state.prepared_percent || 0))));
  const buffered = Math.max(0, Math.floor(
    Number(state.buffered_until || 0) - mpv.getNumber("time-pos")
  ));
  if (subtitlePath && state.current_ready) {
    return "Кэш видео: " + percent + "% · готово вперёд: " + buffered + " с";
  }
  if (state.status === "loading") return "Загружаю локальные модели…";
  if (state.processing) return "Готовлю полный кэш видео: " + percent + "%…";
  return "Готовлю перевод…";
}

function updateMenu() {
  enabledItem.selected = enabled;
  statusItem.title = statusText();
  startItem.enabled = Boolean(mediaPath);
  stopItem.enabled = enabled;
  publishSidebar();
}

function authToken() {
  if (!token) {
    if (!file.exists(TOKEN_PATH)) {
      throw new Error("Не найден ключ локального сервиса");
    }
    token = String(file.read(TOKEN_PATH, {}) || "").trim();
    if (!token) throw new Error("Не найден ключ локального сервиса");
  }
  return token;
}

function stopPath(path) {
  if (path && pendingStops.indexOf(path) < 0) pendingStops.push(path);
}

function requestState(path, isEnabled, requestEpoch) {
  let auth;
  try {
    auth = authToken();
  } catch (error) {
    lastProblem = String(error.message || error);
    bufferFailure(lastProblem);
    updateMenu();
    retryAfter = Date.now() + 5000;
    return;
  }

  const requestSerial = ++requestSequence;
  busy = true;
  const position = isEnabled ? currentPosition() : 0;
  const requestSeekGeneration = seekGeneration;
  const selection = selectedSource();
  // IINA 1.3.5's Just-based HTTP API sends `data` as form fields, not JSON.
  serviceRequest(() => http.post(SERVICE_URL, {
    headers: {
      "Authorization": "Bearer " + auth,
      "Content-Type": "application/x-www-form-urlencoded"
    },
    data: {
      path: path,
      position: String(position),
      paused: mpv.getFlag("pause") ? "true" : "false",
      enabled: isEnabled ? "true" : "false",
      native_russian_selected: String(Boolean(subtitlePath && ownTrack(subtitlePath) &&
        currentSid() === String(ownTrack(subtitlePath).id) && mpv.getFlag("sub-visibility"))),
      waiting_for_translation: String(Boolean(bufferWait)),
      seek_generation: String(seekGeneration),
      selection: JSON.stringify(selection), client_id: clientId,
      session_id: clientId + ":" + requestEpoch, cache_key: selectedCacheKey
    }
  })).then((response) => {
    if (closed || !isEnabled || requestEpoch !== epoch || path !== mediaPath || !enabled) return;
    if (requestSeekGeneration !== seekGeneration) {
      urgentTick = true;
      return;
    }
    const result = responseData(response);
    if (!hasRuntime(result)) {
      retryService("Служба обновляется… Подключусь автоматически.");
      return;
    }
    useRuntime(result);
    acceptDescription(result);
    if (result.source_revision !== undefined) {
      if (backendSourceRevision !== null && backendSourceRevision !== result.source_revision) {
        clearGeneratedTracks();
        if (followRussian) wantSelect = true;
      }
      backendSourceRevision = result.source_revision;
    }
    if (result.initializing && subtitlePath) {
      clearGeneratedTracks();
      if (followRussian) wantSelect = true;
    }
    serviceFailures = 0;
    lastProblem = "";
    retryAfter = 0;
    state = result;
    if (result.error) {
      console.log("Local subtitle service: " + result.error);
    }
    const applied = applySubtitles(result);
    applyOriginal(result);
    handleBuffering(result, position, requestSeekGeneration, applied);
  }).catch((error) => {
    if (closed || requestEpoch !== epoch || !isEnabled || requestSeekGeneration !== seekGeneration) return;
    const code = error && error.statusCode;
    if (code === 401 || code === 403) token = "";
    retryService(code === 401
      ? "Обновляю подключение к локальному сервису…"
      : "Локальный сервис перевода пока недоступен");
    console.log(lastProblem);
  }).finally(() => {
    if (requestSerial !== requestSequence) return;
    busy = false;
    if (!closed) {
      updateMenu();
      if (pendingStops.length || urgentTick) setTimeout(tick, 0);
    }
  });
}

function tick() {
  if (closed) return;
  checkBufferTimeout();
  if (sidebarOperation.busy) return;
  if (enabled && mediaPath) {
    const next = selectionKey(selectedSource());
    if (sourceSelectionKey && next !== sourceSelectionKey) invalidateSource();
    sourceSelectionKey = next;
  }
  if (busy || Date.now() < retryAfter) return;
  if (!serviceReady && (pendingStops.length || (enabled && mediaPath))) {
    checkService();
    return;
  }
  if (pendingStops.length) {
    requestState(pendingStops.shift(), false, epoch);
  } else if (enabled && mediaPath) {
    urgentTick = false;
    requestState(mediaPath, true, epoch);
  }
}

function clearMedia() {
  releaseBuffer(false);
  clearGeneratedTracks();
  requestSequence += 1;
  busy = false;
  bufferBypass = null;
  internalRefresh = null;
  seekGeneration += 1;
  stopPath(mediaPath);
  mediaPath = "";
  epoch += 1;
  subtitlePath = "";
  revision = -1;
  state = null;
  previousSid = "no";
  wantSelect = true;
  followRussian = true;
  sourceSubtitleId = null;
  sourceSelectionKey = "";
  backendSourceRevision = null;
  selectedCacheKey = "";
  secondaryHiddenByUser = false;
  secondaryReplacedByUser = false;
  subtitleIntent = null;
  lastProblem = "";
}

function fileLoaded() {
  clearMedia();
  const rawPath = mpv.getString("path") || "";
  const path = normalizePath(rawPath);
  if (path && file.exists(path)) {
    mediaPath = path;
  } else if (enabled && rawPath && lastUnsupportedPath !== rawPath) {
    lastUnsupportedPath = rawPath;
    osd("Локальный перевод сейчас поддерживает видеофайлы на Mac");
  }
  rememberSubtitleIntent();
  sourceSelectionKey = selectionKey(selectedSource());
  updateMenu();
  tick();
}

function setEnabled(value, selectTrack) {
  enabled = Boolean(value);
  preferences.set("enabled", enabled);
  preferences.sync();
  if (enabled) {
    bufferBypass = null;
    if (selectTrack !== false) {
      wantSelect = true;
      followRussian = true;
    }
    lastProblem = "";
    retryAfter = 0;
    const track = subtitlePath ? ownTrack(subtitlePath) : null;
    if (track && wantSelect) selectOwnTrack(track);
    osd(mediaPath ? "Локальный перевод включён" : "Перевод включён — откройте видео");
  } else {
    releaseBuffer(true);
    stopPath(mediaPath);
    restoreUserSelection();
    osd("Локальный перевод выключен");
  }
  updateMenu();
  tick();
}

const rootItem = menu.item("Локальный перевод", null, {});
const enabledItem = menu.item("Переводить автоматически", () => setEnabled(!enabled), { selected: enabled });
const startItem = menu.item("Включить и показать перевод", () => setEnabled(true), {});
const stopItem = menu.item("Остановить перевод", () => setEnabled(false), {});
const statusItem = menu.item("Готовлю локальный перевод…", () => osd(statusText()), {});
rootItem.addSubMenuItem(enabledItem);
rootItem.addSubMenuItem(startItem);
rootItem.addSubMenuItem(stopItem);
rootItem.addSubMenuItem(menu.separator());
rootItem.addSubMenuItem(statusItem);
rootItem.addSubMenuItem(menu.item("Настройки и кэш…", () => {
  loadSidebar();
  if (sidebar && sidebarLoaded) sidebar.show();
}, {}));
menu.addItem(rootItem);

event.on("mpv.file-loaded", fileLoaded);
event.on("mpv.seek", onSeek);
event.on("mpv.playback-restart", () => {
  urgentTick = true;
  tick();
});
event.on("mpv.pause.changed", onPauseChanged);
event.on("mpv.sid.changed", () => onSubtitleIntentChanged("sid"));
event.on("mpv.sub-visibility.changed", () => onSubtitleIntentChanged("sub-visibility"));
event.on("mpv.secondary-sid.changed", () => onSecondaryChanged("secondary-sid"));
event.on("mpv.secondary-sub-visibility.changed", () => onSecondaryChanged("secondary-sub-visibility"));
for (const property of ["aid", "track-list", "sub-delay", "audio-delay"]) {
  event.on("mpv." + property + ".changed", onSourceChanged);
}
event.on("iina.window-loaded", loadSidebar);
event.on("mpv.end-file", () => {
  clearMedia();
  updateMenu();
  tick();
});
event.on("mpv.shutdown", () => {
  stopPath(mediaPath);
  if (!busy && pendingStops.length) requestState(pendingStops.shift(), false, epoch);
  closed = true;
  clearInterval(timer);
});

const timer = setInterval(tick, 1000);
updateMenu();
loadSidebar();
console.log("Native local subtitle bridge loaded (loopback only)");
// Also attach when IINA reloads plugins while a video is already open.
if (mpv.getString("path") && mpv.getNumber("duration") > 0) fileLoaded();
