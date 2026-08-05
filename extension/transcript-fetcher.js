// ─── Praxis: injected transcript fetcher ─────────────────
// Loaded via <script src="chrome-extension://..."> so YouTube's
// CSP allows execution (inline script injection is blocked).
// Runs inside YouTube's PAGE context => MUST stay ES5-conservative
// (no arrows, no template literals, no let/const, use var + function).
// Reads videoId from its own script tag data-video-id attribute.
// Communicates results via CustomEvent('_yl_tr') on document.
//
// Source chain (v2, Aug 2026 — FRE escalation fix):
//   1. ytInitialPlayerResponse  (evaluated ONCE on cold load; polled only
//      while the SPA object for this video is not yet present)
//   2. <video> textTracks        (no network, cues only)
//   3. innertube player API      (freshly signed URLs + live key/visitorData)
//   4. timedtext baseUrl         (first-party, terminal source)
//
// ESCALATION (the FRE fix): the most common recoverable failures are
// empty_response (bot-check / sign-in block: HTTP 200 with 0 bytes) and
// parse_failed. Both MUST escalate to innertube — which obtains a FRESHLY
// signed URL and a fresh session identity — instead of dying where they
// were detected. fmt=json3 is ONLY a legitimate remedy for parse_failed
// (some tracks only serve parseable data with it); it cannot help
// empty_response (the block is session-level, not format-level), so
// empty_response skips fmt entirely and goes straight to innertube.
// Terminal null is reserved for AFTER innertube and timedtext have both
// been tried. youtubetranscript.com is REMOVED — it is dead.

(function() {
  var id = null;
  var scripts = document.querySelectorAll('script[src*="transcript-fetcher"]');
  for (var i = 0; i < scripts.length; i++) {
    var vid = scripts[i].getAttribute('data-video-id');
    if (vid) { id = vid; break; }
  }
  if (!id) return;

  var maxTry = 3;                // ~1.5s presence poll (SPA nav only)
  var tries = 0;
  var innertubeTried = false;

  // Reason codes so the extension can tell the user WHAT failed:
  // 'no_captions' | 'fetch_blocked' | 'empty_response' | 'parse_failed' | 'timedtext_failed'
  // 'empty_response' = HTTP 200 with 0 bytes (sign-in/anti-bot block).
  // 'fetch_blocked' = network-level failure fetching a caption track.
  // failReason is threaded through each attempt's dispatch — the most
  // specific failure seen so far wins at the terminal dispatch.
  var failReason = 'no_captions';

  // Live page identity — defeats stale-client bot rejection. Read once from
  // ytcfg (what YouTube's own player uses) via the public .get() accessor
  // (falls back to the minified .data_ private field).
  function ytcfgValue(name) {
    try {
      var yt = window.ytcfg;
      if (yt && typeof yt.get === 'function') { var g = yt.get(name); if (g) return g; }
    } catch (e) {}
    try {
      var d = window.ytcfg && window.ytcfg.data_;
      if (d && d[name]) return d[name];
    } catch (e) {}
    return null;
  }
  var innertubeKey = ytcfgValue('INNERTUBE_API_KEY') || '';
  var clientVersion = ytcfgValue('INNERTUBE_CONTEXT_CLIENT_VERSION');
  var visitorData = ytcfgValue('VISITOR_DATA') || '';

  // ─── Timeout budget (must fit inside content.js's 18s outer bound) ───
  //   poll presence       ~1.5s (3 x 500ms)
  //   primary track fetch  4s
  //   fmt=json3 retry      3s
  //   innertube player     5s
  //   timedtext fallback   4s
  // Worst serial chain: 1.5 + 4 + 5 + 4 = 14.5s (< 18s with margin).
  // With fmt: 1.5 + 4 + 3 + 5 + 4 = 17.5s (still under 18).
  function fetchT(url, opts, ms) {
    ms = ms || 4000;
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    var o = opts || {};
    if (ctrl) o.signal = ctrl.signal;
    var timer = setTimeout(function() { if (ctrl) ctrl.abort(); }, ms);
    return fetch(url, o).then(function(r) { clearTimeout(timer); return r; },
      function(e) { clearTimeout(timer); throw e; });
  }

  function poll() {
    // 1. Cold-load fast path: ytInitialPlayerResponse is embedded in the
    //    served HTML and (if the SPA guard below passes) is present for this
    //    id. Evaluate it ONCE and branch immediately — no repeated polling
    //    of a static object.
    try {
      var p = window.ytInitialPlayerResponse;
      // GUARD: ytInitialPlayerResponse goes STALE after YouTube's in-page
      // (SPA) navigation. Only trust it when it belongs to our video.
      if (p && p.videoDetails && p.videoDetails.videoId === id) {
        var c = p.captions && p.captions.playerCaptionsTracklistRenderer;
        var tracks = c && c.captionTracks;
        if (tracks && tracks.length) {
          var tr = pickBestTrack(tracks);
          if (tr && tr.baseUrl) { fetchBaseUrl(tr.baseUrl); return; }
        }
        // present-for-this-id but NO captionTracks: this source is
        // authoritatively done — jump straight to innertube now.
        goInnertube();
        return;
      }
    } catch (e) {}
    // 2. textTracks (cheap, no network, cues only) — check before polling.
    try {
      var videoEl = document.querySelector('video');
      if (videoEl && videoEl.textTracks && videoEl.textTracks.length > 0) {
        if (extractFromTextTracks(videoEl) === true) return;
      }
    } catch (e) {}
    // ytInitialPlayerResponse is either absent or not yet for this id (SPA).
    // Poll PRESENCE only, on a short window — fetches are what need the time.
    if (++tries < maxTry) setTimeout(poll, 500);
    else goInnertube();
  }

  // Prefer manual English, then auto-generated English, then any track.
  function pickBestTrack(tracks) {
    var manual = null, asr = null, any = null;
    for (var i = 0; i < tracks.length; i++) {
      var t = tracks[i];
      if (!t || !t.baseUrl) continue;
      if (!any) any = t;
      var lc = (t.languageCode || '').toLowerCase();
      if (lc.indexOf('en') !== 0) continue;
      if (t.kind === 'asr') { if (!asr) asr = t; }
      else { if (!manual) manual = t; }
    }
    return manual || asr || any;
  }

  // Guarded innertube entry: only ever one innertube attempt per injection.
  function goInnertube() {
    if (innertubeTried) { timedtext(); return; }
    innertubeTried = true;
    tryInnertube();
  }

  // 3rd source: the innertube player API — same-origin POST, returns caption
  // tracks with freshly signed baseUrls. Send the LIVE key + clientVersion +
  // visitorData from ytcfg (player-shaped call) so flagged/stale sessions are
  // far less likely to get the 200-empty bot-check. Reads r.text(), applies
  // isEmptyText (=> empty_response), checks r.ok, THEN JSON.parse — same
  // discipline as fetchBaseUrl, so the innertube bot-check is not lost.
  function tryInnertube() {
    // If we could not obtain a live client version, an old/stale identity is
    // WORSE for the bot-check. Skip innertube and go to timedtext.
    if (!clientVersion) { timedtext(); return; }
    var url = 'https://www.youtube.com/youtubei/v1/player';
    if (innertubeKey) url += '?key=' + encodeURIComponent(innertubeKey);
    var ctxClient = { clientName: 'WEB', clientVersion: clientVersion };
    if (visitorData) ctxClient.visitorData = visitorData;
    fetchT(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ videoId: id, context: { client: ctxClient } })
    }, 5000).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function(text) {
      if (isEmptyText(text)) { failReason = 'empty_response'; timedtext(); return; }
      var d = null;
      try { d = JSON.parse(text); } catch (e) { failReason = 'parse_failed'; timedtext(); return; }
      var c = d && d.captions && d.captions.playerCaptionsTracklistRenderer;
      var tracks = c && c.captionTracks;
      if (tracks && tracks.length) {
        var tr = pickBestTrack(tracks);
        if (tr && tr.baseUrl) { fetchBaseUrl(tr.baseUrl); return; }
      }
      failReason = 'no_captions';
      timedtext();
    }).catch(function() { timedtext(); });
  }

  // 4th source: timedtext fallback (first-party — youtube.com/api/timedtext).
  // Terminal source: always dispatches (success or the most specific reason).
  // Reason 'timedtext_failed' replaces the stale third-party label
  // (youtubetranscript.com was removed long ago — this is never third-party).
  function timedtext() {
    var terminalReason = (failReason === 'no_captions') ? 'timedtext_failed' : failReason;
    fetchT('https://www.youtube.com/api/timedtext?v=' + encodeURIComponent(id) + '&fmt=json3',
      null, 4000).then(function(r) { return r.text(); })
      .then(function(text) {
        if (isEmptyText(text)) { dispatch(null, 'empty_response'); return; }
        var result = parseTranscriptResponse(text);
        if (result) dispatch(result);
        else dispatch(null, terminalReason);
      }).catch(function() {
        dispatch(null, terminalReason === 'timedtext_failed' ? 'fetch_blocked' : terminalReason);
      });
  }

  // Fetch a caption track URL. Both the primary/base origin and the
  // freshly-signed innertube URL land here. DISTINGUISH the empty body
  // (bot-check: HTTP 200 with 0 bytes) from a non-empty body that fails to
  // parse, and ESCALATE both to the next source — never dispatch terminal
  // here (innertube + timedtext may still succeed).
  function fetchBaseUrl(url) {
    fetchT(url, null, 4000).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function(text) {
      if (isEmptyText(text)) {
        // Bot-block: NOT a format issue. fmt=json3 is the same session, same
        // signed URL — it can't help. Go straight to innertube for a fresh
        // signed URL and fresh session identity. (FRE fix)
        failReason = 'empty_response';
        goInnertube();
        return;
      }
      var result = parseTranscriptResponse(text);
      if (result) { dispatch(result); return; }
      // parse_failed: fmt=json3 IS a legitimate remedy — try it first, but
      // if it also fails, ESCALATE to innertube (not terminal).
      failReason = 'parse_failed';
      retryWithFmtJson3(url);
    }).catch(function() {
      // Network-level failure — no retry of the same dead URL; escalate.
      goInnertube();
    });
  }

  // Try appending fmt=json3 to a track URL (some tracks only serve parseable
  // data with it). Thread the reason through; empty/parse/network outcomes
  // all escalate to innertube — the chain never dies here.
  function retryWithFmtJson3(url) {
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    fetchT(url + sep + 'fmt=json3', null, 3000).then(function(r) { return r.text(); })
      .then(function(t2) {
        if (isEmptyText(t2)) { failReason = 'empty_response'; goInnertube(); return; }
        var r2 = parseTranscriptResponse(t2);
        if (r2) dispatch(r2);
        else { failReason = 'parse_failed'; goInnertube(); }
      }).catch(function() { goInnertube(); });
  }

  function isEmptyText(text) {
    return text === null || text === undefined || String(text).trim() === '';
  }

  function parseTranscriptResponse(text) {
    if (isEmptyText(text)) return null;
    try { var d = JSON.parse(text); if (d.events) { var p = []; for (var ei = 0; ei < d.events.length; ei++) { var segs = d.events[ei].segs || []; for (var si = 0; si < segs.length; si++) { if (segs[si].utf8) p.push(segs[si].utf8); } } if (p.length) return p.join(' ').replace(/\s+/g, ' ').trim(); } } catch(e) {}
    try { var xp = new DOMParser(); var xml = xp.parseFromString(text, 'text/xml'); var texts = xml.querySelectorAll('text'); if (texts.length > 0) { var p = []; for (var ti = 0; ti < texts.length; ti++) { if (texts[ti].textContent) p.push(texts[ti].textContent.trim()); } if (p.length) return p.join(' ').replace(/\s+/g, ' ').trim(); } var ps = xml.querySelectorAll('p'); if (ps.length > 0) { var p2 = []; for (var pi = 0; pi < ps.length; pi++) { if (ps[pi].textContent) p2.push(ps[pi].textContent.trim()); } if (p2.length) return p2.join(' ').replace(/\s+/g, ' ').trim(); } } catch(e) {}
    return null;
  }

  // Cheap, no network. Note: .cues is empty unless captions were ACTIVATED
  // by the user (CC button), so this fires rarely — kept as a free win.
  function extractFromTextTracks(video) {
    var tracks = video.textTracks;
    for (var i = 0; i < tracks.length; i++) {
      var t = tracks[i];
      if (t.language && t.language.indexOf('en') !== 0) continue;
      if (t.cues && t.cues.length > 0) { var p = []; for (var j = 0; j < t.cues.length; j++) { if (t.cues[j].text) p.push(t.cues[j].text); } if (p.length) { dispatch(p.join(' ').replace(/\s+/g, ' ').trim()); return true; } }
    }
    return null;
  }

  function dispatch(text, reason) {
    document.dispatchEvent(new CustomEvent('_yl_tr', { detail: { id: id, text: text, reason: text ? '' : (reason || failReason) } }));
  }

  poll();
})();
