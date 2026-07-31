# Praxis Analytics (read-only, zero code changes)

Parses the app's **existing** logs — `backend/traces.log` + `backend/feedback.json` —
and renders a local dashboard. **Never writes to backend/**; only reads.

## Components

| File | Purpose |
|---|---|
| `collector.py` | Tails `../backend/traces.log` (byte-offset, rotation-safe), parses trace blocks, upserts into `analytics.db` (SQLite), emits `stats.json` |
| `dashboard.html` | Static dashboard — KPI cards + 4 Chart.js graphs + per-user table + feedback + blocked reasons + models + top videos |
| `chart.umd.min.js` | Vendored Chart.js 4.4.7 (no CDN dependency) |
| `systemd/` | Unit files: collector oneshot + 15-min timer + dashboard server (127.0.0.1:8004) |

## Run

```bash
python3 collector.py          # incremental tail (from saved offset)
python3 collector.py --full   # re-parse everything from byte 0
python3 -m http.server 8004 --bind 127.0.0.1 --directory .
# view: http://127.0.0.1:8004/dashboard.html
```

## Data model (derived from trace events)

Each request = one `trace_id`. Outcome classification:
- `success` — `[endpoint][end]` (experiment generated)
- `blocked` — `[endpoint][blocked]` (teachability/category gate)
- `error` — `[endpoint][error]`
- `cache_hit` — `[endpoint][cache_hit]`
- `in_progress` — no terminal event seen

`user_id` comes from `[endpoint][start]`; feedback joined from `feedback.json`
(source of truth for comments) + `[feedback][received]` trace blocks.

## Deployment (Oracle VM)

```bash
# on VM, from repo root
sudo cp analytics/systemd/*.service analytics/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now praxis-analytics-collector.timer
sudo systemctl enable --now praxis-analytics-dashboard.service
# view locally:
ssh -L 8004:localhost:8004 ubuntu@64.181.229.187
# → http://127.0.0.1:8004/dashboard.html
```

## Known gap

`/api/signal` (completed / try_again / difficulty) is in-memory only — not
logged to traces.log. "Mark done" counts are NOT recoverable without a code
change to `app.py` (deferred post-launch). Feedback likes/dislikes ARE captured.
