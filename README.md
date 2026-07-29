# Praxis

**Turn knowledge into skill.**

Get a hands-on exercise from any YouTube video — built from what the video actually teaches.

Stop passively watching. Praxis reads the transcript, finds the core insight, and turns it into a step-by-step exercise you can do right now.

---

## What it does

Open a YouTube tutorial or talk → Praxis extracts the transcript → an AI reads it and generates:

- **The key principle** — the one idea that matters most from the video
- **A step-by-step exercise** — concrete actions that practice the skill
- **Why it matters** — what you'll get out of doing it

Work through the steps, check them off, mark it complete.

---

## Features

- **Instant exercise** from any YouTube video with a transcript
- **Checklist steps** — tick them off as you work
- **Try Again** — get a different exercise from the same video
- **Mark Complete** — one tap when you're done
- **Feedback** — thumbs up/down + optional note (explicit Submit)
- **Your own API key** — bring your key from Google, OpenAI, Anthropic, or OpenRouter

---

## How it works

1. Open any YouTube video (tutorial, walkthrough, talk)
2. Click **Praxis** in the YouTube toolbar
3. Click **Generate**
4. Read the principle, work the exercise steps, check them off
5. Mark complete, thumbs up/down — done

---

## Requirements

- **Python 3.10+**
- **Chrome** (or any Chromium-based browser)
- An **API key** from one of: [Google AI Studio](https://aistudio.google.com/apikey), [OpenAI](https://platform.openai.com/api-keys), [Anthropic](https://console.anthropic.com/), or [OpenRouter](https://openrouter.ai/keys)

---

## Installation

### 1. Backend server

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8003
```

Keep this terminal running — the backend processes transcripts and generates exercises.

### 2. Chrome extension

1. Open `chrome://extensions` in Chrome
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select the `extension/` folder

You'll see the Praxis icon appear in the toolbar.

### 3. API key

1. Get a free key from [Google AI Studio](https://aistudio.google.com/apikey) — generous free tier, no credit card required.
2. Open any YouTube video → click **Praxis** in the toolbar.
3. Click the **gear icon** (Settings).
4. Choose your provider (e.g. **Google**) and paste your key.
5. Click **Test Connection** to verify.
6. Done — you're ready to learn.

---

## Tech

| Layer | What it uses |
|---|---|
| Extension | Chrome Manifest V3 |
| Backend | Python / FastAPI |
| Transcript | YouTube Transcript API |
| AI (your key) | Google Gemini, OpenAI, Anthropic Claude, or OpenRouter |

---

## License

Private / Commercial — contact the author for licensing inquiries.
