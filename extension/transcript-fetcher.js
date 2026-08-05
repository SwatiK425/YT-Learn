// ─── Praxis: injected transcript fetcher ─────────────────
// Loaded via <script src="chrome-extension://..."> so YouTube's
// CSP allows execution (inline script injection is blocked).
// Runs inside YouTube's PAGE context => MUST stay ES5-conservative
// (no arrows, no template literals, no let/const, use var + function).
// Reads videoId from its own script tag data-video-id attribute.
// Communicates results via CustomEvent('_yl_tr') on document.
//
// Source chain (harden for reliability):
//   1. ytInitialPlayerResponse  (polled ~6s)
//   2. <video> textTracks        (no network, cues only)
//   3. innertube player API      (freshly signed URLs + live key/visitorData)
//   4. signed timedtext baseUrl  (same-origin, as-is then fmt=json3)
//   youtubetranscript.com is REMOVED — it is dead (Merlin AI landing page).

(function() {
  var id = null;
  var scripts = document.querySelectorAll('script[src*="transcript-fetcher"]');
  for (var i = 0; i < scripts.length; i++) {
    var vid = scripts[i].getAttribute('data-video-id');
    if (vid) { id = vid; break; }
  }
  if (!id) return;

  var maxTry = 12, tries = 0;   // 12 x 500ms = 6s window on player response
  var innertubeTried = false;   // innertube player API used as 2nd source
  // Reason codes so the extension can tell the user WHAT failed:
  // 'no_captions' | 'fetch_blocked' | 'empty_response' | 'parse_failed' | 'third_party_failed'
  // 'empty_response' = HTTP 200 with 0 bytes (sign-in/anti-bot block).
  // 'fetch_blocked' = network-level failure fetching a caption track.
  var failReason = 'no_captions';

  // Live page identity — defeats stale-client bot rejection. Read once from
  // ytcfg (what YouTube's own player uses); fall back to known-good constants.
  function ytcfgValue(name) {
    try {
      var d = window.ytcfg && window.ytcfg.data_;
      if (d && d[name]) return d[name];
    } catch (e) {}
    return null;
  }
  var innertubeKey = ytcfgValue('INNERTUBE_API_KEY') || '';
  var clientVersion = ytcfgValue('INNERTUBE_CONTEXT_CLIENT_VERSION') || '2.20240101.00.00';
  var visitorData = ytcfgValue('VISITOR_DATA') || '';

  function poll() {
    try {
      var p = window.ytInitialPlayerResponse;
      // GUARD: ytInitialPlayerResponse is written once at page load and goes
      // STALE after YouTube's in-page (SPA) navigation — reading it then
      // yields the PREVIOUS video's caption tracks for the wrong video.
      // Only trust it when it belongs to the video we were asked for.
      if (p && p.videoDetails && p.videoDetails.videoId === id) {
        var c = p.captions && p.captions.playerCaptionsTracklistRenderer;
        var tracks = c && c.captionTracks;
        if (tracks && tracks.length) {
          var tr = pickBestTrack(tracks);
          if (tr && tr.baseUrl) { fetchBaseUrl(tr.baseUrl); return; }
        }
      }
    } catch(e) {}
    try {
      var videoEl = document.querySelector('video');
      if (videoEl && videoEl.textTracks && videoEl.textTracks.length > 0) { if (extractFromTextTracks(videoEl) === true) return; }
    } catch(e) {}
    if (++tries < maxTry) setTimeout(poll, 500);
    else if (!innertubeTried) { innertubeTried = true; tryInnertube(); }
    else tryTimedtextFallback();
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

  // fetch with an AbortController timeout: a hung request must NOT eat the
  // whole inject window (18s) — the click fallback in content.js needs room
  // to run. Resolves with the Response; rejects on timeout/abort/network
  // error. Falls back to plain fetch where AbortController is unavailable.
  function fetchT(url, opts, ms) {
    ms = ms || 8000;
    var ctrl = ('AbortController' in window) ? new AbortController() : null;
    var o = opts || {};
    if (ctrl) o.signal = ctrl.signal;
    var timer = setTimeout(function() { if (ctrl) ctrl.abort(); }, ms);
    return fetch(url, o).then(function(r) { clearTimeout(timer); return r; },
      function(e) { clearTimeout(timer); throw e; });
  }

  // 2nd source: the innertube player API — same-origin POST, returns caption
  // tracks with freshly signed baseUrls. Send the LIVE key + clientVersion +
  // visitorData from ytcfg (player-shaped call) so flagged/stale sessions are
  // far less likely to get the 200-empty bot-check.
  function tryInnertube() {
    failReason = 'fetch_blocked';
    var url = 'https://www.youtube.com/youtubei/v1/player' +
      (innertubeKey ? '?key=' + encodeURIComponent(innertubeKey) : '');
    var ctxClient = { clientName: 'WEB', clientVersion: clientVersion };
    if (visitorData) ctxClient.visitorData = visitorData;
    var body = JSON.stringify({
      videoId: id,
      context: { client: ctxClient }
    });
    fetchT(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body
    }).then(function(r) { return r.json(); }).then(function(d) {
      try {
        var c = d && d.captions && d.captions.playerCaptionsTracklistRenderer;
        var tracks = c && c.captionTracks;
        if (tracks && tracks.length) {
          var tr = pickBestTrack(tracks);
          if (tr && tr.baseUrl) { fetchBaseUrl(tr.baseUrl); return; }
        }
      } catch(e) {}
      failReason = 'no_captions';
      tryTimedtextFallback();
    }).catch(function() { tryTimedtextFallback(); });
  }

  // Fetch a caption track URL. Distinguish an EMPTY body (blocked / bot-check:
  // HTTP 200 with 0 bytes) from a non-empty body that fails to parse.
  function fetchBaseUrl(url) {
    failReason = 'fetch_blocked';
    fetchT(url).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function(text) {
      if (isEmptyText(text)) {
        failReason = 'empty_response';          // anti-bot block, not parse failure
        retryWithFmtJson3(url);
        return;
      }
      var result = parseTranscriptResponse(text);
      if (result) { dispatch(result); }
      else {
        failReason = 'parse_failed';
        retryWithFmtJson3(url);
      }
    }).catch(function() {
      // Transient failure — retry once after 800ms, then fall back to the
      // innertube source for a freshly signed URL.
      setTimeout(function() {
        fetchT(url).then(function(r) { if (!r.ok) throw new Error(); return r.text(); })
          .then(function(text) {
            if (isEmptyText(text)) { dispatch(null, 'empty_response'); return; }
            var result = parseTranscriptResponse(text);
            if (result) dispatch(result); else dispatch(null, 'parse_failed');
          }).catch(function() {
            if (!innertubeTried) { innertubeTried = true; tryInnertube(); }
            else dispatch(null, 'fetch_blocked');
          });
      }, 800);
    });
  }

  // Try appending fmt=json3 to a track URL (some tracks only serve parseable
  // data with it). Never clobber the specific reason recorded above.
  function retryWithFmtJson3(url) {
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    fetchT(url + sep + 'fmt=json3').then(function(r) { return r.text(); })
      .then(function(t2) {
        if (isEmptyText(t2)) { dispatch(null, failReason); return; }
        var r2 = parseTranscriptResponse(t2);
        if (r2) dispatch(r2); else dispatch(null, failReason);
      }).catch(function() { dispatch(null, failReason); });
  }

  // Same-origin last-ditch probe: raw timedtext with the video id. Kept as the
  // final fallback (innertube signed URL is preferred). youtubetranscript.com is
  // REMOVED — dead service (returns Merlin AI landing page, never JSON).
  function tryTimedtextFallback() {
    failReason = 'third_party_failed';
    fetchT('https://www.youtube.com/api/timedtext?v=' + encodeURIComponent(id) + '&fmt=json3')
      .then(function(r) { return r.text(); })
      .then(function(t) {
        if (isEmptyText(t)) { dispatch(null, 'empty_response'); return; }
        var r2 = parseTranscriptResponse(t);
        if (r2) dispatch(r2); else dispatch(null, failReason);
      }).catch(function() { dispatch(null, failReason); });
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