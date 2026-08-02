# Bench run log — audit trail for perf-quality-v1

Each run: baseline = prod-snapshot (master @ ac0caaf) on :8003,
candidate = perf-quality-v1 branch on :8004. Same fixtures, same model/key.
Full raw outputs under bench/results/<run_id>/ (gitignored).

| run | tag | change under test | baseline total | candidate total | verdict |
|-----|-----|-------------------|----------------|-----------------|---------|

(append-only — the harness appends a section per run)

## Run 20260801-174409

### baseline — 20260801-174409
- `jNQXAC9IVRw` v=none: 11.11s http=200 steps=None dc=None forbidden=-
- `b2XBO1DrACc` v=none: 67.58s http=200 steps=5 dc=4 forbidden=-
- `ff3j4olCUig` v=none: 28.43s http=200 steps=5 dc=4 forbidden=-
- `SQ3fZ1sAqXI` v=none: 27.15s http=200 steps=5 dc=4 forbidden=-
- `_jGSgzBkzrY` v=none: 43.2s http=200 steps=5 dc=3 forbidden=-
- `kxLmeUIXXtU` v=none: 64.12s http=200 steps=5 dc=4 forbidden=-
- `8yE6G1Lup1s` v=none: 49.57s http=200 steps=5 dc=4 forbidden=-
- TOTALS: n=7 sum=291.2s avg=41.6s
### candidate — 20260801-174409
- `jNQXAC9IVRw` v=none: 4.25s http=200 steps=None dc=None forbidden=-
- `b2XBO1DrACc` v=none: 24.81s http=200 steps=5 dc=3 forbidden=-
- `ff3j4olCUig` v=none: 27.38s http=200 steps=7 dc=3 forbidden=-
- `SQ3fZ1sAqXI` v=none: 33.33s http=200 steps=5 dc=4 forbidden=-
- `_jGSgzBkzrY` v=none: 24.59s http=200 steps=5 dc=4 forbidden=-
- `kxLmeUIXXtU` v=none: 33.63s http=200 steps=5 dc=4 forbidden=-
- `8yE6G1Lup1s` v=none: 206.97s http=200 steps=5 dc=4 forbidden=-
- TOTALS: n=7 sum=355.0s avg=50.7s
