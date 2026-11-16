/* IINA 1.3.5+ bridge: real external subtitle track, no custom video overlay. */
const { console, core, event, file, http, menu, mpv, preferences } = iina;

const SERVICE_URL = "http://127.0.0.1:18743/state";
const TOKEN_PATH = "~/Library/Application Support/LiveTranslate/iina-token";
const TRACK_TITLE = "Русский · Live Translation (локально)";
const CHUNK_SECONDS = 30;
const BUFFER_TIMEOUT_MS = 45000;

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
  if (requestSeekGeneration !== seekGeneration || chunkAt(currentPosition()) !== chunkAt(requestPosition)) return;
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
  if (changingSubtitles || !bufferWait) return;
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
  const previousWasSelected = previousTrack && sidBefore === String(previousTrack.id);
  const currentWasSelected = track && sidBefore === String(track.id);
  const shouldSelect = wantSelect || previousWasSelected || currentWasSelected;
  let changed = false;

  // Replacing our cache filename must not make the old generated track the
  // "user's previous subtitle". Stop should still restore the original choice.
  if (wantSelect && !previousWasSelected && !currentWasSelected) previousSid = sidBefore;

  if (!track) {
    mpv.command("sub-add", [
      path, shouldSelect ? "select" : "auto", TRACK_TITLE, "rus"
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

function statusText() {
  if (!enabled) return "Локальный перевод выключен";
  if (!mediaPath) return "Откройте локальное видео в IINA";
  if (lastProblem) return lastProblem;
  if (bufferWait) return "Готовлю перевод этого участка…";
  if (!state) return "Подключаю локальный перевод…";
  if (state.error) return "Ошибка локального перевода: " + state.error;
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
  return "Готовлю русские субтитры…";
}

function updateMenu() {
  enabledItem.selected = enabled;
  statusItem.title = statusText();
  startItem.enabled = Boolean(mediaPath);
  stopItem.enabled = enabled;
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

  busy = true;
  const position = isEnabled ? currentPosition() : 0;
  const requestSeekGeneration = seekGeneration;
  // IINA 1.3.5's Just-based HTTP API sends `data` as form fields, not JSON.
  http.post(SERVICE_URL, {
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
      seek_generation: String(seekGeneration)
    }
  }).then((response) => {
    lastProblem = "";
    retryAfter = 0;
    if (closed || !isEnabled || requestEpoch !== epoch || path !== mediaPath || !enabled) return;
    if (requestSeekGeneration !== seekGeneration) {
      urgentTick = true;
      return;
    }
    const result = response.data || JSON.parse(response.text);
    state = result;
    if (result.error) {
      console.log("Local subtitle service: " + result.error);
    }
    const applied = applySubtitles(result);
    handleBuffering(result, position, requestSeekGeneration, applied);
  }).catch((error) => {
    if (closed || requestEpoch !== epoch || !isEnabled || requestSeekGeneration !== seekGeneration) return;
    const code = error && error.statusCode;
    if (code === 401 || code === 403) token = "";
    lastProblem = code === 401
      ? "Обновляю подключение к локальному сервису…"
      : "Локальный сервис перевода пока недоступен";
    retryAfter = Date.now() + 3000;
    bufferFailure(lastProblem);
    console.log(lastProblem);
  }).finally(() => {
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
  if (busy || Date.now() < retryAfter) return;
  if (pendingStops.length) {
    requestState(pendingStops.shift(), false, epoch);
  } else if (enabled && mediaPath) {
    urgentTick = false;
    requestState(mediaPath, true, epoch);
  }
}

function clearMedia() {
  releaseBuffer(false);
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
  updateMenu();
  tick();
}

function setEnabled(value, selectTrack) {
  enabled = Boolean(value);
  preferences.set("enabled", enabled);
  preferences.sync();
  if (enabled) {
    bufferBypass = null;
    if (selectTrack !== false) wantSelect = true;
    lastProblem = "";
    retryAfter = 0;
    const track = subtitlePath ? ownTrack(subtitlePath) : null;
    if (track && wantSelect) selectOwnTrack(track);
    osd(mediaPath ? "Локальный перевод на русский включён" : "Перевод включён — откройте видео");
  } else {
    releaseBuffer(true);
    stopPath(mediaPath);
    restoreUserSelection();
    osd("Локальный перевод выключен");
  }
  updateMenu();
  tick();
}

const rootItem = menu.item("Локальные русские субтитры", null, {});
const enabledItem = menu.item("Переводить автоматически", () => setEnabled(!enabled), { selected: enabled });
const startItem = menu.item("Включить и показать русский перевод", () => setEnabled(true), {});
const stopItem = menu.item("Остановить перевод", () => setEnabled(false), {});
const statusItem = menu.item("Готовлю локальный перевод…", () => osd(statusText()), {});
rootItem.addSubMenuItem(enabledItem);
rootItem.addSubMenuItem(startItem);
rootItem.addSubMenuItem(stopItem);
rootItem.addSubMenuItem(menu.separator());
rootItem.addSubMenuItem(statusItem);
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
console.log("Native local subtitle bridge loaded (loopback only)");
// Also attach when IINA reloads plugins while a video is already open.
if (mpv.getString("path") && mpv.getNumber("duration") > 0) fileLoaded();
