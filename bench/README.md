# Bench harness — see bench/README.md
#
# Run side-by-side A/B: baseline (prod-snapshot / master) vs candidate
# (perf-quality-v1 branch). Both servers start locally; identical transcript
# fixtures are replayed to each; latency + stage breakdown + quality heuristics
# are recorded per run in bench/results/ and appended to bench/RESULTS.md.

## 1. One-time setup

- `git tag prod-snapshot` (already done) then a frozen baseline checkout:
  `git worktree add ../YT-Learn-baseline prod-snapshot`
- Create venv (already done in repo root): `python -m venv .venv`
- Install deps: `.venv/Scripts/python -m pip install -r backend/requirements.txt`
- `copy bench\.env.example bench\.env` then fill in `ZEN_API_KEY` (the SAME key
  is sent to both instances — opencode-zen BYOK, never committed).

## 2. Capture fixtures (once)

Transcripts are fetched once and saved as fixtures so the comparison never
depends on network fetch behavior:

    .venv/Scripts/python bench/capture.py --all

## 3. Run the A/B

    .venv/Scripts/python bench/run.py
    .venv/Scripts/python bench/run.py --side candidate --fixtures b2XBO1DrACc --tag p0-cache

Both servers are started and stopped by the harness. Baseline = :8003
(../YT-Learn-baseline), candidate = :8004 (this repo's backend).

## 4. What gets recorded

- Per fixture × variant (none / too_easy / too_hard): HTTP status, wall latency,
  steps count, done_criteria count, forbidden-word scan (the backend's own
  critic vocabulary).
- Stage breakdown parsed from each server's isolated `traces.log` within the
  run window: stage1 avg, stage2 avg, retry count, critic rejects.
- Raw responses: `bench/results/<run_id>/<side>/<id>__<variant>.json`
- Log: `bench/RESULTS.md` (committed — this is the audit trail).

## 5. Rules of engagement

- Run from the `perf-quality-v1` branch. Master is FROZEN — never commit to it.
- Every code change ships with its bench evidence in the commit message
  (`verify: bench run <id>: baseline Xs -> candidate Ys ...`).
- Quality must not regress (critic pass, steps, done_criteria) while latency
  improves. No trade-offs without calling them out.
- Results dir and fixtures are gitignored; only scripts, .env.example, and
  RESULTS.md are committed.
