"""SBGR geometry in local meters + live aircraft state: phase, runway occupancy/clearances, holds, go-arounds."""
import json
import math
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAT0, LON0 = -23.4356, -46.4731
KX, KY = 111320 * math.cos(math.radians(LAT0)), 110540
FIELD_FT = 2459

_map = json.loads((ROOT / "web" / "sbgr.json").read_text())
RWY = {k: (tuple(v[0]), tuple(v[1])) for k, v in _map["runwayEnds"].items()}
STANDS = [(s[0], s[1], s[2]) for s in _map["airport"]["stands"]]
HOLDS = [tuple(h) for h in _map["airport"]["holds"]]


def xy(lat, lon):
    return round((lon - LON0) * KX, 1), round(-(lat - LAT0) * KY, 1)


# each strip is stored once by its 10x end; radio often names the reciprocal (28R = 10L, 28L = 10R)
RECIP = {"28R": "10L", "28L": "10R"}


def canon(rw):
    rw = RECIP.get(rw, rw)
    return rw if rw in RWY else None


def seg(px, py, rw):
    (x0, y0), (x1, y1) = RWY[canon(rw)]
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    u = ((px - x0) * dx + (py - y0) * dy) / L2
    uc = max(0.0, min(1.0, u))
    return math.hypot(px - (x0 + dx * uc), py - (y0 + dy * uc)), u


def nearest_stand(x, y, maxd=70):
    best = min(STANDS, key=lambda s: (s[0] - x) ** 2 + (s[1] - y) ** 2)
    return best[2] if math.hypot(best[0] - x, best[1] - y) < maxd else None


class Airport:
    """Keeps per-aircraft history and derives the states the UI shows."""

    def __init__(self):
        self.hist = {}       # hex -> list[(t, alt, gs)]
        self.clear = {"10L": None, "10R": None}   # runway -> {cs, kind, since}
        self.alerts = []     # recent alerts
        self.holds = {}      # cs -> {"hold": "10L", "since": t}

    # ---------------------------------------------------------------- phases
    def classify(self, a, now):
        x, y = a["x"], a["y"]
        alt = a.get("alt") or 0
        agl = 0 if a.get("ground") else max(0, alt - FIELD_FT)
        gs = a.get("gs") or 0
        vs = a.get("vs") or 0
        on_rw = None
        for rw in RWY:
            d, u = seg(x, y, rw)
            if d < 40 and -0.02 < u < 1.02:
                on_rw = rw
        h = self.hist.setdefault(a["hex"], [])
        h.append((now, agl, gs))
        del h[:-60]
        cat = (a.get("category") or "")
        if cat == "A7" or (a.get("type") or "").startswith(("AS", "EC", "H1", "R44", "R22", "AW", "B06", "S76")):
            return "heli", "HELICÓPTERO" if agl > 50 else "NO SOLO"
        if a.get("ground") or agl < 50:
            if on_rw and gs > 40:
                return ("arr" if self._was_high(h) else "dep"), ("POUSANDO " if self._was_high(h) else "DECOLANDO ") + on_rw
            if on_rw:
                return "ground", "NA PISTA " + on_rw
            if gs < 3:
                st = nearest_stand(x, y)
                return "park", ("POSIÇÃO " + st) if st else "NO PÁTIO"
            return "ground", "TAXIANDO"
        if vs < -300 or (agl < 4000 and self._approaching(x, y, a.get("track"))):
            rw = self._final_for(x, y, a.get("track"))
            return "arr", ("NA FINAL " + rw) if rw else "APROXIMAÇÃO"
        if vs > 300 and agl < 8000:
            return "dep", "SUBINDO"
        return "arr" if vs < 0 else "dep", "EM ROTA"

    def _was_high(self, h):
        return any(agl > 200 for _, agl, _ in h[-30:])

    def _approaching(self, x, y, track):
        return self._final_for(x, y, track) is not None

    def _final_for(self, x, y, track, max_off=600, max_dist=25000):
        if track is None:
            return None
        for rw in RWY:
            (x0, y0), (x1, y1) = RWY[rw]
            ux, uy = (x1 - x0), (y1 - y0)
            L = math.hypot(ux, uy)
            ux, uy = ux / L, uy / L
            along = (x - x0) * ux + (y - y0) * uy
            off = abs(-(x - x0) * uy + (y - y0) * ux)
            hdg = (math.degrees(math.atan2(ux, -uy)) + 360) % 360   # screen y is south
            dh = abs(((track - hdg) + 180) % 360 - 180)
            if -max_dist < along < 200 and off < max_off and dh < 25:
                return rw
        return None

    # ---------------------------------------------------------------- go-around (ADS-B)
    def go_around(self, a, now):
        h = self.hist.get(a["hex"], [])
        if len(h) < 6:
            return False
        lows = [agl for t, agl, _ in h if now - t < 90]
        if not lows:
            return False
        mn = min(lows)
        return 0 < mn < 700 and h[-1][1] > mn + 350 and self._final_for(a["x"], a["y"], a.get("track"), max_off=900, max_dist=6000) is not None

    # ---------------------------------------------------------------- clearances from radio
    def on_intent(self, cs, intent, now):
        rw = canon(intent.get("runway"))
        v = intent.get("verb")
        if not cs:
            return
        if rw in self.clear and v in ("LIVRE POUSO", "DECOLAGEM", "ALINHE", "CRUZE", "ARREMETA"):
            kind = {"LIVRE POUSO": "livre pouso", "DECOLAGEM": "decolando", "ALINHE": "alinhado", "CRUZE": "cruzando", "ARREMETA": "arremetida"}[v]
            self.clear[rw] = {"cs": cs, "txt": kind, "since": now}
        if v == "HOLD" and rw:
            self.holds[cs] = {"rw": rw, "since": now}
        if v in ("CRUZE", "ALINHE", "DECOLAGEM"):
            self.holds.pop(cs, None)

    def runway_state(self, aircraft, now):
        by = {a.get("callsign"): a for a in aircraft}
        out = {}
        for rw in RWY:
            s = self.clear.get(rw)
            occ = [a for a in aircraft if seg(a["x"], a["y"], rw)[0] < 40 and -0.02 < seg(a["x"], a["y"], rw)[1] < 1.02 and (a.get("ground") or (a.get("alt") or 0) - FIELD_FT < 300)]
            if s and now - s["since"] > 180:
                s = self.clear[rw] = None
            if s:
                a = by.get(s["cs"])
                gone = a is None or (now - s["since"] > 20 and a not in occ and s["txt"] != "livre pouso") or \
                    (s["txt"] == "livre pouso" and a is not None and a.get("ground") and a not in occ and now - s["since"] > 30)
                if gone:
                    s = self.clear[rw] = None
            if s:
                a = by.get(s["cs"])
                u = seg(a["x"], a["y"], rw)[1] if a else 0
                out[rw] = {"cs": s["cs"], "txt": s["txt"], "since": s["since"], "u": round(u, 3)}
            elif occ:
                a = occ[0]
                out[rw] = {"cs": a.get("callsign") or a["hex"], "txt": "na pista", "since": now, "u": round(seg(a["x"], a["y"], rw)[1], 3)}
            else:
                out[rw] = None
        return out


# runway-holding points in OSM sit within a few meters of the taxiway; used to draw stop bars
def hold_near(x, y, maxd=250):
    best = min(HOLDS, key=lambda h: (h[0] - x) ** 2 + (h[1] - y) ** 2)
    return best if math.hypot(best[0] - x, best[1] - y) < maxd else None
