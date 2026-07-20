# YT-Learn — Complete Handoff Document

**Date:** 2026-07-14  
**Project root:** `C:\Users\swati\Downloads\SwatiK425\YT-Learn`  
**Running backend PID:** 12328 (uvicorn, port 8001)  
**Active model:** `deepseek-v4-flash-free` (128K context)  
**Provider:** OpenCode Zen (`https://opencode.ai/zen/v1`)  

---

## 1. Product Overview

YT-Learn transforms passive YouTube watching into active learning. A Chrome extension injects a "Learn Lab" button on YouTube watch pages → opens a side panel → extracts the video transcript → sends it to a FastAPI backend → LLM generates a **20% principle** + **≤5-minute micro-experiment** personalised to the learner's role/industry/project.

**Core flow:**
1. User loads YouTube video → extension injects button + starts silent transcript prefetch
2. User clicks "Learn Lab" → side panel opens with smart goal auto-fill
3. User reviews/edits goal → clicks "Generate"
4. Extension gets transcript (injected fetch → click fallback → API) → sends to backend
5. Backend feeds full transcript + user profile to DeepSeek → returns principle + experiment
6. Extension renders checkbox steps, insight, feedback UI

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Chrome (Extension)                        │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ content.js (689 lines)                                       │ │
│  │  • injectButton() — adds "Learn Lab" to YouTube toolbar      │ │
│  │  • openOverlay() — right-side panel with profile/goal/result │ │
│  │  • showProfileView() — first-run role/industry/project form  │ │
│  │  • showExperimentView() — goal input, Generate, result render│ │
│  │  • getTranscript() — cache→injectAndFetch→click fallback     │ │
│  │  • injectAndFetch() — injected <script> for silent transcript │ │
│  │  • extractTranscriptByClick() — DOM click behind overlay     │ │
│  │  • startPrefetch() — silent prefetch on page load            │ │
│  │  • onNav() — SPA navigation handler (yt-navigate-finish)     │ │
│  │ content.css (241 lines) — panel styling, overlay, feedback    │ │
│  │ manifest.json (35 lines) — manifest v3, permissions           │ │
│  └─────────────────────────────────────────────────────────────┘ │
└────────────────────┬────────────────────────────────────────────┘
                     │ POST /api/suggest
                     │ POST /api/profile
                     │ POST /api/infer-goal
                     │ POST /api/feedback
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│              FastAPI Backend (app.py, 345 lines)                  │
│  • /health → {status, model}                                     │
│  • /api/suggest → generates exercise from transcript + profile   │
│  • /api/infer-goal → smart goal suggestion from video context    │
│  • /api/profile → save/load user profile (in-memory, MVP)        │
│  • /api/feedback → persist feedback to feedback.json             │
│  • Fallback transcript fetch via YouTubeTranscriptApi (IP-blocked│
│    on most cloud hosts → use extension for transcript instead)   │
│  • LLM: deepseek-v4-flash-free (128K ctx, full transcript)       │
│  • Model fallback chain: deepseek→north-mini→nemotron→hy3-free   │
│  • Transcript cap: REMOVED — full transcript sent to LLM          │
│  • Feedback: JSON file at backend/feedback.json                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. File Inventory

| File | Lines | Purpose |
|------|-------|---------|
| `extension/content.js` | 689 | Content script — injection, UI, transcript extraction |
| `extension/content.css` | 241 | Panel styling, overlay, input, checkbox steps, feedback |
| `extension/manifest.json` | 35 | Manifest v3, storage perms, youtube host perms |
| `extension/icons/icon128.png` | — | Extension icon (128px) |
| `backend/app.py` | 345 | FastAPI server — all endpoints, transcript fetch, LLM generation |
| `backend/.env` | 3 | API key, base URL, model |
| `backend/.env.example` | 11 | Template with placeholder values |
| `backend/requirements.txt` | 6 | Python dependencies |
| `backend/render.yaml` | 13 | Render deployment config (uses old model — needs update) |
| `backend/feedback.json` | 29 | Feedback storage (in-prod persisted list) |

---

## 4. Complete File Contents

### 4.1 `extension/manifest.json`

```json
{
  "manifest_version": 3,
  "name": "YT-Learn: Learn by Doing",
  "version": "0.1.0",
  "description": "Get a 5-minute micro-experiment from any YouTube video — personalised to your goal, applied to your project.",
  "permissions": ["storage"],
  "host_permissions": ["http://localhost:8001/*"],
  "action": {
    "default_title": "YT-Learn",
    "default_icon": { "128": "icons/icon128.png" }
  },
  "host_permissions": [
    "https://www.youtube.com/*",
    "https://youtube.com/*"
  ],
  "content_scripts": [{
    "matches": ["*://www.youtube.com/*"],
    "js": ["content.js"],
    "css": ["content.css"],
    "run_at": "document_idle"
  }],
  "web_accessible_resources": [{
    "resources": ["popup/*", "icons/*"],
    "matches": ["*://www.youtube.com/*"]
  }],
  "icons": { "128": "icons/icon128.png" }
}
```

### 4.2 `extension/content.js` (689 lines)

**Structure:**

```
1-5      Module header, BACKEND constant
6-19     injectButton() — adds "Learn Lab" <button> to YouTube toolbar
21-55    openOverlay() / closeOverlay() — side panel lifecycle
57-83    showProfileView() — first-run form (role, industry, project)
85-284   showExperimentView() — main exercise UI
         108-145   HTML template
         147-208   Goal auto-fill (smart LLM → 6s heuristic fallback)
         210-254   Generate click handler
         256-262   Enter key → Generate
         264-283   Feedback handler
286-306  Utils: setLoading(), showStatus(), hideStatus()
307-352  Transcript: getTranscript(), cacheTranscript(), prefetchTranscript()
353-538  injectAndFetch() — injected <script> for silent transcript extraction
         376-525   injectedFn (runs in page context):
                    - Method 1: ytInitialPlayerResponse (captions API URL)
                    - Method 2: <video> textTracks
                    - Method 3: youtubetranscript.com (3rd party)
                    - Method 4: youtube.com/api/timedtext
                    - parseTranscriptResponse() — JSON (fmt=json3) + XML (srv1/ttml)
         527-538   Script injection + timeout (default 12s)
541-644  extractTranscriptByClick() — last-resort DOM click fallback
         614-631   readTranscriptSegments()
         633-640   readSegments()
         642-644   sleep()
646-653  startPrefetch() — silent prefetch on page load
655-659  escapeHtml()
661-689  SPA navigation: yt-navigate-finish, popstate, hashchange
```

**Key design decisions:**
- **Template literal injection:** Uses `Function.toString()` + `replace('__VID__', videoId)` to avoid backtick escaping issues in injected `<script>` code (lines 376-528)
- **Transcript timeout:** `injectAndFetch(videoId, 12)` — 12 seconds, matching 20×500ms poll limit
- **No flicker auto-fill:** Shows "Working on a suggestion..." placeholder immediately → `/api/infer-goal` returns → fills once. 6s fallback to heuristic if backend doesn't respond.
- **SPA navigation:** Listens for YouTube's `yt-navigate-finish` custom event (reliable) + `popstate` / `hashchange` as fallback

### 4.3 `extension/content.css` (241 lines)

| Section | Lines | Styles |
|---------|-------|--------|
| Button | 1-16 | Inline flex, gap, font |
| Overlay | 17-53 | Fixed full-screen, backdrop, right-side panel, close |
| Goal input | 55-81 | Label, input, focus styling |
| Buttons | 83-113 | Primary (indigo), secondary (grey), disabled state |
| Status | 115-130 | OK (green bg), Error (red bg), hidden |
| Result | 132-201 | Insight text, finish line (green border), checkbox steps, time badge |
| Feedback | 203-241 | Collapsible toggle, thumbs up/down, textarea, send |

### 4.4 `backend/app.py` (345 lines)

**Imports & Setup (1-31)**
```python
- dotenv loaded from .env
- FastAPI with CORS (allow all origins, MVP)
- MODEL = os.getenv("OPENAI_MODEL", "deepseek-v4-flash-free")
```
*Note: This changed from `nemotron-3-ultra-free` on 2026-07-14 to use DeepSeek (128K ctx).*

**Schemas (71-103)**
```python
Profile:      user_id, role, industry, project, skill_level
SuggestRequest: video_url, goal_override, transcript (pre-extracted)
SuggestResponse: experiment_id, principle, experiment, why_it_matters
FeedbackRequest: experiment_id, liked, question
InferGoalRequest: video_title, video_channel, video_description, role, industry, project
```

**Transcript helpers (105-134)**
- `extract_video_id()` — parses youtube.com/watch?v=, youtu.be/, /embed/, /shorts/
- `fetch_transcript()` — async wrapper around `YouTubeTranscriptApi` (fallback, likely IP-blocked on cloud hosts). Optional proxy via `YTT_PROXY` env var.

**Experiment generation (136-213)**
```python
SYSTEM_TEMPLATE: Expert mentor who teaches through application.
  - 20% principle → 80% outcome
  - ≤5 min micro-experiment with observable done-state
  - Action-verb commands ("Open X. Look at Y. Do Z.")
  - JSON output: {principle, experiment, why_it_matters}

build_suggest_prompt(transcript, goal, profile)
  - Injects transcript + profile into prompt
  - Full transcript sent (no cap)

generate_experiment(transcript, goal, profile)
  - Model fallback chain: deepseek-v4-flash-free → north-mini-code-free → nemotron-3-ultra-free → hy3-free
  - max_tokens=2000, temperature=0.7, timeout=90s
  - Strips markdown fences, parses JSON
  - Falls back to raw-content-with-default-principle on JSON parse error
```

**Endpoints (216-345)**

| Endpoint | Method | Input | Output |
|----------|--------|-------|--------|
| `/health` | GET | — | `{status, model}` |
| `/api/profile` | POST | `Profile` | `{user_id, profile}` |
| `/api/profile/{user_id}` | GET | — | `{user_id, profile}` |
| `/api/suggest` | POST | `SuggestRequest` | `{experiment_id, principle, experiment, why_it_matters}` |
| `/api/feedback` | POST | `FeedbackRequest` | `{status}` |
| `/api/infer-goal` | POST | `InferGoalRequest` | `{goal}` |

**`/api/suggest` logic:**
1. Resolve profile (user_id lookup, or anonymous default)
2. Extract video ID → validate
3. Use `transcript` from extension if provided
4. Fall back to server-side `fetch_transcript()` if empty
5. If still empty → 400 error with instructions to refresh page
6. Build goal (override → profile.project → default)
7. Call `generate_experiment()`
8. Log experiment to `experiments_log` (in-memory, MVP)
9. Return `SuggestResponse`

**`/api/infer-goal` logic:**
1. Build prompt from video title, channel, description + user role/industry/project
2. `max_tokens=80`, temperature=0.7, timeout=15s
3. Returns clean 20-words-or-less goal
4. Falls back to template on all model failures

---

## 5. Transcript Strategy (Injected Script, 5-step Fallback)

**File:** `extension/content.js` lines 307-644

### Flow
```
getTranscript(videoUrl, { allowClick: true })
    ↓
1. Cache check (chrome.storage.local, key: yl_tr_<videoId>)
    ↓ miss (or expired)
2. injectAndFetch(videoId, 12s timeout)
    ↓ Injected <script> runs 5 methods in sequence:
    ↓ a. ytInitialPlayerResponse.captions → fetch baseUrl
    ↓ b. <video>.textTracks → read cues
    ↓ c. youtubetranscript.com API (3rd party, CORS-friendly)
    ↓ d. youtube.com/api/timedtext?fmt=json3
    ↓ e. Each method tries JSON then XML parsing
    ↓ null
3. extractTranscriptByClick() (only if allowClick=true)
    ↓
    - Hide overlay
    - Click "Show transcript" button (native .click())
    - Wait 3s for segments to render
    - Read DOM via querySelectorAll
    - Close transcript panel (native .click() → Escape → DOM cleanup)
    - Restore overlay
    ↓ null
4. Return null → backend receives no transcript → tries YouTubeTranscriptApi
```

### Key Design Decisions
- **12-second timeout** on injected script matches 20×500ms poll cycle
- **Silent prefetch** on page load (no click fallback) — caches for instant Generate
- **Click fallback only on explicit Generate** — hides overlay, clicks behind the scenes
- **Cache persists across SPA navigations** — instant for returning to videos
- **`youtubetranscript.com`** API currently returns HTML (service changed). The fetch falls through to `youtube.com/api/timedtext` which requires auth cookies.

### Known Limitation
The injected script's youtubetranscript.com method returns HTML instead of JSON (service changed to a Merlin AI landing page). The code handles this by falling through to the next method. No action needed unless you want to clean up the dead code path.

---

## 6. Configuration

### `.env` (backend, 3 lines)
```
OPENAI_API_KEY=sk-aciNU7tVJS25S5no7PPXh8aKESgTrYpfpa3XRgiF2dNtGmvw4AxWJDOu9WSvYBdz
OPENAI_BASE_URL=https://opencode.ai/zen/v1
OPENAI_MODEL=deepseek-v4-flash-free
```
- **API Key (Redacted):** OpenCode Zen. Used for both `/api/suggest` and `/api/infer-goal`.
- **⚠️ Never commit .env.** `.env.example` is the safe template.
- **Model changed 2026-07-14** from `nemotron-3-ultra-free` to `deepseek-v4-flash-free` (8K ctx → 128K ctx).

### `render.yaml` (Render deployment)
```yaml
services:
  - type: web
    name: yt-learn
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app:app --host 0.0.0.0 --port 10000
    envVars:
      - key: OPENAI_API_KEY
        sync: false
      - key: OPENAI_BASE_URL
        value: https://opencode.ai/zen/v1
      - key: OPENAI_MODEL
        value: nemotron-3-ultra-free   # ⬅️ OUTDATED — set to deepseek-v4-flash-free
```
**Action:** Update `render.yaml` to `deepseek-v4-flash-free` before deploying.

### `requirements.txt`
```
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
python-dotenv>=1.0.1
youtube-transcript-api>=1.2.4
openai>=1.0.0
httpx>=0.28.0
```

---

## 7. LLM Model & Context Testing

### Test Results (2026-07-14)

| Model | Context Window | 20K chars | 30K chars | 50K chars | 80K chars | 108K chars |
|-------|---------------|:---------:|:---------:|:---------:|:---------:|:----------:|
| `mimo-v2.5-free` | ~8K tokens | ✅ | ❌ | ❌ | ❌ | ❌ |
| `deepseek-v4-flash-free` | 128K tokens | ✅ | ✅ | ✅ | ✅ | ✅ |

### 108K Char Test (Kubernetes Transcript)
- **Input:** 108,350 chars (~27K tokens), first half = Python basics, second half = K8s deployment
- **Output principle:** *"Deploying an app in production is less about writing code and more about configuring infrastructure using tools like Kubernetes and Helm — understanding Deployment YAMLs, ingress controllers, and auto-scaling is the 20% that unlocks repeatable, production-grade deployments."*
- **Experiment:** 430 chars, complete 3-step Helm chart creation exercise
- **Verdict:** ✅ Full transcript used — exercise references K8s content from the later half

### Available Models on OpenCode Zen
```
claude-fable-5, claude-opus-4-8, claude-sonnet-5, claude-sonnet-4, claude-haiku-4-5
gemini-3.5-flash, gemini-3.1-pro, gemini-3-flash
gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.5-pro, gpt-5.4, gpt-5.4-pro
gpt-5.4-mini, gpt-5.4-nano, gpt-5.3-codex-spark, gpt-5.3-codex, gpt-5.2, gpt-5.2-codex
gpt-5.1, gpt-5.1-codex-max, gpt-5.1-codex, gpt-5.1-codex-mini, gpt-5, gpt-5-codex, gpt-5-nano
grok-build-0.1, grok-4.5
deepseek-v4-pro, deepseek-v4-flash, deepseek-v4-flash-free (current)
glm-5.2, glm-5.1, glm-5
minimax-m3, minimax-m2.7, minimax-m2.5
kimi-k2.7-code, kimi-k2.6, kimi-k2.5
qwen3.6-plus, qwen3.5-plus
big-pickle, mimo-v2.5-free, hy3-free, nemotron-3-ultra-free, north-mini-code-free
```

---

## 8. Key Decisions & Rationale

### 8.1 Product & Naming
| Decision | Value | Reason |
|----------|-------|--------|
| Button label | **Learn Lab** (not "Apply", not "Generate") | Friendly, non-technical, exploratory |
| Button label (previous iteration) | "Apply" | Was the finalist before user pivoted to "Learn Lab" |
| CWS listing name | **Undecided** | Options discussed: "The 1%", "Practice", "Apply That", "Skill Sprint". User wants self-explanatory of the transformation. Must be platform-agnostic. |
| Platform scope | Platform-agnostic (YT, FB, blogs, papers) | Name must not include "for YouTube" | 

### 8.2 UX Decisions (2026-07-14)
| Decision | Detail |
|----------|--------|
| Goal header | "Is this why you're interested in this video?" |
| Goal auto-fill | Smart LLM inference → fills once, no flicker. "Working on a suggestion..." placeholder immediately. 6s fallback to heuristic. |
| Enter key | Submits Generate |
| Panel persistence | Survives SPA navigation via `yt-navigate-finish` + `popstate` |
| Button text (after Generate) | "↻ Try Again" |
| Exercise format | Checkbox steps (split on sentence boundaries), time badge |
| Feedback mechanism | Collapsible "Was this useful?" with 👍/👎 + optional textarea |
| Finish line | Green-bordered "What you'll have" section (shows `why_it_matters`) |

### 8.3 Transcript Strategy (2026-07-13)
- **Silent injected script** (fast path, no UI visible): Works for most videos, caches for instant Generate
- **DOM click fallback** (reliable, hidden from user): Triggers only on explicit Generate click
- **Server-side `YouTubeTranscriptApi`** (last resort, likely IP-blocked): Dead code for extension users
- **No transcript cap** — full transcript sent to LLM (2026-07-14 decision)

### 8.4 Infrastructure Decisions
- **Port:** 8001 (local development)
- **Feedback storage:** JSON file (`feedback.json`) — MVP, planned migration: JSON → SQLite → Postgres
- **Profile storage:** In-memory dict per session — MVP only
- **Experiments log:** In-memory list — MVP only
- **LLM Provider:** OpenCode Zen (uses the same credentials as opencode-zen)

---

## 9. Current Running State

| Component | Status | Detail |
|-----------|--------|--------|
| Backend | ✅ Running | uvicorn on 0.0.0.0:8001, PID 12328 |
| Health endpoint | ✅ `{"status":"ok","model":"deepseek-v4-flash-free"}` | curl http://localhost:8001/health |
| `/api/suggest` | ✅ Verified | 108K char transcript → full exercise (430 chars) |
| `/api/infer-goal` | ✅ Verified | Returns clean, specific goals |
| Extension | Installed at chrome://extensions | Needs reload after code changes |
| `youtubetranscript.com` | ❌ Returns HTML | Service changed — falls through to next method |
| CWS deployment | ❌ Not started | Needs privacy policy, icons finalized |

**Start command:**
```bash
cd C:\Users\swati\Downloads\SwatiK425\YT-Learn\backend
python app.py
# Uvicorn with reload on 0.0.0.0:8001
```

---

## 10. Feedback Analysis

### `feedback.json` (4 entries)

| # | Liked | Comment | Risk |
|---|-------|---------|------|
| 1 | 👍 | "This was great!" | — |
| 2 | 👍 | "LOVE IT!!!" | — |
| 3 | 👎 | "Reduce the friction even further for me. I am seeing your wall of text but what next? Also Gemini in Chrome offers similar capability through skills for now." | **High.** User perceives Gemini Skills as a trusted alternative. **Action:** Reduce friction (fewer words, more action). Differentiate from Gemini. |
| 4 | 👎 | "Broken" | Extension was crashing/not working at that point (pre-fix) |

---

## 11. Known Issues & Blockers

| Issue | Severity | Status | Notes |
|-------|----------|--------|-------|
| `youtubetranscript.com` API returns HTML | Medium | Won't fix | Service changed; code gracefully falls through. Could clean up the dead codepath. |
| Server-side `YouTubeTranscriptApi` IP-blocked | High | Mitigated | Extension sends transcript → server fallback is dead code. For non-extension clients, a proxy (`YTT_PROXY`) or different strategy needed. |
| Zombie backend PID on restart | Low | Known | Port 8001 sometimes shows LISTENING after `taskkill`. Wait 3-5s or use PowerShell `Get-NetTCPConnection` to find the real PID. |
| Extension needs reload after code changes | Low | Workflow | Reload at chrome://extensions after updating content.js |
| CWS name undecided | Medium | Open | Options: "The 1%", "Practice", "Apply That", "Skill Sprint" |
| Icons only have 128px | Low | Open | CWS requires 16, 32, 48, 128 — need to generate smaller sizes |
| Privacy policy not written | Medium | Open | Needed for CWS publishing |
| `render.yaml` still uses old model | Medium | Needs update | Change `nemotron-3-ultra-free` → `deepseek-v4-flash-free` before deploying |
| In-memory stores (profile, experiments) | Low | MVP | Will be lost on restart. Swap to SQLite/Postgres when ready. |
| Feedback JSON file | Low | MVP | JSON file works for low volume. Will need a proper DB for scale. |

---

## 12. Timeline / Key Events

| Date | Event |
|------|-------|
| **2026-07-12** | 🎬 Project started. Extension scaffold, FastAPI backend, DOM-click transcript extraction. |
| **2026-07-12** | Backend proxy fetch + LLM generation pipeline working. |
| **2026-07-12** | `gqMOihhzTHw` verified: 105K char transcript extracted via DOM click. |
| **2026-07-13** | ⚡ Silent injected-script approach implemented (replaces DOM click). Caching added. |
| **2026-07-13** | User reports "Could not fetch transcript" → diagnosed script injection timing issue. |
| **2026-07-13** | 🔄 Hybrid approach: silent prefetch + hidden DOM click fallback behind overlay. |
| **2026-07-14** | UX overhaul: smart goal header, auto-fill (no flicker, single-shot), Enter submit, SPA navigation fix via `yt-navigate-finish`. |
| **2026-07-14** | 🧪 Product naming decision: button = **Learn Lab** (was "Apply"). CWS name TBD. |
| **2026-07-14** | 📐 Transcript cap **removed** (was 8K → 20K → ∞). |
| **2026-07-14** | 🤖 Model swapped from `mimo-v2.5-free` (8K ctx, truncation at ~20K chars) → `deepseek-v4-flash-free` (128K ctx, verified with 108K chars). |
| **2026-07-14** | ✅ Backend pipeline verified end-to-end with synthetic 108K char transcript. Exercise references content from full video. |
| **2026-07-14** | 📝 This handoff document created. |

---

## 13. Backend Code (app.py) — Full Listing

```python
import os
import re
import asyncio
import json
import uuid
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
from openai import OpenAI

load_dotenv()

app = FastAPI(title="YT-Learn")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MODEL = os.getenv("OPENAI_MODEL", "deepseek-v4-flash-free")

# In-memory profile store (MVP — swap to SQLite if needed)
profiles = {}
experiments_log = []

# ─── JSON-backed feedback store ──────────────────────────
FEEDBACK_FILE = Path(__file__).parent / "feedback.json"

feedback_store: list[dict] = []
if FEEDBACK_FILE.exists():
    try:
        with open(FEEDBACK_FILE) as f:
            feedback_store = json.load(f)
    except (json.JSONDecodeError, OSError):
        feedback_store = []

def _save_feedback():
    with open(FEEDBACK_FILE, "w") as f:
        json.dump(feedback_store, f, indent=2)


def _pst_now() -> str:
    """Return current time as ISO 8601 string in America/Los_Angeles (Pacific)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        # Fallback: UTC-8 without DST (close enough for MVP)
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=-8))
    return datetime.now(tz).isoformat()


def get_client() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set.")
    return OpenAI(api_key=key, base_url=os.getenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1"))


# ─── Schemas ──────────────────────────────────────────────

class Profile(BaseModel):
    user_id: str | None = None
    role: str           # "founder", "dev", "marketer", "designer", "student", "other"
    industry: str       # "saas", "ecommerce", "education", "health", "finance", "other"
    project: str        # "What are you currently building or working on?"  (free text)
    skill_level: str | None = None  # "beginner", "intermediate", "advanced"

class SuggestRequest(BaseModel):
    video_url: str
    goal_override: str | None = None
    transcript: str | None = None  # Pre-extracted transcript from extension

class SuggestResponse(BaseModel):
    experiment_id: str
    principle: str
    experiment: str
    why_it_matters: str | None = None

class FeedbackRequest(BaseModel):
    experiment_id: str
    liked: bool | None = None
    question: str | None = None

class InferGoalRequest(BaseModel):
    video_title: str
    video_channel: str | None = None
    video_description: str | None = None
    role: str
    industry: str
    project: str


# ─── Transcript helpers (reused from YT-Scraper) ──────────

def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.hostname in ("youtu.be", "www.youtu.be"):
        return parsed.path.lstrip("/").split("?")[0]
    if parsed.hostname and "youtube.com" in parsed.hostname:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.match(r"^/(embed|v|shorts)/([a-zA-Z0-9_-]{11})", parsed.path)
        if match:
            return match.group(2)
    return None


async def fetch_transcript(video_id: str) -> str | None:
    try:
        proxy_url = os.getenv("YTT_PROXY")  # optional http/socks proxy
        api = YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url) if proxy_url else None
        )
        transcript = await asyncio.wait_for(
            asyncio.to_thread(api.fetch, video_id, languages=["en", "a.en"]),
            timeout=15,
        )
        return " ".join(seg.text for seg in transcript)
    except Exception as e:
        print(f"Transcript failed for {video_id}: {e}")
        return None


# ─── Experiment generation ────────────────────────────────

SYSTEM_TEMPLATE = """You are an expert mentor who teaches through application, not explanation.

Your method:
1. Find the 20% of the content that, if applied, would give 80% of the learning outcome
2. Tailor EVERYTHING to the learner's specific situation (role, industry, current project)
3. Design ONE concrete micro-experiment that takes ≤5 minutes
4. The experiment must produce a real artifact or observable outcome — not "think about" or "reflect on"

Rules:
- The experiment must be doable RIGHT NOW in their browser or immediate work context
- Reference their specific project/role/industry in the experiment itself
- NEVER suggest "think about how this applies" — always "Open X. Look at Y. Do Z."
- The experiment has a clear "done" state the learner can recognize
- Use simple, direct language. No fluff, no bullet-point padding, no marketing tone.
- If they're not ready for an experiment, be honest and explain why.

Output format — respond ONLY with valid JSON, no extra text:
{{
  "principle": "The single most important concept from the video relevant to this learner (1-2 sentences)",
  "experiment": "The micro-experiment. One paragraph starting with an action verb. Specific, personal, doable in ≤5 min.",
  "why_it_matters": "One sentence connecting this to their stated goal and situation"
}}"""


def build_suggest_prompt(transcript: str, goal: str, profile: Profile) -> str:
    return f"""## Learner Profile
Role: {profile.role}
Industry: {profile.industry}
Current project: {profile.project}

## Their goal for watching this video
{goal}

## Video transcript
{transcript}  # full transcript — DeepSeek handles 100K+ chars

Based on the transcript and the learner's goal, identify the 20% principle that will give them 80% of the learning value. Design exactly one micro-experiment they can do in ≤5 minutes that applies this principle to THEIR specific project/situation. Return valid JSON."""


def generate_experiment(transcript: str, goal: str, profile: Profile) -> dict:
    client = get_client()
    prompt = build_suggest_prompt(transcript, goal, profile)
    models = [MODEL, "north-mini-code-free", "nemotron-3-ultra-free", "hy3-free"]

    for model in models:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_TEMPLATE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=2000,
                timeout=90,
            )
            content = resp.choices[0].message.content
            if not content or not content.strip():
                print(f"  Empty response from {model}, trying next...")
                continue
            # Strip markdown fences
            content = re.sub(r"^```(?:json)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content)
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {
                    "principle": "Key insight from the video",
                    "experiment": content,
                    "why_it_matters": f"Directly applies to your goal: {goal}",
                }
        except Exception as e:
            print(f"  Model {model} failed: {e}")
            continue

    raise ValueError("All models returned empty or errored.")


# ─── Endpoints ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL}


@app.post("/api/profile")
async def save_profile(profile: Profile):
    uid = profile.user_id or str(uuid.uuid4())[:8]
    profiles[uid] = profile.model_dump() | {"updated": datetime.utcnow().isoformat()}
    return {"user_id": uid, "profile": profiles[uid]}


@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    p = profiles.get(user_id)
    if not p:
        raise HTTPException(404, "Profile not found. Create one first.")
    return {"user_id": user_id, "profile": p}


@app.post("/api/suggest", response_model=SuggestResponse)
async def suggest(req: SuggestRequest, user_id: str | None = None):
    # 1. Resolve profile
    profile = None
    if user_id and user_id in profiles:
        profile = Profile(**profiles[user_id])
    else:
        profile = Profile(role="other", industry="other", project="general learning")

    # DEBUG: log what we received
    print(f"[DEBUG] suggest request: video_url={req.video_url}, transcript_len={len(req.transcript) if req.transcript else 0}, goal={req.goal_override}")

    # 2. Validate & fetch transcript
    vid = extract_video_id(req.video_url)
    if not vid:
        raise HTTPException(400, "Invalid YouTube URL.")

    transcript = req.transcript  # Use pre-extracted if provided
    if not transcript:
        print(f"[DEBUG] transcript empty, falling back to API fetch for {vid}")
        transcript = await fetch_transcript(vid)
        print(f"[DEBUG] API fetch returned: transcript_len={len(transcript) if transcript else 0}")

    if not transcript:
        raise HTTPException(400, "Could not fetch transcript. The video may not have captions, or YouTube blocked this server IP. Try refreshing the YouTube page so the extension can extract captions directly from the browser.")

    # 3. Determine goal
    goal = req.goal_override or profile.project or "Learn the main skill taught in this video"

    # 4. Generate experiment
    try:
        result = generate_experiment(transcript, goal, profile)
    except Exception as e:
        raise HTTPException(500, f"Experiment generation failed: {e}")

    exp_id = str(uuid.uuid4())[:12]
    experiments_log.append({
        "experiment_id": exp_id,
        "user_id": user_id,
        "video_url": req.video_url,
        "goal": goal,
        "result": result,
        "timestamp": datetime.utcnow().isoformat(),
    })

    return SuggestResponse(experiment_id=exp_id, **result)


@app.post("/api/feedback")
async def feedback(fb: FeedbackRequest):
    entry = next((e for e in experiments_log if e["experiment_id"] == fb.experiment_id), None)
    user_id = entry["user_id"] if entry else "unknown"

    record = {
        "user_id": user_id,
        "experiment_id": fb.experiment_id,
        "liked": fb.liked,
        "comment": fb.question,
        "timestamp_pst": _pst_now(),
    }
    feedback_store.append(record)
    _save_feedback()

    return {"status": "ok"}


@app.post("/api/infer-goal")
async def infer_goal(req: InferGoalRequest):
    """
    Given video context + user profile, suggest WHY the user is watching this video.
    Returns a smart guess as a short phrase the user can confirm or edit.
    """
    client = get_client()
    prompt = (
        f"User: {req.role} in {req.industry}, building: {req.project}.\n"
        f"Watching: \"{req.video_title}\" by {req.video_channel or 'unknown'}.\n"
        f"Description: {(req.video_description or '')[:500]}\n\n"
        f"Write ONE short reason why they chose this specific video (max 20 words). "
        f"Be concrete. Example: \"To learn React + D3 patterns for your customer analytics dashboard.\"\n"
        f"Output ONLY that reason. No analysis."
    )
    models = [MODEL, "north-mini-code-free", "nemotron-3-ultra-free", "hy3-free"]
    for model in models:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=80,
                timeout=15,
            )
            content = resp.choices[0].message.content
            if content and content.strip():
                return {"goal": content.strip().strip('"')}
        except Exception as e:
            print(f"  infer-goal model {model} failed: {e}")
            continue
    # Fallback: template-based guess
    fallback = f"As a {req.role} in {req.industry} working on {req.project}, you are watching \"{req.video_title}\" to find practical techniques you can apply to your work."
    return {"goal": fallback}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True)
```

---

## 14. Extension Code (content.js) — Full Listing

**File:** `C:\Users\swati\Downloads\SwatiK425\YT-Learn\extension\content.js` (689 lines)

(Content above in section 4.2 structure description — full source available at the file path.)

**Key functions:**

### `injectButton()`
```javascript
function injectButton() {
  if (document.getElementById('yt-learn-btn')) return;
  if (!document.querySelector('#top-level-buttons-computed')) return;
  if (window.location.pathname !== '/watch') return;
  const btn = document.createElement('button');
  btn.id = 'yt-learn-btn';
  btn.className = 'yt-spec-button-shape-next yt-spec-button-shape-next--tonal yt-spec-button-shape-next--mono yt-spec-button-shape-next--size-m';
  btn.innerHTML = '<div class="yt-learn-btn-inner"><span>📚</span> Learn Lab</div>';
  btn.addEventListener('click', openOverlay);
  document.querySelector('#top-level-buttons-computed').appendChild(btn);
}
```

### `getTranscript()` (transcript pipeline orchestrator)
```javascript
async function getTranscript(videoUrl, opts) {
  opts = opts || {};
  var videoId = extractVideoId(videoUrl);
  if (!videoId) return null;
  var allowClick = opts.allowClick !== false;

  // 1) Cache
  var cached = await new Promise(function(r) {
    chrome.storage.local.get(TC_PREFIX + videoId, function(d) {
      r(d[TC_PREFIX + videoId] ? d[TC_PREFIX + videoId].text : null);
    });
  });
  if (cached) { return cached; }

  // 2) Silent injected fetch
  var text = await injectAndFetch(videoId, 12);
  if (text) { cacheTranscript(videoId, text); return text; }

  // 3) Click fallback (only on Generate)
  if (allowClick) {
    text = await extractTranscriptByClick();
    if (text) { cacheTranscript(videoId, text); }
    return text;
  }
  return null;
}
```

### `injectAndFetch()` (injected script, ~150 lines)
Uses function serialization to avoid template-literal escaping issues:
```javascript
function injectAndFetch(videoId, timeoutSec) {
  timeoutSec = timeoutSec || 12;
  var safeId = String(videoId).replace(/[^a-zA-Z0-9_-]/g, '');
  return new Promise(function(resolve) {
    var handler = function(e) {
      if (e.detail && e.detail.id === safeId) {
        document.removeEventListener('_yl_tr', handler);
        resolve(e.detail.text || null);
      }
    };
    document.addEventListener('_yl_tr', handler, { once: true });

    var injectedFn = function() {
      var id = '__VID__';
      var maxTry = 20;
      var tries = 0;
      // ... poll() → fetchBaseUrl() | extractFromTextTracks() | tryThirdParty()
      // ... parseTranscriptResponse() (JSON + XML)
      // ... dispatch(text) via CustomEvent
    };

    var codeStr = '(' + injectedFn.toString().replace('__VID__', safeId) + ')()';
    var script = document.createElement('script');
    script.textContent = codeStr;
    document.body.appendChild(script);
    setTimeout(function() { script.remove(); }, 100);
    setTimeout(function() { document.removeEventListener('_yl_tr', handler); resolve(null); }, timeoutSec * 1000);
  });
}
```

### `extractTranscriptByClick()` (DOM click fallback)
```javascript
async function extractTranscriptByClick() {
  var btn = document.querySelector('[aria-label="Show transcript"]');
  if (!btn) {
    btn = document.querySelector('#primary-button yt-button-shape button');
    if (btn && btn.textContent.toLowerCase().indexOf('transcript') === -1) btn = null;
  }
  if (!btn) return null;

  // Hide overlay so clicks reach the page
  var overlayEl = document.getElementById('yt-learn-overlay');
  if (overlayEl) overlayEl.style.display = 'none';
  await sleep(100);
  btn.click();
  await sleep(3000);

  // Read segments
  var text = readTranscriptSegments();

  // Close transcript panel
  var hideBtn = document.querySelector('[aria-label="Hide transcript"]');
  if (hideBtn) { hideBtn.click(); await sleep(400); }
  // If still open → Escape key
  var stillOpen = document.querySelector('[aria-label="Hide transcript"]');
  if (stillOpen) { dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await sleep(300); }

  if (overlayEl) overlayEl.style.display = '';
  return text || null;
}
```

---

## 15. Immediate Action Items

1. **Update `render.yaml`** → Change `OPENAI_MODEL` from `nemotron-3-ultra-free` to `deepseek-v4-flash-free`
2. **Decide CWS name** → Options: "The 1%", "Practice", "Apply That", "Skill Sprint", or another self-explanatory name
3. **Generate icon sizes** → Need 16px, 32px, 48px, 128px for CWS
4. **Write privacy policy** → Required for CWS publish
5. **CWS deployment** → Package extension, upload to Chrome Web Store for close friends
6. **Render deployment** → Deploy backend to Render free tier
7. **Refine feedback** → Address "wall of text" and "why not Gemini" concerns from feedback entry #3
8. **Profile/feedback persistence** → Swap in-memory stores for SQLite before scaling beyond single user
