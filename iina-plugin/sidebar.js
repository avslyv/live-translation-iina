(function (root) {
  "use strict";

  function decodeEnvelope(envelope) {
    if (!envelope || typeof envelope.json !== "string" || envelope.json.length > 2000000) {
      throw new Error("Некорректный ответ панели");
    }
    const value = JSON.parse(envelope.json);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Некорректный ответ панели");
    return value;
  }

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(1) + " ГБ";
    if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(1) + " МБ";
    return Math.round(bytes / 1024) + " КБ";
  }

  function formatTime(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    return (hours ? hours + ":" + String(minutes).padStart(2, "0") : String(minutes)) + ":" + String(seconds % 60).padStart(2, "0");
  }

  function languageEntries(value) {
    if (Array.isArray(value)) return value.flatMap(item => {
      if (typeof item === "string") return [[item, item]];
      const code = item && (item.code || item.id);
      return typeof code === "string" ? [[code, String(item.label || item.name || code)]] : [];
    });
    if (value && typeof value === "object") return Object.entries(value).map(([key, label]) => [key, String(label)]);
    return [["ru", "Русский"], ["en", "Английский"]];
  }

  function parseGlossary(text) {
    const lines = String(text || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.length > 128) throw new Error("В словаре можно сохранить до 128 пар.");
    return lines.map(line => {
      const parts = line.split(/\s*(?:→|=>|\t)\s*/);
      if (parts.length !== 2 || !parts[0].trim() || !parts[1].trim()) {
        throw new Error("Укажите каждую пару в формате: оригинал → перевод.");
      }
      return { source: parts[0].trim(), target: parts[1].trim() };
    });
  }

  function profileModel(profile) {
    if (!profile || typeof profile !== "object") return "";
    return String(profile.translation_model || profile.model || (profile.translation || {}).model || (profile.translator || {}).model || "");
  }

  function sourceModeHint(mode) {
    if (mode === "audio") return "Распознаётся речь из выбранной звуковой дорожки IINA.";
    if (mode === "subtitles") return "Переводятся текстовые субтитры IINA. Если их нет, выберите другую дорожку или источник «Звук».";
    return "Сначала используются субтитры на выбранном языке. Если подходящей дорожки нет — распознаётся речь.";
  }

  function headline(data, state) {
    if (!data.enabled) return "Перевод выключен";
    if (!data.connected) return String(data.status || "Подключаю локальный перевод…");
    if (!data.media) return "Откройте видео в IINA";
    if (state.error) return "Не удалось подготовить перевод";
    if (state.read_only) return state.current_ready === false
      ? "В этой версии нет перевода этого участка" : "Сохранённый перевод";
    if (state.initializing) return "Подготавливаю источник…";
    if (state.done) return "Перевод всего видео готов";
    if (state.current_ready) return "Перевод этого участка готов";
    if (state.status === "loading") return "Загружаю локальные модели…";
    return "Готовлю перевод этого участка…";
  }

  function statusDetail(data, state) {
    if (!data.media) return "Перевод появится в обычной дорожке субтитров IINA.";
    if (state.error) return "Можно повторить подготовку после устранения причины.";
    if (state.initializing) return "Проверяю файл и выбранные дорожки.";
    if (state.read_only) return "Доступны только сохранённые участки этой версии.";
    if (state.done) return "Перевод всего видео сохранён.";
    return "Готово вперёд: " + formatTime(Math.max(0, Number(state.buffered_until || 0) - Number(data.position || 0)));
  }

  function sourceSummary(data, state) {
    const source = state.source && typeof state.source === "object" ? state.source : {};
    if (source.label) return String(source.label);
    if (state.initializing) return "Подготавливаю источник…";
    return ({ audio: "Распознавание речи", text: "Текстовые субтитры", embedded: "Встроенные субтитры",
      external: "Файл субтитров", subtitles: "Текстовые субтитры", saved: "Сохранённый перевод" }[source.kind]) ||
      (data.media ? "Подготовка источника не завершена" : "Источник появится после открытия видео");
  }

  function operationLabel(action) {
    return ({ describe: "Обновляю…", settings: "Сохраняю…", cache_use: "Открываю версию…",
      cache_delete: "Удаляю версию…", retry: "Повторяю подготовку…" }[action]) || "Выполняю действие…";
  }

  const api = { decodeEnvelope, formatBytes, formatTime, languageEntries, parseGlossary, profileModel,
    sourceModeHint, headline, statusDetail, sourceSummary, operationLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.LiveTranslationSidebar = api;
  if (typeof document === "undefined") return;

  const byId = id => document.getElementById(id);
  const fieldNames = ["translation_model", "source_mode", "source_language", "target_language", "bilingual", "wait_for_translation", "vad_mode", "batch_size", "glossary"];
  let snapshot = {};
  let dirty = false;
  let sequence = 0;
  let waitingFor = "";

  function send(action, data) {
    const requestId = "sidebar-" + ++sequence;
    if (action === "settings") waitingFor = requestId;
    if (root.iina) root.iina.postMessage("lt:action", { action, data: data || {}, requestId });
    return requestId;
  }

  function replaceOptions(element, entries, selected) {
    const previous = selected || element.value;
    element.replaceChildren();
    for (const [value, label] of entries) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(label);
      element.append(option);
    }
    if (entries.some(entry => String(entry[0]) === String(previous))) element.value = String(previous);
  }

  function renderSettings(settings, data) {
    const languages = languageEntries(data.languages);
    replaceOptions(byId("source_language"), languages, settings.source_language || "en");
    replaceOptions(byId("target_language"), languages.filter(entry => entry[0] !== "auto"), settings.target_language || "ru");
    const models = (Array.isArray(data.models) ? data.models : []).flatMap(model => {
      const name = typeof model === "string" ? model : model && model.name;
      return typeof name === "string" ? [[name, name + (model.size ? " · " + formatBytes(model.size) : "")]] : [];
    });
    if (settings.translation_model && !models.some(model => model[0] === settings.translation_model)) {
      models.unshift([settings.translation_model, settings.translation_model + " · текущая настройка"]);
    }
    replaceOptions(byId("translation_model"), models.length ? models : [["", "Нет установленных моделей"]], settings.translation_model);
    for (const name of ["source_mode", "vad_mode", "batch_size"]) {
      byId(name).value = String(settings[name] === undefined ? ({ source_mode: "auto", vad_mode: "off", batch_size: 4 }[name]) : settings[name]);
    }
    byId("bilingual").checked = Boolean(settings.bilingual);
    byId("wait_for_translation").checked = settings.wait_for_translation !== false;
    byId("glossary").value = (Array.isArray(settings.glossary) ? settings.glossary : []).map(item => item.source + " → " + item.target).join("\n");
    byId("source-hint").textContent = sourceModeHint(byId("source_mode").value);
  }

  function renderCoverage(data, state) {
    const rail = byId("coverage");
    for (const element of Array.from(rail.querySelectorAll(".range"))) element.remove();
    const duration = Number(data.duration) || 0;
    const ranges = Array.isArray(state.coverage) ? state.coverage : [];
    if (duration > 0) for (const range of ranges) {
      if (!Array.isArray(range) || !Number.isFinite(range[0]) || !Number.isFinite(range[1])) continue;
      const start = Math.max(0, Math.min(1, range[0] / duration));
      const end = Math.max(start, Math.min(1, range[1] / duration));
      const bar = document.createElement("span");
      bar.className = "range";
      bar.style.left = start * 100 + "%";
      bar.style.width = (end - start) * 100 + "%";
      rail.append(bar);
    }
    byId("playhead").style.left = Math.max(0, Math.min(100, duration ? Number(data.position) / duration * 100 : 0)) + "%";
  }

  function renderCache(data) {
    const cache = Array.isArray(data.cache) ? data.cache : [];
    byId("cache-count").textContent = String(cache.length);
    byId("cache-empty").hidden = cache.length > 0;
    const list = byId("cache-list");
    list.replaceChildren();
    for (const item of cache) {
      const card = document.createElement("article");
      card.className = "cache-card";
      const title = document.createElement("h3");
      title.textContent = String(item.label || "Сохранённый перевод");
      const metadata = document.createElement("p");
      metadata.className = "metadata";
      metadata.textContent = [profileModel(item.profile), item.read_only ? "Архив · только просмотр" : "",
        item.complete ? "Полностью готов" : "Подготовлен частично", formatBytes(item.bytes)].filter(Boolean).join(" · ");
      const actions = document.createElement("div");
      actions.className = "cache-actions";
      const use = document.createElement("button");
      use.className = "secondary";
      use.type = "button";
      use.textContent = item.current || item.active ? "Используется" : "Использовать";
      use.disabled = Boolean(item.current || item.active || (data.operation || {}).busy);
      use.addEventListener("click", () => send("cache_use", { key: item.key }));
      const remove = document.createElement("button");
      remove.className = "delete";
      remove.type = "button";
      remove.textContent = "Удалить";
      remove.disabled = Boolean((data.operation || {}).busy);
      remove.addEventListener("click", () => send("cache_delete", { key: item.key }));
      actions.append(use, remove);
      card.append(title, metadata, actions);
      list.append(card);
    }
  }

  function render(data) {
    snapshot = data;
    const state = data.state || {};
    const operation = data.operation || {};
    if (waitingFor && operation.requestId === waitingFor && !operation.busy) {
      waitingFor = "";
      if (!operation.error) dirty = false;
    }
    byId("enabled").checked = Boolean(data.enabled);
    byId("connection").textContent = data.connected ? (state.cache_only ? "Из кэша" : "Локально") : "Подключение";
    byId("status-title").textContent = headline(data, state);
    byId("file-name").textContent = String(data.media || "Откройте видео в IINA");
    byId("progress-value").textContent = Math.max(0, Math.min(100, Math.floor(Number(state.prepared_percent) || 0))) + "%";
    byId("status-detail").textContent = statusDetail(data, state);
    byId("source-info").textContent = sourceSummary(data, state);
    byId("model-info").textContent = state.read_only ? "Архивная версия · модели не запускаются" :
      String(state.translation_model || (data.settings || {}).translation_model || "Локальная модель");
    byId("model-error").hidden = !data.modelError;
    byId("model-error").textContent = String(data.modelError || "");
    byId("translation_model").disabled = Boolean(data.modelError);
    byId("bilingual-hint").textContent = data.secondaryBusy
      ? "Вторая дорожка занята. Выбранные вами субтитры сохранятся."
      : "Оригинал появится во второй дорожке IINA, если она свободна.";
    byId("pipeline-error").hidden = !state.error;
    byId("pipeline-error").textContent = state.error ? String(state.error) : "";
    byId("retry").hidden = !state.error;
    byId("operation-message").textContent = String(operation.error || (operation.busy ? operationLabel(operation.action) : ""));
    byId("operation-message").className = operation.error ? "operation-message error" : "operation-message";
    byId("draft-status").textContent = operation.busy ? operationLabel(operation.action) : dirty ? "Есть изменения" : "";
    byId("save").disabled = Boolean(operation.busy || waitingFor);
    byId("refresh").disabled = Boolean(operation.busy);
    if (!dirty && !waitingFor) renderSettings(data.settings || {}, data);
    renderCoverage(data, state);
    renderCache(data);
  }

  byId("enabled").addEventListener("change", () => send("enabled", { enabled: byId("enabled").checked }));
  for (const name of fieldNames) byId(name).addEventListener("input", () => {
    dirty = true;
    byId("draft-status").textContent = "Есть изменения";
    if (name === "source_mode") byId("source-hint").textContent = sourceModeHint(byId(name).value);
  });
  byId("settings-form").addEventListener("submit", event => {
    event.preventDefault();
    try {
      const data = {};
      for (const name of fieldNames) data[name] = ["bilingual", "wait_for_translation"].includes(name) ? byId(name).checked : byId(name).value;
      data.batch_size = Number(data.batch_size);
      data.glossary = parseGlossary(data.glossary);
      byId("save").disabled = true;
      byId("operation-message").textContent = "";
      byId("operation-message").className = "operation-message";
      send("settings", data);
    } catch (error) {
      byId("operation-message").textContent = error.message;
      byId("operation-message").className = "operation-message error";
    }
  });
  byId("refresh").addEventListener("click", () => send("describe", {}));
  byId("refresh-cache").addEventListener("click", () => send("describe", {}));
  byId("retry").addEventListener("click", () => send("retry", {}));
  for (const tab of document.querySelectorAll("[data-tab]")) tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll("[data-tab]")) other.setAttribute("aria-pressed", String(other === tab));
    byId("tab-translate").hidden = tab.dataset.tab !== "translate";
    byId("tab-cache").hidden = tab.dataset.tab !== "cache";
  });
  api.render = render;
  api.snapshot = () => snapshot;
  if (root.iina) {
    root.iina.onMessage("lt:state", envelope => {
      try { render(decodeEnvelope(envelope)); } catch (error) {
        byId("operation-message").textContent = "Не удалось обновить панель. Откройте её ещё раз.";
        byId("operation-message").className = "operation-message error";
      }
    });
    root.iina.postMessage("lt:ready", {});
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
