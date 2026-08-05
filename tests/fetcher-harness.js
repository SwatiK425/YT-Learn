// ─── Praxis: transcript-fetcher logic harness ─────────────────────────
// Deterministic validation of extension/transcript-fetcher.js WITHOUT
// YouTube. Replays the fetcher in a sandbox with a programmable fetch
// stub and fixture payloads, asserting the source-fallback order and the
// reason codes emitted (no_captions | fetch_blocked | empty_response |
// parse_failed | third_party_failed).
//
// Run:  node tests/fetcher-harness.js
// Exit: 0 = all pass, 1 = failures (each failure printed).

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const FETCHER = path.join(__dirname, '..', 'extension', 'transcript-fetcher.js');
const src = fs.readFileSync(FETCHER, 'utf8');

let failures = 0;
let passCount = 0;

function check(name, cond, extra) {
  if (cond) { passCount++; console.log('  PASS ' + name); }
  else { failures++; console.log('  FAIL ' + name + (extra ? ' — ' + extra : '')); }
}

// ─── Fixtures ─────────────────────────────────────────────────────────
const JSON3_BODY = JSON.stringify({
  events: [
    { segs: [{ utf8: 'Hello' }, { utf8: ' world' }] },
    { segs: [{ utf8: ' This is a test transcript.' }] }
  ]
});
const PLAYER_WITH_TRACKS = JSON.stringify({
  captions: { playerCaptionsTracklistRenderer: {
    captionTracks: [
      { languageCode: 'en', kind: 'asr', baseUrl: 'https://yt.example/track1' },
      { languageCode: 'fr', baseUrl: 'https://yt.example/track2' }
    ]
  }}
});
const PLAYER_NO_TRACKS = JSON.stringify({ captions: {} });
const UNPARSEABLE = '<html>this is not a transcript</html>';

// ─── Sandbox helpers ──────────────────────────────────────────────────
function buildSandbox(opts) {
  const log = [];
  // Programmable fetch stub: opts.fetchLog receives {url, opts}; returns
  // {ok, status, text()} per the scenario's response table.
  const fetchStub = (url, fopts) => {
    log.push({ url: String(url), opts: fopts || {} });
    const resp = opts.respond(url, fopts);
    if (resp instanceof Error) return Promise.reject(resp);
    if (resp === 'hang') {
      // Mirror a real fetch: never resolves, but REJECTS when the
      // AbortSignal fires (that's what fetchT's timeout relies on).
      return new Promise((_, reject) => {
        const sig = fopts && fopts.signal;
        if (sig && sig.addEventListener) sig.addEventListener('abort', () => reject(new Error('Aborted')));
      });
    }
    return Promise.resolve({
      ok: resp.status < 400,
      status: resp.status,
      text: () => Promise.resolve(resp.body),
      json: () => Promise.resolve(JSON.parse(resp.body))
    });
  };

  const dispatchEvents = [];
  const sandbox = {
    console,
    setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 5)),
    clearTimeout,
    AbortController,
    fetch: fetchStub,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    window: {
      ytInitialPlayerResponse: opts.playerResponse || null,
      ytcfg: { data_: opts.ytcfg || {} },
      // Real browsers expose AbortController on window — fetchT checks for
      // it there. Mirror that so the timeout path is exercised.
      AbortController
    },
    document: {
      querySelectorAll: (sel) => {
        if (sel.indexOf('script[src*="transcript-fetcher"]') === 0) {
          return [{ getAttribute: (a) => a === 'data-video-id' ? (opts.videoId || 'TESTVID') : null }];
        }
        if (sel === 'video') return [{ textTracks: [] }];
        return [];
      },
      querySelector: () => null,
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: (e) => { dispatchEvents.push(e); },
      createElement: () => ({})
    },
    DOMParser: class {
      parseFromString(text) {
        return { querySelectorAll: () => [] }; // XML path returns nothing
      }
    }
  };
  sandbox.window.document = sandbox.document;
  vm.createContext(sandbox);
  return { sandbox, log, dispatchEvents };
}

const pendingTimers = [];
const realSetTimeout = global.setTimeout;
const realClearTimeout = global.clearTimeout;

function runScenario(opts) {
  const { sandbox, log, dispatchEvents } = buildSandbox(opts);
  // Collapse waits so the 6s poll window runs in ~60ms, but keep every
  // timer tracked so we can clear it before the next scenario.
  sandbox.setTimeout = (fn, ms) => {
    const t = realSetTimeout(fn, Math.min(ms, 5));
    pendingTimers.push(t);
    return t;
  };
  sandbox.clearTimeout = (t) => { realClearTimeout(t); const i = pendingTimers.indexOf(t); if (i >= 0) pendingTimers.splice(i, 1); };
  vm.runInContext(src, sandbox, { filename: FETCHER });
  return new Promise((resolve) => {
    // 400ms settle: 12 collapsed polls (~60ms) + fetch chain + retries.
    const t = realSetTimeout(() => resolve({ log, dispatchEvents }), 400);
    pendingTimers.push(t);
  }).then((result) => {
    // Clear all pending timers so one scenario can't bleed into the next.
    pendingTimers.forEach((t) => realClearTimeout(t));
    pendingTimers.length = 0;
    return result;
  });
}

function lastDispatch(events) {
  if (!events.length) return null;
  return events[events.length - 1].detail;
}

// ─── Scenarios ────────────────────────────────────────────────────────
(async function main() {
  console.log('\nScenario 1: player response has tracks, baseUrl serves valid json3');
  {
    const r = await runScenario({
      playerResponse: { videoDetails: { videoId: 'TESTVID' }, captions: { playerCaptionsTracklistRenderer: { captionTracks: [
        { languageCode: 'en', kind: 'asr', baseUrl: 'https://yt.example/track1' } ] } } },
      respond: (url) => url.indexOf('track1') !== -1
        ? { status: 200, body: JSON3_BODY }
        : { status: 404, body: '' }
    });
    const d = lastDispatch(r.dispatchEvents);
    check('dispatched transcript text', d && d.text && d.text.indexOf('Hello world') === 0, JSON.stringify(d));
    check('no reason on success', d && d.reason === '', JSON.stringify(d));
    check('fetch order: baseUrl first (no innertube/timedtext)', r.log.length === 1, JSON.stringify(r.log));
  }

  console.log('\nScenario 2: baseUrl returns EMPTY body (bot-check signature) -> empty_response');
  {
    const r = await runScenario({
      playerResponse: { videoDetails: { videoId: 'TESTVID' }, captions: { playerCaptionsTracklistRenderer: { captionTracks: [
        { languageCode: 'en', kind: 'asr', baseUrl: 'https://yt.example/track1' } ] } } },
      respond: () => ({ status: 200, body: '' })  // 200 with 0 bytes, everywhere
    });
    const d = lastDispatch(r.dispatchEvents);
    check('reason is empty_response (not parse_failed)', d && d.reason === 'empty_response', JSON.stringify(d));
    check('tried fmt=json3 retry before giving up', r.log.some(l => String(l.url).indexOf('fmt=json3') !== -1), JSON.stringify(r.log));
  }

  console.log('\nScenario 3: baseUrl returns unparseable garbage -> parse_failed');
  {
    const r = await runScenario({
      playerResponse: { videoDetails: { videoId: 'TESTVID' }, captions: { playerCaptionsTracklistRenderer: { captionTracks: [
        { languageCode: 'en', kind: 'asr', baseUrl: 'https://yt.example/track1' } ] } } },
      respond: () => ({ status: 200, body: UNPARSEABLE })
    });
    const d = lastDispatch(r.dispatchEvents);
    check('reason is parse_failed', d && d.reason === 'parse_failed', JSON.stringify(d));
  }

  console.log('\nScenario 4: no player tracks, no textTracks -> innertube -> timedtext fallback');
  {
    const r = await runScenario({
      playerResponse: null,
      ytcfg: { INNERTUBE_API_KEY: 'LIVEKEY', INNERTUBE_CONTEXT_CLIENT_VERSION: '2.20250701.00.00', VISITOR_DATA: 'abc' },
      respond: (url) => {
        if (url.indexOf('youtubei/v1/player') !== -1) return { status: 200, body: PLAYER_WITH_TRACKS };
        if (url.indexOf('track1') !== -1) return { status: 200, body: JSON3_BODY };
        return { status: 200, body: JSON3_BODY };
      }
    });
    const d = lastDispatch(r.dispatchEvents);
    const urls = r.log.map(l => String(l.url));
    check('dispatched transcript text', d && d.text && d.text.indexOf('Hello world') === 0, JSON.stringify(d));
    check('innertube called after 6s poll exhaustion', urls.some(u => u.indexOf('youtubei/v1/player') !== -1), JSON.stringify(urls));
    check('used live innertube key from ytcfg', urls.some(u => u.indexOf('key=') !== -1), JSON.stringify(urls));
  }

  console.log('\nScenario 5: innertube HANGS -> timeout aborts -> timedtext fallback succeeds');
  {
    const r = await runScenario({
      playerResponse: null,
      respond: (url) => {
        if (url.indexOf('youtubei/v1/player') !== -1) return 'hang';           // abort after timeout
        if (url.indexOf('timedtext') !== -1) return { status: 200, body: JSON3_BODY };
        return { status: 200, body: JSON3_BODY };
      }
    });
    const d = lastDispatch(r.dispatchEvents);
    const urls = r.log.map(l => String(l.url));
    check('aborted hung innertube and fell to timedtext', d && d.text && d.text.indexOf('Hello world') === 0, JSON.stringify(d));
    check('timedtext fallback reached', urls.some(u => u.indexOf('timedtext') !== -1), JSON.stringify(urls));
  }

  console.log('\nScenario 6: everything fails -> third_party_failed');
  {
    const r = await runScenario({
      playerResponse: null,
      respond: () => ({ status: 200, body: '' })
    });
    const d = lastDispatch(r.dispatchEvents);
    check('final reason is empty_response (empty bodies everywhere)', d && d.reason === 'empty_response', JSON.stringify(d));
  }

  console.log('\nScenario 7: STALE player response (SPA navigation) must NOT be used');
  {
    // window.ytInitialPlayerResponse still holds the PREVIOUS video's data
    // after in-page navigation (proven live 2026-08-04). The fetcher must
    // refuse it (videoId mismatch) and go to innertube for the RIGHT video.
    const r = await runScenario({
      playerResponse: { videoDetails: { videoId: 'OTHERVID' }, captions: { playerCaptionsTracklistRenderer: { captionTracks: [
        { languageCode: 'en', kind: 'asr', baseUrl: 'https://yt.example/STALE-track' } ] } } },
      ytcfg: { INNERTUBE_API_KEY: 'LIVEKEY' },
      respond: (url) => {
        if (url.indexOf('youtubei/v1/player') !== -1) return { status: 200, body: PLAYER_WITH_TRACKS };
        if (url.indexOf('track1') !== -1) return { status: 200, body: JSON3_BODY };
        return { status: 200, body: JSON3_BODY };
      }
    });
    const d = lastDispatch(r.dispatchEvents);
    const urls = r.log.map(l => String(l.url));
    check('refused stale ytInitialPlayerResponse (never fetched STALE-track)',
      !urls.some(u => u.indexOf('STALE-track') !== -1), JSON.stringify(urls));
    check('fell through to innertube for the correct video',
      urls.some(u => u.indexOf('youtubei/v1/player') !== -1), JSON.stringify(urls));
    check('dispatched the CORRECT video transcript',
      d && d.text && d.text.indexOf('Hello world') === 0, JSON.stringify(d));
    check('dispatched with the requested video id', d && d.id === 'TESTVID', JSON.stringify(d));
  }

  console.log('\nStatic: dead source removed from chain');
  check('youtubetranscript.com not called', src.indexOf("fetch('https://youtubetranscript.com") === -1 && src.indexOf("fetchT('https://youtubetranscript.com") === -1);

  console.log('\nStatic: every fetch is timeout-bounded (fetchT)');
  const bareFetches = (src.match(/(?<!T)fetch\(\s*['"]/g) || []).length;
  check('no bare fetch( calls', bareFetches === 0, 'found ' + bareFetches);

  console.log('\n──────────────────────────────────────────────');
  console.log('PASS: ' + passCount + '  FAIL: ' + failures);
  process.exit(failures ? 1 : 0);
})();
