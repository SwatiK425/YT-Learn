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
    BYOK disabled — always uses server env defaults."""
    # BYOK commented out: always use server .env defaults
    # if cfg and cfg.api_key and cfg.model:
    #     base = cfg.base_url or PROVIDER_BASE_URLS.get(cfg.provider, "")
    #     if not base:
    #         raise HTTPException(400, "Unknown provider and no base_url given.")
    #     client = OpenAI(api_key=cfg.api_key, base_url=base)
    #     return ResolvedLLM(client, cfg.model, cfg.fast_model or cfg.model)
    if not API_KEY:
        raise HTTPException(400, "No API key configured in .env")
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
    principle: str = ""  # backward compat: maps from chosen_insight
    experiment: str
    why_it_matters: str = ""
    candidates: list[dict] = []  # backward compat: maps from five_insights
    # New pipeline fields
    main_problem: str = ""
    creator_thesis: str = ""
    chosen_insight: str = ""
    importance_score: int = 0
    done_criteria: str = ""
    five_insights: list[dict] = []
    selection_reasoning: dict = {}
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

# ─── Pipeline Prompts ──────────────────────────────────────

ANALYSIS_SYSTEM = """You are an expert teacher analyzing a video transcript.

Read the transcript and identify:
1. Main problem being solved
2. Creator's thesis (the single main argument)
3. Three to five key insights (each with evidence from the transcript)
4. The ONE insight that matters most — the 80/20: the single idea that creates the biggest change in someone's ability after watching this video

Return ONLY valid JSON — no extra text:
{
  "main_problem": "description of the core problem being addressed",
  "creator_thesis": "the single main argument the creator is making",
  "insights": [
    {"insight": "insight text", "evidence": "supporting evidence from transcript"}
  ],
  "pareto_insight": "the single insight that matters most (80/20)",
  "pareto_why": "why this one insight matters more than the others (2-3 sentences)"
}"""

EXERCISE_SYSTEM = """You are a world-class mentor. The learner just watched a video.

The single most important insight is:

{insight}

What would a world-class mentor stop the learner and insist they practice before continuing the video?

BAD EXERCISES (never do these):
- Reflect on your day...
- Think about how you feel...
- Imagine a scenario where...
- Consider what would happen if...
- Understand the concept of...

GOOD EXAMPLES (use this style):
1. Open your last product idea. Write down 10 assumptions. Call out the riskiest one. Find one person today who can validate it.
2. Draft a one-paragraph counter-argument to your own position. Then interview someone who disagrees. Rewrite it based on what you learned.
3. Draw a quick diagram of the system. Label every interaction point. Modify one label to reduce friction.

Your exercise MUST:
- Take ≤5 minutes
- Produce a real artifact or observable outcome
- Be doable right now in the learner's browser or work context
- Have a clear "done" state
- Start with Build, Write, Draw, Modify, Prototype, Measure, Interview, Ship, Open, Find, List, Draft, Create, Design, or Code

Return ONLY valid JSON — no extra text:
{{
  "experiment": "Numbered steps (3-5), each starting with an action verb, newline-separated",
  "done_criteria": "What counts as done (1 sentence, clear and measurable)",
  "why_it_matters": "One sentence connecting this to real-world application"
}}"""


# ─── Pipeline Functions ────────────────────────────────────

FORBIDDEN_WORDS = {"reflect", "think about ", "imagine ", "consider ", "understand "}
ACCEPTED_VERBS = {
    "build", "interview", "write", "draw", "modify", "prototype", "measure", "ship",
    "open", "find", "list", "call", "draft", "create", "design", "code", "test",
    "record", "map", "sketch", "diagram", "collect", "identify", "define", "prepare",
}


async def _llm_call(
    llm: ResolvedLLM,
    system: str,
    user: str,
    temp: float,
    max_tokens: int = 2000,
) -> str:
    """Call LLM with system + user messages at given temperature."""
    messages = [{"role": "system", "content": system}]
    if user:
        messages.append({"role": "user", "content": user})
    resp = await asyncio.to_thread(
        llm.client.chat.completions.create,
        model=llm.model,
        messages=messages,
        temperature=temp,
        max_tokens=max_tokens,
        timeout=120,
    )
    text = resp.choices[0].message.content.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0].strip()
    return text


def critic_exercise(experiment_text: str) -> dict:
    """Check exercise requires producing something real."""
    lower = experiment_text.lower()

    for word in FORBIDDEN_WORDS:
        if word in lower:
            return {"pass": False, "reason": f"Exercise contains '{word.strip()}' — must produce something real"}

    first_line = experiment_text.strip().split("\n")[0] if experiment_text else ""
    import re as _re
    first_word = _re.sub(r"^\s*\d+[\.\)]\s*", "", first_line).split()[0].lower() if first_line else ""
    if first_word and first_word not in ACCEPTED_VERBS:
        return {"pass": False, "reason": f"First step must start with an action verb. Got: '{first_word}'"}

    return {"pass": True, "reason": ""}


# ─── Pipeline Orchestrator (2 stages) ──────────────────────

async def generate_experiment(
    transcript: str,
    retry_reason: str | None,
    llm: ResolvedLLM,
) -> dict:
    """2-stage pipeline: understand → exercise.

    Stage 1 (temp=0.2): Extract thesis, insights, pareto insight.
    Stage 2 (temp=0.7): Create exercise from pareto insight only. No transcript.
    """
    # Stage 1: Video understanding — deterministic, low temp
    capped = transcript[:80000]
    analysis_text = await _llm_call(llm, ANALYSIS_SYSTEM, capped, 0.2, 2000)
    analysis = parse_json_lenient(analysis_text)
    if not analysis or not analysis.get("insights"):
        raise ValueError(f"Analysis step failed: {analysis_text[:200]}")

    # Stage 2: Exercise from pareto insight only — creative, higher temp
    pareto = analysis.get("pareto_insight", analysis["insights"][0]["insight"])
    evidence = analysis.get("pareto_why", "")

    difficulty_note = ""
    if retry_reason == "too_easy":
        difficulty_note = "DIFFICULTY: Their last exercise was TOO EASY. Make this significantly more demanding."
    elif retry_reason == "too_hard":
        difficulty_note = "DIFFICULTY: Their last exercise was TOO HARD. Simplify and break it down further."

    system = EXERCISE_SYSTEM.format(insight=pareto)
    user = f"Selected insight: {pareto}\nWhy it matters: {evidence}\n\n{difficulty_note}" if difficulty_note else f"Selected insight: {pareto}\nWhy it matters: {evidence}"

    last_err = None
    for attempt in range(2):
        text = await _llm_call(llm, system, user, 0.7, 1500)
        parsed = parse_json_lenient(text)
        if not parsed or not parsed.get("experiment"):
            last_err = f"Unparseable exercise ({len(text)} chars)"
            print(f"  exercise attempt {attempt}: unparseable")
            continue

        verdict = critic_exercise(parsed["experiment"])
        if verdict["pass"]:
            parsed.setdefault("done_criteria", "")
            parsed.setdefault("why_it_matters", "")
            parsed.setdefault("principle", pareto)
            # Compose full result
            result = {
                "principle": pareto,
                "experiment": parsed["experiment"],
                "why_it_matters": parsed.get("why_it_matters", ""),
                "candidates": analysis.get("insights", []),
                "main_problem": analysis.get("main_problem", ""),
                "creator_thesis": analysis.get("creator_thesis", ""),
                "chosen_insight": pareto,
                "importance_score": 0,
                "done_criteria": parsed.get("done_criteria", ""),
                "five_insights": analysis.get("insights", []),
                "selection_reasoning": {"why_best": analysis.get("pareto_why", ""), "why_others_less_important": []},
            }
            print(f"  [PIPELINE] {len(analysis.get('insights', []))} insights → pareto → exercise ({len(parsed['experiment'])} chars)")
            return result

        print(f"  exercise attempt {attempt}: critic rejected — {verdict['reason']}")
        last_err = verdict["reason"]
        user += f"\n\nFEEDBACK FROM CRITIC: {verdict['reason']} Rewrite the exercise."

    raise ValueError(f"Exercise generation failed: {last_err}")





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
        prepared_transcripts.get(vid) or transcript
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
      status   — progress notes (e.g. compressing, analyzing)
      done     — final result: experiment_id + all fields
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
            # The new pipeline handles its own compression internally.
            # Just pass the raw transcript — hierarchical compression is now
            # always the first step (not a separate conditional path).
            yield f"event: status\ndata: {json.dumps({'message': 'Analyzing video...'})}\n\n"

            result = await generate_experiment(transcript, req.retry_reason, llm)

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
