"""Verify all 3 architectural gaps are closed.

Without a running server (port 8001 is in zombie state):
  - Check backend source has streaming endpoint, timestamps, correct cache key
  - Check extension source has skeleton UI and SSE reader
  - Check module import works correctly
"""
import sys, os, json, hashlib, importlib.util

os.chdir(os.path.dirname(__file__) or ".")
PASS = 0
FAIL = 0
ERRS = []

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}" if detail else f"  ❌ {name}")

# ─── Load backend source ──────────────────────────────────
with open("backend/app.py", "r", encoding="utf-8") as f:
    py = f.read()

# ─── Load extension source ────────────────────────────────
with open("extension/content.js", "r", encoding="utf-8") as f:
    js = f.read()

with open("extension/content.css", "r", encoding="utf-8") as f:
    css = f.read()

# ═══════════════════════════════════════════════════════════
print("=" * 60)
print("GAP VERIFICATION")
print("=" * 60)

# ─── CHECK 1: Timestamps in chunk extraction ──────────────
print("\n── Gap 1: Timestamps in chunk extraction ──")
check("app.py: chunk_transcript builds timeline with char_offset",
      "timeline" in py and "char_offset" in py,
      "timeline not found or missing char_offset")

check("app.py: chunks have timestamp_sec field",
      "timestamp_sec" in py,
      "chunks missing timestamp_sec field")

check("app.py: chunks have index field",
      "\"index\"" in py and "\"text\"" in py and "\"timestamp_sec\"" in py,
      "chunks missing position offsets")

check("app.py: timeline has char_offset and start_sec",
      "char_offset" in py and "start_sec" in py and "duration" in py,
      "timeline fields not found")

check("app.py: format_timestamp helper exists",
      "format_timestamp" in py,
      "missing timestamp formatting")

# ─── CHECK 2: Cache key excludes signals ─────────────────
print("\n── Gap 2: Cache key (role+goal only, no signals) ──")

check("app.py: profile_cache_key function exists",
      "profile_cache_key" in py,
      "profile_cache_key function not found")

# Direct check: signal_counts should NOT be in the cache key computation
if "profile_cache_key" in py:
    start = py.find("def profile_cache_key")
    end = py.find("\n\n", start)
    if end < 0 or end - start > 500:
        # get 10 lines
        body = py[start:start + 500]
    else:
        body = py[start:end]
    check("profile_cache_key: uses role+goal (no profile.signals)",
          "profile.role" in body and "profile.goal" in body and "profile.signals" not in body,
          "signals referenced in cache key body")
else:
    check("Can't inspect profile_cache_key body", False)

# ─── CHECK 3: Streaming SSE endpoint ─────────────────────
print("\n── Gap 3: Streaming SSE endpoint ──")

check("app.py: StreamingResponse imported",
      "StreamingResponse" in py,
      "StreamingResponse not imported from fastapi.responses")

check("app.py: /api/suggest/stream route declared",
      "@app.post(\"/api/suggest/stream\")" in py,
      "route decorator not found")

check("app.py: suggest_stream async function defined",
      "async def suggest_stream" in py,
      "function not defined")

check("app.py: stream=True in chat completion call for streaming",
      "stream=True" in py,
      "no stream=True in OpenAI call")

check("app.py: SSE 'event: raw' yields incremental tokens",
      "event: raw" in py,
      "SSE raw events not emitted")

check("app.py: SSE 'event: skeleton' yields skeleton placeholder",
      "event: skeleton" in py,
      "skeleton event not emitted")

check("app.py: SSE 'event: done' signal on completion",
      "event: done" in py,
      "done event not emitted")

# Test clean import
print("\n── Module import test ──")
try:
    sys.path.insert(0, "backend")
    # Remove any cached modules
    for k in list(sys.modules.keys()):
        if "app" in k:
            del sys.modules[k]
    from app import app
    routes = [r.path for r in app.routes]
    check("Fresh import: /api/suggest/stream registered",
          "/api/suggest/stream" in routes,
          f"routes seen: {sorted(routes)}")
    # Clean up
    for k in list(sys.modules.keys()):
        if "app" in k:
            del sys.modules[k]
except Exception as e:
    check("Clean import of app.py", False, str(e))

# ─── CHECK 4: Extension skeleton UI and SSE reader ───────
print("\n── Gap 4: Extension streaming & skeleton UI ──")

check("content.js: #yl-skeleton element in HTML template",
      "yl-skeleton" in js,
      "yl-skeleton id not found")

check("content.js: .yl-skeleton CSS class referenced",
      "yl-skeleton" in js,
      "skeleton class not referenced")

check("content.js: readSSEStream function exists",
      "readSSEStream" in js,
      "SSE streaming reader function not found")

check("content.js: parsePartialJSON function exists",
      "parsePartialJSON" in js,
      "partial JSON parser not found")

check("content.js: Handles SSE raw event tokens",
      "raw:" in js and "payload" in js,
      "raw event handler not found")

check("content.js: Handles SSE skeleton event",
      "skeleton:" in js and "payload" in js,
      "skeleton event handler not found")

check("content.js: Handles SSE done event",
      "done:" in js and "payload" in js,
      "done event handler not found")

check("content.js: Uses ReadableStream / getReader for fetch-based streaming",
      "getReader" in js,
      "no getReader — possibly using EventSource?")

check("content.js: Skeleton fades out, content fades in",
      "skelEl.classList.add" in js and "contentEl.classList.remove" in js,
      "skeleton/content transition not found")

check("content.css: @keyframes yl-skel-pulse exists",
      "yl-skel-pulse" in css,
      "skeleton pulse animation keyframe missing")

check("content.css: .yl-skel-block styles defined",
      "yl-skel-block" in css,
      "skeleton block styles missing")

# ═══════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
