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

## Run 20260801-181141-p1c-mechanism

### baseline — 20260801-181141-p1c-mechanism
- `jNQXAC9IVRw` v=none: 4.35s http=200 steps=None dc=None forbidden=-
- `b2XBO1DrACc` v=none: 39.11s http=200 steps=5 dc=3 forbidden=-
- `ff3j4olCUig` v=none: 32.94s http=200 steps=5 dc=5 forbidden=-
- `SQ3fZ1sAqXI` v=none: 21.07s http=200 steps=None dc=None forbidden=-
- `_jGSgzBkzrY` v=none: 36.66s http=200 steps=5 dc=3 forbidden=-
- `kxLmeUIXXtU` v=none: 36.69s http=200 steps=5 dc=4 forbidden=-
- `8yE6G1Lup1s` v=none: 59.39s http=200 steps=5 dc=3 forbidden=-
- TOTALS: n=7 sum=230.2s avg=32.9s
### candidate — 20260801-181141-p1c-mechanism
- `jNQXAC9IVRw` v=none: 3.56s http=200 steps=None dc=None forbidden=-
- `b2XBO1DrACc` v=none: 38.76s http=200 steps=6 dc=3 forbidden=-
- `ff3j4olCUig` v=none: 29.96s http=200 steps=5 dc=4 forbidden=-
- `SQ3fZ1sAqXI` v=none: 39.31s http=200 steps=5 dc=5 forbidden=-
- `_jGSgzBkzrY` v=none: 70.58s http=200 steps=5 dc=4 forbidden=-
- `kxLmeUIXXtU` v=none: 37.39s http=200 steps=5 dc=5 forbidden=-
- `8yE6G1Lup1s` v=none: 56.39s http=200 steps=5 dc=4 forbidden=-
- TOTALS: n=7 sum=275.9s avg=39.4s
