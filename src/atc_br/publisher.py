"""Push the live backend to Supabase so the public site works without reaching this Mac.

No SQL needed: live events go over Realtime broadcast (channel "sbgr"), the snapshot a new visitor loads
first is a JSON file in Storage (live/hello.json), and audio clips are uploaded to the public "clips" bucket.
Enabled when SUPABASE_URL + SUPABASE_SERVICE_KEY are in .env.
"""
import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SNAPSHOT_EVERY = 10     # seconds between live/hello.json uploads
CLIP_DAYS = 7           # clips older than this are deleted from Storage


def load_env(root: Path) -> dict:
    """.env on the Mac; real environment variables (Space secrets) win."""
    import os
    env = {}
    if (root / ".env").exists():
        for line in (root / ".env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith(("SUPABASE_", "GROQ_", "HF_"))})
    return env


class Publisher:
    def __init__(self, url, key, data_dir: Path, log=print):
        self.url, self.data, self.log = url.rstrip("/"), data_dir, log
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.q: queue.Queue = queue.Queue(maxsize=2000)
        self.hello = {}
        self.down_until, self.backoff = 0.0, 5
        self.ok = self._ensure_buckets()
        for target in (self._sender, self._snapshots, self._janitor):
            threading.Thread(target=target, daemon=True).start()

    # ---------------------------------------------------------------- setup
    def _ensure_buckets(self):
        try:
            have = {b["id"] for b in requests.get(f"{self.url}/storage/v1/bucket", headers=self.h, timeout=15).json()}
            for b in ("live", "clips"):
                if b not in have:
                    requests.post(f"{self.url}/storage/v1/bucket", headers=self.h, json={"id": b, "name": b, "public": True}, timeout=15)
            self.log("[supabase] connected, buckets ready")
            return True
        except Exception as e:
            self.log(f"[supabase] setup failed: {e}")
            return False

    # ---------------------------------------------------------------- public API (called from the pipeline)
    def event(self, name, payload):
        if name == "state":
            self.hello["state"] = payload
        elif name == "flights":
            self.hello["flights"] = payload
        elif name == "tx":
            rec = self.hello.setdefault("recent", [])
            rec.append(payload)
            del rec[:-40]
            if payload.get("clip"):
                self.q.put(("clip", payload["clip"]))
        elif name == "alert":
            al = self.hello.setdefault("alerts", [])
            al.append(payload)
            del al[:-20]
        if name in ("state", "tx", "alert", "flights", "vox"):
            try:
                self.q.put_nowait(("bc", name, payload))
            except queue.Full:
                pass

    def clip_url(self, rel):
        return f"{self.url}/storage/v1/object/public/clips/{rel.removeprefix('clips/')}"

    # ---------------------------------------------------------------- outage backoff
    def _down(self):
        return time.time() < self.down_until

    def _fail(self, what, e):
        """Log once per outage, then go quiet with exponential backoff (max 10 min)."""
        if not self._down():
            self.log(f"[supabase] {what}: {e} — backing off {self.backoff}s")
            self.down_until = time.time() + self.backoff
            self.backoff = min(self.backoff * 2, 600)

    def _recovered(self):
        if self.backoff > 5:
            self.log("[supabase] reachable again")
        self.backoff = 5

    # ---------------------------------------------------------------- workers
    def _sender(self):
        while True:
            item = self.q.get()
            if self._down():
                continue  # live events are ephemeral; clips stay on disk
            try:
                if item[0] == "bc":
                    _, ev, payload = item
                    if ev == "tx" and payload.get("clip"):
                        payload = {**payload, "clip_url": self.clip_url(payload["clip"])}
                    requests.post(f"{self.url}/realtime/v1/api/broadcast", headers=self.h, timeout=10,
                                  json={"messages": [{"topic": "sbgr", "event": ev, "payload": payload}]})
                elif item[0] == "clip":
                    rel = item[1]
                    p = self.data / rel
                    if p.exists():
                        requests.post(f"{self.url}/storage/v1/object/clips/{rel.removeprefix('clips/')}", timeout=20,
                                      headers={**self.h, "Content-Type": "audio/ogg", "x-upsert": "true"}, data=p.read_bytes())
                self._recovered()
            except Exception as e:
                self._fail(item[0], e)

    def _snapshots(self):
        while True:
            time.sleep(SNAPSHOT_EVERY)
            if not self.hello or self._down():
                continue
            body = dict(self.hello)
            body["recent"] = [{**t, "clip_url": self.clip_url(t["clip"])} if t.get("clip") else t for t in body.get("recent", [])]
            try:
                requests.post(f"{self.url}/storage/v1/object/live/hello.json", timeout=20,
                              headers={**self.h, "Content-Type": "application/json", "x-upsert": "true", "cache-control": "max-age=5"},
                              data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
                self._recovered()
            except Exception as e:
                self._fail("snapshot", e)

    def _janitor(self):
        """Delete clip folders older than CLIP_DAYS (they stay on the Mac for the archive export)."""
        while True:
            try:
                cut = (datetime.now(timezone.utc) - timedelta(days=CLIP_DAYS)).strftime("%Y-%m-%d")
                for feed in requests.post(f"{self.url}/storage/v1/object/list/clips", headers=self.h, timeout=20,
                                          json={"prefix": "SBGR/", "limit": 100}).json():
                    days = requests.post(f"{self.url}/storage/v1/object/list/clips", headers=self.h, timeout=20,
                                         json={"prefix": f"SBGR/{feed['name']}/", "limit": 1000}).json()
                    for d in days:
                        if d["name"] < cut:
                            files = requests.post(f"{self.url}/storage/v1/object/list/clips", headers=self.h, timeout=20,
                                                  json={"prefix": f"SBGR/{feed['name']}/{d['name']}/", "limit": 10000}).json()
                            paths = [f"SBGR/{feed['name']}/{d['name']}/{f['name']}" for f in files]
                            if paths:
                                requests.delete(f"{self.url}/storage/v1/object/clips", headers=self.h, json={"prefixes": paths}, timeout=60)
                                self.log(f"[supabase] pruned {len(paths)} clips from {d['name']}")
            except Exception as e:
                self.log(f"[supabase] janitor: {e}")
            time.sleep(6 * 3600)
