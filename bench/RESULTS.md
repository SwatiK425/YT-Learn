# Bench run log — audit trail for perf-quality-v1

Each run: baseline = prod-snapshot (master @ ac0caaf) on :8003,
candidate = perf-quality-v1 branch on :8004. Same fixtures, same model/key.
Full raw outputs under bench/results/<run_id>/ (gitignored).

| run | tag | change under test | baseline total | candidate total | verdict |
|-----|-----|-------------------|----------------|-----------------|---------|

(append-only — the harness appends a section per run)
