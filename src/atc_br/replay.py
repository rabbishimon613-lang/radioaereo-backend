"""Delayed-live feed for the website: every event the pipeline publishes, re-timed to when it happened, in 10-minute chunks.

Live, each source arrives with its own lag (stream buffer, end-of-transmission VAD, Groq batching, ADS-B poll age), so a
transcript lands after the plane has moved on. The site plays these chunks one hour behind on a single clock instead:
transcripts at the second the transmission started, audio from the same clip, aircraft from the same moment.

Storage (Supabase, public bucket "replay"):  SBGR/<YYYY-MM-DD>/<HHMM>.json.gz  =  gzip({"t0", "t1", "ev": [[t, event, payload], ...]})
(gzip so a viewer pulls ~100 KB per 10 min instead of ~1 MB; the browser inflates it with DecompressionStream)
"""
import gzip
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

CHUNK = 600          # seconds per file
SETTLE = 120         # wait this long after a chunk ends for late transcriptions before uploading it
STATE_EVERY = 4      # seconds between aircraft snapshots kept (the pipeline publishes every 2 s)
KEEP_H = 26          # hours kept in the bucket


class Replay:
    def __init__(self, url, key, clip_url, log=print):
        self.url, self.clip_url, self.log = url.rstrip("/"), clip_url, log
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.chunks: dict[int, list] = {}
        self.lock = threading.Lock()
        self.last_state = 0.0
        try:
            have = {b["id"] for b in requests.get(f"{self.url}/storage/v1/bucket", headers=self.h, timeout=15).json()}
            if "replay" not in have:
                requests.post(f"{self.url}/storage/v1/bucket", headers=self.h, timeout=15,
                              json={"id": "replay", "name": "replay", "public": True})
        except Exception as e:
            log(f"[replay] bucket: {e}")
        threading.Thread(target=self._run, daemon=True).start()

    def event(self, name, payload):
        """Called for every published event; files it under the moment it describes."""
        if name == "state":
            if payload["t"] - self.last_state < STATE_EVERY:
                return
            self.last_state = t = payload["t"]
        elif name == "tx":
            t = payload["t"]                                       # start of the transmission, not when Groq answered
            if payload.get("clip"):
                payload = {**payload, "clip_url": self.clip_url(payload["clip"])}
        elif name == "vox":
            t = payload["start"]
        elif name in ("alert", "flights"):
            t = payload.get("t", time.time()) if isinstance(payload, dict) else time.time()
        else:
            return
        with self.lock:
            self.chunks.setdefault(int(t // CHUNK) * CHUNK, []).append([round(t, 2), name, payload])

    def _run(self):
        last_prune = 0
        while True:
            time.sleep(20)
            now = time.time()
            with self.lock:
                ready = [c for c in self.chunks if c + CHUNK + SETTLE <= now]
                done = {c: self.chunks.pop(c) for c in ready}
            for c, ev in sorted(done.items()):
                ev.sort(key=lambda e: e[0])
                d = datetime.fromtimestamp(c, timezone.utc)
                path = f"SBGR/{d:%Y-%m-%d}/{d:%H%M}.json.gz"
                body = gzip.compress(json.dumps({"t0": c, "t1": c + CHUNK, "ev": ev}, ensure_ascii=False,
                                                separators=(",", ":")).encode(), 6)
                try:
                    r = requests.post(f"{self.url}/storage/v1/object/replay/{path}", data=body, timeout=60,
                                      headers={**self.h, "Content-Type": "application/gzip", "x-upsert": "true",
                                               "cache-control": "max-age=86400"})
                    r.raise_for_status()
                    self.log(f"[replay] {path} ({len(ev)} events, {len(body) // 1024} KB)")
                except Exception as e:
                    self.log(f"[replay] upload {path}: {e}")
            if now - last_prune > 3600:
                last_prune = now
                self._prune()

    def _prune(self):
        cut = datetime.now(timezone.utc) - timedelta(hours=KEEP_H)
        try:
            for day in requests.post(f"{self.url}/storage/v1/object/list/replay", headers=self.h, timeout=20,
                                     json={"prefix": "SBGR/", "limit": 100}).json():
                files = requests.post(f"{self.url}/storage/v1/object/list/replay", headers=self.h, timeout=20,
                                      json={"prefix": f"SBGR/{day['name']}/", "limit": 1000}).json()
                old = [f"SBGR/{day['name']}/{f['name']}" for f in files
                       if datetime.strptime(f"{day['name']} {f['name'][:4]}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc) < cut]
                if old:
                    requests.delete(f"{self.url}/storage/v1/object/replay", headers=self.h, json={"prefixes": old}, timeout=60)
        except Exception as e:
            self.log(f"[replay] prune: {e}")
