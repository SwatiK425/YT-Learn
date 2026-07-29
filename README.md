# YT-Learn

**Turn any YouTube video into a hands-on learning exercise — in 5 minutes.**

Stop passively watching. YT-Learn extracts the core insight from any tutorial, talk, or walkthrough and builds you a personalised micro-exercise: concrete steps, real thinking, done in under 5 minutes.

---

## Features

- **One-click exercises** — Open a YouTube video, click "Learn Lab", and get a step-by-step exercise built from the transcript.
- **Personalised to you** — Tell it your role and project goal. Every exercise is tailored to what you actually want to learn.
- **Action steps, not theory** — Checkbox steps you can tick off as you work. No fluff.
- **Mark complete + feedback** — Track progress with a single button. Thumbs up/down and optional notes help the AI improve.
- **Always fresh** — Hit "Try Again" for a different exercise from the same video.
- **Your API key, your data** — Bring your own LLM key. Nothing is shared with a third-party service.

---

## How it works

```
YouTube video → transcript extracted → AI reads it → 
personalised exercise → you do it → mark done
```

1. Open any YouTube video (tutorial, talk, walkthrough).
2. Click **Learn Lab** in the YouTube toolbar.
3. Set your learning goal (e.g. *"I'm a product manager learning about AI agents"*).
4. Click **Generate**.
5. Read the principle, work through the exercise steps, check them off.
6. Thumbs up/down when you're done.

---

## Prerequisites

- **Python 3.10+** installed on your machine
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

Leave this terminal running. The backend processes transcripts and generates exercises.

### 2. Chrome extension

1. Open `chrome://extensions` in Chrome
2. Toggle **Developer mode** (top-right)
3. Click **Load unpacked**
4. Select the `extension/` folder

You'll see a YT-Learn icon appear in the toolbar.

### 3. API key

1. Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey) (generous free tier) — or use any key from OpenAI, Anthropic, or OpenRouter.
2. Open any YouTube video → click the **Learn Lab** button in the toolbar.
3. Click **Settings** (gear icon) in the panel.
4. Choose your provider (e.g. **Google**) and paste your key.
5. Click **Test Connection** to verify.
6. That's it — you're ready to learn.

---

## Usage Tips

- **Be specific in your goal.** Instead of *"learn Python"*, try *"build a CLI tool to rename files in bulk"*. The exercise gets much better.
- **Use Try Again** if the exercise isn't quite right — you get a fresh take from the same video.
- **Thumbs down + a note** helps the model learn what you actually needed.
- **Mark Complete** logs your progress. Use it to track what you've worked through.

---

## Tech stack

| Layer | Technology |
|---|---|
| Extension | Chrome Manifest V3 |
| Backend | Python + FastAPI |
| Transcript | YouTube Transcript API |
| AI models | Google Gemini, OpenAI, Anthropic Claude, OpenRouter |

---

## License

Private / Commercial — contact the author for licensing inquiries.
