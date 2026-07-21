// ─── YT-Learn Content Script ─────────────────────────
// Inject button, handle overlay, fetch transcript, call backend.

const BACKEND = 'http://localhost:8002';

// Set by retry pills; consumed by the next performGenerate. Travels in the
// /api/suggest request body so the regeneration is guaranteed to see it
// (the old signal-POST side channel raced the suggest call).
var pendingRetryReason = null;

// ─── Model settings (BYOK) ──────────────────────────────
// Stored locally in chrome.storage; sent per-request to the backend, which
// holds nothing. Model lists are editable suggestions, not hard constraints,
// so they never go stale.
var PROVIDERS = {
  google:     { label: 'Google AI',    needsBase: false, models: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-3.1-flash', 'gemini-3.1-pro'], fast: 'gemini-2.5-flash' },
  anthropic:  { label: 'Anthropic',    needsBase: false, models: ['claude-sonnet-5', 'claude-opus-4-8', 'claude-haiku-4-5-20251001', 'claude-fable-5'], fast: 'claude-sonnet-5' },
  openai:     { label: 'OpenAI',       needsBase: false, models: ['gpt-4o', 'gpt-4o-mini'], fast: 'gpt-4o-mini' },
  openrouter: { label: 'OpenRouter',   needsBase: false, models: ['anthropic/claude-sonnet-4-6', 'deepseek/deepseek-chat', 'google/gemini-2.5-flash'], fast: '' },
  'opencode-zen': { label: 'OpenCode Zen', needsBase: false, models: ['deepseek-v4-flash-free', 'hy3-free', 'north-mini-code-free', 'nemotron-3-ultra-free'], fast: 'hy3-free' },
  custom:     { label: 'Custom (OpenAI-compatible)', needsBase: true, models: [], fast: '' }
};

function getLLMConfig() {
  return new Promise(function(resolve) {
    chrome.storage.local.get('yl_llm', function(d) {
      var c = d.yl_llm;
      if (c && c.api_key && c.model) resolve(c);
      else resolve(null); // backend falls back to its env defaults
    });
  });
}

// ─── Cache helpers ──────────────────────────────────────

function makeCacheKey(videoUrl, userId) {
  var m = videoUrl.match(/[?&]v=([^&]+)/) || videoUrl.match(/youtu\.be\/([^?&]+)/);
  return (m ? m[1] : videoUrl) + '_' + (userId || 'anon');
}

function loadCachedExercise(videoUrl, userId) {
  return new Promise(function(resolve) {
    var key = makeCacheKey(videoUrl, userId);
    chrome.storage.local.get('yl_exercises', function(data) {
      var exercises = data.yl_exercises || {};
      var cached = exercises[key];
      if (cached && Date.now() - cached.ts < 86400000) resolve(cached.experiment);
      else resolve(null);
    });
  });
}

function saveCachedExercise(videoUrl, userId, experiment) {
  var key = makeCacheKey(videoUrl, userId);
  chrome.storage.local.get('yl_exercises', function(data) {
    var exercises = data.yl_exercises || {};
    exercises[key] = { experiment: experiment, ts: Date.now() };
    chrome.storage.local.set({ yl_exercises: exercises });
  });
}

function clearCacheForVideo(videoUrl, userId) {
  var key = makeCacheKey(videoUrl, userId);
  chrome.storage.local.get('yl_exercises', function(data) {
    var exercises = data.yl_exercises || {};
    delete exercises[key];
    chrome.storage.local.set({ yl_exercises: exercises });
  });
}

// ─── Inject button into YouTube toolbar ───────────────
var _yl_pollTimer = null;
var _yl_obs = null;

function injectButton() {
  if (document.getElementById('yt-learn-btn')) return true;
  if (window.location.pathname !== '/watch') return false;

  var target = document.querySelector('#top-level-buttons-computed');
  if (!target) {
    // Toolbar not ready yet — set up both observer AND poll fallback
    if (!window._yl_injecting) {
      window._yl_injecting = true;

      // MutationObserver catches toolbar the instant it renders
      if (!_yl_obs) {
        var root = document.body || document.documentElement;
        if (root) {
          _yl_obs = new MutationObserver(function() {
            if (document.querySelector('#top-level-buttons-computed') && !document.getElementById('yt-learn-btn')) {
              injectButton();
            }
          });
          _yl_obs.observe(root, { childList: true, subtree: true });
        }
      }

      // Poll fallback — retries every 800ms up to 15s in case observer misses it
      if (!_yl_pollTimer) {
        var tries = 0;
        _yl_pollTimer = setInterval(function() {
          tries++;
          if (document.getElementById('yt-learn-btn') || tries > 18) {
            clearInterval(_yl_pollTimer);
            _yl_pollTimer = null;
            return;
          }
          injectButton();
        }, 800);
      }
    }
    return false;
  }

  // Clean up injection machinery once we succeed
  if (_yl_obs) { _yl_obs.disconnect(); _yl_obs = null; }
  if (_yl_pollTimer) { clearInterval(_yl_pollTimer); _yl_pollTimer = null; }

  const btn = document.createElement('button');
  btn.id = 'yt-learn-btn';
  btn.className = 'yt-spec-button-shape-next yt-spec-button-shape-next--tonal yt-spec-button-shape-next--mono yt-spec-button-shape-next--size-m';
  btn.innerHTML = '<div class="yt-learn-btn-inner"><span>📚</span> Learn Lab</div>';
  btn.addEventListener('click', openOverlay);
  target.appendChild(btn);
  return true;
}

let overlay = null;

function openOverlay() {
  if (overlay) { overlay.remove(); overlay = null; return; }
  overlay = document.createElement('div');
  overlay.id = 'yt-learn-overlay';
  overlay.innerHTML = `
    <div id="yt-learn-panel">
      <button id="yt-learn-settings" title="Model settings">⚙️</button>
      <button id="yt-learn-close">×</button>
      <div id="yt-learn-views"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  document.getElementById('yt-learn-close').addEventListener('click', closeOverlay);
  document.getElementById('yt-learn-settings').addEventListener('click', showModelSettingsView);
  overlay.addEventListener('click', function(e) { if (e.target === overlay) closeOverlay(); });
  showHomeView();
}

// Route to profile setup or exercise view based on stored profile.
function showHomeView() {
  var videoUrl = window.location.href;
  chrome.storage.local.get('yl_profile', function(data) {
    var profile = data.yl_profile;
    if (profile) showExperimentView(profile.user_id, profile, videoUrl);
    else showProfileView();
  });
}

function closeOverlay() { if (overlay) { overlay.remove(); overlay = null; } }

// ─── Profile View (first-run) — 2 questions only ────────
function showProfileView() {
  const views = document.getElementById('yt-learn-views');
  if (!views) return;
  views.innerHTML = `
    <div class="yt-learn-step">Welcome to YT-Learn</div>
    <p class="yt-learn-sub">Two quick questions so every exercise is relevant to you.</p>
    <label style="font-size:13px;color:#888;display:block;margin-top:12px;">What's your role?</label>
    <input id="yl-role" class="yl-input" placeholder="e.g. PM, designer, founder..." />
    <label style="font-size:13px;color:#888;display:block;margin-top:10px;">What are you trying to be better at?</label>
    <input id="yl-goal-setup" class="yl-input" placeholder="e.g. product strategy, coding, design..." />
    <button id="yl-save" class="yl-btn yl-btn-primary" style="margin-top:16px;">Save & Start</button>
  `;
  document.getElementById('yl-save').addEventListener('click', function() {
    var role = document.getElementById('yl-role').value.trim();
    var goal = document.getElementById('yl-goal-setup').value.trim();
    if (!role || !goal) { showStatus('Please fill in both fields.', true); return; }
    var userId = 'u_' + Date.now();
    var profile = { user_id: userId, role: role, goal: goal };
    chrome.storage.local.set({ yl_profile: profile }, function() {
      showExperimentView(userId, profile, window.location.href);
    });
  });
}

// ─── Model Settings View (BYOK) ─────────────────────────
function showModelSettingsView() {
  const views = document.getElementById('yt-learn-views');
  if (!views) return;

  var providerOptions = Object.keys(PROVIDERS).map(function(k) {
    return '<option value="' + k + '">' + PROVIDERS[k].label + '</option>';
  }).join('');

  views.innerHTML = `
    <div class="yt-learn-step">Model settings</div>
    <p class="yt-learn-sub">Exercises run on your own API key. It's stored only in this browser and sent only to your YT-Learn backend — never anywhere else.</p>

    <label style="font-size:13px;color:#888;display:block;margin-top:12px;">Provider</label>
    <select id="yl-provider" class="yl-input">${providerOptions}</select>

    <div id="yl-base-wrap" class="hidden">
      <label style="font-size:13px;color:#888;display:block;margin-top:10px;">Base URL</label>
      <input id="yl-base-url" class="yl-input" placeholder="https://your-endpoint/v1" />
    </div>

    <label style="font-size:13px;color:#888;display:block;margin-top:10px;">Model</label>
    <input id="yl-model" class="yl-input" list="yl-model-list" placeholder="Model name" />
    <datalist id="yl-model-list"></datalist>

    <label style="font-size:13px;color:#888;display:block;margin-top:10px;">API key</label>
    <input id="yl-api-key" class="yl-input" type="password" placeholder="sk-..." autocomplete="off" />

    <div id="yl-settings-status" class="yl-status hidden"></div>

    <div style="display:flex;gap:8px;margin-top:16px;">
      <button id="yl-settings-back" class="yl-btn yl-btn-secondary" style="flex:1;">Back</button>
      <button id="yl-settings-save" class="yl-btn yl-btn-primary" style="flex:2;">Save</button>
    </div>
  `;

  var providerSel = document.getElementById('yl-provider');
  var modelInput = document.getElementById('yl-model');
  var datalist = document.getElementById('yl-model-list');
  var baseWrap = document.getElementById('yl-base-wrap');

  function refreshProviderUI(keepModel) {
    var p = PROVIDERS[providerSel.value] || PROVIDERS.custom;
    baseWrap.classList.toggle('hidden', !p.needsBase);
    datalist.innerHTML = p.models.map(function(mo) { return '<option value="' + mo + '"></option>'; }).join('');
    if (!keepModel) modelInput.value = p.models[0] || '';
  }

  providerSel.addEventListener('change', function() { refreshProviderUI(false); });

  // Load existing config
  chrome.storage.local.get('yl_llm', function(d) {
    var c = d.yl_llm || {};
    if (c.provider && PROVIDERS[c.provider]) providerSel.value = c.provider;
    refreshProviderUI(true);
    if (c.model) modelInput.value = c.model;
    if (c.base_url) document.getElementById('yl-base-url').value = c.base_url;
    if (c.api_key) document.getElementById('yl-api-key').value = c.api_key;
    if (!c.model) refreshProviderUI(false);
  });

  document.getElementById('yl-settings-back').addEventListener('click', showHomeView);

  document.getElementById('yl-settings-save').addEventListener('click', function() {
    var provider = providerSel.value;
    var model = modelInput.value.trim();
    var apiKey = document.getElementById('yl-api-key').value.trim();
    var baseUrl = (document.getElementById('yl-base-url').value || '').trim();
    var status = document.getElementById('yl-settings-status');

    function err(msg) { status.textContent = msg; status.className = 'yl-status yl-status-err'; }
    if (!model) { err('Enter a model name.'); return; }
    if (!apiKey) { err('Enter your API key.'); return; }
    if (PROVIDERS[provider].needsBase && !baseUrl) { err('Custom provider needs a base URL.'); return; }

    var cfg = {
      provider: provider,
      base_url: baseUrl,
      model: model,
      fast_model: PROVIDERS[provider].fast || '',
      api_key: apiKey
    };
    chrome.storage.local.set({ yl_llm: cfg }, function() {
      status.textContent = 'Saved. Exercises will use ' + model + '.';
      status.className = 'yl-status yl-status-ok';
      setTimeout(showHomeView, 900);
    });
  });
}



// ─── Exercise View ────────────────────────────────────

function parseSteps(text) {
  if (!text) return [];
  var steps = text.split(/\n+/).map(function(s) { return s.trim(); }).filter(Boolean);
  if (steps.length <= 1) {
    steps = text.split(/(?<=[.!?])\s+/).map(function(s) { return s.trim(); }).filter(function(s) { return s.length > 5; });
  }
  return steps.slice(0, 6);
}

function showExperimentView(userId, profile, videoUrl) {
  if (!videoUrl) videoUrl = window.location.href;
  const views = document.getElementById('yt-learn-views');
  if (!views) return;

  views.innerHTML = `
    <label class="yl-goal-label">Is this why you're interested in this video?</label>
    <input id="yl-goal" class="yl-input" placeholder="Loading..." />

    <div id="yl-status" class="yl-status hidden"></div>

    <button id="yl-generate" class="yl-btn yl-btn-primary">Generate</button>

    <div id="yl-result" class="hidden">
      <!-- Skeleton (shown during streaming) -->
      <div id="yl-skeleton" class="yl-skeleton">
        <div class="yl-skel-block" style="width:85%;height:16px;"></div>
        <div class="yl-skel-block" style="width:60%;height:16px;margin-top:8px;"></div>
        <div class="yl-skel-block" style="width:100%;height:48px;margin-top:14px;"></div>
      </div>

      <div id="yl-content" class="hidden">
        <div id="yl-insight" class="yl-insight"></div>
        <div id="yl-finish-line" class="yl-finish-line hidden">
          <span class="yl-fl-label">What you'll have:</span>
          <span id="yl-fl-text"></span>
        </div>
        <div class="yl-ex-section">
          <div class="yl-ex-title">Your exercise</div>
          <div id="yl-steps"></div>
          <div class="yl-time-badge">⏱️ 3 min</div>
          <div id="yl-done-wrap" class="hidden" style="margin-top:10px;">
            <button id="yl-mark-done" class="yl-btn yl-btn-secondary" style="width:100%;font-size:12px;">✓ Mark complete</button>
          </div>
        </div>

        <div id="yl-retry-row" class="hidden" style="margin-top:8px;display:flex;gap:4px;align-items:center;justify-content:center;">
          <span style="font-size:11px;color:#888;white-space:nowrap;">Why retry?</span>
          <button class="yl-retry-pill" data-reason="too_easy">😴 Too Easy</button>
          <button class="yl-retry-pill" data-reason="too_hard">💪 Too Hard</button>
          <button class="yl-retry-pill" data-reason="wrong_topic">🎯 Wrong Topic</button>
        </div>

        <div class="yl-difficulty-row" id="yl-diff-row" style="margin-top:8px;display:flex;gap:6px;">
          <button class="yl-diff-btn" data-diff="too_easy">😴 Too Easy</button>
          <button class="yl-diff-btn" data-diff="just_right">👍 Just Right</button>
          <button class="yl-diff-btn" data-diff="too_hard">💪 Too Hard</button>
        </div>

        <div class="yl-fb-wrap">
          <details>
            <summary class="yl-fb-toggle">Was this useful?</summary>
            <div class="yl-fb-inner">
              <div class="yl-feedback-row">
                <button id="yl-like" class="yl-fb-btn" title="Helpful">👍</button>
                <button id="yl-dislike" class="yl-fb-btn" title="Not helpful">👎</button>
              </div>
              <textarea id="yl-question" class="yl-input" rows="1" placeholder="What would be more useful? (optional)"></textarea>
              <button id="yl-send-fb" class="yl-btn yl-btn-secondary">Send</button>
              <div id="yl-fb-done" class="hidden yl-status yl-status-ok">Thanks for the feedback!</div>
            </div>
          </details>
        </div>
      </div>
    </div>
  `;

  let currentExpId = null;
  let likedState = null;
  let generateCount = 0;
  var goalSetByUser = false;

  // ─── Check cache first ────────────────────────────────
  loadCachedExercise(videoUrl, userId).then(function(cached) {
    if (cached && cached.experiment_id) {
      renderExerciseFromCache(cached, userId, videoUrl);
      generateCount = 1;
      currentExpId = cached.experiment_id;
      document.getElementById('yl-generate').textContent = '↻ Try Again';
      if (cached.goal) document.getElementById('yl-goal').value = cached.goal;
      console.log('[YT-Learn] rendered from cache');
      return;
    }
    startGoalAutoFill(profile);
  });

  // ─── Generate / Try Again button ──────────────────────
  document.getElementById('yl-generate').addEventListener('click', function() {
    if (generateCount > 0 && currentExpId) {
      var retryRow = document.getElementById('yl-retry-row');
      if (retryRow && retryRow.classList.contains('hidden')) {
        retryRow.classList.remove('hidden');
        this.disabled = true;
        this.textContent = 'Select a reason...';
        var timeoutId = setTimeout(function() {
          if (retryRow && !retryRow.classList.contains('hidden')) {
            retryRow.classList.add('hidden');
            document.getElementById('yl-generate').disabled = false;
            document.getElementById('yl-generate').textContent = '↻ Try Again';
            performGenerate(videoUrl, userId, profile, currentExpId);
          }
        }, 4000);
        retryRow._timeoutId = timeoutId;
        return;
      }
      return;
    }
    goalSetByUser = true;
    performGenerate(videoUrl, userId, profile, null);
  });

  // ─── Goal input ──
  document.getElementById('yl-goal').addEventListener('keydown', function(e) {
    goalSetByUser = true;
    this.dataset.userSet = 'true';  // stop autofill from overwriting user input
    if (e.key === 'Enter') {
      e.preventDefault();
      document.getElementById('yl-generate').click();
    }
  });

  // ─── Feedback ──
  document.getElementById('yl-send-fb')?.addEventListener('click', async function() {
    if (!currentExpId) return;
    var fb = { experiment_id: currentExpId, liked: likedState, question: document.getElementById('yl-question')?.value || null };
    try {
      await fetch(BACKEND + '/api/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fb) });
      document.getElementById('yl-fb-done').classList.remove('hidden');
      document.getElementById('yl-send-fb').disabled = true;
    } catch(e) {}
  });
  document.getElementById('yl-like')?.addEventListener('click', function() { likedState = true; this.dataset.selected = 'true'; document.getElementById('yl-dislike').dataset.selected = 'false'; });
  document.getElementById('yl-dislike')?.addEventListener('click', function() { likedState = false; this.dataset.selected = 'true'; document.getElementById('yl-like').dataset.selected = 'false'; });
}

// ─── Render from cache (skips form, no API call) ─────────
function renderExerciseFromCache(cached, userId, videoUrl) {
  currentExpId = cached.experiment_id;
  document.getElementById('yl-result').classList.remove('hidden');
  document.getElementById('yl-skeleton').classList.add('hidden');
  document.getElementById('yl-content').classList.remove('hidden');
  document.getElementById('yl-insight').textContent = cached.principle || '';
  if (cached.why_it_matters) {
    document.getElementById('yl-fl-text').textContent = cached.why_it_matters;
    document.getElementById('yl-finish-line').classList.remove('hidden');
  }
  var steps = parseSteps(cached.experiment || '');
  var stepsHtml = '';
  for (var i = 0; i < steps.length; i++) {
    stepsHtml += '<label class="yl-step"><input type="checkbox" /> <span>' + escapeHtml(steps[i]) + '</span></label>';
  }
  document.getElementById('yl-steps').innerHTML = stepsHtml || escapeHtml(cached.experiment || '');
  wireCheckboxes(userId, currentExpId);
  wireDifficultyButtons(userId, currentExpId);
  wireRetryPills(userId, currentExpId, videoUrl);
  generateCount = 1;
  document.getElementById('yl-generate').textContent = '↻ Try Again';
  document.getElementById('yl-goal').style.display = 'none';
}

// ─── Streaming JSON parser ────────────────────────────────
function unescapeJsonString(s) {
  // Turn a raw JSON string body (backslash escapes intact) into real text.
  try { return JSON.parse('"' + s + '"'); } catch (e) { return s; }
}

function parsePartialJSON(buf) {
  var r = {};
  var pm = buf.match(/"principle"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (pm) r.principle = unescapeJsonString(pm[1]);
  var em = buf.match(/"experiment"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (em) r.experiment = unescapeJsonString(em[1]);
  var wm = buf.match(/"why_it_matters"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (wm) r.why_it_matters = unescapeJsonString(wm[1]);
  return r;
}

// ─── SSE reader for streaming suggest ─────────────────────
async function readSSEStream(resp, handlers) {
  var reader = resp.body.getReader();
  var decoder = new TextDecoder();
  var buf = '';
  var event = '', data = '';

  function flush() {
    if (event && data) {
      try { handlers[event] && handlers[event](JSON.parse(data)); } catch(e) {}
      event = ''; data = '';
    }
  }

  while (true) {
    var r = await reader.read();
    if (r.done) { flush(); break; }
    buf += decoder.decode(r.value, { stream: true });
    var parts = buf.split('\n\n');
    buf = parts.pop();
    for (var pi = 0; pi < parts.length; pi++) {
      event = ''; data = '';
      var lines = parts[pi].split('\n');
      for (var li = 0; li < lines.length; li++) {
        var line = lines[li];
        if (line.startsWith('event: ')) event = line.slice(7).trim();
        else if (line.startsWith('data: ')) data = line.slice(6).trim();
      }
      flush();
    }
  }
}

// ─── Generate logic (streaming-first, with skeleton) ─────
async function performGenerate(videoUrl, userId, profile, currentExpId) {
  // Never block generation on the goal — backend has a sensible default.
  var goal = (document.getElementById('yl-goal').value || '').trim();

  if (currentExpId) {
    fetch(BACKEND + '/api/signal', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, signal_type: 'try_again', value: '', experiment_id: currentExpId })
    }).catch(function() {});
  }

  setLoading(true);

  // Show skeleton immediately
  var resultEl = document.getElementById('yl-result');
  resultEl.classList.remove('hidden');
  var skelEl = document.getElementById('yl-skeleton');
  skelEl.classList.remove('hidden');
  var contentEl = document.getElementById('yl-content');
  contentEl.classList.add('hidden');
  document.getElementById('yl-insight').textContent = '';
  document.getElementById('yl-steps').innerHTML = '';
  document.getElementById('yl-finish-line').classList.add('hidden');

  try {
    const transcript = await getTranscript(videoUrl, { allowClick: true });
    const llmCfg = await getLLMConfig();
    const body = { video_url: videoUrl, goal_override: goal || undefined };
    if (transcript) body.transcript = transcript;
    if (llmCfg) body.llm = llmCfg;
    if (currentExpId) body.force = true; // Try Again must bypass cache
    if (pendingRetryReason) { body.retry_reason = pendingRetryReason; pendingRetryReason = null; }

    // Try streaming first
    var resp = await fetch(BACKEND + '/api/suggest/stream?user_id=' + userId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var useStreaming = resp.ok && resp.body && typeof resp.body.getReader === 'function';

    if (useStreaming) {
      // Stream via SSE. Live preview from partial JSON; the authoritative
      // render comes from the server-parsed `done` payload.
      var jsonBuf = '';
      var donePayload = null;
      var streamErr = null;
      var revealed = false;

      await readSSEStream(resp, {
        skeleton: function() { /* skeleton already visible */ },
        status: function(payload) {
          if (payload.message) showStatus(payload.message, false);
        },
        raw: function(payload) {
          jsonBuf += payload.text || '';
          var fields = parsePartialJSON(jsonBuf);
          if (!revealed && (fields.principle || fields.experiment)) {
            revealed = true;
            skelEl.classList.add('hidden');
            contentEl.classList.remove('hidden');
          }
          if (fields.principle) document.getElementById('yl-insight').textContent = fields.principle;
          if (fields.experiment) document.getElementById('yl-steps').textContent = fields.experiment;
        },
        done: function(payload) { donePayload = payload; },
        error: function(payload) { streamErr = payload.message || 'Stream error'; },
      });

      if (streamErr || !donePayload) {
        showStatus(streamErr || 'Generation failed. Try again.', true);
        setLoading(false);
        return;
      }

      var expId = donePayload.experiment_id;
      document.getElementById('yl-insight').textContent = donePayload.principle || '';
      if (donePayload.why_it_matters) {
        document.getElementById('yl-fl-text').textContent = donePayload.why_it_matters;
        document.getElementById('yl-finish-line').classList.remove('hidden');
      }

      var steps = parseSteps(donePayload.experiment || '');
      var stepsHtml = '';
      for (var i = 0; i < steps.length; i++) {
        stepsHtml += '<label class="yl-step"><input type="checkbox" /> <span>' + escapeHtml(steps[i]) + '</span></label>';
      }
      document.getElementById('yl-steps').innerHTML = stepsHtml || escapeHtml(donePayload.experiment || '');

      wireCheckboxes(userId, expId);
      wireDifficultyButtons(userId, expId);
      wireRetryPills(userId, expId, videoUrl);

      document.getElementById('yl-goal').style.display = 'none';
      skelEl.classList.add('hidden');
      contentEl.classList.remove('hidden');

      saveCachedExercise(videoUrl, userId, {
        experiment_id: expId,
        principle: donePayload.principle || '',
        experiment: donePayload.experiment || '',
        why_it_matters: donePayload.why_it_matters || '',
        goal: goal,
      });

      hideStatus();
      setLoading(false);
      return;
    }

    // Streaming not available — fallback to non-streaming
    resp = await fetch(BACKEND + '/api/suggest?user_id=' + userId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var data = await resp.json();
    if (!resp.ok) { showStatus(data.detail || 'Generation failed.', true); setLoading(false); return; }

    var expId = data.experiment_id;
    document.getElementById('yl-insight').textContent = data.principle;
    if (data.why_it_matters) {
      document.getElementById('yl-fl-text').textContent = data.why_it_matters;
      document.getElementById('yl-finish-line').classList.remove('hidden');
    }
    var steps = parseSteps(data.experiment);
    var stepsHtml = '';
    for (var i = 0; i < steps.length; i++) {
      stepsHtml += '<label class="yl-step"><input type="checkbox" /> <span>' + escapeHtml(steps[i]) + '</span></label>';
    }
    document.getElementById('yl-steps').innerHTML = stepsHtml || escapeHtml(data.experiment);
    wireCheckboxes(userId, expId);
    wireDifficultyButtons(userId, expId);
    wireRetryPills(userId, expId, videoUrl);
    document.getElementById('yl-goal').style.display = 'none';
    skelEl.classList.add('hidden');
    contentEl.classList.remove('hidden');

    saveCachedExercise(videoUrl, userId, {
      experiment_id: expId,
      principle: data.principle,
      experiment: data.experiment,
      why_it_matters: data.why_it_matters || '',
      goal: goal,
    });

    hideStatus();
  } catch (err) {
    showStatus('Server unreachable. Is the backend running?', true);
  }
  setLoading(false);
}

// ─── Wire helpers ──────────────────────────────────────

function wireCheckboxes(userId, expId) {
  var sc = document.getElementById('yl-steps');
  if (!sc) return;
  if (sc.dataset.wired === 'true') return;
  sc.dataset.wired = 'true';
  var cbs = sc.querySelectorAll('input[type="checkbox"]');
  for (var ci = 0; ci < cbs.length; ci++) {
    cbs[ci].addEventListener('change', function() {
      var all = sc.querySelectorAll('input[type="checkbox"]');
      var anyChecked = false, allChecked = true;
      for (var ai = 0; ai < all.length; ai++) {
        if (all[ai].checked) anyChecked = true; else allChecked = false;
      }
      var dw = document.getElementById('yl-done-wrap');
      if (dw) {
        if (anyChecked) dw.classList.remove('hidden');
        if (allChecked) {
          sendSignal(BACKEND, userId, 'completed', '', expId);
          var md = document.getElementById('yl-mark-done');
          if (md) { md.textContent = '✓ Done!'; md.disabled = true; }
        }
      }
    });
  }
  var mdBtn = document.getElementById('yl-mark-done');
  if (mdBtn && mdBtn.dataset.wired !== 'true') {
    mdBtn.dataset.wired = 'true';
    mdBtn.addEventListener('click', function() {
      sendSignal(BACKEND, userId, 'completed', '', expId);
      this.textContent = '✓ Done!'; this.disabled = true;
    });
  }
}

function wireDifficultyButtons(userId, expId) {
  var dr = document.getElementById('yl-diff-row');
  if (!dr) return;
  if (dr.dataset.wired === 'true') return;
  dr.dataset.wired = 'true';
  dr.querySelectorAll('.yl-diff-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      if (this.dataset.selected === 'true') { this.dataset.selected = 'false'; return; }
      dr.querySelectorAll('.yl-diff-btn').forEach(function(b) { b.dataset.selected = 'false'; });
      this.dataset.selected = 'true';
      sendSignal(BACKEND, userId, 'difficulty', this.dataset.diff, expId);
    });
  });
}

function wireRetryPills(userId, expId, videoUrl) {
  var rr = document.getElementById('yl-retry-row');
  if (!rr) return;
  if (rr.dataset.wired === 'true') return;
  rr.dataset.wired = 'true';
  rr.querySelectorAll('.yl-retry-pill').forEach(function(pill) {
    pill.addEventListener('click', function() {
      if (rr._timeoutId) { clearTimeout(rr._timeoutId); rr._timeoutId = null; }
      var reason = this.dataset.reason;
      rr.classList.add('hidden');
      pendingRetryReason = reason;                       // consumed by performGenerate
      sendSignal(BACKEND, userId, 'retry_reason', reason, expId);  // analytics only
      clearCacheForVideo(videoUrl, userId);
      document.getElementById('yl-generate').disabled = false;
      document.getElementById('yl-generate').textContent = '↻ Try Again';
      document.getElementById('yl-generate').disabled = true;
      performGenerate(videoUrl, userId, null, expId);
    });
  });
}

// ─── Goal auto-fill (used when no cache) ─────────────────

var GOAL_PREFIX = 'yl_goal_';

function fetchInferredGoal(profile, videoId) {
  var title = '';
  var h1 = document.querySelector('h1 yt-formatted-string.ytd-video-primary-info-renderer');
  if (h1) title = h1.textContent.trim();
  if (!title) title = document.title.replace(' - YouTube', '').trim();
  var channel = '';
  var chEl = document.querySelector('#owner ytd-channel-name yt-formatted-string a');
  if (chEl) channel = chEl.textContent.trim();
  var desc = '';
  var descEl = document.querySelector('#description yt-formatted-string, #description-inline-expander');
  if (descEl) desc = descEl.textContent.trim().slice(0, 500);
  var profile_data = profile || {};
  return getLLMConfig().then(function(llmCfg) {
    var body = {
      video_title: title, video_channel: channel, video_description: desc,
      role: profile_data.role || '', goal: profile_data.goal || ''
    };
    if (llmCfg) body.llm = llmCfg;
    return fetch(BACKEND + '/api/infer-goal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  })
  .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
  .then(function(data) {
    var g = data.goal && data.goal.length > 10 && data.goal.length < 200 ? data.goal : null;
    if (g && videoId) {
      chrome.storage.local.set({ [GOAL_PREFIX + videoId]: { goal: g, ts: Date.now() } });
    }
    return g;
  })
  .catch(function() { return null; });
}

// Warm the goal cache at page load — by the time the overlay opens, the
// inferred goal is already sitting in storage.
function prefetchGoal() {
  var videoId = extractVideoId(window.location.href);
  if (!videoId) return;
  chrome.storage.local.get([GOAL_PREFIX + videoId, 'yl_profile'], function(d) {
    var cached = d[GOAL_PREFIX + videoId];
    if (cached && Date.now() - cached.ts < 86400000) return; // already warm
    fetchInferredGoal(d.yl_profile || null, videoId);
  });
}

function startGoalAutoFill(profile) {
  var input = document.getElementById('yl-goal');
  if (!input) return;
  var videoId = extractVideoId(window.location.href);
  input.value = '';
  input.placeholder = 'Working on a suggestion...';

  function fill(goal) {
    var inp = document.getElementById('yl-goal');
    if (inp && inp.dataset.userSet !== 'true' && goal) {
      inp.value = goal;
      inp.placeholder = '';
    }
  }

  chrome.storage.local.get(GOAL_PREFIX + videoId, function(d) {
    var cached = d[GOAL_PREFIX + videoId];
    if (cached && cached.goal && Date.now() - cached.ts < 86400000) {
      fill(cached.goal);  // instant — prefetched at page load
      return;
    }
    fetchInferredGoal(profile, videoId).then(fill);
  });
}

// ─── Utils ────────────────────────────────────────────────

function setLoading(on) {
  const btn = document.getElementById('yl-generate');
  if (!btn) return;
  btn.disabled = on;
  btn.textContent = on ? '⚡ Generating...' : '↻ Try Again';
}

function showStatus(msg, isError) {
  const el = document.getElementById('yl-status');
  if (!el) return;
  el.textContent = msg;
  el.className = 'yl-status' + (isError ? ' yl-status-err' : ' yl-status-ok');
}

function hideStatus() {
  const el = document.getElementById('yl-status');
  if (el) el.classList.add('hidden');
}

// ─── Transcript: injected fetch first, click fallback last ─

const TC_PREFIX = 'yl_tr_';

function extractVideoId(url) {
  var m = url.match(/[?&]v=([^&]+)/) || url.match(/youtu\.be\/([^?&]+)/);
  return m ? m[1] : null;
}

function cacheTranscript(videoId, text) {
  chrome.storage.local.set({ [TC_PREFIX + videoId]: { text: text, fetched_at: Date.now() } });
}

async function getTranscript(videoUrl, opts) {
  opts = opts || {};
  var videoId = extractVideoId(videoUrl);
  if (!videoId) return null;
  var allowClick = opts.allowClick !== false;
  var cached = await new Promise(function(r) {
    chrome.storage.local.get(TC_PREFIX + videoId, function(d) { r(d[TC_PREFIX + videoId] ? d[TC_PREFIX + videoId].text : null); });
  });
  if (cached) { console.log('[YT-Learn] transcript from cache'); return cached; }
  console.log('[YT-Learn] trying injectAndFetch for ' + videoId);
  var text = await injectAndFetch(videoId, 12);
  if (text) { console.log('[YT-Learn] injectAndFetch got', text.length, 'chars'); cacheTranscript(videoId, text); return text; }
  console.log('[YT-Learn] injectAndFetch returned null');
  if (allowClick) {
    console.log('[YT-Learn] trying click fallback');
    text = await extractTranscriptByClick();
    if (text) cacheTranscript(videoId, text);
    return text;
  }
  return null;
}

async function prefetchTranscript(videoUrl) {
  return getTranscript(videoUrl, { allowClick: false });
}

// ─── Injected script for silent transcript fetch ──────────

function injectAndFetch(videoId, timeoutSec) {
  timeoutSec = timeoutSec || 12;
  var safeId = String(videoId).replace(/[^a-zA-Z0-9_-]/g, '');
  return new Promise(function(resolve) {
    var handler = function(e) {
      if (e.detail && e.detail.id === safeId) { document.removeEventListener('_yl_tr', handler); resolve(e.detail.text || null); }
    };
    document.addEventListener('_yl_tr', handler, { once: true });
    var injectedFn = function() {
      var id = '__VID__';
      var maxTry = 20, tries = 0;
      function poll() {
        try {
          var p = window.ytInitialPlayerResponse;
          var c = p && p.captions && p.captions.playerCaptionsTracklistRenderer;
          var tracks = c && c.captionTracks;
          if (tracks && tracks.length) {
            var tr = tracks.find(function(x) { return x.languageCode && x.languageCode.indexOf('en') === 0; }) || tracks[0];
            if (tr && tr.baseUrl) { fetchBaseUrl(tr.baseUrl); return; }
          }
        } catch(e) {}
        try {
          var videoEl = document.querySelector('video');
          if (videoEl && videoEl.textTracks && videoEl.textTracks.length > 0) { if (extractFromTextTracks(videoEl) === true) return; }
        } catch(e) {}
        if (++tries < maxTry) setTimeout(poll, 500);
        else tryThirdParty();
      }
      function fetchBaseUrl(url) {
        fetch(url).then(function(r) { if (!r.ok) throw new Error(); return r.text(); })
          .then(function(text) {
            var result = parseTranscriptResponse(text);
            if (result) dispatch(result);
            else {
              var sep = url.indexOf('?') >= 0 ? '&' : '?';
              fetch(url + sep + 'fmt=json3').then(function(r) { return r.text(); })
                .then(function(t2) { dispatch(parseTranscriptResponse(t2) || null); }).catch(function() { dispatch(null); });
            }
          }).catch(function() { dispatch(null); });
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
        var url = 'https://youtubetranscript.com/?v=' + id + '&format=json';
        fetch(url).then(function(r) { return r.json(); }).then(function(d) {
          if (d && d.length) { var p = []; for (var i = 0; i < d.length; i++) { if (d[i].text) p.push(d[i].text); } if (p.length) { dispatch(p.join(' ').replace(/\s+/g, ' ').trim()); return; } }
          fetch('https://www.youtube.com/api/timedtext?v=' + id + '&fmt=json3').then(function(r) { return r.text(); }).then(function(t2) { dispatch(parseTranscriptResponse(t2) || null); }).catch(function() { dispatch(null); });
        }).catch(function() { dispatch(null); });
      }
      function dispatch(text) { document.dispatchEvent(new CustomEvent('_yl_tr', { detail: { id: id, text: text } })); }
      poll();
    };
    var codeStr = '(' + injectedFn.toString().replace('__VID__', safeId) + ')()';
    var script = document.createElement('script');
    script.textContent = codeStr;
    document.body.appendChild(script);
    setTimeout(function() { try { script.remove(); } catch(e) {} }, 100);
    setTimeout(function() { document.removeEventListener('_yl_tr', handler); resolve(null); }, timeoutSec * 1000);
  });
}

// ─── Click transcript fallback ────────────────────────────

async function extractTranscriptByClick() {
  console.log('[YT-Learn] transcript click fallback');
  var panel = document.querySelector('ytd-transcript-body-renderer');
  var isOpen = panel && panel.offsetParent !== null;
  if (isOpen) {
    var segs = panel.querySelectorAll('.segment-text, .segment');
    if (segs.length > 0) return readSegments(segs);
  }
  var hideBtn = document.querySelector('[aria-label="Hide transcript"]');
  if (hideBtn) isOpen = true;
  var btn = null;
  if (!isOpen) {
    btn = document.querySelector('[aria-label="Show transcript"]');
    if (!btn) {
      btn = document.querySelector('#primary-button yt-button-shape button');
      if (btn && btn.textContent.toLowerCase().indexOf('transcript') === -1) btn = null;
    }
  }
  if (!btn && !isOpen) return null;
  var overlayEl = document.getElementById('yt-learn-overlay');
  if (overlayEl) overlayEl.style.display = 'none';
  await sleep(100);
  if (!isOpen && btn) { btn.click(); await sleep(3000); }
  var text = readTranscriptSegments();
  hideBtn = document.querySelector('[aria-label="Hide transcript"]');
  if (hideBtn) { hideBtn.click(); await sleep(400); }
  var stillOpen = document.querySelector('[aria-label="Hide transcript"]');
  if (stillOpen) { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); await sleep(300); }
  var tp = document.querySelector('ytd-transcript-renderer, #transcript');
  if (tp && tp.offsetParent !== null) { var hb = document.querySelector('[aria-label="Hide transcript"]'); if (hb) hb.click(); }
  if (overlayEl) overlayEl.style.display = '';
  return text || null;
}

function readTranscriptSegments() {
  var segs = document.querySelectorAll('ytd-transcript-segment-renderer .segment-text, ytd-transcript-segment-renderer .segment, .ytd-transcript-segment-renderer .segment-text');
  if (segs.length > 0) return readSegments(segs);
  var raw = document.querySelectorAll('ytd-transcript-segment-renderer');
  if (raw.length > 0) { var p = []; for (var i = 0; i < raw.length; i++) { var t = raw[i].querySelector('.segment-text'); if (t && t.textContent) p.push(t.textContent.trim()); } if (p.length) return p.join(' ').replace(/\s+/g, ' ').trim(); }
  return null;
}

function readSegments(els) { var p = []; for (var i = 0; i < els.length; i++) { var t = (els[i].textContent || '').trim(); if (t) p.push(t); } return p.length ? p.join(' ').replace(/\s+/g, ' ').trim() : null; }
function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

function startPrefetch() {
  if (window.location.pathname !== '/watch') return;
  prefetchTranscript(window.location.href).then(function(text) {
    if (text) console.log('[YT-Learn] prefetched transcript:', text.length, 'chars');
    else console.log('[YT-Learn] prefetch: no transcript');
  });
}

function escapeHtml(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function sendSignal(backend, userId, signalType, value, experimentId) {
  fetch(backend + '/api/signal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: userId, signal_type: signalType, value: value, experiment_id: experimentId }) }).catch(function() {});
  console.log('[YT-Learn] signal:', signalType, value);
}

// ─── SPA navigation ───────────────────────────────────────

var navReady = false;
function onNav() {
  if (overlay) { overlay.remove(); overlay = null; }
  // Clean up stale injection machinery from previous page
  if (_yl_obs) { _yl_obs.disconnect(); _yl_obs = null; }
  if (_yl_pollTimer) { clearInterval(_yl_pollTimer); _yl_pollTimer = null; }
  window._yl_injecting = false;

  if (window.location.pathname === '/watch') {
    setTimeout(function() { injectButton(); startPrefetch(); prefetchGoal(); }, 100);
  }
}
document.addEventListener('yt-navigate-finish', function() { navReady = false; setTimeout(function() { navReady = true; onNav(); }, 500); });
window.addEventListener('popstate', function() { setTimeout(onNav, 1000); });
window.addEventListener('hashchange', function() { setTimeout(onNav, 1000); });
if (window.location.pathname === '/watch') setTimeout(function() { injectButton(); startPrefetch(); prefetchGoal(); }, 500);
