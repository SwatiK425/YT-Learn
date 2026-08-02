#!/usr/bin/env python3
"""bench/capture.py — fetch transcripts ONCE and save them as local fixtures.

Usage:
    python bench/capture.py b2XBO1DrACc ff3j4olCUig ...
    python bench/capture.py --all   # capture the default fixture set

Each transcript is saved to bench/fixtures/<video_id>.txt and registered in
bench/manifest.json. Transcripts are test fixtures — never committed (gitignored).

Why fixtures: the A/B harness replays the SAME transcript text to both
instances, so transcript-fetch variance (and the Oracle IP block on server-side
fetches) can never pollute the comparison.
"""

import argparse
import asyncio
import json
import os
import sys

from youtube_transcript_api import YouTubeTranscriptApi

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(BENCH_DIR, "fixtures")
MANIFEST = os.path.join(BENCH_DIR, "manifest.json")

# Default set: pulled from production traces.log — deliberately diverse in
# length (217 chars to 714k) so the harness covers tiny, medium, and
# over-the-80k-cap transcripts.
DEFAULT_IDS = [
    "jNQXAC9IVRw",    # tiny (217 chars) — classic test video
    "b2XBO1DrACc",    # medium (25.8k)
    "ff3j4olCUig",    # medium (54.6k)
    "SQ3fZ1sAqXI",    # large (126k)
    "_jGSgzBkzrY",    # large (149k)
    "kxLmeUIXXtU",    # huge (473k)
    "8yE6G1Lup1s",    # monster (714k) — exercises the 80k cap hard
]


def load_manifest() -> dict:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {"videos": []}


def save_manifest(manifest: dict) -> None:
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


async def fetch_one(video_id: str) -> str | None:
    api = YouTubeTranscriptApi()
    try:
        segs = await asyncio.wait_for(
            asyncio.to_thread(
                api.fetch, video_id, languages=["en", "a.en", "hi", "a.hi", "es", "a.es"]
            ),
            timeout=30,
        )
        return " ".join(s.text for s in segs)
    except Exception as e:
        # Fallback: list and grab the first available transcript
        try:
            available = await asyncio.wait_for(
                asyncio.to_thread(lambda: list(api.list(video_id))), timeout=15
            )
            if available:
                segs = await asyncio.wait_for(
                    asyncio.to_thread(available[0].fetch), timeout=30
                )
                return " ".join(s.text for s in segs)
        except Exception:
            pass
        print(f"  ✗ {video_id}: {e}", flush=True)
        return None


async def main(ids: list[str]) -> int:
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    manifest = load_manifest()
    known = {v["id"] for v in manifest["videos"]}

    ok = 0
    for vid in ids:
        print(f"fetching {vid} ...", flush=True)
        text = await fetch_one(vid)
        if text is None:
            continue
        with open(os.path.join(FIXTURES_DIR, f"{vid}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        if vid not in known:
            manifest["videos"].append({
                "id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "chars": len(text),
                "variants": ["none"],   # extend later if needed
            })
        else:
            for v in manifest["videos"]:
                if v["id"] == vid:
                    v["chars"] = len(text)
        ok += 1
        print(f"  ✓ {vid}: {len(text)} chars", flush=True)

    save_manifest(manifest)
    print(f"\n{ok}/{len(ids)} captured -> {FIXTURES_DIR}")
    return 0 if ok == len(ids) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    ids = DEFAULT_IDS if args.all else args.ids
    if not ids:
        ap.print_help()
        sys.exit(2)
    sys.exit(asyncio.run(main(ids)))
