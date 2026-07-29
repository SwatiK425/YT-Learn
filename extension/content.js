// ─── YT-Learn Content Script ─────────────────────────
// Inject button, handle overlay, fetch transcript, call backend.

const BACKEND = 'http://localhost:8003';

// Set by retry pills; consumed by the next performGenerate. Travels in the
// /api/suggest request body so the regeneration is guaranteed to see it
// (the old signal-POST side channel raced the suggest call).
var pendingRetryReason = null;

// ─── Model settings (BYOK) ──────────────────────────────
// Stored locally in chrome.storage; sent per-request to the backend, which
// holds nothing. Model lists are editable suggestions, not hard constraints,
// so they never go stale.
var PROVIDERS = {
  google:     { label: 'Google AI',    needsBase: false, fast: 'gemini-2.5-flash' },
  anthropic:  { label: 'Anthropic',    needsBase: false, fast: 'claude-sonnet-5-20250202' },
  openai:     { label: 'OpenAI',       needsBase: false, fast: 'gpt-4o-mini' },
  openrouter: { label: 'OpenRouter',   needsBase: false, fast: '' },
  'opencode-zen': { label: 'OpenCode Zen', needsBase: false, fast: 'deepseek-v4-flash-free' },
  custom:     { label: 'Custom (OpenAI-compatible)', needsBase: true, fast: '' }
};

function getLLMConfig() {
  return new Promise(function(resolve) {
    chrome.storage.local.get('yl_llm', function(d) {
      var c = d.yl_llm;
      // Model is optional — backend uses provider default if empty
      if (c && c.api_key) resolve(c);
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

// ─── Blocked result cache (declined exercises remember their state) ──
function loadCachedBlocked(videoUrl, userId) {
  return new Promise(function(resolve) {
    var key = makeCacheKey(videoUrl, userId) + '_blocked';
    chrome.storage.local.get('yl_blocked', function(data) {
      var blocked = data.yl_blocked || {};
      var cached = blocked[key];
      if (cached && Date.now() - cached.ts < 86400000) resolve(cached.data);
      else resolve(null);
    });
  });
}

function saveCachedBlocked(videoUrl, userId, data) {
  var key = makeCacheKey(videoUrl, userId) + '_blocked';
  chrome.storage.local.get('yl_blocked', function(d) {
    var blocked = d.yl_blocked || {};
    blocked[key] = { data: data, ts: Date.now() };
    chrome.storage.local.set({ yl_blocked: blocked });
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

// Route straight to exercise view. Profile is auto-created on first run.
function showHomeView() {
  var videoUrl = window.location.href;
  chrome.storage.local.get('yl_profile', function(data) {
    var profile = data.yl_profile;
    if (!profile) {
      // First visit — auto-create a userId silently
      profile = { user_id: 'u_' + Date.now(), role: '', goal: '' };
      chrome.storage.local.set({ yl_profile: profile });
    }
    showExperimentView(profile.user_id, videoUrl);
  });
}

function closeOverlay() { if (overlay) { overlay.remove(); overlay = null; } }

// ─── Profile View (REMOVED — was role + goal onboarding, no longer needed) ───────

// ─── Model Settings View (BYOK) ─────────────────────────
// Updated: Test Connection button + live model list from API.
var _yl_modelsCache = null; // { provider: [{id, name}], default_model } from last test

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

    <label style="font-size:13px;color:#888;display:block;margin-top:10px;">API key</label>
    <input id="yl-api-key" class="yl-input" type="password" placeholder="sk-... or AIza..." autocomplete="off" />

    <button id="yl-test-connection" class="yl-btn yl-btn-secondary" style="width:100%;margin-top:10px;text-align:center;">
      ↻ Test Connection
    </button>

    <div id="yl-connection-status" class="yl-status hidden" style="margin-top:8px;"></div>

    <div id="yl-model-section" class="hidden">
      <label style="font-size:13px;color:#888;display:block;margin-top:12px;">Model <span style="color:#666;font-size:11px;">(optional — default used if not selected)</span></label>
      <div id="yl-model-list-container" style="max-height:200px;overflow-y:auto;border:1px solid #444;border-radius:8px;padding:4px;"></div>
    </div>

    <div id="yl-settings-status" class="yl-status hidden"></div>

    <div style="display:flex;gap:8px;margin-top:16px;">
      <button id="yl-settings-back" class="yl-btn yl-btn-secondary" style="flex:1;">Back</button>
      <button id="yl-settings-save" class="yl-btn yl-btn-primary" style="flex:2;">Save</button>
    </div>
  `;

  var providerSel = document.getElementById('yl-provider');
  var baseWrap = document.getElementById('yl-base-wrap');

  function refreshProviderUI(keepModel) {
    var p = PROVIDERS[providerSel.value] || PROVIDERS.custom;
    baseWrap.classList.toggle('hidden', !p.needsBase);
    // Clear model cache when provider changes
    _yl_modelsCache = null;
    document.getElementById('yl-model-section').classList.add('hidden');
    var status = document.getElementById('yl-connection-status');
    status.className = 'yl-status hidden';
    // For custom provider, show text input instead of dropdown
    if (providerSel.value === 'custom') {
      document.getElementById('yl-model-section').innerHTML = `
        <label style="font-size:13px;color:#888;display:block;margin-top:12px;">Model</label>
        <input id="yl-model-custom" class="yl-input" placeholder="e.g. gpt-4o-mini" />
      `;
    } else {
      document.getElementById('yl-model-section').innerHTML = `
        <label style="font-size:13px;color:#888;display:block;margin-top:12px;">Model <span style="color:#666;font-size:11px;">(optional — click Test Connection first)</span></label>
        <div id="yl-model-list-container" style="max-height:200px;overflow-y:auto;border:1px solid #444;border-radius:8px;padding:4px;"><p style="color:#666;font-size:12px;text-align:center;padding:12px;">Click "Test Connection" to load available models</p></div>
      `;
    }
  }

  providerSel.addEventListener('change', function() { refreshProviderUI(false); });

  // ── Test Connection ──────────────────────────────────
  document.getElementById('yl-test-connection').addEventListener('click', async function() {
    var btn = this;
    var provider = providerSel.value;
    var apiKey = document.getElementById('yl-api-key').value.trim();
    var status = document.getElementById('yl-connection-status');
    var modelSection = document.getElementById('yl-model-section');

    function setStatus(ok, msg) {
      status.textContent = msg;
      status.className = 'yl-status ' + (ok ? 'yl-status-ok' : 'yl-status-err');
    }

    if (!apiKey) { setStatus(false, 'Enter your API key first.'); return; }

    btn.disabled = true;
    btn.textContent = '⏳ Testing...';
    setStatus(true, 'Contacting backend...');
    modelSection.classList.add('hidden');

    try {
      var resp = await fetch(BACKEND + '/api/llm/test-connection', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: provider, api_key: apiKey })
      });
      var data = await resp.json();

      if (data.valid) {
        _yl_modelsCache = data;
        setStatus(true, '✓ Connected — ' + data.models.length + ' model(s) available');

        // Populate model list
        var container = document.getElementById('yl-model-list-container');
        if (container) {
          container.innerHTML = '';
          data.models.forEach(function(m, idx) {
            var isDefault = m.id === data.default_model;
            var div = document.createElement('div');
            div.className = 'yl-model-option' + (isDefault ? ' yl-model-selected' : '');
            div.dataset.modelId = m.id;
            div.textContent = m.name;
            if (isDefault) div.textContent += ' ⭐';
            div.addEventListener('click', function() {
              container.querySelectorAll('.yl-model-option').forEach(function(el) { el.classList.remove('yl-model-selected'); });
              this.classList.add('yl-model-selected');
            });
            container.appendChild(div);
          });
          // If no default marker, select first
          if (!container.querySelector('.yl-model-selected') && container.firstChild) {
            container.firstChild.classList.add('yl-model-selected');
          }
        }
        modelSection.classList.remove('hidden');
      } else {
        _yl_modelsCache = null;
        setStatus(false, '❌ ' + (data.error || 'Invalid API key'));
      }
    } catch(e) {
      _yl_modelsCache = null;
      setStatus(false, '❌ Server unreachable — is the backend running?');
    } finally {
      btn.disabled = false;
      btn.textContent = '↻ Test Connection';
    }
  });

  // ── Load existing config ────────────────────────────
  chrome.storage.local.get('yl_llm', function(d) {
    var c = d.yl_llm || {};
    if (c.provider && PROVIDERS[c.provider]) providerSel.value = c.provider;
    refreshProviderUI(true);
    if (c.api_key) document.getElementById('yl-api-key').value = c.api_key;
    if (c.base_url) document.getElementById('yl-base-url').value = c.base_url;
  });

  document.getElementById('yl-settings-back').addEventListener('click', showHomeView);

  document.getElementById('yl-settings-save').addEventListener('click', function() {
    var provider = providerSel.value;
    var apiKey = document.getElementById('yl-api-key').value.trim();
    var baseUrl = (document.getElementById('yl-base-url').value || '').trim();
    var status = document.getElementById('yl-settings-status');

    function err(msg) { status.textContent = msg; status.className = 'yl-status yl-status-err'; }

    if (!apiKey) { err('Enter your API key.'); return; }
    if (PROVIDERS[provider].needsBase && !baseUrl) { err('Custom provider needs a base URL.'); return; }

    // Get selected model (optional)
    var model = '';
    if (provider === 'custom') {
      model = (document.getElementById('yl-model-custom') || {}).value || '';
    } else if (_yl_modelsCache) {
      var selected = document.querySelector('#yl-model-list-container .yl-model-selected');
      if (selected) model = selected.dataset.modelId || '';
    } else {
      // re-saving without testing — read previous model from storage
      chrome.storage.local.get('yl_llm', function(prev) {
        model = (prev.yl_llm && prev.yl_llm.model) || '';
        doSave(model, provider, apiKey, baseUrl, status);
      });
      return; // doSave called async
    }

    doSave(model, provider, apiKey, baseUrl, status);
  });

  function doSave(model, provider, apiKey, baseUrl, status) {
    var cfg = {
      provider: provider,
      base_url: baseUrl,
      model: model,
      fast_model: PROVIDERS[provider].fast || '',
      api_key: apiKey
    };
    chrome.storage.local.set({ yl_llm: cfg }, function() {
      status.textContent = model ? 'Saved. Using ' + model + '.' : 'Saved. Backend will use default model.';
      status.className = 'yl-status yl-status-ok';
      setTimeout(showHomeView, 900);
    });
  }
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

function showExperimentView(userId, videoUrl) {
  if (!videoUrl) videoUrl = window.location.href;
  const views = document.getElementById('yt-learn-views');
  if (!views) return;

  views.innerHTML = `
    <!-- API key input (coming soon - uncomment when ready)
    <label style="font-size:13px;color:#888;display:block;margin-top:12px;">Your API key</label>
    <input id="yl-api-key-self" class="yl-input" type="password" placeholder="sk-..." autocomplete="off" />
    -->

    <div id="yl-status" class="yl-status hidden"></div>

    <button id="yl-generate" class="yl-btn yl-btn-primary hidden" style="display:none;">↻ Try Again</button>

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

  // ─── Check cache first ────────────────────────────────
  loadCachedExercise(videoUrl, userId).then(function(cached) {
    if (cached && cached.experiment_id) {
      renderExerciseFromCache(cached, userId, videoUrl);
      generateCount = 1;
      currentExpId = cached.experiment_id;
      document.getElementById('yl-generate').classList.remove('hidden');
      document.getElementById('yl-generate').textContent = '↻ Try Again';
      console.log('[YT-Learn] rendered from cache');
      return;
    }
    // Check for cached blocked result
    loadCachedBlocked(videoUrl, userId).then(function(blocked) {
      if (blocked) {
        renderBlockedView(blocked, videoUrl, userId);
        console.log('[YT-Learn] rendered blocked from cache');
        return;
      }
      // No cache — auto-generate immediately
      performGenerate(videoUrl, userId, currentExpId);
    });
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
            performGenerate(videoUrl, userId, currentExpId);
          }
        }, 4000);
        retryRow._timeoutId = timeoutId;
        return;
      }
      return;
    }
    performGenerate(videoUrl, userId, null);
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
}

// ─── Render blocked / not-supported view ───────────────────
function renderBlockedView(data, videoUrl, userId) {
  setLoading(false);
  hideStatus();
  var resultEl = document.getElementById('yl-result');
  if (!resultEl) return;
  resultEl.classList.remove('hidden');
  document.getElementById('yl-skeleton').classList.add('hidden');
  document.getElementById('yl-content').classList.add('hidden');
  document.getElementById('yl-generate').classList.remove('hidden');
  document.getElementById('yl-generate').textContent = '↻ Try Again';

  // Cache the blocked result so reopening the overlay is instant
  saveCachedBlocked(videoUrl, userId, data);

  var safeReason = escapeHtml(data.reason || 'This video does not teach a skill you can practice.');
  resultEl.innerHTML = '\
    <div class="yl-blocked" style="text-align:center;padding:24px 12px;color:#bbb;">\
      <div style="font-size:32px;margin-bottom:8px;">🔬</div>\
      <div style="font-size:14px;font-weight:600;color:#e0e0e0;margin-bottom:6px;">\
        No exercise for this one\
      </div>\
      <div style="font-size:12px;color:#999;line-height:1.5;margin-bottom:16px;">\
        ' + safeReason + '\
        <br><br>\
        This product builds exercises from specific types of tutorials only.\
        <br><br>\
        Wrong call?\
      </div>\
      <button id="yl-retry-blocked" class="yl-btn yl-btn-primary" style="display:inline-block;">\
        ↻ Try Again\
      </button>\
    </div>';

  document.getElementById('yl-retry-blocked').addEventListener('click', function() {
    // Fresh generation (not a retry) — clear any stale retry state
    currentExpId = null;
    generateCount = 0;
    performGenerate(videoUrl, userId, null);
  });
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
async function performGenerate(videoUrl, userId, currentExpId) {
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
    const body = { video_url: videoUrl };
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
        blocked: function(payload) {
          streamErr = 'blocked';
          renderBlockedView(payload, videoUrl, userId);
        },
      });

      if (streamErr === 'blocked') return;
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

      skelEl.classList.add('hidden');
      contentEl.classList.remove('hidden');

      saveCachedExercise(videoUrl, userId, {
        experiment_id: expId,
        principle: donePayload.principle || '',
        experiment: donePayload.experiment || '',
        why_it_matters: donePayload.why_it_matters || '',
      });

      hideStatus();
      setLoading(false);
      document.getElementById('yl-generate').classList.remove('hidden');
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
    if (data.status === 'blocked') {
      renderBlockedView(data, videoUrl, userId);
      return;
    }

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
    skelEl.classList.add('hidden');
    contentEl.classList.remove('hidden');

    saveCachedExercise(videoUrl, userId, {
      experiment_id: expId,
      principle: data.principle,
      experiment: data.experiment,
      why_it_matters: data.why_it_matters || '',
    });

    document.getElementById('yl-generate').classList.remove('hidden');
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

// ─── Goal auto-fill (REMOVED — no longer personalising exercises) ─────────────────

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
    var script = document.createElement('script');
    script.src = chrome.runtime.getURL('transcript-fetcher.js');
    script.setAttribute('data-video-id', safeId);
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
    setTimeout(function() { injectButton(); startPrefetch(); }, 100);
  }
}
document.addEventListener('yt-navigate-finish', function() { navReady = false; setTimeout(function() { navReady = true; onNav(); }, 500); });
window.addEventListener('popstate', function() { setTimeout(onNav, 1000); });
window.addEventListener('hashchange', function() { setTimeout(onNav, 1000); });
if (window.location.pathname === '/watch') setTimeout(function() { injectButton(); startPrefetch(); }, 500);
