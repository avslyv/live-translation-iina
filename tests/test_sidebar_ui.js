const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ui = require('../iina-plugin/sidebar.js');

test('Russian timing counters do not depend on plural fragments', () => {
  assert.equal(ui.formatTime(0), '0:00');
  assert.equal(ui.formatTime(91), '1:31');
  assert.equal(ui.formatTime(3721), '1:02:01');
  assert.equal(ui.formatTime(-30), '0:00');
});

test('model and cache sizes use binary byte units consistently', () => {
  assert.equal(ui.formatBytes(0), '0 КБ');
  assert.equal(ui.formatBytes(1024), '1 КБ');
  assert.equal(ui.formatBytes(1024 ** 2), '1.0 МБ');
  assert.equal(ui.formatBytes(8 * 1024 ** 3), '8.0 ГБ');
});

test('source help and status describe the selected behavior rather than implementation counters', () => {
  assert.match(ui.sourceModeHint('audio'), /звуковой дорожки/);
  assert.match(ui.sourceModeHint('subtitles'), /Если их нет/);
  assert.match(ui.sourceModeHint('auto'), /Сначала/);
  assert.equal(ui.headline({ enabled: true, connected: true, media: 'test.mp4' }, { current_ready: true }), 'Перевод этого участка готов');
  assert.equal(ui.headline({ enabled: true, connected: false, status: 'Подключаюсь…' }, {}), 'Подключаюсь…');
  assert.equal(ui.headline({ enabled: true, connected: true, media: 'test.mp4' }, { read_only: true, current_ready: false }), 'В этой версии нет перевода этого участка');
  assert.equal(ui.operationLabel('describe'), 'Обновляю…');
  assert.equal(ui.operationLabel('cache_delete'), 'Удаляю версию…');
  assert.equal(ui.operationLabel('cache_use'), 'Открываю версию…');
  assert.equal(ui.operationLabel('settings'), 'Сохраняю…');
});

test('the Russian panel handles source metadata, model recovery, saved versions and synchronous saves', () => {
  class Element {
    constructor() { this.value = ''; this.checked = false; this.children = []; this.style = {}; this.listeners = new Map(); }
    append(...nodes) { nodes.forEach(node => { node.parent = this; }); this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    querySelectorAll(selector) { return selector === '.range' ? this.children.filter(child => child.className === 'range') : []; }
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  }
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const listeners = new Map();
  const snapshot = { enabled: true, connected: true, models: [{ name: 'gemma4:12b' }], cache: [],
    languages: { en: 'Английский', ru: 'Русский' },
    settings: { translation_model: 'gemma4:12b', source_language: 'en', target_language: 'ru', source_mode: 'auto', glossary: [] }, operation: {} };
  const context = vm.createContext({
    document: { getElementById: element, createElement() { return new Element(); }, querySelectorAll() { return []; } },
    iina: { onMessage(name, callback) { listeners.set(name, callback); }, postMessage(name, request) {
      if (name === 'lt:action') {
        snapshot.settings = request.data;
        snapshot.operation = { busy: false, requestId: request.requestId, error: '' };
      }
      listeners.get('lt:state')({ json: JSON.stringify(snapshot) });
    } },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../iina-plugin/sidebar.js'), 'utf8'), context);
  element('source_mode').value = 'audio';
  element('source_mode').listeners.get('input')();
  element('settings-form').listeners.get('submit')({ preventDefault() {} });
  assert.equal(snapshot.settings.source_mode, 'audio');
  assert.equal(element('save').disabled, false);
  assert.equal(element('draft-status').textContent, '');
  const render = context.LiveTranslationSidebar.render;
  render({ ...snapshot, media: 'película.mp4', languages: { en: 'English', es: 'Español', ru: 'Русский' },
    settings: { ...snapshot.settings, source_language: 'es' },
    state: { source: { kind: 'text', label: 'Субтитры: Español', language: 'es' } } });
  assert.equal(element('source_language').value, 'es');
  assert.equal(element('source-info').textContent, 'Субтитры: Español');
  render({ ...snapshot, media: 'test.mp4', state: { initializing: true }, operation: { busy: true, action: 'describe' } });
  assert.equal(element('source-info').textContent, 'Подготавливаю источник…');
  assert.equal(element('draft-status').textContent, 'Обновляю…');
  render({ ...snapshot, media: 'test.mp4', state: { read_only: true, current_ready: false, source: { kind: 'saved' } },
    modelError: 'Ollama недоступен. Сохранённые переводы можно открыть без него.' });
  assert.equal(element('model-error').hidden, false);
  assert.equal(element('translation_model').disabled, true);
  assert.match(element('model-info').textContent, /модели не запускаются/);
  assert.match(element('status-detail').textContent, /сохранённые участки/);
  assert.equal(element('source-info').textContent, 'Сохранённый перевод');
  render({ ...snapshot, media: 'test.mp4', modelError: '', state: { source: { kind: 'audio' } }, secondaryBusy: true });
  assert.equal(element('model-error').hidden, true);
  assert.equal(element('translation_model').disabled, false);
  assert.equal(element('source-info').textContent, 'Распознавание речи');
  assert.match(element('bilingual-hint').textContent, /дорожка занята/);
  render({ ...snapshot, media: 'test.mp4', state: { error: 'Не найден файл субтитров' } });
  assert.equal(element('status-title').textContent, 'Не удалось подготовить перевод');
  assert.equal(element('pipeline-error').textContent, 'Не найден файл субтитров');
  render({ ...snapshot, media: 'test.mp4', position: 35, duration: 100,
    state: { coverage: [[-10, 30], [80, 200]], current_ready: true, buffered_until: 40 } });
  const ranges = element('coverage').querySelectorAll('.range');
  assert.equal(ranges[0].style.left, '0%');
  assert.equal(parseFloat(ranges[0].style.width), 30);
  assert.equal(ranges[1].style.left, '80%');
  assert.ok(Math.abs(parseFloat(ranges[1].style.width) - 20) < 0.001);
  assert.equal(element('playhead').style.left, '35%');
  assert.equal(element('status-detail').textContent, 'Готово вперёд: 0:05');
});

test('glossary input preserves Unicode names and rejects incomplete pairs', () => {
  assert.deepEqual(ui.parseGlossary('IINA → IINA\n machine learning => машинное обучение\n'), [
    { source: 'IINA', target: 'IINA' }, { source: 'machine learning', target: 'машинное обучение' },
  ]);
  assert.throws(() => ui.parseGlossary('missing translation'), /оригинал → перевод/);
  assert.throws(() => ui.parseGlossary('one → two → three'), /оригинал → перевод/);
  assert.equal(ui.parseGlossary(Array.from({ length: 128 }, (_, index) => 'term' + index + ' → перевод').join('\n')).length, 128);
  assert.throws(() => ui.parseGlossary(Array.from({ length: 129 }, (_, index) => 'term' + index + ' → перевод').join('\n')), /128/);
});

test('language choices follow backend capabilities without inventing auto-detection', () => {
  assert.deepEqual(ui.languageEntries({ en: 'Английский', ru: 'Русский' }), [
    ['en', 'Английский'], ['ru', 'Русский'],
  ]);
  assert.deepEqual(ui.languageEntries([{ code: 'uk', name: 'Украинский' }]), [['uk', 'Украинский']]);
  assert.ok(!ui.languageEntries({ en: 'Английский' }).some(([code]) => code === 'auto'));
});

test('the sidebar envelope decodes Unicode and code-looking text as data only', () => {
  const value = { label: 'Русский `${throw Error()}` <img src=x>', state: { coverage: [[0, 30]] } };
  assert.deepEqual(ui.decodeEnvelope({ json: JSON.stringify(value) }), value);
  assert.throws(() => ui.decodeEnvelope({ json: '[]' }));
  assert.throws(() => ui.decodeEnvelope({ json: '{partial' }));
});

test('the WebView makes no network requests and never inserts translated HTML', () => {
  const script = fs.readFileSync(path.join(__dirname, '../iina-plugin/sidebar.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../iina-plugin/sidebar.html'), 'utf8');
  assert.match(html, /connect-src 'none'/);
  assert.ok(!/innerHTML|\beval\s*\(|\bfetch\s*\(|XMLHttpRequest|localStorage/.test(script));
  assert.match(script, /textContent/);
});
