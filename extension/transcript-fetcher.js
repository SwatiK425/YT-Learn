// ─── Praxis: injected transcript fetcher ─────────────────
// Loaded via <script src="chrome-extension://..."> so YouTube's
// CSP allows execution (inline script injection is blocked).
// Reads videoId from its own script tag data-video-id attribute.
// Communicates results via CustomEvent('_yl_tr') on document.

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
  // 'no_captions' | 'fetch_blocked' | 'parse_failed' | 'third_party_failed'
  var failReason = 'no_captions';

  function poll() {
    try {
      var p = window.ytInitialPlayerResponse;
      var c = p && p.captions && p.captions.playerCaptionsTracklistRenderer;
      var tracks = c && c.captionTracks;
      if (tracks && tracks.length) {
        var tr = pickBestTrack(tracks);
        if (tr && tr.baseUrl) { fetchBaseUrl(tr.baseUrl); return; }
      }
    } catch(e) {}
    try {
      var videoEl = document.querySelector('video');
      if (videoEl && videoEl.textTracks && videoEl.textTracks.length > 0) { if (extractFromTextTracks(videoEl) === true) return; }
    } catch(e) {}
    if (++tries < maxTry) setTimeout(poll, 500);
    else if (!innertubeTried) { innertubeTried = true; tryInnertube(); }
    else tryThirdParty();
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

  // 2nd source: the innertube player API — same-origin POST, returns
  // caption tracks with freshly signed baseUrls (more reliable than the
  // page-scraped ytInitialPlayerResponse).
  function tryInnertube() {
    failReason = 'fetch_blocked';
    var body = JSON.stringify({
      videoId: id,
      context: { client: { clientName: 'WEB', clientVersion: '2.20240101.00.00' } }
    });
    fetch('https://www.youtube.com/youtubei/v1/player', {
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
      tryThirdParty();
    }).catch(function() { tryThirdParty(); });
  }

  function fetchBaseUrl(url) {
    failReason = 'fetch_blocked';
    fetch(url).then(function(r) { if (!r.ok) throw new Error(); return r.text(); })
      .then(function(text) {
        var result = parseTranscriptResponse(text);
        if (result) dispatch(result);
        else {
          failReason = 'parse_failed';
          var sep = url.indexOf('?') >= 0 ? '&' : '?';
          fetch(url + sep + 'fmt=json3').then(function(r) { return r.text(); })
            .then(function(t2) { var r2 = parseTranscriptResponse(t2); if (r2) dispatch(r2); else dispatch(null, 'parse_failed'); })
            .catch(function() { dispatch(null, 'fetch_blocked'); });
        }
      }).catch(function() {
        // Transient failures happen — retry once after 800ms, then fall
        // back to the innertube source for a freshly signed URL.
        setTimeout(function() {
          fetch(url).then(function(r) { if (!r.ok) throw new Error(); return r.text(); })
            .then(function(text) {
              var result = parseTranscriptResponse(text);
              if (result) dispatch(result); else dispatch(null, 'parse_failed');
            }).catch(function() {
              if (!innertubeTried) { innertubeTried = true; tryInnertube(); }
              else dispatch(null, 'fetch_blocked');
            });
        }, 800);
      });
  }

  function parseTranscriptResponse(text) {
    if (!text) return null;
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

  function tryThirdParty() {
    failReason = 'third_party_failed';
    var url = 'https://youtubetranscript.com/?v=' + id + '&format=json';
    fetch(url).then(function(r) { return r.json(); }).then(function(d) {
      if (d && d.length) { var p = []; for (var i = 0; i < d.length; i++) { if (d[i].text) p.push(d[i].text); } if (p.length) { dispatch(p.join(' ').replace(/\s+/g, ' ').trim()); return; } }
      fetch('https://www.youtube.com/api/timedtext?v=' + id + '&fmt=json3').then(function(r) { return r.text(); }).then(function(t2) { var r2 = parseTranscriptResponse(t2); if (r2) dispatch(r2); else dispatch(null, failReason); }).catch(function() { dispatch(null, failReason); });
    }).catch(function() { dispatch(null, failReason); });
  }

  function dispatch(text, reason) {
    document.dispatchEvent(new CustomEvent('_yl_tr', { detail: { id: id, text: text, reason: text ? '' : (reason || failReason) } }));
  }

  poll();
})();
