#!/usr/bin/env python3
"""bench/run.py — side-by-side A/B harness: baseline (prod-snapshot) vs candidate (branch).

Usage:
    python bench/run.py                    # full run: baseline + candidate
    python bench/run.py --side baseline    # only baseline
    python bench/run.py --side candidate   # only candidate
    python bench/run.py --fixtures vid1 vid2   # subset of fixtures by video id
    python bench/run.py --tag p0-cache     # custom run tag for RESULTS.md

Requirements:
  - bench/.env with:
        ZEN_API_KEY=<opencode-zen api key>
        MODEL=deepseek-v4-flash-free
        PROVIDER=opencode-zen
  - Baseline server checkout at ../YT-Learn-baseline (git worktree, prod-snapshot).
  - Candidate server = this repo (perf-quality-v1 branch) backend/app.py.
  - Fixtures in bench/fixtures/*.txt (see bench/capture.py).

The harness starts BOTH servers itself (baseline :8003, candidate :8004),
replays every fixture to both, records latency + output, parses each server's
own traces.log for stage breakdown, and appends a summary to bench/RESULTS.md.
Results land in bench/results/<run_id>/ (gitignored).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent
BASELINE_DIR = REPO_DIR.parent / "YT-Learn-baseline"
BACKEND_DIR = REPO_DIR / "backend"
RESULTS_DIR = BENCH_DIR / "results"
FIXTURES_DIR = BENCH_DIR / "fixtures"
MANIFEST = BENCH_DIR / "manifest.json"

BASELINE_PORT = 8003
CANDIDATE_PORT = 8004

# Match the backend's trace format: entries start with a header line, fields follow indented
TRACE_HEADER = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) PDT\]\[([a-f0-9]+)\]\[([a-z_]+)\]\[([a-z_]+)\]\n"
)
FIELD = re.compile(r"^  ([a-z_]+): (.*)$", re.MULTILINE)

FORBIDDEN = ("reflect", "think about ", "imagine ", "consider ", "understand ")


def load_env() -> dict:
    env = {}
    env_file = BENCH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in ("ZEN_API_KEY", "MODEL") if not env.get(k)]
    if missing:
        print(f"✗ bench/.env missing keys: {missing} (copy bench/.env.example -> bench/.env)")
        sys.exit(2)
    return env


def wait_health(port: int, timeout: float = 60.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def start_server(port: int, cwd: Path, label: str) -> subprocess.Popen:
    """Start uvicorn in a dedicated backend dir so its traces.log is isolated."""
    log = open(RESULTS_DIR / f"server-{label}.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
        cwd=str(cwd / "backend"),
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return proc


def load_fixtures(subset: list[str] | None) -> list[dict]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"videos": []}
    fixtures = []
    for v in manifest["videos"]:
        if subset and v["id"] not in subset:
            continue
        fpath = FIXTURES_DIR / f"{v['id']}.txt"
        if not fpath.exists():
            print(f"  ! fixture missing: {fpath.name} (run bench/capture.py)")
            continue
        fixtures.append({
            "id": v["id"],
            "url": v["url"],
            "transcript": fpath.read_text(encoding="utf-8"),
            "variants": v.get("variants", ["none"]),
        })
    return fixtures


def post_suggest(port: int, fixture: dict, variant: str, env: dict, force: bool = True) -> dict:
    body = {
        "video_url": fixture["url"],
        "transcript": fixture["transcript"],
        "retry_reason": None if variant == "none" else variant,
        "force": force,
        "llm": {
            "provider": env.get("PROVIDER", "opencode-zen"),
            "model": env["MODEL"],
            "api_key": env["ZEN_API_KEY"],
        },
    }
    url = f"http://127.0.0.1:{port}/api/suggest"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read().decode()
            elapsed = time.time() - t0
            return {"http": r.status, "elapsed": round(elapsed, 2), "body": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return {"http": e.code, "elapsed": round(time.time() - t0, 2), "body": raw}
    except Exception as e:
        return {"http": 0, "elapsed": round(time.time() - t0, 2), "body": str(e)}


def quality_checks(body: str) -> dict:
    """Automated output-quality heuristics on the response JSON."""
    checks = {"json": False, "critic_pass": None, "forbidden": [], "steps": None, "done_criteria": None, "blocked": False}
    try:
        data = json.loads(body)
        checks["json"] = True
        if data.get("status") == "blocked":
            checks["blocked"] = True
            return checks
        steps = data.get("steps") or []
        if isinstance(steps, str):
            steps = [s for s in steps.split("\n") if s.strip()]
        checks["steps"] = len(steps)
        dc = data.get("done_criteria_list") or []
        checks["done_criteria"] = len(dc) if isinstance(dc, list) else 1
        joined = " ".join(steps).lower()
        checks["forbidden"] = [w for w in FORBIDDEN if w in joined]
        checks["critic_pass"] = len(checks["forbidden"]) == 0
    except Exception:
        pass
    return checks


def parse_traces(trace_path: Path, start_pos: int) -> dict:
    """Stage breakdown from a server's traces.log, parsed from byte offset
    start_pos to EOF (timezone-proof: no wall-clock window comparison)."""
    if not trace_path.exists():
        return {}
    stats = {"stage1": [], "stage2": [], "retries": 0, "rejects": 0, "pipeline": []}
    with open(trace_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_pos)
        text = f.read()
    headers = list(TRACE_HEADER.finditer(text))
    for i, m in enumerate(headers):
        ts, tid, comp, phase = m.groups()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[m.end():body_end]
        fields = {}
        for fm in FIELD.finditer(body):
            fields[fm.group(1)] = fm.group(2)
        if comp == "llm" and phase == "output":
            try:
                dur = float(fields.get("elapsed_sec"))
            except (TypeError, ValueError):
                continue
            label = fields.get("call", "")
            if label == "stage1_analysis":
                stats["stage1"].append(dur)
            elif label.startswith("stage2"):
                stats["stage2"].append(dur)
                if "attempt1" in label:
                    stats["retries"] += 1
        elif comp == "critic" and phase == "reject":
            stats["rejects"] += 1
    return stats


def avg(xs: list) -> float:
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def summarize(side: str, results: list[dict], stats: dict, run_id: str) -> str:
    lines = [f"### {side} — {run_id}"]
    total = sum(r["elapsed"] for r in results)
    n = len(results)
    for r in results:
        q = r["checks"]
        line = (f"- `{r['id']}` v={r['variant']}: {r['elapsed']}s http={r['http']} "
                f"steps={q['steps']} dc={q['done_criteria']} forbidden={q['forbidden'] or '-'}")
        lines.append(line)
    lines.append(f"- TOTALS: n={n} sum={round(total,1)}s avg={round(total/n,1) if n else 0}s")
    if stats:
        lines.append(f"- stage1 avg={avg(stats['stage1'])}s (n={len(stats['stage1'])}) | "
                     f"stage2 avg={avg(stats['stage2'])}s (n={len(stats['stage2'])}, retries={stats['retries']}) | "
                     f"critic rejects={stats['rejects']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["baseline", "candidate", "both"], default="both")
    ap.add_argument("--fixtures", nargs="*", default=None)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    env = load_env()
    RESULTS_DIR.mkdir(exist_ok=True)
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}" + (f"-{args.tag}" if args.tag else "")

    fixtures = load_fixtures(args.fixtures)
    if not fixtures:
        print("✗ no fixtures. Run: python bench/capture.py --all")
        sys.exit(2)
    print(f"fixtures: {[f['id'] for f in fixtures]}")

    # Start servers
    procs = {}
    if args.side in ("baseline", "both"):
        print(f"starting baseline on :{BASELINE_PORT} (from {BASELINE_DIR.name}) ...")
        procs["baseline"] = start_server(BASELINE_PORT, BASELINE_DIR, "baseline")
    if args.side in ("candidate", "both"):
        print(f"starting candidate on :{CANDIDATE_PORT} (from {REPO_DIR.name}) ...")
        procs["candidate"] = start_server(CANDIDATE_PORT, REPO_DIR, "candidate")

    for label, proc in procs.items():
        port = BASELINE_PORT if label == "baseline" else CANDIDATE_PORT
        if not wait_health(port):
            print(f"✗ {label} server failed health check on :{port}")
            for p in procs.values():
                p.terminate()
            sys.exit(1)
        print(f"  ✓ {label} healthy on :{port}")

    start_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trace_offsets = {}
    for label in procs:
        trace_path = (BASELINE_DIR if label == "baseline" else REPO_DIR) / "backend" / "traces.log"
        trace_offsets[label] = trace_path.stat().st_size if trace_path.exists() else 0

    # Run all fixtures against the chosen sides
    side_results = {side: [] for side in procs}
    for fx in fixtures:
        for variant in fx["variants"]:
            for label in procs:
                port = BASELINE_PORT if label == "baseline" else CANDIDATE_PORT
                resp = post_suggest(port, fx, variant, env)
                checks = quality_checks(resp["body"])
                # save raw output
                outdir = RESULTS_DIR / run_id / label
                outdir.mkdir(parents=True, exist_ok=True)
                (outdir / f"{fx['id']}__{variant}.json").write_text(
                    json.dumps({"resp": resp, "checks": checks}, indent=2), encoding="utf-8"
                )
                side_results[label].append({"id": fx["id"], "variant": variant, **resp, "checks": checks})
                q = checks
                print(f"  [{label:9s}] {fx['id']} v={variant:8s} {resp['elapsed']:6.1f}s "
                      f"steps={q['steps']} forbidden={q['forbidden'] or '-'}")
                time.sleep(0.4)  # be gentle with free-tier rate limits

    end_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Stage breakdown from each server's own traces.log (offset -> EOF, no wall-clock)
    for label in procs:
        trace_path = (BASELINE_DIR if label == "baseline" else REPO_DIR) / "backend" / "traces.log"
        stats = parse_traces(trace_path, trace_offsets[label])
        summary = summarize(label, side_results[label], stats, run_id)
        print(f"\n{summary}\n")
        (RESULTS_DIR / run_id / f"summary-{label}.md").write_text(summary, encoding="utf-8")

    # Append to RESULTS.md
    results_md = BENCH_DIR / "RESULTS.md"
    with open(results_md, "a", encoding="utf-8") as f:
        f.write(f"\n## Run {run_id}\n\n")
        for label in procs:
            f.write(summarize(label, side_results[label], {}, run_id) + "\n")

    for p in procs.values():
        p.terminate()
    print(f"done -> {RESULTS_DIR / run_id}")


if __name__ == "__main__":
    main()
