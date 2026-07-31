#!/usr/bin/env python3
"""
Praxis Analytics Collector — READ-ONLY on app logs.

Parses backend/traces.log (append-only, byte-offset tailing) + backend/feedback.json,
normalizes into analytics.db (SQLite), and emits stats.json for the dashboard.

Zero code changes to the app: this never writes to backend/, only to analytics/.

Usage:
    python3 collector.py            # incremental run (tails from last offset)
    python3 collector.py --full     # re-parse everything from byte 0
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TRACES = os.path.join(HERE, "..", "backend", "traces.log")
FEEDBACK = os.path.join(HERE, "..", "backend", "feedback.json")
DB = os.path.join(HERE, "analytics.db")
OFFSET = os.path.join(HERE, ".trace_offset")
OUT = os.path.join(HERE, "stats.json")

# [2026-07-30 23:54:17.555 PDT][7351896b6ca4][endpoint][start]
HEADER_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ([A-Z]+)\]\[([0-9a-f]+)\]\[([a-z_]+)\]\[([a-z_]+)\]")
FIELD_RE = re.compile(r"^  ([a-z_][a-z0-9_]*): (.*)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    trace_id    TEXT PRIMARY KEY,
    ts          TEXT,        -- PDT "YYYY-MM-DD HH:MM:SS.mmm"
    endpoint    TEXT,        -- /api/suggest | /api/suggest/stream
    user_id     TEXT,        -- u_... | anon
    video_id    TEXT,
    model       TEXT,
    outcome     TEXT,        -- success | blocked | error | cache_hit | in_progress
    reason      TEXT,        -- blocked reason / error message
    category    TEXT,        -- blocked category
    experiment_id TEXT,
    elapsed_sec REAL,
    retry_reason TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    experiment_id TEXT PRIMARY KEY,
    user_id     TEXT,
    liked       INTEGER,     -- 1 liked, 0 disliked, NULL skipped
    comment     TEXT,
    ts          TEXT
);
"""


def parse_trace_blocks(path, start_offset=0):
    """Yield (ts, tz, trace_id, component, phase, fields) for complete blocks.

    A block = header line + indented key: value lines. A block is "complete"
    if at least one field line followed; a lone header at EOF is skipped (and
    its offset NOT advanced, so it is retried next run).
    """
    blocks = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        pos = start_offset
        buf = None  # current block dict
        while True:
            raw = f.readline()
            if raw == "":
                break
            line = raw.rstrip("\n")
            m = HEADER_RE.match(line)
            if m:
                if buf is not None:
                    blocks.append(buf)
                buf = {
                    "ts": m.group(1),
                    "tz": m.group(2),
                    "trace_id": m.group(3),
                    "component": m.group(4),
                    "phase": m.group(5),
                    "fields": {},
                    "start": pos,
                }
            elif buf is not None:
                fm = FIELD_RE.match(line)
                if fm:
                    buf["fields"][fm.group(1)] = fm.group(2)
                # multi-line values (indented continuation / "| " lines) ignored
            pos = f.tell()
            if buf is not None:
                buf["end"] = pos
        if buf is not None:
            blocks.append(buf)
    return blocks


def apply_request_block(rows, b):
    """Merge one trace block into the per-trace_id request row."""
    comp, phase, f = b["component"], b["phase"], b["fields"]
    tid = b["trace_id"]
    r = rows.setdefault(tid, {
        "trace_id": tid, "ts": b["ts"], "endpoint": None, "user_id": None,
        "video_id": None, "model": None, "outcome": "in_progress",
        "reason": None, "category": None, "experiment_id": None,
        "elapsed_sec": None, "retry_reason": None,
    })
    r["ts"] = b["ts"]  # keep earliest = request start

    if comp == "endpoint":
        if phase == "start":
            r["endpoint"] = f.get("endpoint") or r["endpoint"]
            r["user_id"] = f.get("user_id") or r["user_id"]
            r["retry_reason"] = f.get("retry_reason") or r["retry_reason"]
        elif phase == "end":
            r["outcome"] = "success"
            r["experiment_id"] = f.get("experiment_id") or r["experiment_id"]
            r["elapsed_sec"] = _f(f.get("elapsed_sec"), r["elapsed_sec"])
            r["video_id"] = f.get("video_id") or r["video_id"]
            r["model"] = f.get("model") or r["model"]
        elif phase == "blocked":
            r["outcome"] = "blocked"
            r["reason"] = f.get("reason") or r["reason"]
            r["category"] = f.get("category") or r["category"]
            r["elapsed_sec"] = _f(f.get("elapsed_sec"), r["elapsed_sec"])
            r["video_id"] = f.get("video_id") or r["video_id"]
            r["model"] = f.get("model") or r["model"]
        elif phase == "error":
            r["outcome"] = "error"
            r["reason"] = f.get("message") or r["reason"]
            r["elapsed_sec"] = _f(f.get("elapsed_sec"), r["elapsed_sec"])
            r["video_id"] = f.get("video_id") or r["video_id"]
            r["model"] = f.get("model") or r["model"]
        elif phase == "cache_hit":
            r["outcome"] = "cache_hit"
            r["video_id"] = f.get("video_id") or r["video_id"]
            r["model"] = f.get("model") or r["model"]
    elif comp == "transcript" and phase == "fetched":
        r["video_id"] = f.get("video_id") or r["video_id"]
    elif comp == "llm" and phase == "ready":
        r["model"] = f.get("model") or r["model"]
    return r


def apply_feedback_block(fb_rows, b):
    if b["component"] == "feedback" and b["phase"] == "received":
        f = b["fields"]
        liked = None
        if f.get("liked") == "True":
            liked = 1
        elif f.get("liked") == "False":
            liked = 0
        fb_rows[b["trace_id"]] = {  # trace_id == experiment_id here
            "experiment_id": b["trace_id"],
            "user_id": f.get("user_id"),
            "liked": liked,
            "comment": None,
            "ts": b["ts"],
        }
    return fb_rows


def _f(val, default=None):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def load_feedback_json():
    """feedback.json is the source of truth for comments; traces fill gaps."""
    if not os.path.exists(FEEDBACK):
        return {}
    try:
        with open(FEEDBACK, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = {}
        for rec in data:
            out[rec.get("experiment_id")] = {
                "experiment_id": rec.get("experiment_id"),
                "user_id": rec.get("user_id"),
                "liked": rec.get("liked"),
                "comment": rec.get("comment"),
                "ts": rec.get("timestamp_pst"),
            }
        return out
    except Exception:
        return {}


def main():
    full = "--full" in sys.argv
    if not os.path.exists(TRACES):
        print(f"ERROR: {TRACES} not found")
        sys.exit(1)

    # Byte-offset tailing; --full or rotation resets to 0.
    start = 0
    size = os.path.getsize(TRACES)
    if not full and os.path.exists(OFFSET):
        try:
            start = int(open(OFFSET).read().strip())
        except Exception:
            start = 0
        if start > size:  # log rotated/truncated
            start = 0

    blocks = parse_trace_blocks(TRACES, start)
    if not blocks:
        # Nothing new — still rebuild stats from DB.
        print("no new trace blocks (offset={})".format(start))
        rebuild_stats()
        return

    rows = {}
    fb_rows = {}
    for b in blocks:
        apply_request_block(rows, b)
        apply_feedback_block(fb_rows, b)

    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    for r in rows.values():
        conn.execute(
            """INSERT INTO requests (trace_id, ts, endpoint, user_id, video_id, model,
               outcome, reason, category, experiment_id, elapsed_sec, retry_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trace_id) DO UPDATE SET
                 ts=excluded.ts, endpoint=excluded.endpoint, user_id=excluded.user_id,
                 video_id=excluded.video_id, model=excluded.model, outcome=excluded.outcome,
                 reason=excluded.reason, category=excluded.category,
                 experiment_id=excluded.experiment_id, elapsed_sec=excluded.elapsed_sec,
                 retry_reason=excluded.retry_reason""",
            (r["trace_id"], r["ts"], r["endpoint"], r["user_id"], r["video_id"],
             r["model"], r["outcome"], r["reason"], r["category"], r["experiment_id"],
             r["elapsed_sec"], r["retry_reason"]),
        )
    # Feedback from traces (may lack comments) — merge with feedback.json below.
    for k, v in fb_rows.items():
        conn.execute(
            """INSERT INTO feedback (experiment_id, user_id, liked, comment, ts)
               VALUES (?,?,?,?,?)
               ON CONFLICT(experiment_id) DO UPDATE SET
                 user_id=excluded.user_id, liked=excluded.liked,
                 comment=excluded.comment, ts=excluded.ts""",
            (v["experiment_id"], v["user_id"], v["liked"], v["comment"], v["ts"]),
        )
    conn.commit()

    # feedback.json overrides/enriches (comments + canonical liked).
    fb_json = load_feedback_json()
    for k, v in fb_json.items():
        conn.execute(
            """INSERT INTO feedback (experiment_id, user_id, liked, comment, ts)
               VALUES (?,?,?,?,?)
               ON CONFLICT(experiment_id) DO UPDATE SET
                 user_id=excluded.user_id, liked=excluded.liked,
                 comment=excluded.comment, ts=excluded.ts""",
            (v["experiment_id"], v["user_id"], v["liked"], v["comment"], v["ts"]),
        )
    conn.commit()

    with open(OFFSET, "w") as f:
        f.write(str(size))

    print(f"parsed {len(blocks)} new blocks, {len(rows)} requests, offset={size}")
    rebuild_stats(conn)
    conn.close()


def rebuild_stats(conn=None):
    """Aggregate analytics.db -> stats.json (idempotent, cheap)."""
    if conn is None:
        conn = sqlite3.connect(DB)
        conn.executescript(SCHEMA)

    reqs = conn.execute(
        "SELECT ts, user_id, outcome, elapsed_sec, model, video_id, endpoint FROM requests"
    ).fetchall()
    fbs = conn.execute("SELECT user_id, liked, comment, ts FROM feedback").fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    d7 = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    d30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    def day(s):
        return (s or "")[:10]

    users_all, users_7, users_30 = set(), set(), set()
    daily = {}   # date -> counters
    per_user = {}  # user_id -> counters
    lat = {"success": [], "blocked": [], "error": []}
    models, videos, endpoints = {}, {}, {}
    blocked_reasons = {}

    for ts, uid, outcome, elapsed, model, vid, ep in reqs:
        d = day(ts)
        dd = daily.setdefault(d, {
            "date": d, "requests": 0, "success": 0, "blocked": 0,
            "error": 0, "cache_hit": 0, "in_progress": 0, "users": set(), "latency_sum": 0.0, "latency_n": 0,
        })
        dd["requests"] += 1
        dd[outcome or "in_progress"] += 1
        if uid and uid != "anon":
            dd["users"].add(uid)
            users_all.add(uid)
            if d >= d7:
                users_7.add(uid)
            if d >= d30:
                users_30.add(uid)
        pu = per_user.setdefault(uid or "anon", {
            "user_id": uid or "anon", "requests": 0, "success": 0, "blocked": 0,
            "error": 0, "cache_hit": 0, "in_progress": 0, "last_ts": None, "first_ts": None,
        })
        pu["requests"] += 1
        pu[outcome or "in_progress"] += 1
        if ts:
            if not pu["first_ts"] or ts < pu["first_ts"]:
                pu["first_ts"] = ts
            if not pu["last_ts"] or ts > pu["last_ts"]:
                pu["last_ts"] = ts
        if elapsed is not None and outcome in lat:
            lat[outcome].append(elapsed)
            dd["latency_sum"] += elapsed
            dd["latency_n"] += 1
        if model:
            models[model] = models.get(model, 0) + 1
        if vid:
            videos[vid] = videos.get(vid, 0) + 1
        if ep:
            endpoints[ep] = endpoints.get(ep, 0) + 1

    # ---- blocked-reason pass (separate query for the reason field) ----
    blocked_reasons = {}
    for row in conn.execute("SELECT reason FROM requests WHERE outcome='blocked'"):
        r = row[0] or "unknown"
        blocked_reasons[r] = blocked_reasons.get(r, 0) + 1

    # ---- daily series (sorted) ----
    series = []
    cum_users = set()
    for d in sorted(daily):
        dd = daily[d]
        cum_users |= dd["users"]
        series.append({
            "date": d,
            "requests": dd["requests"],
            "success": dd["success"],
            "blocked": dd["blocked"],
            "error": dd["error"],
            "cache_hit": dd["cache_hit"],
            "in_progress": dd["in_progress"],
            "users": len(dd["users"]),
            "cumulative_users": len(cum_users),
            "avg_latency": round(dd["latency_sum"] / dd["latency_n"], 1) if dd["latency_n"] else None,
        })

    total_req = sum(d["requests"] for d in daily.values())
    ok = sum(d["success"] for d in daily.values())
    blocked = sum(d["blocked"] for d in daily.values())
    err = sum(d["error"] for d in daily.values())
    cached = sum(d["cache_hit"] for d in daily.values())

    all_lat = lat["success"] + lat["blocked"] + lat["error"]

    stats = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": {
            "distinct_users": len(users_all),
            "distinct_users_7d": len(users_7),
            "distinct_users_30d": len(users_30),
            "total_requests": total_req,
            "success": ok,
            "blocked": blocked,
            "error": err,
            "cache_hit": cached,
            "success_rate": round(ok / total_req * 100, 1) if total_req else 0,
            "avg_latency_sec": round(sum(all_lat) / len(all_lat), 1) if all_lat else None,
            "feedback_liked": sum(1 for f in fbs if f[1] == 1),
            "feedback_disliked": sum(1 for f in fbs if f[1] == 0),
        },
        "daily": series,
        "per_user": sorted(per_user.values(), key=lambda p: p["requests"], reverse=True),
        "feedback": sorted(
            [{"user_id": f[0], "liked": f[1], "comment": f[2], "ts": f[3]} for f in fbs],
            key=lambda x: x["ts"] or "", reverse=True,
        ),
        "blocked_reasons": sorted(blocked_reasons.items(), key=lambda kv: kv[1], reverse=True),
        "models": sorted(models.items(), key=lambda kv: kv[1], reverse=True),
        "top_videos": sorted(videos.items(), key=lambda kv: kv[1], reverse=True)[:10],
        "endpoints": sorted(endpoints.items(), key=lambda kv: kv[1], reverse=True),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print(f"stats.json written ({len(series)} days, {len(users_all)} users, {total_req} requests)")


if __name__ == "__main__":
    main()
