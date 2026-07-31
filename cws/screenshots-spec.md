# Praxis — CWS Screenshot Spec

CWS requires 1–5 screenshots at **1280x800 or 640x400**. Use 1280x800.

**⚠️ CRITICAL — avoid the full-page trap:** use the DevTools **"Capture screenshot"** option (viewport only), NEVER "Capture full size screenshot". Full-size grabs the entire page height (comments, sidebar, everything) and makes the video a tiny 10-20% strip. The viewport-only capture is exactly 1280x800 and puts the player front and center.

## Method (verified)

1. Load the extension (chrome://extensions → Developer mode → Load unpacked → `extension/`).
2. Open a tutorial/how-to YouTube video that teaches a process.
3. **Enable Theater mode** — press `t` or click the theater icon (⛶) in the player controls. This makes the player the dominant element (~65-70% of width; at 1280x800 it fills ~86% of the frame height). Verified: in theater mode the player runs from just below the site header to just above the title block.
4. F12 → click the device-toolbar icon (mobile/tablet toggle) → set the viewport to **1280x800** → refresh so the page re-renders at that size.
5. Scroll to the top of the page (player at the top of the frame).
6. F12 → **⋮ menu → "Capture screenshot"** (NOT "Capture full size screenshot") → saves exactly 1280x800.
7. Repeat for each shot below (open the Praxis panel where required, keep the viewport at 1280x800, re-capture).

### If you only have a normal capture
Capture with Windows Snipping Tool (Win+Shift+S), then resize/crop to exactly 1280x800. Keep the video + product in the center — CWS crops listings to 640x400, so nothing critical should touch the edges.

## The 5 shots

| # | What to show | Notes |
|---|---|---|
| 1 | Theater-mode video page, scroll to top, Praxis button visible in the toolbar | The RGB infinity-loop button should be clearly visible. Player dominant. |
| 2 | Praxis exercise panel open: title + numbered steps | Exercise generated in the Praxis panel. Player + panel both in frame. |
| 3 | Exercise panel scrolled: done criteria + why it matters | Shows the concrete, measurable success criteria. |
| 4 | Feedback row (👍/👎) + "Mark done" | Shows the close-the-loop interaction. |
| 5 | Optional: settings (BYOK key entry) | Shows the "bring your own key" flow. Blur the key if one is present. |

## Rules
- No misleading content — screenshots must show the real extension UI.
- Blur/crop any API keys, personal emails, or private metadata.
- Keep the RGB branding consistent — it's the visual identity.
- Video does not need to be playing; a paused frame is fine and avoids random mid-video content.
