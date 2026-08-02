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
from providers import test_connection as prov_test_connection, get_adapter

load_dotenv()

app = FastAPI(title="Praxis")

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
# Model info is now BYOK-driven. The env defaults shown in /health are advisory only.
MODEL = os.getenv("MODEL", "gemini-2.5-flash")
FAST_MODEL = os.getenv("FAST_MODEL", MODEL)
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/")

CACHE_TTL = 86400  # 24 hours
GEN_TIMEOUT = 60   # fail fast; a hung call should never cost minutes

# Domains Praxis currently serves. The category label from the LLM
# guides this filter, but teachability_score does the heavy gating.
ACTIVE_CATEGORIES = {
    "coding", "software", "technical", "data", "design",
    "business", "career", "entrepreneurship", "sales",
    "marketing", "leadership", "communication", "storytelling",
    "negotiation", "personal-finance",
    "productivity", "science", "language", "academic",
}

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

# ─── Trace logging ─────────────────────────────────
_TRACE_LOG = os.path.join(os.path.dirname(__file__), "traces.log")
_FEEDBACK_LOG = os.path.join(os.path.dirname(__file__), "feedback.json")
_PDT = timezone.utc  # placeholder; we'll use a fixed offset for PDT
_PDT_OFFSET = -7 * 3600  # UTC-7 (PDT)


def _pdt_now() -> datetime:
    """Return current time as a naive datetime in PDT."""
    utc = datetime.now(timezone.utc)
    return utc.replace(tzinfo=None) + __import__("datetime").timedelta(seconds=_PDT_OFFSET)


def _trace(trace_id: str, component: str, phase: str, **fields) -> None:
    """Append a structured trace entry to traces.log.

    Args:
        trace_id: unique request-scoped id.
        component: e.g. 'endpoint', 'llm', 'pipeline', 'critic', 'transcript', 'feedback'.
        phase: e.g. 'input', 'output', 'start', 'end', 'error'.
        **fields: arbitrary key=value pairs (content, model, temp, etc.).
    """
    ts = _pdt_now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " PDT"
    parts = [f"[{ts}]", f"[{trace_id}]", f"[{component}]", f"[{phase}]"]
    for k, v in fields.items():
        if isinstance(v, str) and len(v) > 3000:
            parts.append(f"\n  {k} ({len(v)} chars):")
            parts.append(f"    {v[:3000]}")
            parts.append(f"    ... [{len(v) - 3000} more chars]")
        elif isinstance(v, str) and "\n" in v:
            parts.append(f"\n  {k}:")
            for line in v.split("\n"):
                parts.append(f"    | {line}")
        else:
            parts.append(f"\n  {k}: {v}")
    entry = "".join(parts) + "\n\n"

    try:
        with open(_TRACE_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass

    # Terminal preview — one compact line
    if component == "llm" and phase == "output":
        content_len = len(fields.get("content", ""))
        dur = fields.get("elapsed_sec", "")
        dur_str = f" in {dur}s" if dur else ""
        print(f"  [TRACE {trace_id}] {component} {phase} — {content_len}c{dur_str}", flush=True)
    elif component == "feedback":
        liked = fields.get("liked")
        print(f"  [TRACE {trace_id}] feedback: {'liked' if liked else 'disliked' if liked is False else 'skipped'}", flush=True)
    elif component in ("endpoint", "pipeline", "critic", "transcript"):
        brief = fields.get("content", fields.get("message", fields.get("reason", "")))
        if isinstance(brief, str) and len(brief) > 120:
            brief = brief[:120] + "..."
        dur = fields.get("elapsed_sec", "")
        dur_str = f" [{dur}s]" if dur else ""
        print(f"  [TRACE {trace_id}] {component} {phase}{dur_str}: {brief}", flush=True)


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
    BYOK is required — rejects requests without api_key."""
    if cfg and cfg.api_key:
        adapter = get_adapter(cfg.provider) if cfg.provider else None
        if adapter:
            base = cfg.base_url or adapter.base_url
            model = cfg.model or adapter.default_model
            fast = cfg.fast_model or model
            client = OpenAI(api_key=cfg.api_key, base_url=base)
            return ResolvedLLM(client, model, fast)
        # Unknown provider — treat as custom OpenAI-compatible
        base = cfg.base_url
        if not base:
            raise HTTPException(400, f"Unknown provider '{cfg.provider}' and no base_url provided.")
        model = cfg.model or "gpt-4o-mini"
        fast = cfg.fast_model or model
        client = OpenAI(api_key=cfg.api_key, base_url=base)
        return ResolvedLLM(client, model, fast)
    raise HTTPException(400, "No API key provided. Configure a model in the extension settings first.")

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
    transcript_error: str | None = None  # client-side fetch failure reason (diagnostics)
    goal_override: str | None = None
    # Passed directly in the request instead of via a racing side-channel
    # signal POST — the regeneration is guaranteed to see it.
    retry_reason: str | None = None   # too_easy | too_hard | wrong_topic
    force: bool = False               # bypass cache (Try Again)
    llm: LLMConfig | None = None      # BYOK model routing

class SuggestResponse(BaseModel):
    experiment_id: str
    principle: str = ""
    experiment: str = ""  # backward compat — populated from steps with newlines
    steps: list[str] = []
    title: str = ""
    duration: str = ""
    challenge_type: str = ""
    why_it_matters: str = ""
    candidates: list[dict] = []
    main_problem: str = ""
    creator_thesis: str = ""
    chosen_insight: str = ""
    importance_score: int = 0
    done_criteria: str = ""  # backward compat — populated from done_criteria_list with newlines
    done_criteria_list: list[str] = []
    five_insights: list[dict] = []
    selection_reasoning: dict = {}
    cached: bool = False

    def model_post_init(self, __context):
        if not self.experiment and self.steps:
            self.experiment = "\n".join(self.steps)
        if not self.done_criteria and self.done_criteria_list:
            self.done_criteria = "\n".join(self.done_criteria_list)
        return super().model_post_init(__context)

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

class PrivacyDataResponse(BaseModel):
    has_profile: bool = False
    experiment_count: int = 0
    feedback_count: int = 0
    stored_signals: list[str] = []


# ─── Store metadata (for privacy introspection) ──────────
# _pii_notes is a compile-time inventory of everything this service persists,
# so the privacy endpoint can report it without hard-coding.

_PRIVACY_INVENTORY = {
    "profiles": "In-memory dict: user_id → {role, goal, signals}. Never written to disk.",
    "experiments_log": "In-memory list: {experiment_id, user_id, video_id}. Insight text is NOT stored (only metadata).",
    "feedback_store": "In-memory list: {user_id, experiment_id, liked, comment}. Used to improve exercise quality.",
    "exercise_cache": "In-memory dict: keyed by user_id:video_id:model. Cached exercise results. Volatile — lost on restart.",
    "prepared_transcripts": "In-memory dict: video_id → compressed transcript. No user linkage. Volatile.",
    "traces_log": "File (traces.log): LLM call metadata (model, lengths, timing). Transcript/exercise content is NOT logged.",
}

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
        # Try explicit languages first, then auto-generated, then any available
        try_langs = ["en", "a.en", "hi", "a.hi", "es", "a.es"]
        segments = await asyncio.wait_for(
            asyncio.to_thread(api.fetch, video_id, languages=try_langs),
            timeout=15,
        )
        return " ".join(seg.text for seg in segments)
    except Exception as e:
        # Last resort — list available transcripts and try the first one
        try:
            api2 = YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url) if proxy_url else None
            )
            available = await asyncio.wait_for(
                asyncio.to_thread(lambda: list(api2.list(video_id))),
                timeout=10,
            )
            if available:
                segs = await asyncio.wait_for(
                    asyncio.to_thread(available[0].fetch),
                    timeout=15,
                )
                return " ".join(s.text for s in segs)
        except Exception:
            pass
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

FIRST — JUDGE ACTIONABILITY: Can a viewer walk away from this video
with ONE nameable thing to try?

Ignore the format — podcast, interview, lecture, livestream, demo, vlog,
tutorial. The packaging tells you nothing. Judge only the teachability of
the content itself.

TECHABILITY SCORE (0–100):
• High (70+): The creator teaches a specific mechanism, process, mental
  model, or technique the viewer can replicate. The viewer could name
  exactly one thing to try after watching.
• Medium (40–69): Conceptual or educational content whose ideas can be
  applied or practiced, even if indirectly. The viewer could name a
  principle to act on or a behavior to adopt.
• Low (<40): Content designed to inform, entertain, opine, or document —
  not to enable action. The viewer walks away knowing more but cannot
  name a single thing to try.

Be honest and neutral. Never inflate the score. A well-produced news
analysis or documentary scores low even if it's excellent at what it does.
A casual livestream might score high if it contains real technique.

CATEGORY: A hint for organization, not a gate. Pick the best fit from:
coding, software, technical, data, design,
business, career, entrepreneurship, sales,
marketing, leadership, communication, storytelling,
negotiation, personal-finance,
productivity, science, language, academic,
or a short label (1–2 words) describing the actual domain. Use "other"
only when nothing fits.

If teachability_score is below 40:
  Output ONLY the three teachability fields with a one-sentence
  friendly "reason", and empty strings for all analysis fields.

If score IS 40+, continue with the full analysis below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FULL ANALYSIS (score 40+ only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Read the transcript and identify:
1. Main problem being solved
2. Creator's thesis (the single main argument)
3. Three to five key insights (each with evidence from the transcript)
4. The ONE insight that matters most — the 80/20: the single idea that creates
   the biggest change in someone's ability after watching this video

To identify the 80/20 insight, evaluate which insight:
- Is the foundational prerequisite — without it, nothing else in the video works
- Makes every other concept in the video click into place
- Would most change how the learner practices this skill
- Would leave the learner stuck if misunderstood

The insight must be specific to this video.

Do not produce generic statements such as:
- "Take action"
- "Be consistent"
- "Think critically"
- "Practice what you learned"
unless the creator specifically teaches that idea.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY valid JSON — no extra text:

{
  "teachability_score": <0-100>,
  "category": "<domain>",
  "reason": "One-sentence explanation",
  "main_problem": "... or empty if score < 40",
  "creator_thesis": "... or empty if score < 40",
  "insights": [
    {"insight": "insight text", "evidence": "supporting evidence from transcript"}
  ] or empty [] if score < 40,
  "pareto_insight": "... or empty if score < 40",
  "pareto_why": "... or empty if score < 40",
  "pareto_mechanism": "The EXACT steps, method, code pattern, tool, or framework the creator teaches to execute this insight. Quote the specifics from the transcript — parameter names, commands, tools, order of operations, numbers. Empty string if the video teaches no concrete mechanism."
}"""

EXERCISE_SYSTEM = """You are an expert teacher and skill-transfer designer.

Your job is NOT to summarize this video.
Your job is NOT to generate a generic exercise.

Your job is to convert the creator's most important idea into a
real-world action that helps this specific learner internalize and
practice the skill.

The final action must be derived from the CONTENT of this video.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VIDEO ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Main problem the video solves: {main_problem}
Creator's thesis: {creator_thesis}
Most important insight: {insight}
Why this insight matters: {evidence}
Exact mechanism/method taught in the video: {mechanism}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — UNDERSTAND THE MECHANISM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Study the analysis above.

Ask: What specific mechanism, process, mental model, or behavior makes this insight useful in practice?

The "Exact mechanism/method taught in the video" line is the MOST IMPORTANT input.
Your exercise MUST be built on that mechanism — reuse its exact steps, tools,
parameters, or framework. Do not replace it with generic advice. If the mechanism
field is empty, derive the mechanism from the insight and evidence above.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — CONVERT KNOWLEDGE INTO A SKILL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Ask:

"What complete feedback loop, diagnostic workflow, or execution cycle would a master of this content run to measure and improve their work?"

Describe the skill as an observable behavior.

For example:

BAD:
"Understand customer discovery."

GOOD:
"Observe a real user's existing workaround and identify the repeated
pain that makes the workaround necessary."

The skill must come from the specific content of the video.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 3 — DESIGN THE SKILL-TRANSFER CHALLENGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create ONE challenge that makes the learner practice the exact mechanism taught in the video.

The challenge must:

- Be derived directly from the creator's main insight.
- Require the learner to perform the behavior or process taught.
- Produce a concrete artifact, decision, observation, experiment,
  conversation, prototype, analysis, or other observable outcome.
- Have a clear definition of done.
- Be achievable now, but meaningful enough to create genuine practice.
- Have the learner observe, audit, or benchmark the output of their action against a concrete standard (e.g., test cases, metrics, rubrics, or traces).

Do not create an exercise merely because it is easy to describe.

Do not use generic activities such as:
- "Reflect on..."
- "Think about..."
- "Write down your thoughts..."
- "Consider how..."
- "Imagine..."
unless the video specifically teaches reflection, thinking, or
imagination as the skill.

The challenge must test whether the learner can APPLY the video's
specific idea.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 4 — VERIFY THE CHALLENGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Before returning the answer, ask yourself:

A. If I removed the video transcript, would this challenge still make sense as a generic productivity exercise?
   If YES, redesign it.

B. Does completing this challenge require executing and evaluating the specific mechanism taught by the creator?"
   If NO, redesign it.

C. Could the learner complete this challenge without understanding the video?
   If YES, redesign it.

D. Does the challenge use the learner's real context?
   If NO, look again for a way to apply it to their actual goal, work, project, or life.

E. Can I point to the specific idea in the video that this challenge exercises?
   If NO, redesign it.

Return ONLY valid JSON — no extra text:
{{
  "title": "Short, memorable name for this challenge (2-6 words)",
  "steps": [
    "Step 1: action verb + specific thing to do",
    "Step 2: action verb + specific thing to do",
    "Step 3: action verb + specific thing to do",
    "Step 4 (optional): action verb + specific thing to do",
    "Step 5 (optional): action verb + specific thing to do"
  ],
  "done_criteria": [
    "Criterion 1: what success looks like — measurable",
    "Criterion 2 (optional): another check",
    "Criterion 3 (optional): another check"
  ],
  "why_it_matters": "One sentence connecting this to real-world application",
  "duration": "Estimated time (e.g., '15 min', '30 min', '1 hour')",
  "challenge_type": "One of: build | analyze | debug | refactor | practice | experiment | design | audit"
}}"""


# ─── Pipeline Functions ────────────────────────────────────

FORBIDDEN_WORDS = {"reflect", "think about ", "imagine ", "consider ", "understand "}
async def _llm_call(
    llm: ResolvedLLM,
    system: str,
    user: str,
    temp: float,
    max_tokens: int = 2000,
    trace_id: str = "",
    call_label: str = "",
) -> str:
    """Call LLM with system + user messages at given temperature."""
    messages = [{"role": "system", "content": system}]
    if user:
        messages.append({"role": "user", "content": user})

    # ── Trace input (metadata only — never log transcripts, prompts, or user content) ──
    _trace(trace_id, "llm", "input",
        call=call_label,
        model=llm.model,
        temp=temp,
        max_tokens=max_tokens,
        system_len=len(system),
        user_len=len(user) if user else 0,
    )
    _call_start = _pdt_now()

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

    # ── Trace output (metadata only) ──
    _trace(trace_id, "llm", "output",
        call=call_label,
        model=llm.model,
        finish_reason=resp.choices[0].finish_reason,
        content_len=len(text),
        elapsed_sec=round((_pdt_now() - _call_start).total_seconds(), 1),
    )
    return text


def critic_exercise(experiment_text: str) -> dict:
    """Check exercise requires producing something real."""
    lower = experiment_text.lower()

    for word in FORBIDDEN_WORDS:
        if word in lower:
            return {"pass": False, "reason": f"Exercise contains '{word.strip()}' — must produce something real"}

    return {"pass": True, "reason": ""}


# ─── Pipeline Orchestrator (2 stages) ──────────────────────

async def generate_experiment(
    transcript: str,
    retry_reason: str | None,
    llm: ResolvedLLM,
    trace_id: str = "",
) -> dict:
    """2-stage pipeline: understand → exercise.

    Stage 1 (temp=0.2): Extract thesis, insights, pareto insight with quality rubric.
    Stage 2 (temp=0.7): Design a skill-transfer challenge using full video context.
    """
    _trace(trace_id, "pipeline", "start",
        transcript_length=len(transcript),
        retry_reason=retry_reason or "none",
        model=llm.model)
    _pipeline_start = _pdt_now()
    # Privacy: transcript content is NEVER written to logs or disk (except transient
    # LLM requests). The trace above records length only.

    # Stage 1: Video understanding — deterministic, low temp
    capped = transcript[:80000]
    analysis_text = await _llm_call(llm, ANALYSIS_SYSTEM, capped, 0.2, 100000, trace_id=trace_id, call_label="stage1_analysis")
    analysis = parse_json_lenient(analysis_text)
    if not analysis:
        raise ValueError(f"Analysis step failed — unparseable JSON: {analysis_text[:200]}")

    # ── Classification gate ──
    teach_score = analysis.get("teachability_score", 0)
    category = (analysis.get("category", "other") or "other").lower()
    reason = analysis.get("reason", "")

    if teach_score < 40:
        result = {
            "status": "blocked",
            "reason": reason or "This video doesn't teach a practical skill.",
            "teachability_score": teach_score,
            "category": category,
            "is_out_of_scope": False,
        }
        _trace(trace_id, "pipeline", "blocked",
            teachability_score=teach_score, category=category,
            reason=reason)
        return result

    if category not in ACTIVE_CATEGORIES:
        # High-score content is a real skill even if the category label doesn't match.
        # Only gate medium-scoring conceptual content by category.
        if teach_score >= 70:
            _trace(trace_id, "pipeline", "note",
                msg="bypassing category gate — high-scoring content",
                teachability_score=teach_score, category=category)
        else:
            domain_hint = f"Praxis doesn't support '{category}' yet."
            result = {
                "status": "blocked",
                "reason": reason or domain_hint,
                "teachability_score": teach_score,
                "category": category,
                "is_out_of_scope": True,
            }
            _trace(trace_id, "pipeline", "blocked",
                teachability_score=teach_score, category=category,
                reason=reason, is_out_of_scope=True)
            return result

    # ── Validate analysis fields ──
    if not analysis.get("insights"):
        raise ValueError(f"Analysis step failed — no insights: {analysis_text[:200]}")

    _trace(trace_id, "pipeline", "stage1_done",
        insight_count=len(analysis.get("insights", [])),
        pareto_insight=analysis.get("pareto_insight", "")[:200],
        model=llm.model,
        elapsed_sec=round((_pdt_now() - _pipeline_start).total_seconds(), 1),
    )

    # Stage 2: Exercise from pareto insight + full video context
    pareto = analysis.get("pareto_insight", analysis["insights"][0]["insight"])
    evidence = analysis.get("pareto_why", "")
    mechanism = analysis.get("pareto_mechanism", "") or analysis.get("pareto_why", "")
    main_problem = analysis.get("main_problem", "")
    creator_thesis = analysis.get("creator_thesis", "")

    difficulty_note = ""
    if retry_reason == "too_easy":
        difficulty_note = "DIFFICULTY: Their last exercise was TOO EASY. Make this significantly more demanding."
    elif retry_reason == "too_hard":
        difficulty_note = "DIFFICULTY: Their last exercise was TOO HARD. Simplify and break it down further."

    system = EXERCISE_SYSTEM.format(
        insight=pareto,
        main_problem=main_problem,
        creator_thesis=creator_thesis,
        evidence=evidence,
        mechanism=mechanism,
    )
    user = (f"Selected insight: {pareto}\nWhy it matters: {evidence}\nMechanism taught: {mechanism}"
            + (f"\n\n{difficulty_note}" if difficulty_note else ""))

    last_err = None
    for attempt in range(2):
        text = await _llm_call(llm, system, user, 0.7, 100000, trace_id=trace_id, call_label=f"stage2_exercise_attempt{attempt}")
        parsed = parse_json_lenient(text)
        if not parsed or (not parsed.get("experiment") and not parsed.get("steps")):
            last_err = f"Unparseable exercise ({len(text)} chars)"
            _trace(trace_id, "pipeline", "retry", reason=last_err, attempt=attempt)
            continue

        verdict_text = parsed.get("experiment") or "\n".join(parsed.get("steps", []))
        verdict = critic_exercise(verdict_text)
        _trace(trace_id, "critic", verdict["pass"] and "pass" or "reject",
            attempt=attempt,
            reason=verdict.get("reason", ""),
            exercise_len=len(parsed.get("experiment", "")),
        )
        if verdict["pass"]:
            parsed.setdefault("why_it_matters", "")
            parsed.setdefault("principle", pareto)
            steps = parsed.get("steps", [])
            if not steps and parsed.get("experiment"):
                if isinstance(parsed["experiment"], str):
                    steps = [s.strip() for s in parsed["experiment"].split("\n") if s.strip()]
                elif isinstance(parsed["experiment"], list):
                    steps = parsed["experiment"]
            done_criteria_list = parsed.get("done_criteria_list", [])
            dc_raw = parsed.get("done_criteria")
            if not done_criteria_list and dc_raw:
                if isinstance(dc_raw, list):
                    done_criteria_list = dc_raw
                elif isinstance(dc_raw, str):
                    done_criteria_list = [s.strip() for s in dc_raw.split("\n") if s.strip()]
            done_criteria_str = "\n".join(done_criteria_list) if done_criteria_list else (dc_raw if isinstance(dc_raw, str) else "")
            experiment_str = "\n".join(steps) if steps else (parsed.get("experiment", "") if isinstance(parsed.get("experiment"), str) else "")
            # Compose full result
            result = {
                "principle": pareto,
                "experiment": experiment_str,
                "steps": steps,
                "title": parsed.get("title", ""),
                "duration": parsed.get("duration", ""),
                "challenge_type": parsed.get("challenge_type", ""),
                "why_it_matters": parsed.get("why_it_matters", ""),
                "candidates": analysis.get("insights", []),
                "main_problem": analysis.get("main_problem", ""),
                "creator_thesis": analysis.get("creator_thesis", ""),
                "chosen_insight": pareto,
                "importance_score": 0,
                "done_criteria": done_criteria_str,
                "done_criteria_list": done_criteria_list,
                "five_insights": analysis.get("insights", []),
                "selection_reasoning": {"why_best": analysis.get("pareto_why", ""), "why_others_less_important": []},
            }
            _trace(trace_id, "pipeline", "end",
                status="success",
                exercise_length=len(experiment_str),
                model=llm.model,
                elapsed_sec=round((_pdt_now() - _pipeline_start).total_seconds(), 1))
            return result

        _trace(trace_id, "critic", "retry", attempt=attempt, reason=verdict["reason"])
        last_err = verdict["reason"]
        user += f"\n\nFEEDBACK FROM CRITIC: {verdict['reason']} Rewrite the exercise."

    _trace(trace_id, "pipeline", "end",
        status="error",
        reason=last_err or "unknown",
        model=llm.model,
        elapsed_sec=round((_pdt_now() - _pipeline_start).total_seconds(), 1))
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
    trace_id = uuid.uuid4().hex[:12]
    llm = resolve_llm(req.llm)
    vid = extract_video_id(req.video_url)
    _trace(trace_id, "endpoint", "start",
        endpoint="/api/suggest",
        video_url=req.video_url,
        user_id=user_id or "anon",
        has_transcript="yes" if req.transcript else "no",
        transcript_error=req.transcript_error or "",
        retry_reason=req.retry_reason or "none",
        force=req.force,
        model=llm.model)
    _start_ts = _pdt_now()

    if not vid:
        _trace(trace_id, "endpoint", "error", message="Invalid YouTube URL", user_id=user_id or "anon")
        raise HTTPException(400, "Invalid YouTube URL.")

    # Log BYOK provider+model so user can verify their key is in use
    byok_prefix = req.llm.provider if req.llm and req.llm.provider else "custom"
    print(f"  [BYOK] suggest — provider={byok_prefix}, model={llm.model}", flush=True)

    transcript = req.transcript or await fetch_transcript(vid)
    if not transcript:
        _trace(trace_id, "endpoint", "error", message="Could not fetch transcript",
               transcript_error=req.transcript_error or "server_fetch_failed",
               user_id=user_id or "anon")
        raise HTTPException(400, "Could not fetch transcript.")
    _trace(trace_id, "transcript", "fetched", length=len(transcript), video_id=vid)

    cache_key = f"{user_id or 'anon'}:{vid}:{llm.model}"
    now_ts = datetime.now(timezone.utc).timestamp()

    if not req.force and cache_key in exercise_cache:
        entry = exercise_cache[cache_key]
        if now_ts - entry["ts"] < CACHE_TTL:
            _trace(trace_id, "endpoint", "cache_hit", user_id=user_id or "anon", video_id=vid)
            return SuggestResponse(**entry["result"], cached=True)

    prepared = (
        prepared_transcripts.get(vid) or transcript
    )
    if vid not in prepared_transcripts:
        prepared_transcripts[vid] = prepared

    try:
        result = await generate_experiment(prepared, req.retry_reason, llm, trace_id=trace_id)
        if result.get("status") == "blocked":
            exercise_cache[cache_key] = {"result": result, "ts": now_ts}
            _trace(trace_id, "endpoint", "blocked",
                reason=result.get("reason",""),
                category=result.get("category",""),
                user_id=user_id or "anon", video_id=vid, model=llm.model,
                elapsed_sec=round((_pdt_now() - _start_ts).total_seconds(), 1))
            return {"status": "blocked", **result}
    except Exception as e:
        _trace(trace_id, "endpoint", "error",
            message=str(e), user_id=user_id or "anon", video_id=vid, model=llm.model,
            elapsed_sec=round((_pdt_now() - _start_ts).total_seconds(), 1))
        raise HTTPException(502, f"Experiment generation failed: {e}")

    exp_id = str(uuid.uuid4())[:12]
    response_data = {"experiment_id": exp_id, **result}
    exercise_cache[cache_key] = {"result": response_data, "ts": now_ts}
    _log_experiment(exp_id, user_id, vid, result)
    _trace(trace_id, "endpoint", "end",
        experiment_id=exp_id, cached=False,
        user_id=user_id or "anon", video_id=vid, model=llm.model,
        elapsed_sec=round((_pdt_now() - _start_ts).total_seconds(), 1))
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
    trace_id = uuid.uuid4().hex[:12]

    _trace(trace_id, "endpoint", "start",
        endpoint="/api/suggest/stream",
        video_url=req.video_url,
        user_id=user_id or "anon",
        has_transcript="yes" if req.transcript else "no",
        transcript_error=req.transcript_error or "",
        retry_reason=req.retry_reason or "none",
        force=req.force,
    )
    _start_ts = _pdt_now()

    async def gen():
        nonlocal trace_id
        # 1. Skeleton first — the user sees structure within milliseconds.
        yield "event: skeleton\ndata: {}\n\n"

        try:
            llm = resolve_llm(req.llm)
        except HTTPException as he:
            _trace(trace_id, "endpoint", "error", message=he.detail, user_id=user_id or "anon")
            yield f"event: error\ndata: {json.dumps({'message': he.detail})}\n\n"
            return

        _trace(trace_id, "endpoint", "llm_ready", model=llm.model)

        if not vid:
            _trace(trace_id, "endpoint", "error", message="Invalid YouTube URL", user_id=user_id or "anon")
            yield f"event: error\ndata: {json.dumps({'message': 'Invalid YouTube URL'})}\n\n"
            return

        transcript = req.transcript or await fetch_transcript(vid)
        if not transcript:
            _trace(trace_id, "endpoint", "error", message="Could not fetch transcript",
                   transcript_error=req.transcript_error or "server_fetch_failed",
                   user_id=user_id or "anon")
            yield f"event: error\ndata: {json.dumps({'message': 'Could not fetch transcript'})}\n\n"
            return

        _trace(trace_id, "transcript", "fetched", length=len(transcript), video_id=vid)

        cache_key = f"{user_id or 'anon'}:{vid}:{llm.model}"
        now_ts = datetime.now(timezone.utc).timestamp()

        # 2. Cache hit → instant done.
        if not req.force and cache_key in exercise_cache:
            entry = exercise_cache[cache_key]
            if now_ts - entry["ts"] < CACHE_TTL:
                payload = {**entry["result"], "cached": True}
                _trace(trace_id, "endpoint", "cache_hit", user_id=user_id or "anon", video_id=vid)
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                return

        try:
            yield f"event: status\ndata: {json.dumps({'message': 'Analyzing video...'})}\n\n"

            result = await generate_experiment(transcript, req.retry_reason, llm, trace_id=trace_id)

            # Blocked classification → yield blocked event and stop
            if result.get("status") == "blocked":
                exercise_cache[cache_key] = {"result": result, "ts": now_ts}
                _trace(trace_id, "endpoint", "blocked",
                    reason=result.get("reason",""),
                    category=result.get("category",""),
                    user_id=user_id or "anon",
                    video_id=vid,
                    model=llm.model,
                    elapsed_sec=round((_pdt_now() - _start_ts).total_seconds(), 1))
                yield f"event: blocked\ndata: {json.dumps(result)}\n\n"
                return

            exp_id = str(uuid.uuid4())[:12]
            response_data = {"experiment_id": exp_id, **result}
            exercise_cache[cache_key] = {"result": response_data, "ts": now_ts}
            _log_experiment(exp_id, user_id, vid, result)

            _trace(trace_id, "endpoint", "end",
                experiment_id=exp_id,
                cached=False,
                user_id=user_id or "anon",
                video_id=vid,
                model=llm.model,
                elapsed_sec=round((_pdt_now() - _start_ts).total_seconds(), 1))
            yield f"event: done\ndata: {json.dumps(response_data)}\n\n"
        except Exception as e:
            _trace(trace_id, "endpoint", "error",
                message=str(e),
                user_id=user_id or "anon",
                video_id=vid,
                model=getattr(llm, 'model', 'unknown'),
                elapsed_sec=round((_pdt_now() - _start_ts).total_seconds(), 1))
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ─── Endpoint: Feedback ───────────────────────────────────

@app.post("/api/feedback")
async def feedback(fb: FeedbackRequest):
    entry = next((e for e in experiments_log if e["experiment_id"] == fb.experiment_id), None)
    user_id = entry["user_id"] if entry else "unknown"
    record = {
        "user_id": user_id,
        "experiment_id": fb.experiment_id,
        "liked": fb.liked,
        "comment": fb.question,
        "timestamp": _utc_now(),
    }
    feedback_store.append(record)

    _trace(fb.experiment_id, "feedback", "received",
        user_id=user_id,
        liked=fb.liked,
        has_comment="yes" if fb.question else "no",
    )

    try:
        existing = []
        if os.path.exists(_FEEDBACK_LOG):
            with open(_FEEDBACK_LOG, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing.append(record)
        with open(_FEEDBACK_LOG, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _trace(fb.experiment_id, "feedback", "persist_error", error=str(e))

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


# ─── Endpoint: Privacy ────────────────────────────────────

@app.get("/api/privacy/data")
async def privacy_data(user_id: str = ""):
    """Return a summary of stored data for a user. Never returns sensitive content."""
    profile = profiles.get(user_id)
    exps = [e for e in experiments_log if e["user_id"] == user_id]
    fbs = [f for f in feedback_store if f["user_id"] == user_id]
    return PrivacyDataResponse(
        has_profile=profile is not None,
        experiment_count=len(exps),
        feedback_count=len(fbs),
        stored_signals=list(profile.get("signals", {}).keys()) if profile else [],
    )


@app.get("/api/privacy/inventory")
async def privacy_inventory():
    """Return what data the service stores and for how long. No user data returned."""
    return {
        "stores": _PRIVACY_INVENTORY,
        "transience": "All stores are in-memory (volatile). Data resets on server restart.",
        "traces_log_retention": "traces.log is append-only. Rotate or delete manually.",
        "cache_ttl_seconds": CACHE_TTL,
        "api_keys": "API keys are NEVER stored server-side. They are sent per-request, used immediately, and discarded.",
        "transcripts": "Transcripts are fetched on-demand and cached in-memory by video_id (not user_id). Volatile.",
    }


@app.post("/api/privacy/delete")
async def privacy_delete(body: dict):
    """Delete all stored data for a user. Hard delete — no soft delete."""
    user_id = body.get("user_id", "")
    if not user_id:
        raise HTTPException(400, "user_id is required")

    count = 0
    if user_id in profiles:
        del profiles[user_id]
        count += 1

    global experiments_log
    before = len(experiments_log)
    experiments_log = [e for e in experiments_log if e.get("user_id") != user_id]
    count += before - len(experiments_log)

    global feedback_store
    before = len(feedback_store)
    feedback_store = [f for f in feedback_store if f.get("user_id") != user_id]
    count += before - len(feedback_store)

    global exercise_cache
    prefix = f"{user_id}:"
    before = len(exercise_cache)
    exercise_cache = {k: v for k, v in exercise_cache.items() if not k.startswith(prefix)}
    count += before - len(exercise_cache)

    return {"status": "ok", "deleted_entries": count}


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
    return {"status": "ok", "default_model": MODEL, "default_fast_model": FAST_MODEL, "byok": "enabled"}

@app.post("/api/llm/test-connection")
async def llm_test_connection(body: dict):
    provider = (body.get("provider") or "").strip().lower()
    api_key = (body.get("api_key") or "").strip()
    if not provider:
        raise HTTPException(400, "provider is required")
    if not api_key:
        raise HTTPException(400, "api_key is required")
    return prov_test_connection(provider, api_key)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8003, reload=True)
