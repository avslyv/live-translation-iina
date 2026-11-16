// Native-track lifecycle contract; no IINA process, models, or network involved.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../iina-plugin/main.js'), 'utf8');

function playerHarness(options = {}) {
  const properties = {
    path: '/tmp/first-video.avi', duration: 600, 'time-pos': 0,
    pause: false, sid: 'no', 'sub-visibility': false, ...options.properties,
  };
  const listeners = new Map();
  const intervals = [];
  const timeouts = [];
  const commands = [];
  const writes = [];
  const requests = [];
  const preferences = new Map();
  const subtitles = [];
  const osd = [];
  const roots = [];
  let nextId = 1;
  let now = 1000;
  let responseFor = options.response || ((request) => ({
    path: '/tmp/first-ru.srt', revision: 1, current_ready: true,
    status: 'ready', buffered_until: 180, prepared_percent: 30, processing: false,
  }));
  function emit(name) { listeners.get(name)?.(); }
  function write(name, value) {
    const changed = properties[name] !== value;
    properties[name] = value;
    writes.push([name, value]);
    if (changed && options.nativeEvents) emit(`mpv.${name}.changed`);
  }
  function item(title, action, options) {
    return { title, action, ...options, children: [],
      addSubMenuItem(child) { this.children.push(child); } };
  }
  const context = vm.createContext({
    iina: {
      console: { log() {} },
      core: { osd(message) { osd.push(message); } },
      event: { on(name, action) { listeners.set(name, action); } },
      file: { exists() { return true; }, read() { return 'test-scoped-local-token'; } },
      preferences: {
        get(name) { return preferences.get(name); },
        set(name, value) { preferences.set(name, value); }, sync() {},
      },
      menu: { item, separator() { return {}; }, addItem(root) { roots.push(root); } },
      http: { post(url, options) {
        const request = { url, ...options.data };
        requests.push(request);
        return Promise.resolve(responseFor(request)).then(data => ({ data }));
      } },
      mpv: {
        getString(name) { return String(properties[name] ?? ''); },
        getNumber(name) { return Number(properties[name] ?? 0); },
        getFlag(name) { return Boolean(properties[name]); },
        getNative(name) { return name === 'track-list' ? subtitles : null; },
        set: write,
        command(name, args) {
          commands.push([name, Array.from(args)]);
          if (name === 'sub-add') {
            const track = { type: 'sub', id: nextId++, 'external-filename': args[0] };
            subtitles.push(track);
            if (args[1] === 'select') write('sid', String(track.id));
          } else if (name === 'sub-reload') {
            const index = subtitles.findIndex(t => String(t.id) === args[0]);
            if (index >= 0) subtitles[index] = { ...subtitles[index], id: nextId++ };
          } else if (name === 'sub-remove') {
            const index = subtitles.findIndex(t => String(t.id) === args[0]);
            if (index >= 0) subtitles.splice(index, 1);
          } else if (name === 'seek' && options.nativeEvents) {
            emit('mpv.seek');
            emit('mpv.playback-restart');
          }
        },
      },
    },
    setInterval(action) { intervals.push(action); return intervals.length; },
    clearInterval() {},
    setTimeout(action) { timeouts.push(action); return timeouts.length; },
    Date: { now() { return now; } },
  });
  vm.runInContext(source, context);
  async function settle() {
    for (let round = 0; round < 10; round++) {
      await new Promise(setImmediate);
      if (!timeouts.length) return;
      while (timeouts.length) timeouts.shift()();
    }
    throw new Error('Plugin did not settle');
  }
  return {
    properties, subtitles, commands, writes, requests, osd, roots, context,
    setResponse(fn) { responseFor = fn; },
    emit,
    advance(ms) { now += ms; },
    userSet(name, value) { properties[name] = value; emit(`mpv.${name}.changed`); },
    seek(position) { properties['time-pos'] = position; emit('mpv.seek'); },
    async tick() { intervals[0](); await settle(); },
    settle,
  };
}

test('a new local video starts and selects native Russian subtitles without a menu action', async () => {
  const h = playerHarness();
  await h.settle();
  assert.equal(h.requests[0].enabled, 'true');
  assert.equal(h.requests[0].path, '/tmp/first-video.avi');
  assert.ok(h.commands.some(([name, args]) => name === 'sub-add' && args[1] === 'select'));
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
  assert.equal(h.properties['sub-visibility'], true);
});

test('a seek posts the current native playhead and keeps the Russian track selected', async () => {
  const h = playerHarness();
  await h.settle();
  h.properties['time-pos'] = 337;
  h.setResponse(() => ({ path: '/tmp/first-ru.srt', revision: 1,
    status: 'processing', current_ready: false, buffered_until: 337,
    prepared_percent: 15, processing: true }));
  await h.tick();
  assert.equal(h.requests.at(-1).position, '337');
  assert.equal(h.requests.at(-1).enabled, 'true');
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
  h.setResponse(() => ({ path: '/tmp/first-ru.srt', revision: 2,
    status: 'ready', current_ready: true, buffered_until: 390,
    prepared_percent: 25, processing: false }));
  await h.tick();
  assert.ok(h.commands.some(([name]) => name === 'sub-reload'));
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
});

test('changing videos automatically requests and selects the new generated track', async () => {
  const h = playerHarness();
  await h.settle();
  h.properties.path = '/tmp/other-video.avi';
  h.properties['time-pos'] = 12;
  h.subtitles.splice(0);
  h.properties.sid = 'no';
  h.setResponse(request => ({ path: request.path === '/tmp/other-video.avi'
      ? '/tmp/other-ru.srt' : '/tmp/first-ru.srt',
    revision: 1, status: 'ready', current_ready: true, buffered_until: 90,
    prepared_percent: 15, processing: false }));
  h.emit('mpv.file-loaded');
  await h.settle();
  await h.tick();
  const request = h.requests.find(r => r.path === '/tmp/other-video.avi' && r.enabled === 'true');
  assert.ok(request);
  assert.equal(request.position, '12');
  assert.equal(h.subtitles[0]['external-filename'], '/tmp/other-ru.srt');
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
});

test('refreshing generated subtitles preserves an explicit user selection', async () => {
  const h = playerHarness();
  await h.settle();
  h.subtitles.push({ type: 'sub', id: 99, 'external-filename': '/tmp/user-choice.srt' });
  h.properties.sid = '99';
  h.properties['sub-visibility'] = false;
  h.setResponse(() => ({ path: '/tmp/first-ru.srt', revision: 2,
    current_ready: true, buffered_until: 180, prepared_percent: 30 }));
  await h.tick();
  assert.equal(h.properties.sid, '99');
  assert.equal(h.properties['sub-visibility'], false);
});

function ready(revision = 1) {
  return { path: '/tmp/first-ru.srt', revision, current_ready: true,
    status: 'ready', buffered_until: 600, prepared_percent: 100 };
}

function missing(revision = 1) {
  return { ...ready(revision), current_ready: false, status: 'recognizing',
    processing: true, buffered_until: 90, prepared_percent: 15 };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('an uncached first section pauses automatically and resumes only after native subtitles load', async () => {
  const h = playerHarness({ response: () => ({ ...missing(), path: null }), nativeEvents: true });
  await h.settle();
  assert.equal(h.properties.pause, true);
  assert.ok(h.osd.includes('Готовлю перевод этого участка…'));
  assert.match(h.roots[0].children[4].title, /Готовлю перевод/);
  h.setResponse(() => ready());
  await h.tick();
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
  assert.equal(h.properties['sub-visibility'], true);
  assert.equal(h.properties.pause, false);
  assert.deepEqual(h.writes.filter(([name]) => name === 'pause'), [['pause', true], ['pause', false]]);
  assert.ok(h.commands.some(([name]) => name === 'seek'), 'paused subtitle render is refreshed');
  assert.equal(h.requests.at(-1).native_russian_selected, 'true');
  assert.equal(h.requests.at(-1).waiting_for_translation, 'false');
});

test('a video paused by the user remains paused when the section becomes ready', async () => {
  const h = playerHarness({ properties: { pause: true }, response: () => missing(), nativeEvents: true });
  await h.settle();
  h.setResponse(() => ready(2));
  await h.tick();
  assert.equal(h.properties.pause, true);
  assert.equal(h.writes.some(([name]) => name === 'pause'), false);
});

test('a cached seek never pauses and requests its new position immediately', async () => {
  const h = playerHarness();
  await h.settle();
  const count = h.requests.length;
  h.seek(337);
  await h.settle();
  assert.equal(h.requests[count].position, '337');
  assert.equal(h.writes.some(([name]) => name === 'pause'), false);
});

test('an uncached seek pauses an already selected native track and resumes after reload', async () => {
  const h = playerHarness({ nativeEvents: true });
  await h.settle();
  const oldSid = h.properties.sid;
  h.setResponse(() => missing());
  h.seek(337);
  await h.settle();
  assert.equal(h.properties.pause, true);
  h.setResponse(() => ready(2));
  await h.tick();
  assert.equal(h.properties.pause, false);
  assert.notEqual(h.properties.sid, oldSid);
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
});

test('rapid seeks transfer play intent and ignore a ready response for an old target', async () => {
  const h = playerHarness({ nativeEvents: true });
  await h.settle();
  h.setResponse(() => missing());
  h.seek(337);
  await h.settle();
  assert.equal(h.properties.pause, true);
  const oldResponse = deferred();
  const newResponse = deferred();
  h.setResponse(request => Number(request.position) === 337 ? oldResponse.promise : newResponse.promise);
  await h.tick();
  h.seek(511);
  oldResponse.resolve(ready(2));
  await h.settle();
  assert.equal(h.properties.pause, true, 'old target must not resume a new target');
  assert.equal(h.requests.at(-1).position, '511');
  h.setResponse(() => ready(3));
  newResponse.resolve(ready(3));
  await h.settle();
  assert.equal(h.properties.pause, false);
  assert.equal(h.writes.filter(([name, value]) => name === 'pause' && !value).length, 1);
});

test('an explicit user resume bypasses this wait until a new seek', async () => {
  const h = playerHarness({ response: () => missing(), nativeEvents: true });
  await h.settle();
  h.userSet('pause', false);
  await h.tick();
  await h.tick();
  assert.equal(h.properties.pause, false);
  assert.equal(h.writes.filter(([name, value]) => name === 'pause' && value).length, 1);
  h.seek(3);
  await h.settle();
  assert.equal(h.properties.pause, true, 'a new explicit seek may wait again, even in the same chunk');
});

for (const action of ['hide', 'other track']) {
  test(`a user can cancel an owned wait by choosing ${action}`, async () => {
    const h = playerHarness({ nativeEvents: true });
    await h.settle();
    h.setResponse(() => missing());
    h.seek(337);
    await h.settle();
    assert.equal(h.properties.pause, true);
    if (action === 'hide') h.userSet('sub-visibility', false);
    else {
      h.subtitles.push({ type: 'sub', id: 99, 'external-filename': '/tmp/user-choice.srt' });
      h.userSet('sid', '99');
    }
    assert.equal(h.properties.pause, false);
    await h.tick();
    assert.equal(h.properties.pause, false);
  });
}

for (const failure of ['HTTP', 'backend']) {
  test(`${failure} failure releases an owned wait and reports the cause`, async () => {
    const h = playerHarness({ response: () => missing(), nativeEvents: true });
    await h.settle();
    h.setResponse(() => failure === 'HTTP'
      ? Promise.reject(new Error('test transport failure')) : { error: 'test model failure' });
    await h.tick();
    assert.equal(h.properties.pause, false);
    assert.match(h.osd.at(-1), failure === 'HTTP' ? /недоступен/ : /test model failure/);
    h.setResponse(() => missing());
    h.advance(4000);
    await h.tick();
    assert.equal(h.properties.pause, false, 'an error must not cause repeated automatic pauses');
  });
}

test('the wait times out after 45 seconds even while HTTP is pending', async () => {
  const h = playerHarness({ response: () => missing(), nativeEvents: true });
  await h.settle();
  const pending = deferred();
  h.setResponse(() => pending.promise);
  await h.tick();
  h.advance(45001);
  await h.tick();
  assert.equal(h.properties.pause, false);
  assert.match(h.osd.at(-1), /45 секунд/);
  pending.resolve(missing());
  await h.settle();
  assert.equal(h.properties.pause, false);
});

test('timing out a preexisting user pause does not unpause the user', async () => {
  const h = playerHarness({ properties: { pause: true }, response: () => missing() });
  await h.settle();
  h.advance(45001);
  await h.tick();
  assert.equal(h.properties.pause, true);
});

test('stopping translation releases its owned pause and sends a stop request', async () => {
  const h = playerHarness({ response: () => missing(), nativeEvents: true });
  await h.settle();
  h.roots[0].children[2].action();
  await h.settle();
  assert.equal(h.properties.pause, false);
  assert.equal(h.requests.at(-1).enabled, 'false');
  assert.equal(h.properties.sid, 'no');
});

test('switching files invalidates an old ready response without resuming a newly paused video', async () => {
  const h = playerHarness({ response: () => missing(), nativeEvents: true });
  await h.settle();
  const oldResponse = deferred();
  h.setResponse(request => request.enabled === 'false' ? {} : oldResponse.promise);
  await h.tick();
  h.properties.path = '/tmp/new-video.avi';
  h.properties.pause = true;
  h.properties.sid = 'no';
  h.subtitles.splice(0);
  h.emit('mpv.file-loaded');
  h.setResponse(request => request.enabled === 'false' ? {} :
    { ...ready(), path: '/tmp/new-ru.srt' });
  oldResponse.resolve(ready(2));
  await h.settle();
  await h.tick();
  assert.equal(h.properties.pause, true);
  assert.equal(h.subtitles[0]['external-filename'], '/tmp/new-ru.srt');
});

test('an internal paused-render refresh preserves seek generation and owned pause', async () => {
  const h = playerHarness({ response: () => missing(), nativeEvents: true });
  await h.settle();
  const generation = h.requests[0].seek_generation;
  h.setResponse(() => missing(2));
  await h.tick();
  assert.equal(h.properties.pause, true);
  assert.equal(h.requests.at(-1).seek_generation, generation);
  h.setResponse(() => ready(3));
  await h.tick();
  assert.equal(h.properties.pause, false);
  assert.equal(h.requests.at(-1).seek_generation, generation);
});

for (const action of ['hide', 'other track']) {
  test(`a user choosing ${action} during the first no-SRT wait cancels automatic selection`, async () => {
    const h = playerHarness({ properties: { 'sub-visibility': true },
      response: () => ({ ...missing(), path: null }), nativeEvents: true });
    await h.settle();
    assert.equal(h.properties.pause, true);
    if (action === 'hide') h.userSet('sub-visibility', false);
    else {
      h.subtitles.push({ type: 'sub', id: 99, 'external-filename': '/tmp/user-choice.srt' });
      h.userSet('sid', '99');
    }
    assert.equal(h.properties.pause, false);
    h.setResponse(() => ready());
    await h.tick();
    assert.equal(h.properties.pause, false);
    if (action === 'hide') assert.equal(h.properties['sub-visibility'], false);
    else assert.equal(h.properties.sid, '99');
  });
}

test('initial unchanged native subtitle notifications preserve automatic start', async () => {
  const h = playerHarness({ response: () => ({ ...missing(), path: null }), nativeEvents: true });
  await h.settle();
  h.emit('mpv.sid.changed');
  h.emit('mpv.sub-visibility.changed');
  assert.equal(h.properties.pause, true);
  h.setResponse(() => ready());
  await h.tick();
  assert.equal(h.properties.pause, false);
  assert.equal(h.properties.sid, String(h.subtitles[0].id));
  assert.equal(h.properties['sub-visibility'], true);
});
