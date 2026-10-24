"""Live aircraft around an airport: adsb.lol primary, OpenSky fallback. Keeps short trails + daily gz log."""
import gzip
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

TRAIL_SECONDS = 600
LOG_EVERY = 10  # seconds between positions written to disk


def _get(url: str) -> dict:
    with urlopen(Request(url, headers={"User-Agent": "atc-br/0.1"}), timeout=10) as r:
        return json.load(r)


def _from_adsblol(lat, lon, nm):
    for a in _get(f"https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}").get("ac", []):
        if a.get("lat") is None:
            continue
        alt = a.get("alt_baro")
        yield {
            "hex": a["hex"], "callsign": (a.get("flight") or "").strip(), "reg": a.get("r"), "type": a.get("t"),
            "lat": a["lat"], "lon": a["lon"], "alt": 0 if alt == "ground" else alt, "ground": alt == "ground",
            "gs": a.get("gs"), "track": a.get("track") or a.get("true_heading"), "vs": a.get("baro_rate"),
            "squawk": a.get("squawk"), "category": a.get("category"), "emergency": a.get("emergency"),
        }


def _from_opensky(lat, lon, nm):
    d = nm / 60
    url = (f"https://opensky-network.org/api/states/all?lamin={lat - d}&lomin={lon - d}"
           f"&lamax={lat + d}&lomax={lon + d}")
    for s in _get(url).get("states") or []:
        if s[6] is None:
            continue
        yield {
            "hex": s[0], "callsign": (s[1] or "").strip(), "reg": None, "type": None,
            "lat": s[6], "lon": s[5], "alt": round((s[7] or 0) * 3.281), "ground": s[8],
            "gs": round((s[9] or 0) * 1.944), "track": s[10], "vs": round((s[11] or 0) * 196.85),
            "squawk": s[14],
        }


class Tracker:
    def __init__(self, lat: float, lon: float, radius_nm: int, log_dir: Path, interval: float = 10, log=print):
        self.lat, self.lon, self.nm, self.interval, self.log = lat, lon, radius_nm, interval, log
        self.log_dir = log_dir
        self.aircraft: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._last_log = 0.0

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def snapshot(self, trails: bool = True) -> list[dict]:
        """trails=False for the live stream — clients grow their own trails, keeping each push tiny."""
        with self.lock:
            if trails:
                return [dict(a, trail=list(a["trail"])) for a in self.aircraft.values()]
            return [{k: v for k, v in a.items() if k != "trail"} for a in self.aircraft.values()]

    def callsigns(self) -> list[str]:
        with self.lock:
            return [a["callsign"] for a in self.aircraft.values() if a["callsign"]]

    def _run(self):
        source, wait = None, self.interval
        while True:
            for name, fetch in (("adsb.lol", _from_adsblol), ("OpenSky", _from_opensky)):
                try:
                    rows = list(fetch(self.lat, self.lon, self.nm))
                    break
                except Exception as e:
                    err = f"{name}: {e}"
            else:
                rows, name = [], None
            if name != source:  # log source changes only, not every failed poll
                self.log(f"[adsb] source → {name or 'NONE (' + err + ')'}")
                source = name
            wait = self.interval if name == "adsb.lol" else 30  # give adsb.lol's rate limit time to reset
            self._update(rows)
            time.sleep(wait)

    def _update(self, rows: list[dict]):
        now = time.time()
        with self.lock:
            for r in rows:
                a = self.aircraft.setdefault(r["hex"], {"trail": []})
                a.update(r, seen=now)
                if not a["trail"] or a["trail"][-1][:2] != [r["lon"], r["lat"]]:
                    a["trail"].append([r["lon"], r["lat"], r["alt"] or 0, round(now)])
                a["trail"] = [p for p in a["trail"] if now - p[3] < TRAIL_SECONDS]
            for h in [h for h, a in self.aircraft.items() if now - a["seen"] > 60]:
                del self.aircraft[h]
        if now - self._last_log >= LOG_EVERY and rows:
            self._last_log = now
            self.log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with gzip.open(self.log_dir / f"{day}.jsonl.gz", "at") as f:
                f.write(json.dumps({"t": round(now), "ac": rows}, separators=(",", ":")) + "\n")
