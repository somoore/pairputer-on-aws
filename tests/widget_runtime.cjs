// Execute production scripts; fake only the host, DOM, transport, clock and codecs.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = resolve(__dirname, '..');
const app = readFileSync(resolve(root, 'substrate/mcp-server/app.html'), 'utf8');
const relay = readFileSync(resolve(root, 'substrate/stateful-relay/index.mjs'), 'utf8');
const script = html => html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const playerHtml = vm.runInNewContext(
  relay.slice(relay.indexOf('function playerHtml()'), relay.indexOf('async function authorize(')) + '\nplayerHtml()'
);
const tick = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
const session = {
  imageId: 'workbench-test', openNonce: 'open-one', state: 'RUNNING',
  relayUrl: 'https://relay.example.test', token: 'test-token', host: 'chatgpt',
};

function environment({ storage = new Map(), blockedStorage = false, snapshot = null,
  toolOutput = session, controlState = session, mode = 'inline' } = {}) {
  const listeners = new Map(), elements = new Map(), timers = new Map();
  const commands = [], tools = [], contexts = [], decoders = [], streams = [];
  let timerId = 0, context;
  const emit = (type, event) => (listeners.get(type) || []).forEach(fn => fn(event));
  const on = (type, fn) => listeners.set(type, [...listeners.get(type) || [], fn]);
  const element = (tag = 'div') => {
    const classes = new Set();
    const el = { tag, hidden: true, style: { setProperty() {} }, textContent: '', children: [],
      classList: { add: x => classes.add(x), remove: x => classes.delete(x),
        toggle: (x, yes) => yes ? classes.add(x) : classes.delete(x), contains: x => classes.has(x) },
      addEventListener() {}, removeEventListener() {}, focus() {},
      append(...children) { children.forEach(c => this.appendChild(c)); },
      appendChild(child) {
        this.children.push(child); if (child.id) elements.set(child.id, child);
        if (child.tag === 'iframe') queueMicrotask(() => emit('message', {
          source: child.contentWindow, origin: 'https://relay.example.test', data: { pairputer: 'booted' },
        }));
        return child;
      },
      remove() { if (this.id) elements.delete(this.id); },
      getContext() { return { drawImage() {} }; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 1280, height: 720 }; },
    };
    if (tag === 'iframe') el.contentWindow = { focus() {}, postMessage(m) {
      commands.push(m);
      if (m.id) queueMicrotask(() => emit('message', { source: el.contentWindow,
        origin: 'https://relay.example.test',
        data: { pairputer: 'reply', id: m.id, ok: true, result: { state: 'RUNNING' } },
      }));
    } };
    return el;
  };
  for (const match of app.matchAll(/id="([^"]+)"/g)) elements.set(match[1], element());
  elements.set('c', element('canvas')); elements.set('s', element());
  const document = { getElementById: id => elements.get(id) || null,
    createElement: element, body: element(), documentElement: element(),
    addEventListener: on, removeEventListener() {}, hidden: false };
  const sdk = { widgetState: snapshot, toolOutput, displayMode: mode,
    setWidgetState(s) { this.widgetState = structuredClone(s); },
    async callTool(name, args) { tools.push({ name, args }); return controlState; },
    async requestDisplayMode({ mode: requested }) { this.displayMode = requested; return { mode: requested }; },
  };
  class AudioContext {
    constructor() { this.state = 'suspended'; this.currentTime = 0; this.scheduled = []; contexts.push(this); }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    createBuffer(ch, n, sr) { return { duration: n / sr, copyToChannel() {} }; }
    createBufferSource() { return { connect() {}, start: t => this.scheduled.push(t) }; }
    async resume() { this.state = 'running'; }
    async close() { this.state = 'closed'; }
  }
  class AudioDecoder {
    constructor(options) { this.options = options; this.state = 'unconfigured'; decoders.push(this); }
    configure() { this.state = 'configured'; }
    decode() {}
    close() { this.state = 'closed'; }
  }
  class EventSource {
    constructor(url) { this.url = url; streams.push(this); }
    addEventListener() {}
    close() { this.closed = true; }
  }
  const sandbox = { console, URL, URLSearchParams, performance, crypto: require('node:crypto').webcrypto,
    atob, btoa, structuredClone, queueMicrotask, document, location: { href: 'https://widget.example.test/', search: '?autostart=0' },
    navigator: {}, AudioContext, AudioDecoder, EventSource,
    localStorage: {
      getItem(k) { if (blockedStorage) throw new Error('SecurityError'); return storage.get(k) ?? null; },
      setItem(k, v) { if (blockedStorage) throw new Error('SecurityError'); storage.set(k, String(v)); },
      removeItem(k) { if (blockedStorage) throw new Error('SecurityError'); storage.delete(k); },
    },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout: id => timers.delete(id), setInterval() { return ++timerId; }, clearInterval() {},
    addEventListener: on, removeEventListener() {}, ResizeObserver: class { observe() {} },
    requestAnimationFrame() {}, cancelAnimationFrame() {},
    fetch: async () => ({ ok: true, text: async () => '{"state":"RUNNING"}' }),
    parent: { postMessage() {} }, openai: sdk,
  };
  sandbox.window = sandbox;
  context = vm.createContext(sandbox);
  return { context, commands, tools, contexts, decoders, streams, sdk, storage, elements,
    run: code => vm.runInContext(code, context),
    async boot() { this.run(script(app)); await tick(); },
  };
}

test('mute survives a complete host remount of the same card', async () => {
  const first = environment(); await first.boot();
  first.elements.get('mute').onclick();
  const second = environment({ snapshot: first.sdk.widgetState, storage: first.storage });
  await second.boot();
  assert.equal(second.elements.get('mute').textContent, '🔇 Muted');
  assert.equal(second.commands.find(c => c.cmd === 'start').muted, true);
});

for (const intent of ['frozen', 'stopped']) {
  test(`${intent} replay stays inactive when localStorage is unavailable`, async () => {
    const e = environment({ blockedStorage: true, snapshot: {
      imageId: session.imageId, openNonce: session.openNonce, lastOpenNonce: session.openNonce,
      intent, muted: true,
    } });
    await e.boot();
    assert.equal(e.commands.filter(c => ['start', 'thaw'].includes(c.cmd)).length, 0);
    assert.ok(e.tools.every(t => !t.args.ensure_running));
    assert.match(e.elements.get('state').textContent, intent === 'frozen' ? /SUSPENDED/ : /STOPPED/);
  });
}

test('a fresh explicit open can override frozen intent without browser storage', async () => {
  const e = environment({ blockedStorage: true, snapshot: {
    imageId: session.imageId, openNonce: 'old-open', lastOpenNonce: 'old-open', intent: 'frozen',
  } });
  await e.boot();
  assert.ok(e.commands.some(c => c.cmd === 'start'));
  assert.equal(e.run('canAutoWake()'), true);
});

test('Freeze persists intent before awaiting the control plane and survives remount', async () => {
  const first = environment({ blockedStorage: true }); await first.boot();
  first.elements.get('freeze').onclick(); // intentionally do not await the network
  assert.equal(first.sdk.widgetState.intent, 'frozen');
  const second = environment({ blockedStorage: true, snapshot: first.sdk.widgetState });
  await second.boot();
  assert.ok(second.tools.every(t => !t.args.ensure_running));
  assert.ok(!second.commands.some(c => c.cmd === 'start'));
});

test('state snapshots contain no relay credentials', async () => {
  const e = environment(); await e.boot();
  assert.deepEqual(Object.keys(e.sdk.widgetState).sort(),
    ['imageId', 'intent', 'lastOpenNonce', 'mode', 'muted', 'openNonce', 'ts']);
});

test('another capsule never inherits the previous card intent or mute state', async () => {
  const e = environment({ snapshot: {
    imageId: 'different-capsule', intent: 'frozen', muted: true, openNonce: session.openNonce,
  } });
  await e.boot();
  assert.ok(e.commands.some(c => c.cmd === 'start'));
  assert.equal(e.elements.get('mute').textContent, '🔊 Sound');
});

test('newer shared storage intent wins over a stale widget snapshot', async () => {
  const storage = new Map([
    [`pairputer_intent:${session.imageId}`, 'frozen'],
    [`pairputer_open_nonce:${session.imageId}`, session.openNonce],
  ]);
  const e = environment({ storage, snapshot: {
    imageId: session.imageId, intent: 'running', openNonce: session.openNonce,
  } });
  await e.boot();
  assert.ok(!e.commands.some(c => c.cmd === 'start'));
  assert.match(e.elements.get('state').textContent, /SUSPENDED/);
});

test('granted fullscreen renders correctly and boot never requests a mode', async () => {
  const e = environment(); let requests = 0;
  e.sdk.requestDisplayMode = async () => { requests++; return { mode: 'fullscreen' }; };
  await e.boot(); assert.equal(requests, 0);
  await e.elements.get('full').onclick();
  assert.equal(requests, 1);
  assert.equal(e.elements.get('full').textContent, '⇲ Inline');
});

test('an empty mode response uses observed host mode instead of claiming fullscreen', async () => {
  const e = environment(); await e.boot();
  e.sdk.requestDisplayMode = async () => undefined;
  await e.elements.get('full').onclick();
  assert.equal(e.elements.get('full').textContent, '⛶ Fullscreen');
  assert.match(e.elements.get('status').textContent, /does not allow fullscreen/);
});

test('native fullscreen fallback is attempted after an explicit host decline', async () => {
  const e = environment(); await e.boot(); let nativeRequests = 0;
  e.sdk.requestDisplayMode = async () => ({ mode: 'inline' });
  e.context.document.documentElement.requestFullscreen = async () => {
    nativeRequests++; e.context.document.fullscreenElement = e.context.document.documentElement;
  };
  await e.elements.get('full').onclick();
  assert.equal(nativeRequests, 1);
  assert.equal(e.elements.get('full').textContent, '⇲ Inline');
});

for (const engine of ['relay', 'direct']) {
  test(`${engine} drops suspended audio and schedules only fresh frames after resume`, () => {
    const e = environment();
    if (engine === 'relay') { e.run(script(playerHtml)); e.run('startAudio()'); }
    else {
      // Run the complete direct engine with its external widget hooks stubbed.
      e.run('const $ = id => document.getElementById(id); let muted=false, token="t", edgeAuth="", base="https://relay.example.test", RELAY_VIEWER_ID="test"; function updateVideoMetadata(){} function setStatus(){}');
      const start = app.indexOf('function makeDirectPlayer()');
      // Brace matching avoids copying the engine into a test-only implementation.
      let depth = 0, finish = start;
      for (let i = app.indexOf('{', start); i < app.length; i++) {
        if (app[i] === '{') depth++; else if (app[i] === '}' && --depth === 0) { finish = i + 1; break; }
      }
      e.run(app.slice(start, finish) + '; const player = makeDirectPlayer(); player.rpc("start", {});');
    }
    const ctx = e.contexts[0], decoder = e.decoders[0];
    let closed = 0;
    const frame = () => ({ numberOfChannels: 2, numberOfFrames: 960, sampleRate: 48000,
      copyTo(data) { data.fill(0.25); }, close() { closed++; } });
    for (let i = 0; i < 100; i++) decoder.options.output(frame());
    assert.equal(closed, 100, 'every decoded frame must be released');
    assert.equal(ctx.scheduled.length, 0, 'autoplay denial must not accumulate scheduled sound');
    ctx.state = 'running'; ctx.currentTime = 10;
    decoder.options.output(frame());
    assert.deepEqual(ctx.scheduled, [10]);
    // Stop/restart represents token refresh or remount recovery. A late output from the old
    // decoder must be released, never scheduled into the replacement AudioContext.
    if (engine === 'relay') e.run('stopStreams(); startAudio()');
    else e.run('player.rpc("stop", {}); player.rpc("start", {})');
    const replacement = e.contexts.at(-1); replacement.state = 'running';
    decoder.options.output(frame());
    assert.equal(replacement.scheduled.length, 0);
    assert.equal(closed, 102);
  });
}
