import os
import re
import asyncio
import json
import uuid
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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

# ─── Config ───────────────────────────────────────────────
# ONE strong model. Quality of the exercise IS the product — don't run it on
# free-tier models. Any OpenAI-compatible endpoint works; Google AI Studio,
# Anthropic API (via SDK compat layer), or OpenRouter all work.
#
# To switch providers, set OPENAI_BASE_URL + OPENAI_API_KEY + MODEL in .env.
#   Google AI:   OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/
#   Anthropic:   OPENAI_BASE_URL=https://api.anthropic.com/v1/
#   OpenRouter:  OPENAI_BASE_URL=https://openrouter.ai/api/v1
#   OpenAI:      OPENAI_BASE_URL=https://api.openai.com/v1
MODEL = os.getenv("MODEL", "gemini-2.5-flash")
FAST_MODEL = os.getenv("FAST_MODEL", MODEL)  # for infer-goal + long-video notes
API_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/")

CACHE_TTL = 86400  # 24 hours
GEN_TIMEOUT = 60   # fail fast; a hung call should never cost minutes
# ~45k tokens. Covers roughly a 3-hour video. Above this we compress first.
MAX_SINGLE_PASS_CHARS = 180_000

# Provider presets: name → OpenAI-compatible base URL. The extension sends a
# provider + model + api_key per request (BYOK); env vars are the fallback
# when no per-request config is given.
PROVIDER_BASE_URLS = {
    "google": "https://generativelanguage.googleapis.com/v1beta/",
    "anthropic": "https://api.anthropic.com/v1/",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "opencode-zen": "https://opencode.ai/zen/v1",
}

_client: OpenAI | None = None

def get_default_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    return _client


class LLMConfig(BaseModel):
    """Per-request model routing. api_key never touches logs or storage."""
    provider: str = ""          # google | anthropic | openai | openrouter | custom
    base_url: str = ""          # required only for provider == custom
    model: str = ""
    fast_model: str = ""        # optional; falls back to model
    api_key: str = ""


class ResolvedLLM:
    def __init__(self, client: OpenAI, model: str, fast_model: str):
        self.client = client
        self.model = model
        self.fast_model = fast_model

def resolve_llm(cfg: LLMConfig | None) -> ResolvedLLM:
    """Turn optional per-request config into (client, model, fast_model).
    Falls back to server env defaults when config is absent/incomplete."""
    if cfg and cfg.api_key and cfg.model:
        base = cfg.base_url or PROVIDER_BASE_URLS.get(cfg.provider, "")
        if not base:
            raise HTTPException(400, "Unknown provider and no base_url given.")
        client = OpenAI(api_key=cfg.api_key, base_url=base)
        return ResolvedLLM(client, cfg.model, cfg.fast_model or cfg.model)
    if not API_KEY:
        raise HTTPException(400, "No API key configured. Add one in the extension's model settings.")
    return ResolvedLLM(get_default_client(), MODEL, FAST_MODEL)

# ─── In-memory stores ─────────────────────────────────────
profiles: dict[str, dict] = {}
experiments_log: list[dict] = []
feedback_store: list[dict] = []
exercise_cache: dict[str, dict] = {}
# Cache the prepared (compressed) transcript per video so retries don't
# re-fetch or re-compress. Keyed by video ID.
prepared_transcripts: dict[str, str] = {}

# ─── Schemas ──────────────────────────────────────────────

class Profile(BaseModel):
    user_id: str = ""
    role: str = "other"
    goal: str = ""
    signals: dict = {}

class SuggestRequest(BaseModel):
    video_url: str
    transcript: str | None = None
    goal_override: str | None = None
    # Passed directly in the request instead of via a racing side-channel
    # signal POST — the regeneration is guaranteed to see it.
    retry_reason: str | None = None   # too_easy | too_hard | wrong_topic
    force: bool = False               # bypass cache (Try Again)
    llm: LLMConfig | None = None      # BYOK model routing

class SuggestResponse(BaseModel):
    experiment_id: str
    principle: str = ""  # backward compat: maps from top_concept or eighty_twenty
    experiment: str
    why_it_matters: str = ""
    candidates: list[dict] = []  # backward compat: maps from concepts
    video_class: dict = {"type": "other", "domain": ""}
    top_concept: str = ""
    creator_thesis: str = ""
    eighty_twenty: str = ""
    concepts: list[dict] = []
    cached: bool = False

class FeedbackRequest(BaseModel):
    experiment_id: str
    liked: bool | None = None
    question: str = ""

class SignalRequest(BaseModel):
    user_id: str
    signal_type: str
    value: str
    experiment_id: str = ""

class InferGoalRequest(BaseModel):
    role: str = ""
    goal: str = ""
    video_title: str = ""
    video_channel: str = ""
    video_description: str = ""
    llm: LLMConfig | None = None

# ─── Helpers ──────────────────────────────────────────────

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
        proxy_url = os.getenv("YTT_PROXY")
        api = YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url) if proxy_url else None
        )
        segments = await asyncio.wait_for(
            asyncio.to_thread(api.fetch, video_id, languages=["en", "a.en"]),
            timeout=15,
        )
        return " ".join(seg.text for seg in segments)
    except Exception as e:
        print(f"Transcript failed for {video_id}: {e}")
        return None



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_json_lenient(content: str) -> dict | None:
    """Strip code fences and parse. Returns None on failure — never fabricate."""
    content = re.sub(r"^```(?:json)?\s*", "", content.strip())
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Salvage: grab the outermost JSON object if the model added prose.
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None

# ─── Prompts ──────────────────────────────────────────────

SYSTEM_TEMPLATE = """You are a master tutor who teaches through application, not explanation.

Your job: identify the single highest-leverage concept from the video transcript below and force the learner to apply it right now. Do NOT "generate an exercise" — identify what matters most and make them use it.

Work through these steps IN ORDER. Keep your reasoning internal — output only the final JSON.

## Steps

1. **Classify** — What type of content is this? (tutorial, case study, theoretical talk, debate, documentary, review, or other) What field or domain?

2. **Extract all concepts** — List every distinct concept, technique, system, example, framework, method, or principle mentioned in the transcript. Scan beginning, middle, end. Be exhaustive — don't skip anything.

3. **Rank by leverage** — Rank concepts from highest intellectual leverage to lowest. The highest-leverage concept is the one that, if understood and applied, produces the most outsized result.

4. **Creator's thesis** — What is the single main argument or point the creator is making? What do they want the viewer to understand or do?

5. **80/20 insight** — What is the 20% of this video that yields 80% of the value? The core insight worth extracting from this video.

6. **Design exercise** — Design ONE concrete exercise that forces the learner to apply the 80/20 insight immediately. Rules:
   - Takes ≤5 minutes
   - Produces a real artifact or observable outcome (never "think about" or "reflect on")
   - Doable right now in the learner's browser or immediate work context
   - Has a clear "done" state the learner can recognize
   - Write as 3-5 short numbered steps, each starting with an action verb
   - Simple, direct language. No fluff, no marketing tone.

7. **Adjust difficulty** — (Applied automatically from the Difficulty note in the prompt below.)

Output format — respond ONLY with valid JSON, no extra text:
{
  "video_class": {
    "type": "tutorial|case_study|theoretical|talk|debate|documentary|review|other",
    "domain": "the specific field or domain"
  },
  "concepts": [
    {"name": "concept name", "where": "early|middle|late"},
    ...
  ],
  "top_concept": "The single highest-leverage concept (1-2 sentences)",
  "creator_thesis": "The creator's main argument (1-2 sentences)",
  "eighty_twenty": "The 80/20 insight (1-2 sentences)",
  "experiment": "Numbered steps, newline-separated",
  "why_it_matters": "One sentence connecting this to real-world application"
}"""


def build_prompt(transcript: str, retry_reason: str | None) -> str:
    retry_note = ""
    if retry_reason == "too_easy":
        retry_note = "\n[DIFFICULTY] Their last exercise was TOO EASY. Make this one significantly more demanding.\n"
    elif retry_reason == "too_hard":
        retry_note = "\n[DIFFICULTY] Their last exercise was TOO HARD. Simplify and break it down further.\n"
    elif retry_reason == "wrong_topic":
        retry_note = "\n[DIFFICULTY] Their last exercise targeted the WRONG TOPIC. Pick a different concept.\n"

    return (
        f"## Full Transcript\n{transcript}\n\n"
        f"{retry_note}"
        f"Work through the steps. Return ONLY the JSON object."
    )


# ─── Long-video compression (rare path, >~3h of video) ────

async def compress_transcript(transcript: str, llm: ResolvedLLM) -> str:
    """Parallel dense notes over large windows, preserving order. Only used
    when the transcript exceeds single-pass budget."""
    window = 40_000
    windows = [transcript[i:i + window] for i in range(0, len(transcript), window)]
    sem = asyncio.Semaphore(8)

    async def notes(idx: int, text: str) -> tuple[int, str]:
        prompt = (
            "Write dense notes on this video-transcript section: every distinct concept, "
            "technique, example, and concrete detail, in order. No commentary. Max 400 words.\n\n"
            + text
        )
        async with sem:
            try:
                resp = await asyncio.to_thread(
                    llm.client.chat.completions.create,
                    model=llm.fast_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2, max_tokens=700, timeout=30,
                )
                return idx, (resp.choices[0].message.content or "")
            except Exception as e:
                print(f"  notes window {idx} failed: {e}")
                return idx, ""

    results = await asyncio.gather(*[notes(i, w) for i, w in enumerate(windows)])
    results.sort(key=lambda r: r[0])
    frac = 100 // max(len(windows), 1)
    parts = [f"[Section {i + 1} of {len(windows)}, ~{i * frac}-{(i + 1) * frac}% through the video]\n{txt}"
             for i, txt in results if txt]
    return "\n\n".join(parts)


# ─── Core generation ──────────────────────────────────────

async def prepare_transcript(transcript: str, llm: ResolvedLLM) -> str:
    if len(transcript) > MAX_SINGLE_PASS_CHARS:
        print(f"[COMPRESS] {len(transcript)} chars → notes")
        return await compress_transcript(transcript, llm)
    return transcript

def _validate(parsed: dict) -> dict:
    if not parsed.get("experiment"):
        raise ValueError("Model output missing required field: experiment")
    parsed.setdefault("video_class", {"type": "other", "domain": ""})
    parsed.setdefault("concepts", [])
    parsed.setdefault("top_concept", "")
    parsed.setdefault("creator_thesis", "")
    parsed.setdefault("eighty_twenty", "")
    parsed.setdefault("why_it_matters", "")
    # Backward compat: map new fields to old field names the extension expects
    parsed.setdefault("principle", parsed["top_concept"] or parsed["eighty_twenty"])
    parsed.setdefault("candidates", parsed["concepts"])
    return parsed

async def generate_experiment(transcript: str,
                              retry_reason: str | None, llm: ResolvedLLM) -> dict:
    """Single call, full transcript, one retry on malformed JSON. Never
    fabricates a fallback exercise — bad output fails loudly."""
    prompt = build_prompt(transcript, retry_reason)
    messages = [
        {"role": "system", "content": SYSTEM_TEMPLATE},
        {"role": "user", "content": prompt},
    ]
    last_err = None
    for attempt in range(2):
        try:
            resp = await asyncio.to_thread(
                llm.client.chat.completions.create,
                model=llm.model, messages=messages,
                temperature=0.7 if attempt == 0 else 0.4,
                max_tokens=4096, timeout=GEN_TIMEOUT,
            )
            content = resp.choices[0].message.content or ""
            parsed = parse_json_lenient(content)
            if parsed:
                return _validate(parsed)
            last_err = ValueError(f"Unparseable model output ({len(content)} chars)")
            print(f"  attempt {attempt}: unparseable, retrying")
        except Exception as e:
            last_err = e
            print(f"  attempt {attempt} failed: {e}")
    raise last_err or ValueError("Generation failed")




def _log_experiment(exp_id: str, user_id: str | None, vid: str, result: dict):
    experiments_log.append({
        "experiment_id": exp_id,
        "user_id": user_id or "anon",
        "video_id": vid,
        "principle": result.get("principle", ""),
        "candidates": result.get("candidates", []),
        "timestamp": _utc_now(),
    })


# ─── Endpoint: Profile ────────────────────────────────────

@app.post("/api/profile")
async def save_profile(profile: Profile):
    uid = profile.user_id or str(uuid.uuid4())[:8]
    profiles[uid] = profile.model_dump() | {"updated": _utc_now()}
    return {"user_id": uid, "profile": profiles[uid]}

@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    p = profiles.get(user_id)
    if not p:
        raise HTTPException(404, "Profile not found.")
    return {"user_id": user_id, "profile": p}


# ─── Endpoint: Suggest (JSON) ─────────────────────────────

@app.post("/api/suggest")
async def suggest(req: SuggestRequest, user_id: str | None = None):
    llm = resolve_llm(req.llm)
    vid = extract_video_id(req.video_url)
    if not vid:
        raise HTTPException(400, "Invalid YouTube URL.")

    transcript = req.transcript or await fetch_transcript(vid)
    if not transcript:
        raise HTTPException(400, "Could not fetch transcript.")

    cache_key = f"{user_id or 'anon'}:{vid}:{llm.model}"
    now_ts = datetime.now(timezone.utc).timestamp()

    if not req.force and cache_key in exercise_cache:
        entry = exercise_cache[cache_key]
        if now_ts - entry["ts"] < CACHE_TTL:
            return SuggestResponse(**entry["result"], cached=True)

    prepared = (
        prepared_transcripts.get(vid) or await prepare_transcript(transcript, llm)
    )
    if vid not in prepared_transcripts:
        prepared_transcripts[vid] = prepared

    try:
        result = await generate_experiment(prepared, req.retry_reason, llm)
    except Exception as e:
        raise HTTPException(502, f"Experiment generation failed: {e}")

    exp_id = str(uuid.uuid4())[:12]
    response_data = {"experiment_id": exp_id, **result}
    exercise_cache[cache_key] = {"result": response_data, "ts": now_ts}
    _log_experiment(exp_id, user_id, vid, result)
    return SuggestResponse(**response_data)


# ─── Endpoint: Suggest (Streaming SSE) ────────────────────

@app.post("/api/suggest/stream")
async def suggest_stream(req: SuggestRequest, user_id: str | None = None):
    """Events:
      skeleton — emitted IMMEDIATELY, before any model work
      status   — progress notes (e.g. long-video compression)
      raw      — incremental model tokens (client may show live preview)
      done     — server-parsed final result: experiment_id + all fields
      error    — message
    """
    vid = extract_video_id(req.video_url)

    async def gen():
        # 1. Skeleton first — the user sees structure within milliseconds.
        yield "event: skeleton\ndata: {}\n\n"

        try:
            llm = resolve_llm(req.llm)
        except HTTPException as he:
            yield f"event: error\ndata: {json.dumps({'message': he.detail})}\n\n"
            return

        if not vid:
            yield f"event: error\ndata: {json.dumps({'message': 'Invalid YouTube URL'})}\n\n"
            return

        transcript = req.transcript or await fetch_transcript(vid)
        if not transcript:
            yield f"event: error\ndata: {json.dumps({'message': 'Could not fetch transcript'})}\n\n"
            return

        cache_key = f"{user_id or 'anon'}:{vid}:{llm.model}"
        now_ts = datetime.now(timezone.utc).timestamp()

        # 2. Cache hit → instant done.
        if not req.force and cache_key in exercise_cache:
            entry = exercise_cache[cache_key]
            if now_ts - entry["ts"] < CACHE_TTL:
                payload = {**entry["result"], "cached": True}
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                return

        try:
            # Re-use prepared (compressed) transcript from a prior request
            if vid in prepared_transcripts:
                prepared = prepared_transcripts[vid]
            else:
                if len(transcript) > MAX_SINGLE_PASS_CHARS:
                    yield f"event: status\ndata: {json.dumps({'message': 'Long video — reading it in sections...'})}\n\n"
                prepared = await prepare_transcript(transcript, llm)
                prepared_transcripts[vid] = prepared

            prompt = build_prompt(prepared, req.retry_reason)

            # 3. Stream the single generation call.
            resp = await asyncio.to_thread(
                llm.client.chat.completions.create,
                model=llm.model,
                messages=[
                    {"role": "system", "content": SYSTEM_TEMPLATE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7, max_tokens=4096, timeout=GEN_TIMEOUT,
                stream=True,
            )

            buf = ""
            for chunk in resp:
                delta = chunk.choices[0].delta if chunk.choices else None
                content = (delta.content or "") if delta else ""
                if content:
                    buf += content
                    yield f"event: raw\ndata: {json.dumps({'text': content})}\n\n"

            # 4. Server parses & validates — the client never has to regex JSON.
            parsed = parse_json_lenient(buf)
            if not parsed:
                yield f"event: error\ndata: {json.dumps({'message': 'Model returned malformed output. Try again.'})}\n\n"
                return
            result = _validate(parsed)

            exp_id = str(uuid.uuid4())[:12]
            response_data = {"experiment_id": exp_id, **result}
            exercise_cache[cache_key] = {"result": response_data, "ts": now_ts}
            _log_experiment(exp_id, user_id, vid, result)

            yield f"event: done\ndata: {json.dumps(response_data)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ─── Endpoint: Feedback ───────────────────────────────────

@app.post("/api/feedback")
async def feedback(fb: FeedbackRequest):
    entry = next((e for e in experiments_log if e["experiment_id"] == fb.experiment_id), None)
    feedback_store.append({
        "user_id": entry["user_id"] if entry else "unknown",
        "experiment_id": fb.experiment_id,
        "liked": fb.liked,
        "comment": fb.question,
        "timestamp": _utc_now(),
    })
    return {"status": "ok"}


# ─── Endpoint: Signal ─────────────────────────────────────

@app.post("/api/signal")
async def signal(sig: SignalRequest):
    uid = sig.user_id
    if uid not in profiles:
        profiles[uid] = Profile(user_id=uid, role="other", goal="").model_dump()
    p = profiles[uid]
    s = p.get("signals") if isinstance(p.get("signals"), dict) else {}

    st, val = sig.signal_type, sig.value
    if st == "try_again":
        s["try_again_count"] = s.get("try_again_count", 0) + 1
    elif st == "completed":
        s["completed_count"] = s.get("completed_count", 0) + 1
    elif st == "difficulty":
        s[f"{val}_count"] = s.get(f"{val}_count", 0) + 1
    elif st == "retry_reason":
        # Analytics only — the regeneration itself receives retry_reason in
        # its own request body, so there is no race.
        s["try_again_count"] = s.get("try_again_count", 0) + 1
        s[f"retry_{val}_count"] = s.get(f"retry_{val}_count", 0) + 1
    else:
        return {"status": "ignored", "reason": f"unknown signal_type: {st}"}

    p["signals"] = s
    p["updated"] = _utc_now()
    profiles[uid] = p
    return {"status": "ok", "signals": s}


# ─── Endpoint: Infer Goal ─────────────────────────────────

def _template_goal(req: InferGoalRequest) -> str:
    title = req.video_title or "this video"
    if req.goal:
        return f"To pick up practical techniques from \"{title}\" for {req.goal}."
    return f"To learn the main skill taught in \"{title}\"."

@app.post("/api/infer-goal")
async def infer_goal(req: InferGoalRequest):
    """One fast model, tight timeout, instant template fallback. This endpoint
    must never be the reason the UI feels slow."""
    try:
        llm = resolve_llm(req.llm)
    except HTTPException:
        return {"goal": _template_goal(req)}  # no key yet → template, never an error
    prompt = (
        f"User: {req.role}. Growth goal: {req.goal}.\n"
        f"Watching: \"{req.video_title}\" by {req.video_channel or 'unknown'}.\n"
        f"Description: {(req.video_description or '')[:500]}\n\n"
        f"Write ONE short reason why they chose this specific video (max 20 words). "
        f"Be concrete. Output ONLY that reason."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                llm.client.chat.completions.create,
                model=llm.fast_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=60, timeout=6,
            ),
            timeout=7,
        )
        content = (resp.choices[0].message.content or "").strip().strip('"')
        if content:
            return {"goal": content}
    except Exception as e:
        print(f"  infer-goal fast path failed: {e}")
    return {"goal": _template_goal(req)}


# ─── Health ───────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "default_model": MODEL,
        "default_fast_model": FAST_MODEL,
        "byok": "clients may send llm config per request",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8002, reload=True)
