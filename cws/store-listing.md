# Praxis — Chrome Web Store Listing

## Name
Praxis: Turn knowledge into skill
(31 chars — under the 45-char limit)

## Summary (≤132 chars)
Turn any YouTube video into a hands-on practice exercise — built from what the video actually teaches.
(117 chars ✓)

## Category
Education

## Detailed description

Praxis turns any YouTube video into a hands-on practice exercise built from what the video actually teaches.

Watch a tutorial? Praxis converts it into a concrete challenge — with specific steps, measurable done-criteria, and a real artifact to produce. Not a quiz. Not "reflect on the video." A skill-transfer exercise you can do right now.

**How it works**
1. Open any YouTube video (coding, business, design, science, communication, and more).
2. Click the Praxis button in the YouTube toolbar.
3. Praxis analyzes the transcript and designs a challenge around the exact mechanism the creator teaches.
4. Complete the challenge, mark it done, and rate it — the next one gets tuned to your goal.

**Anti-generic by design**
Every exercise passes a four-step check (Understand → Convert → Design → Verify) that rejects generic prompts like "think about" or "reflect on this" — unless the video itself teaches thinking. If a video doesn't teach something actionable, Praxis tells you instead of forcing a generic exercise.

**Bring your own key**
Praxis uses your own API key — Google Gemini, OpenAI, Anthropic, OpenRouter, or OpenCode Zen. Your key is sent with each request and is never stored on our servers.

**Works best with** videos that teach a method, process, or technique: tutorials, how-tos, talks with concrete frameworks, and similar.

## Single purpose
Turn YouTube videos into hands-on practice exercises that build real skill.

## Permissions justification
- **storage** — Saves your Praxis user ID and preferences locally in your browser. Nothing is uploaded except what you explicitly trigger.
- **Read and change data on youtube.com** — Shows the Praxis button and exercise panel on YouTube video pages. Required for the extension's core function.
- **Read and change data on praxis.midnightbuilds.fyi** — Calls the Praxis backend that generates exercises from the video transcript. This is the product's own API.

## Privacy practices (disclosure)
- Collects: user ID (random local ID), video URL, video transcript (sent to the LLM provider to generate the exercise), feedback ratings/comments, completion signals, and usage timestamps.
- Your API key is sent per-request to the provider you choose and is **never stored server-side**.
- Server logs store metadata only (timestamps, model used, outcome) — never transcripts, prompts, or user content.
- No data is sold or shared with third parties beyond the LLM provider you select.
- See full policy: https://praxis.midnightbuilds.fyi/privacy

## Support email (for listing)
[PLACEHOLDER — replace with user's real contact email before submitting]

## Website (optional)
https://praxis.midnightbuilds.fyi
