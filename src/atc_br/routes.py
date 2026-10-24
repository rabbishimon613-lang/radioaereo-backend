"""Callsign → airline + origin/destination via adsbdb.com (free, no key). Cached on disk, rate-limited."""
import json
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "routes.json"


class Routes:
    def __init__(self, log=print):
        self.log = log
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.q, self.lock = [], threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def get(self, cs):
        if not cs:
            return None
        r = self.cache.get(cs)
        if r is None and cs not in self.q:
            with self.lock:
                self.q.append(cs)
        return r or None

    def _run(self):
        while True:
            time.sleep(1.2)
            with self.lock:
                cs = self.q.pop(0) if self.q else None
            if not cs:
                continue
            try:
                with urlopen(Request(f"https://api.adsbdb.com/v0/callsign/{cs}", headers={"User-Agent": "atc-br/0.1"}), timeout=10) as r:
                    fr = json.load(r).get("response", {}).get("flightroute")
            except Exception:
                fr = None
            if isinstance(fr, dict):
                pick = lambda a: {"icao": a.get("icao_code"), "iata": a.get("iata_code"), "city": a.get("municipality"), "name": a.get("name")}
                self.cache[cs] = {"airline": (fr.get("airline") or {}).get("name"), "from": pick(fr["origin"]), "to": pick(fr["destination"])}
            else:
                self.cache[cs] = {}
            CACHE.parent.mkdir(exist_ok=True)
            CACHE.write_text(json.dumps(self.cache, ensure_ascii=False))
