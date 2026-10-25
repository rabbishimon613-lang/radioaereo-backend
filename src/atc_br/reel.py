"""Render a vertical (1080x1920) TikTok of one radio episode at SBGR.

An episode is a small JSON spec (title, which radio clips, caption overrides). The video is the airport map in the
site's look, every aircraft from the ADS-B log moving in real time, the real radio audio, and karaoke captions.
Gaps between transmissions are squeezed; the map fast-forwards through them so traffic stays in sync with the radio.
"""
import bisect
import gzip
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from . import taxiroute
from .airport import xy

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
FONTS = ROOT / "assets" / "fonts"
W, H, FPS, SR = 1080, 1920, 30, 48000
# Platform safe zone, v2 (2026-10-24), measured on a real tall iPhone running TikTok: the app scales the 1080x1920 frame to
# ~0.94 and crops ~47 px off each side; status bar + search bar cover y 0–220, the like/comment rail starts at x ≈ 900,
# caption + handle the bottom ~480 px. Everything that carries meaning stays inside; the map runs full-bleed underneath.
SAFE_L, SAFE_R, SAFE_T, SAFE_B = 100, 860, 240, 1436
SAFE_CX = (SAFE_L + SAFE_R) / 2
# The focus aircraft lives in a protected window between the step text and the caption card (+ note): the camera puts it
# in the middle of that gap on every beat, and nothing is ever drawn over it. PLANE_CLEAR = room kept above/below it.
PLANE_CLEAR_UP, PLANE_CLEAR_DOWN = 90, 80
BIG_PT, BIG_LEAD, ORIG_PT, ORIG_LEAD = 50, 62, 29, 38   # caption card text (px at 1080 wide)
MAX_LEN = 50.0                                 # hard cap, seconds, end card included
INTRO, GAP, FF, OUTRO = 0.05, 1.1, 2.4, 2.4   # FF: screen time for a long real-time jump
BRT = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------- look (same tokens as web/app.html)
def oklch(L, C, h, a=255):
    h = math.radians(h)
    l_ = L + 0.3963377774 * C * math.cos(h) + 0.2158037573 * C * math.sin(h)
    m_ = L - 0.1055613458 * C * math.cos(h) - 0.0638541728 * C * math.sin(h)
    s_ = L - 0.0894841775 * C * math.cos(h) - 1.2914855480 * C * math.sin(h)
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    rgb = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    f = lambda c: 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(max(0, min(255, round(f(c) * 255))) for c in rgb) + (a,)


INK, TEXT, DIM, FAINT = oklch(.135, .012, 255), oklch(.94, .006, 85), oklch(.68, .012, 255), oklch(.46, .012, 255)
CH = {"torre": oklch(.80, .105, 245), "solo": oklch(.80, .105, 155), "final": oklch(.80, .105, 290),
      "trafego": oklch(.80, .105, 200), "atis": oklch(.72, .02, 85)}
CH_NAME = {"torre": ("TORRE", "135.200"), "solo1269": ("SOLO", "126.900"), "solo1217": ("SOLO 2", "121.700"),
           "final": ("CONTROLE SP", "129.750"), "trafego": ("TRÁFEGO", "121.000"), "atis": ("ATIS", "127.750")}
AMBER = (255, 196, 82, 255)


@lru_cache(None)
def font(kind, size, wght=600, wdth=100):
    f = ImageFont.truetype(str(FONTS / ("GeistMono.ttf" if kind == "mono" else "Archivo.ttf")), size)
    f.set_variation_by_axes([wght] if kind == "mono" else [wght, wdth])
    return f


def chan(feed):
    return "solo" if feed.startswith("solo") else feed


# ---------------------------------------------------------------- data
def load_tx():
    return {d["clip"]: d for d in map(json.loads, open(DATA / "transcripts.jsonl")) if d.get("clip")}


def load_tracks(t0, t1):
    """hex -> sorted [(t, x, y, track, gs, alt, ground, callsign, type)] for snapshots in [t0-60, t1+60]."""
    tracks = {}
    d = datetime.fromtimestamp(t0 - 60, timezone.utc).date()
    while d <= datetime.fromtimestamp(t1 + 60, timezone.utc).date():
        p = DATA / "adsb" / f"{d}.jsonl.gz"
        if p.exists():
            for line in gzip.open(p, "rt"):
                s = json.loads(line)
                if not t0 - 60 <= s["t"] <= t1 + 60:
                    continue
                for a in s["ac"]:
                    if a.get("lat") is None:
                        continue
                    x, y = xy(a["lat"], a["lon"])
                    # trust the transponder's ground flag; the altitude guess only for slow, field-level targets
                    # (raw pressure altitude reads near field elevation on short final, so speed must be taxi-like)
                    g = bool(a.get("ground")) or ((a.get("alt") or 0) - 2459 < 60 and (a.get("gs") or 0) < 50)
                    tracks.setdefault(a["hex"], []).append((s["t"], x, y, a.get("track"), a.get("gs") or 0, a.get("alt"), g,
                                                            (a.get("callsign") or "").strip(), a.get("type") or ""))
        d += timedelta(days=1)
    for h, v in tracks.items():
        v.sort()
        keep = [v[0]] if v else []
        for p in v[1:]:          # drop source-switch glitches: a jump far faster than the aircraft can move
            q = keep[-1]
            dt = max(p[0] - q[0], 1)
            if math.dist(p[1:3], q[1:3]) / dt > max(p[4], q[4]) * .5144 * 1.3 + 20:
                continue
            if q[6] and not p[6] and (p[5] or 0) - 2459 > 600 and dt < 90:   # ground -> high in seconds
                continue
            keep.append(p)
        for i in range(1, len(keep) - 1):     # a lone "airborne" fix between ground fixes, at field level, is a stale flag
            p, q, r = keep[i - 1], keep[i], keep[i + 1]
            if p[6] and r[6] and not q[6] and (q[5] or 0) - 2459 < 300 and r[0] - p[0] < 120:
                keep[i] = q[:6] + (True,) + q[7:]
        names = [p[7] for p in keep if p[7] and "@" not in p[7]]
        cs = max(set(names), key=names.count) if names else ""
        tracks[h] = taxiroute.fill(taxiroute.destale([p[:7] + (cs,) + p[8:] for p in keep]))   # one callsign; ground gaps follow the taxiways
    return tracks


def at(track, T, gap=45):
    ts = [p[0] for p in track]
    i = bisect.bisect_right(ts, T)
    if i == 0 or i == len(track):
        p = track[0] if i == 0 else track[-1]
        return p if abs(p[0] - T) < 20 else None
    a, b = track[i - 1], track[i]
    if b[0] - a[0] > gap:
        return None
    u = (T - a[0]) / (b[0] - a[0])
    ta, tb = a[3], b[3]
    if ta is None or tb is None:
        tr = ta if ta is not None else tb
    else:
        tr = ta + ((tb - ta + 540) % 360 - 180) * u
    gs = a[4] + (b[4] - a[4]) * u
    alt = a[5] if b[5] is None else (b[5] if a[5] is None else a[5] + (b[5] - a[5]) * u)
    return (T, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u, tr, gs, alt, b[6] if u > .5 else a[6], a[7] or b[7], a[8] or b[8])


def clip_time(rel):
    """clips/SBGR/<feed>/<YYYY-MM-DD>/<HHMMSS_mmm>.ogg -> epoch (UTC)."""
    parts = rel.split("/")
    hms, ms = Path(parts[-1]).stem.split("_")
    return datetime.strptime(parts[-2] + hms, "%Y-%m-%d%H%M%S").replace(tzinfo=timezone.utc).timestamp() + int(ms) / 1000


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


# ---------------------------------------------------------------- map
def _rot0():
    (x0, y0), (x1, y1) = json.loads((ROOT / "web" / "sbgr.json").read_text())["runwayEnds"]["10L"]
    th = -math.pi / 2 - math.atan2(y1 - y0, x1 - x0)
    return th, math.cos(th), math.sin(th)


ROT = _rot0()


def rot(x, y):
    """world meters (x east, y south) -> screen-aligned meters, runways pointing up (east at the top)."""
    return x * ROT[1] - y * ROT[2], x * ROT[2] + y * ROT[1]


class Map:
    """The airport rotated so the runways run up the screen (east at the top), drawn once (supersampled) at k px/m.
    Each frame is a crop of it: a camera centre plus a view width in meters."""
    SS = 2

    def __init__(self, k=.62, bounds=None, margin=1100):
        sb = json.loads((ROOT / "web" / "sbgr.json").read_text())
        self.A, self.C, self.ends = sb["airport"], sb["city"], sb["runwayEnds"]
        self.th, self.cos, self.sin = ROT
        self.k = k
        if not bounds:
            pts = [self.rot(*p) for kind in ("runways", "taxiways", "aprons", "terminals") for f in self.A[kind] for p in f["p"]]
            xs, ys = zip(*pts)
            bounds = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
        self.x0, self.y0 = bounds[0], bounds[1]
        self.BW, self.BH = int((bounds[2] - self.x0) * k), int((bounds[3] - self.y0) * k)
        self.SS = 2 if self.BW * self.BH < 30e6 else 1
        self.home = self.rot(400, -380)          # rotated meters: middle of the field
        self.taxi = {}
        for t in self.A["taxiways"]:
            for ref in (t.get("ref") or "").split(";"):
                if ref.strip():
                    self.taxi.setdefault(ref.strip(), []).append(t["p"])
        self.base = self._draw()
        dim = Image.new("RGBA", self.base.size, INK[:3] + (95,))   # calm: push the whole map back
        self.base.alpha_composite(dim)

    rot = staticmethod(lambda x, y: rot(x, y))

    def px(self, x, y, s=1.0):
        """world meters -> base-image pixels (s = supersample factor)."""
        rx, ry = self.rot(x, y)
        return (rx - self.x0) * self.k * s, (ry - self.y0) * self.k * s

    def _draw(self):
        s = self.SS
        im = Image.new("RGBA", (self.BW * s, self.BH * s), INK)
        d = ImageDraw.Draw(im)
        P = lambda pts: [self.px(x, y, s) for x, y in pts]
        flat = lambda a, o=0: [self.px(a[i], a[i + 1], s) for i in range(o, len(a) - 1, 2)]
        for g in self.C.get("green", []):
            d.polygon(flat(g), fill=(60, 90, 70, 22))
        for w in self.C.get("water", []):
            d.polygon(flat(w), fill=(46, 70, 110, 70))
        for w in self.C.get("rivers", []):
            d.line(flat(w), fill=(70, 100, 150, 120), width=2 * s)
        for w in self.C.get("streets", []):
            d.line(flat(w), fill=(120, 132, 155, 34), width=1 * s)
        road = [(4, (255, 196, 130, 80)), (3, (220, 205, 180, 60)), (2, (190, 195, 210, 46)), (1.4, (170, 178, 198, 34))]
        for r in sorted(self.C.get("roads", []), key=lambda r: -r[0]):
            wd, col = road[r[0]]
            d.line(flat(r, 1), fill=col, width=max(1, round(wd * s)), joint="curve")
        A, k = self.A, self.k * s
        for a in A["aprons"]:
            d.polygon(P(a["p"]), fill=(40, 46, 60, 255))
        for t in A["taxilanes"]:
            d.line(P(t["p"]), fill=(52, 58, 74, 255), width=max(1, round(10 * k)))
        for t in A["taxiways"]:
            d.line(P(t["p"]), fill=(58, 64, 82, 255), width=max(2, round(23 * k)), joint="curve")
            d.line(P(t["p"]), fill=(205, 170, 70, 70), width=max(1, round(1.2 * s)))
        for b in A["terminals"] + A["hangars"]:
            d.polygon(P(b["p"]), fill=(70, 78, 98, 255), outline=(110, 120, 145, 255))
        for r in A["runways"] + [{"p": p["p"], "w": 60} for p in A["stopways"]]:
            d.line(P(r["p"]), fill=(92, 98, 116, 255), width=max(3, round(r.get("w", 45) * k)), joint="curve")
        for r in A["runways"]:
            pts = P(r["p"])
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                n = int(math.hypot(bx - ax, by - ay) / (30 * k) / 1.6)
                for i in range(n):
                    u0, u1 = i / n, (i + .55) / n
                    d.line([(ax + (bx - ax) * u0, ay + (by - ay) * u0), (ax + (bx - ax) * u1, ay + (by - ay) * u1)],
                           fill=(233, 237, 245, 150), width=max(1, round(1.4 * s)))
        im = im.resize((self.BW, self.BH), Image.LANCZOS)
        d = ImageDraw.Draw(im)
        lab = font("mono", 24, 700)
        for name, recip in (("10L", "28R"), ("10R", "28L")):
            (ax, ay), (bx, by) = self.ends[name]
            ux, uy = bx - ax, by - ay
            L = math.hypot(ux, uy)
            for (x, y), txt, off in (((ax, ay), name, 1), ((bx, by), recip, -1)):
                X, Y = self.px(x - ux / L * 130 * off, y - uy / L * 130 * off)
                d.text((X, Y), txt, font=lab, fill=TEXT[:3] + (210,), anchor="mm")
        tl = font("sans", 20, 700, 90)
        for b in A["terminals"]:
            if b.get("name") and "Terminal" in b["name"]:
                xs, ys = zip(*b["p"])
                X, Y = self.px(sum(xs) / len(xs), sum(ys) / len(ys))
                d.text((X, Y), b["name"].upper(), font=tl, fill=DIM[:3] + (220,), anchor="mm", stroke_width=3, stroke_fill=INK)
        return im

    def frame(self, cx, cy, vw, oy=.5):
        """Crop centred on rotated-meter point (cx, cy) showing vw meters across; the point lands at height oy."""
        bw = vw * self.k
        bh = bw * H / W
        X, Y = (cx - self.x0) * self.k, (cy - self.y0) * self.k
        l, t = X - bw * SAFE_CX / W, Y - bh * oy
        l, t = max(0, min(self.BW - bw, l)), max(0, min(self.BH - bh, t))
        box = (l, t, l + bw, t + bh)
        return self.base.resize((W, H), Image.BICUBIC, box=box), box

    def to_frame(self, box, X, Y):
        return (X - box[0]) * W / (box[2] - box[0]), (Y - box[1]) * H / (box[3] - box[1])


@lru_cache(None)
def plane_icon(size, rgba):
    """Top-down airliner silhouette pointing up, drawn 4x and downsampled for clean edges."""
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = S / 2
    u = S / 24
    body = [(c, 1 * u), (c + 1.3 * u, 3 * u), (c + 1.3 * u, 9 * u), (c + 11 * u, 14 * u), (c + 11 * u, 15.6 * u),
            (c + 1.3 * u, 13 * u), (c + 1.1 * u, 19 * u), (c + 4.2 * u, 21.4 * u), (c + 4.2 * u, 22.6 * u), (c, 21.6 * u)]
    full = body + [(2 * c - x, y) for x, y in reversed(body)]
    d.polygon(full, fill=rgba)
    return im.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- text helpers
def wrap(words, f, maxw):
    lines, cur = [], []
    for w in words:
        if cur and f.getlength(" ".join(cur + [w])) > maxw:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def rounded(d, box, r, fill, outline=None, width=1):
    d.rounded_rectangle(box, r, fill=fill, outline=outline, width=width)


# ---------------------------------------------------------------- render
def render(spec_path: Path, out: Path | None = None, log=print):
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text())
    out = out or spec_path.parent / "video.mp4"
    tx = load_tx()

    # timeline: open right on the first word, each clip at real speed; long real gaps become a short fast-forward
    items, prev_end = [], None
    for c in spec["clips"]:
        rec = tx.get(c.get("clip"), {})
        t = c.get("t", rec.get("t")) or clip_time(c["clip"])
        if c.get("clip") and not rec:
            rec = {"feed": c["clip"].split("/")[2]}
        pcm = decode(DATA / c["clip"]) if c.get("clip") else None
        dur = len(pcm) / SR if pcm is not None else c["silent"]
        span = dur if pcm is not None else c["span"]
        jump = None if prev_end is None else t - prev_end
        items.append([c, rec, pcm, dur, span, t, jump])
        prev_end = t + span

    # fit the 50 s cap by squeezing pauses, fast-forwards, silent beats and the end card (never the speech)
    gap, ff, outro, intro = GAP, FF, OUTRO, spec.get("intro", INTRO)
    n_gap = 1 + sum(1 for it in items if it[6] is not None and it[6] < 40)
    n_ff = sum(1 for it in items if it[6] is not None and it[6] >= 40)
    sil = [it for it in items if it[2] is None]
    natural = intro + sum(it[3] for it in items) + n_gap * gap + n_ff * ff + outro
    flex = n_gap * (GAP - .45) + n_ff * (FF - 1.4) + (OUTRO - 1.8) + sum(max(0, it[3] - 1.6) for it in sil)
    if natural > MAX_LEN and flex > 0:
        k = min(1, (natural - MAX_LEN) / flex)
        gap, ff, outro = GAP - k * (GAP - .45), FF - k * (FF - 1.4), OUTRO - k * (OUTRO - 1.8)
        for it in sil:
            it[3] -= k * max(0, it[3] - 1.6)

    segs, tv = [], intro
    for c, rec, pcm, dur, span, t, jump in items:
        if pcm is None:                          # silent beat: show `span` real seconds in `dur` screen seconds
            pcm = np.zeros(int(dur * SR), np.float32)
        if segs:
            tv += gap if jump < 40 else ff
        dur = len(pcm) / SR
        segs.append({**rec, **c, "v0": tv, "v1": tv + dur, "pcm": pcm, "t": t, "rate": span / dur})
        tv += dur
    total = tv + gap + outro
    n = int(total * FPS)
    mix = np.zeros(int(total * SR) + SR, np.float32)
    for s in segs:
        i = int(s["v0"] * SR)
        mix[i:i + len(s["pcm"])] += s["pcm"]
    mix = np.clip(mix / (np.abs(mix).max() or 1) * .89, -1, 1)

    def real(v):
        """video seconds -> wall-clock epoch (fast-forward through squeezed gaps)."""
        if v <= segs[0]["v0"]:
            return segs[0]["t"] - (segs[0]["v0"] - v)
        for a, b in zip(segs, segs[1:]):
            if a["v1"] < v < b["v0"]:
                u = (v - a["v1"]) / (b["v0"] - a["v1"])
                ra = a["t"] + (a["v1"] - a["v0"]) * a["rate"]
                return ra + (b["t"] - ra) * u
        cur = max((s for s in segs if s["v0"] <= v), key=lambda s: s["v0"])
        return cur["t"] + (v - cur["v0"]) * cur["rate"]

    tracks = load_tracks(real(0), real(total))
    focus = spec.get("focus") or []
    focus = [focus] if isinstance(focus, str) else focus
    fhexes = [next((h for h, tr in tracks.items() if any(fc in (p[7], h) for p in tr)), None) for fc in focus]
    fhexes = [h for h in fhexes if h]
    fhex = fhexes[0] if fhexes else None
    cam = spec.get("camera", {})
    head, head_h = _header_layer(spec, segs[0]["t"])

    # protected window for the focus aircraft, per beat: below the step text, above the caption card + note
    def window(sg):
        lo = _step_bottom(sg, head_h) + PLANE_CLEAR_UP
        hi = (_card_layout(sg)[0] if sg.get("clip") else SAFE_B - 60) - PLANE_CLEAR_DOWN
        return lo, hi
    wins = [window(sg) for sg in segs]
    beat_at = [max((i for i, sg in enumerate(segs) if sg["v0"] - .05 <= f / FPS), default=0) for f in range(n)]
    want, tight = [], []
    for f in range(n):                      # intersect the windows of the beats around this frame so transitions stay clear
        near = {beat_at[j] for j in range(max(0, f - FPS // 2), min(n, f + FPS // 2 + 1))}
        lo, hi = max(wins[b][0] for b in near), min(wins[b][1] for b in near)
        if hi < lo:
            tight.append(round(f / FPS, 1))
            lo, hi = wins[beat_at[f]]
        want.append((lo + hi) / 2)
    ys = [sum(want[max(0, f - 8):f + 9]) / len(want[max(0, f - 8):f + 9]) for f in range(n)]
    oys = [y / H for y in ys]
    LAYOUT.update(wins=wins, beat_at=beat_at)
    if tight:
        log(f"  ⚠ layout: no clear window for the plane at {tight[:5]}… (step/card too tall)")

    # camera path: follow the first focus aircraft (smoothed), or sit on a fixed point (default: middle of the field)
    if fhex and cam.get("follow", True):
        raw, spread, pair = [], [], []
        h2 = fhexes[1] if len(fhexes) > 1 else None
        base = max(cam.get("vw", 1500), cam.get("vw_end", 1250))
        for f in range(n):
            p = at(tracks[fhex], real(f / FPS), gap=900)
            q = at(tracks[h2], real(f / FPS), gap=900) if h2 else None
            a = rot(p[1], p[2]) if p else None
            b = rot(q[1], q[2]) if q else None
            lo_, hi_ = wins[beat_at[f]]        # same window + formula as the zoom below
            far = a and b and max(abs(a[0] - b[0]) * W / max(SAFE_R - SAFE_L - 260, 200),
                                  abs(a[1] - b[1]) * W / max((hi_ - lo_) * .8, 120)) * 1.05 > 3 * base
            pair.append(bool(a and b and not far))
            if a and b and not far:     # both in the shot (unless that needs > 3x zoom-out): centre between them
                raw.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
                spread.append((abs(a[0] - b[0]), abs(a[1] - b[1])))
            else:
                raw.append(a or (raw[-1] if raw else None))
                spread.append((0, 0))
        first = next(r for r in raw if r)
        raw = [r or first for r in raw]
        # smooth over ±0.5 s of video and never more than ±20 real seconds: steady, but the plane stays on its mark
        rt = [real(f / FPS) for f in range(n)]
        path = []
        for i in range(n):
            lo, hi = max(0, i - FPS // 2), min(n, i + FPS // 2 + 1)
            win = [raw[j] for j in range(lo, hi) if abs(rt[j] - rt[i]) <= 20]
            path.append(tuple(sum(c) / len(c) for c in zip(*win)))
        vw0, vw1, oy = cam.get("vw", 1500), cam.get("vw_end", 1250), cam.get("oy", .42)
        # zoom out just enough that both focus planes stay inside the protected window (x and y), smoothed ±1 s
        need = []
        for f in range(n):
            lo, hi = wins[beat_at[f]]
            sx, sy = spread[f]
            need.append(max(sx * W / max(SAFE_R - SAFE_L - 260, 200), sy * W / max((hi - lo) * .8, 120)) * 1.05)
        need = [max(need[max(0, f - FPS):f + FPS + 1]) for f in range(n)]
        vws = [sum(need[max(0, f - FPS):f + FPS + 1]) / len(need[max(0, f - FPS):f + FPS + 1]) for f in range(n)]
    else:
        path = [rot(*cam.get("center", (400, -380)))] * n
        vws, pair = [0] * n, [False] * n
        vw0, vw1, oy = cam.get("vw", 2700), cam.get("vw_end", 2400), cam.get("oy", .40)
    # (spec camera "oy" is ignored: the protected window decides where the plane sits)
    # only draw the part of the world the camera will see, at a resolution that matches the tightest zoom
    vmax = max(vw0, vw1, max(vws))
    hw, hh = vmax / 2 + 300, vmax * H / W + 300
    xs, ys = zip(*path)
    bounds = (min(xs) - hw, min(ys) - hh, max(xs) + hw, max(ys) + hh)
    k = min(W / min(vw0, vw1) * 1.1, math.sqrt(60e6 / ((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))))
    M = Map(k=k, bounds=bounds)
    log(f"render {spec_path.parent.name}: {total:.1f}s, {len(segs)} clips, {len(tracks)} aircraft, focus={fhexes}, map {M.BW}x{M.BH}")

    import soundfile
    wav = out.parent / "audio.wav"
    soundfile.write(wav, mix[:int(total * SR)], SR)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-i", str(wav), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE)

    header_grad = _vgrad(900, (0, 0, 0, 240), (0, 0, 0, 0))
    footer_grad = _vgrad(900, (0, 0, 0, 0), (0, 0, 0, 245))
    radio = any(s.get("clip") for s in segs)
    covered = []
    cover = None
    for f in range(n):
        v = f / FPS
        T = real(v)
        u = (v / total) ** .9
        oy = oys[f]
        LAYOUT["lo"], LAYOUT["hi"] = wins[beat_at[f]]
        im, box = M.frame(*path[f], max(vw0 + (vw1 - vw0) * u, vws[f]), oy)
        im = im.convert("RGBA")
        d = ImageDraw.Draw(im)
        beat = max((i for i, s in enumerate(segs) if s["v0"] - .05 <= v), default=0)
        _taxi(im, M, box, segs[beat].get("taxi", []), v - segs[beat]["v0"], (SAFE_CX, H * oy))
        _aircraft(im, d, M, box, tracks, T, fhexes)
        for hx_ in (fhexes[:2] if pair[f] else fhexes[:1]):   # hard check: the focus planes are never under the text
            q = at(tracks[hx_], T, gap=900)
            if q:
                X, Y = M.to_frame(box, *M.px(q[1], q[2]))
                # the plane's whole footprint (icon + ring + label, ~75 px up/down) must sit between step text and card
                if SAFE_L - 40 < X < SAFE_R + 40 and not (LAYOUT["lo"] - PLANE_CLEAR_UP + 75 <= Y <= LAYOUT["hi"] + PLANE_CLEAR_DOWN - 75):
                    covered.append(round(v, 1))
        if spec.get("measure") and spec.get("measure_from", 0) <= beat < spec.get("measure_until", len(segs)):
            _measure(im, M, box, tracks, T, [h for h in fhexes[:2]])
        ffa = _ff_amount(v, segs)
        if ffa > 0:
            im = _ffx(im, ffa, v)
            d = ImageDraw.Draw(im)
        im.alpha_composite(header_grad, (0, 0))
        im.alpha_composite(footer_grad, (0, H - 900))
        im.alpha_composite(head)
        d = ImageDraw.Draw(im)
        _clock(d, T, v, radio)
        _step(im, segs, beat, v, head_h)
        cur = next((s for s in segs if s["v0"] - .05 <= v <= s["v1"] + gap * .9), None)
        if cur and cur.get("silent"):
            cur = None
        elif cur:
            lvl = float(np.sqrt(np.mean(mix[int(v * SR):int(v * SR) + SR // FPS] ** 2) + 1e-9))
            _caption(im, d, cur, v, lvl)
        else:
            _skip(d, v, segs)
        if v > total - outro + .2:
            _outro(im, (v - (total - outro + .2)) / (outro - .2))
        if cover is None and (cur or not radio) and v > segs[min(1, len(segs) - 1)]["v0"] + .6 * (segs[min(1, len(segs) - 1)]["v1"] - segs[min(1, len(segs) - 1)]["v0"]):
            cover = im.convert("RGB")
        proc.stdin.write(im.convert("RGB").tobytes())
        if f % (FPS * 5) == 0:
            log(f"  {v:5.1f}s / {total:.1f}s")
    proc.stdin.close()
    proc.wait()
    wav.unlink()
    if cover:
        cover.save(out.parent / "cover.jpg", quality=90)
    (out.parent / "layout.json").write_text(json.dumps({"tight_seconds": tight, "covered_seconds": covered,
                                                          "safe": [SAFE_L, SAFE_T, SAFE_R, SAFE_B]}))
    if covered:
        log(f"  ⚠ focus plane under text for {len(covered) / FPS:.1f}s (from {covered[0]}s)")
    log(f"wrote {out}")
    return out


LAYOUT = {"lo": SAFE_T + 400, "hi": SAFE_B - 500}   # current frame's protected window (set by render)


def _vgrad(h, top, bot):
    a = np.linspace(0, 1, h)[:, None]
    arr = np.zeros((h, W, 4), np.uint8)
    for i in range(4):
        arr[..., i] = (top[i] + (bot[i] - top[i]) * a).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _aircraft(im, d, M, box, tracks, T, fhexes):
    lab, labs = font("mono", 22, 700), font("mono", 19, 500)
    for hx, tr in tracks.items():
        hot = hx in fhexes
        if fhexes and not hot:
            p = at(tr, T)
            if p:
                X, Y = M.to_frame(box, *M.px(p[1], p[2]))
                if -60 < X < W + 60 and -60 < Y < H + 60:
                    r = plane_icon(34, (150, 158, 178, 120)).rotate(-((p[3] or 0) + math.degrees(M.th)), resample=Image.BICUBIC, expand=True)
                    im.alpha_composite(r, (int(X - r.width / 2), int(Y - r.height / 2)))
            continue
        p = at(tr, T, gap=900 if hot else 45)
        if not p:
            continue
        _, x, y, trk, gs, alt, ground, cs, typ = p
        X, Y = M.to_frame(box, *M.px(x, y))
        if not (-60 < X < W + 60 and -60 < Y < H + 60):
            continue
        moving = gs > 3
        col = (AMBER if hx == fhexes[0] else CH["torre"]) if hot else (CH["final"] if not ground else (TEXT if moving else FAINT))
        size = 64 if hot else (46 if not ground else 38)
        pts = [M.to_frame(box, *M.px(q[1], q[2])) for q in tr if T - (240 if hot else 40) <= q[0] <= T]
        if len(pts) > 1 and (moving or hot):
            d.line(pts + [(X, Y)], fill=col[:3] + (140 if hot else 90,), width=4 if hot else 2)
        ic = plane_icon(size, col)
        heading = trk if trk is not None else 0
        r = ic.rotate(-(heading + math.degrees(M.th)), resample=Image.BICUBIC, expand=True)
        if hot:
            rr = size * .95
            d.ellipse((X - rr, Y - rr, X + rr, Y + rr), outline=col[:3] + (210,), width=3)
        im.alpha_composite(r, (int(X - r.width / 2), int(Y - r.height / 2)))
        if hot or (moving and cs):
            tx_, ty_ = X + size * .65, Y - size * .95
            if hot and hx != fhexes[0]:            # second focus plane labels on the left so the two never collide
                tx_ = X - size * .65 - font("mono", 28, 700).getlength(cs or hx) - 8
            d.line([(X + size * .35, Y - size * .35), (tx_, ty_ + 22)], fill=col[:3] + (160,), width=2)
            f1 = font("mono", 28, 700) if hot else lab
            d.text((tx_ + 4, ty_), cs or hx.upper(), font=f1, fill=col, stroke_width=3, stroke_fill=INK)
            sub = (f"{typ} · " if typ else "") + ("SOLO" if ground else f"{round(alt or 0, -1):,}ft".replace(",", ".")) + f" · {int(gs)}kt"
            d.text((tx_ + 4, ty_ + (32 if hot else 26)), sub, font=labs, fill=DIM, stroke_width=3, stroke_fill=INK)


def _measure(im, M, box, tracks, T, hexes):
    """Dashed line between two aircraft with the live distance (km and nautical miles)."""
    if len(hexes) < 2:
        return
    ps = [at(tracks[h], T, gap=900) for h in hexes]
    if not all(ps):
        return
    (ax, ay), (bx, by) = [M.to_frame(box, *M.px(p[1], p[2])) for p in ps]
    dist = math.dist(ps[0][1:3], ps[1][1:3])
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    L = math.hypot(bx - ax, by - ay) or 1
    n = int(L / 22)
    for i in range(n):
        if i % 2 == 0:
            u0, u1 = i / n, (i + 1) / n
            d.line([(ax + (bx - ax) * u0, ay + (by - ay) * u0), (ax + (bx - ax) * u1, ay + (by - ay) * u1)], fill=(255, 255, 255, 200), width=4)
    f1, f2 = font("mono", 40, 800), font("mono", 24, 600)
    txt = f"{dist / 1000:.1f} km".replace(".", ",")
    sub = f"{dist / 1852:.1f} milhas".replace(".", ",")
    w = max(f1.getlength(txt), f2.getlength(sub)) + 40
    # keep-out boxes: each plane (ring) and its label (first focus labels to the right, second to the left)
    keep = [(ax - 70, ay - 70, ax + 70, ay + 70), (bx - 70, by - 70, bx + 70, by + 70),
            (ax + 20, ay - 80, ax + 340, ay - 2), (bx - 340, by - 80, bx - 20, by - 2)]
    lo, hi = LAYOUT["lo"] - PLANE_CLEAR_UP + 50, LAYOUT["hi"] + PLANE_CLEAR_DOWN - 50
    L = math.hypot(bx - ax, by - ay) or 1
    nx, ny = -(by - ay) / L, (bx - ax) / L

    def hits(x, y):
        r = (x - w / 2 - 8, y - 56, x + w / 2 + 8, y + 56)
        return sum(1 for k in keep if r[0] < k[2] and k[0] < r[2] and r[1] < k[3] and k[1] < r[3])
    cands = []
    for u in (.5, .4, .6, .3, .7, .2, .8):
        for off in (0, 130, -130, 220, -220):
            x = min(max(ax + (bx - ax) * u + nx * off, SAFE_L + w / 2), SAFE_R - w / 2)
            y = min(max(ay + (by - ay) * u + ny * off, lo), hi)
            cands.append((hits(x, y), abs(off), abs(u - .5), x, y))
    _, _, _, mx, my = min(cands)
    ox = 0
    d.rounded_rectangle((mx + ox - w / 2, my - 48, mx + ox + w / 2, my + 48), 16, fill=(14, 17, 26, 240), outline=(255, 255, 255, 120), width=2)
    d.text((mx + ox, my - 14), txt, font=f1, fill=TEXT, anchor="mm")
    d.text((mx + ox, my + 24), sub, font=f2, fill=DIM, anchor="mm")
    im.alpha_composite(lay)


MESES = "Janeiro Fevereiro Março Abril Maio Junho Julho Agosto Setembro Outubro Novembro Dezembro".split()
KICKER_DATE = re.compile(r"\s*·\s*\d{1,2}/\d{1,2}(?:/\d{2,4})?,?\s*\d{1,2}[:h]\d{2}h?(?:\s*[–-]\s*\d{1,2}[:h]\d{2}h?)?\s*$")


def when(t):
    """'23 de Janeiro 2026' (Brasília) — the episode's date line; the exact time runs in the big clock top-right."""
    d = datetime.fromtimestamp(t, BRT)
    return f"{d.day} de {MESES[d.month - 1]} {d.year}"


def _header_layer(spec, t0=None):
    """Static top block in Portuguese: kicker (airline · aircraft), date line, and a short title."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m, y = SAFE_L + 4, SAFE_T + 62
    if spec.get("kicker"):
        kicker = KICKER_DATE.sub("", spec["kicker"]) if t0 else spec["kicker"]   # the date gets its own, bigger line
        d.text((m, y), kicker.upper(), font=font("mono", 26, 700), fill=AMBER)
        y += 44
    if t0:
        d.text((m, y), when(t0), font=font("sans", 36, 700, 100), fill=TEXT)
        y += 56
    tf = font("sans", 76, 830, 80)
    for line in wrap(spec["title"].split(), tf, SAFE_R - m):
        d.text((m, y), " ".join(line), font=tf, fill=TEXT)
        y += 80
    return im, y


def _step(im, segs, beat, v, y0):
    """One plain-language sentence for the current moment, numbered, fading in when the beat changes."""
    s = segs[beat]
    if not s.get("step"):
        return
    a = max(0, min(1, (v - s["v0"] + .05) / .35))
    m = SAFE_L + 4
    lay = Image.new("RGBA", (W, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    nf, sf = font("mono", 26, 700), font("sans", 38, 560, 100)
    num = f"{beat + 1}/{len(segs)}"
    d.rounded_rectangle((m, 22, m + nf.getlength(num) + 28, 66), 22, fill=(255, 255, 255, 30))
    d.text((m + 14, 44), num, font=nf, fill=TEXT, anchor="lm")
    x0 = m + nf.getlength(num) + 46
    y = 20
    for line in wrap(s["step"].split(), sf, SAFE_R - x0):
        d.text((x0, y), " ".join(line), font=sf, fill=(225, 229, 238, 255))
        y += 50
    if a < 1:
        lay.putalpha(lay.getchannel("A").point(lambda q: int(q * a)))
    im.alpha_composite(lay, (0, int(y0 + 6 + (1 - a) * 14)))


NATO = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "Alfa Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett Kilo Lima Mike "
                "November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu".split()))


def _taxi(im, M, box, refs, t, anchor=(SAFE_CX, H * .42)):
    """Light up the taxiways the radio is talking about, with their spoken name."""
    if not refs:
        return
    a = min(1, max(0, t / .5))
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    k = W / (box[2] - box[0])
    for ref in refs:
        best = None
        for line in M.taxi.get(ref, []):
            pts = [M.to_frame(box, *M.px(x, y)) for x, y in line]
            d.line(pts, fill=(255, 196, 82, int(90 * a)), width=max(6, int(26 * M.k * k)), joint="curve")
            d.line(pts, fill=(255, 214, 110, int(255 * a)), width=4, joint="curve")
            on = [p for p in pts if SAFE_L + 120 < p[0] < SAFE_R - 120
                  and LAYOUT["lo"] - PLANE_CLEAR_UP + 30 < p[1] < LAYOUT["hi"] + PLANE_CLEAR_DOWN - 30]
            if on and (best is None or len(on) > len(best)):
                best = on
        if best:
            # dense points along the visible part, label placed ~300 px from the plane (frame centre) so they never collide
            dense = [(a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u) for a, b in zip(best, best[1:]) for u in (0, .25, .5, .75)] or best
            X, Y = min(dense, key=lambda p: abs(math.dist(p, anchor) - 300))
            txt = f"{ref} · {NATO.get(ref, ref).upper()}" if len(ref) == 1 else ref
            f = font("mono", 30, 800)
            w = f.getlength(txt) + 30
            d.rounded_rectangle((X - w / 2, Y - 24, X + w / 2, Y + 24), 12, fill=(30, 24, 8, int(240 * a)), outline=(255, 214, 110, int(255 * a)), width=3)
            d.text((X, Y), txt, font=f, fill=(255, 214, 110, int(255 * a)), anchor="mm")
    im.alpha_composite(lay)


def _clock(d, T, v, radio=True):
    m, f, y = SAFE_L + 4, font("mono", 24, 600), SAFE_T + 22
    d.ellipse((m, y - 8, m + 16, y + 8), fill=(255, 84, 84, 255) if int(v * 2) % 2 == 0 else (150, 50, 50, 255))
    d.text((m + 28, y), ("RÁDIO REAL" if radio else "DADOS REAIS") + " · GUARULHOS", font=f, fill=DIM, anchor="lm")
    # big running clock with seconds, right edge on the safe line, a little below the status line (never higher)
    d.text((SAFE_R, y + 8), datetime.fromtimestamp(T, BRT).strftime("%H:%M:%Sh"), font=font("mono", 40, 700), fill=TEXT, anchor="rm")


def _card_layout(s):
    """(card top y, card height, note height) for a beat — the same numbers _caption draws with."""
    cw = SAFE_R - SAFE_L
    big, of = font("sans", BIG_PT, 720, 92), font("sans", ORIG_PT, 500, 100)
    lines = wrap((s.get("pt") or s.get("text") or "").split(), big, cw - 70)
    orig = s["text"] if s.get("pt") and s["pt"] != s["text"] else None
    olines = wrap(f"“{orig}”".split(), of, cw - 70) if orig else []
    h = 104 + len(lines) * BIG_LEAD + (36 + len(olines) * ORIG_LEAD if orig else 0) + 30
    nh = 0
    if s.get("note"):
        term, _, expl = s["note"].partition(":")
        lf, tf = font("mono", 24, 800), font("sans", 32, 520, 100)
        nh = 26 + len(wrap(expl.split(), tf, cw - 60 - lf.getlength(term.upper()) - 24)) * 42 + 18
    return SAFE_B - h - nh, h, nh


def _step_bottom(s, y0):
    if not s.get("step"):
        return y0
    nf, sf = font("mono", 26, 700), font("sans", 38, 560, 100)
    x0 = SAFE_L + 4 + nf.getlength("0/0") + 46
    return y0 + 6 + 20 + len(wrap(s["step"].split(), sf, SAFE_R - x0)) * 50


def _caption(im, d, s, v, lvl):
    """Card: channel + speaker, big Portuguese meaning (karaoke), then the real transcript small underneath."""
    m, cw = SAFE_L, SAFE_R - SAFE_L
    feed = s.get("feed", "")
    c = CH.get(chan(feed), DIM)
    name, freq = CH_NAME.get(feed, (feed.upper(), ""))
    who = {"atc": "CONTROLADOR", "plt": "PILOTO"}.get(s.get("who"), "")
    big = font("sans", BIG_PT, 720, 92)
    pt = s.get("pt") or s["text"]
    words = pt.split()
    lines = wrap(words, big, cw - 70)
    orig = s["text"] if s.get("pt") and s["pt"] != s["text"] else None
    of = font("sans", ORIG_PT, 500, 100)
    olines = wrap(f"“{orig}”".split(), of, cw - 70) if orig else []
    h = 104 + len(lines) * BIG_LEAD + (36 + len(olines) * ORIG_LEAD if orig else 0) + 30
    y0 = SAFE_B - h
    card = Image.new("RGBA", (cw, h), (0, 0, 0, 0))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((0, 0, cw - 1, h - 1), 26, fill=(14, 17, 26, 232), outline=(255, 255, 255, 26), width=2)
    cd.rounded_rectangle((0, 0, 6, h - 1), 3, fill=c)
    im.alpha_composite(card, (m, int(y0)))
    if s.get("note"):
        term, _, expl = s["note"].partition(":")
        lf, tf = font("mono", 24, 800), font("sans", 32, 520, 100)
        tl = wrap(expl.split(), tf, cw - 60 - lf.getlength(term.upper()) - 24)
        nh = 26 + len(tl) * 42
        ny = y0 - nh - 18
        d.rounded_rectangle((m, ny, SAFE_R, ny + nh), 18, fill=(30, 24, 8, 235), outline=(255, 214, 110, 110), width=2)
        d.text((m + 26, ny + 15 + 2), term.upper(), font=lf, fill=AMBER)
        tx0 = m + 26 + lf.getlength(term.upper()) + 22
        for i, line in enumerate(tl):
            d.text((tx0, ny + 10 + i * 42), " ".join(line), font=tf, fill=(240, 228, 200, 255))
    x = m + 36
    cf, cf2 = font("mono", 26, 700), font("mono", 26, 500)
    d.text((x, y0 + 50), name, font=cf, fill=c, anchor="lm")
    xx = x + cf.getlength(name) + 14
    d.text((xx, y0 + 50), freq, font=cf2, fill=FAINT, anchor="lm")
    if who:
        d.text((SAFE_R - 30, y0 + 50), who, font=cf, fill=TEXT if s.get("who") == "plt" else DIM, anchor="rm")
    bx = xx + cf2.getlength(freq) + 20
    for i in range(7):
        amp = min(1, lvl * 9 * (0.55 + 0.45 * math.sin(i * 1.7 + v * 13)))
        bh = 6 + 26 * amp
        d.rounded_rectangle((bx + i * 9, y0 + 50 - bh / 2, bx + i * 9 + 4, y0 + 50 + bh / 2), 2, fill=c)
    u = (v - s["v0"]) / max((s["v1"] - s["v0"]) * .92, .1)
    lit = u * sum(len(w) + 1 for w in words)
    acc, y = 0, y0 + 88
    for line in lines:
        lx = x
        for w in line:
            col = c if acc <= lit < acc + len(w) + 1 else (TEXT if acc + len(w) / 2 < lit else (255, 255, 255, 80))
            d.text((lx, y), w, font=big, fill=col)
            lx += big.getlength(w + " ")
            acc += len(w) + 1
        y += BIG_LEAD
    if orig:
        y += 22
        d.line((x, y - 8, SAFE_R - 30, y - 8), fill=(255, 255, 255, 22), width=1)
        y += 6
        for line in olines:
            d.text((x, y), " ".join(line), font=of, fill=DIM)
            y += ORIG_LEAD


def _ff_amount(v, segs):
    """0..1 strength of the fast-forward look: full in long real-time jumps between clips, half in fast silent beats."""
    for a, b in zip(segs, segs[1:]):
        if a["v1"] < v < b["v0"]:
            if b["t"] - (a["t"] + (a["v1"] - a["v0"]) * a["rate"]) >= 40:
                return math.sin(math.pi * (v - a["v1"]) / (b["v0"] - a["v1"]))
            return 0
    for x in segs:
        if x.get("silent") and x["rate"] >= 8 and x["v0"] < v < x["v1"]:
            return .5 * math.sin(math.pi * (v - x["v0"]) / (x["v1"] - x["v0"]))
    return 0


@lru_cache(None)
def _scanlines():
    arr = np.zeros((H, W, 4), np.uint8)
    arr[::5, :, :3] = 255
    arr[::5, :, 3] = 1
    return arr


def _ffx(im, a, v):
    """Slight VCR fast-forward on the radar only: vertical smear, faint scanlines, a fast tracking band."""
    s = max(1, int(10 * a))
    smear = ImageChops.add(ImageChops.multiply(ImageChops.offset(im, 0, s), Image.new("RGBA", im.size, (128,) * 4)),
                           ImageChops.multiply(ImageChops.offset(im, 0, -s), Image.new("RGBA", im.size, (128,) * 4)))
    im = Image.blend(im, smear, .55 * a)
    sl = _scanlines().copy()
    sl[..., 3] *= int(26 * a)
    band_y = int((v * 1700) % (H + 200)) - 100
    sl[max(0, band_y):max(0, band_y + 46), :, :3] = (180, 215, 255)
    sl[max(0, band_y):max(0, band_y + 46), :, 3] = int(34 * a)
    im.alpha_composite(Image.fromarray(sl, "RGBA"))
    return im


def _skip(d, v, segs):
    """Between clips: a big fast-forward tag in the middle of the screen, popping in and out."""
    for a, b in zip(segs, segs[1:]):
        if a["v1"] < v < b["v0"]:
            jump = b["t"] - (a["t"] + (a["v1"] - a["v0"]) * a["rate"])
            if jump < 40:
                return
            u = (v - a["v1"]) / (b["v0"] - a["v1"])
            pop = min(1, u / .12, (1 - u) / .12)                  # quick in, quick out
            k = .82 + .18 * (1 - (1 - min(1, u / .12)) ** 3)        # slight scale overshoot-free pop
            n = round(jump / 60)
            big, small = font("mono", int(92 * k), 800), font("mono", int(34 * k), 700)
            t1, t2 = f"» {n} MIN", "DEPOIS"
            w = max(big.getlength(t1), small.getlength(t2)) + 90 * k
            h = 190 * k
            cy = SAFE_B - 260          # below the plane's window (no caption card during a fast-forward): never over it
            al = int(255 * pop)
            d.rounded_rectangle((SAFE_CX - w / 2, cy - h / 2, SAFE_CX + w / 2, cy + h / 2), int(36 * k),
                                fill=(255, 196, 82, int(240 * pop)), outline=(255, 236, 190, al), width=4)
            d.text((SAFE_CX, cy - 22 * k), t1, font=big, fill=(20, 16, 6, al), anchor="mm")
            d.text((SAFE_CX, cy + 52 * k), t2, font=small, fill=(20, 16, 6, al), anchor="mm")
            return


def _outro(im, u):
    u = max(0, min(1, u))
    ov = Image.new("RGBA", (W, H), (10, 13, 20, int(230 * min(1, u * 1.6))))
    d = ImageDraw.Draw(ov)
    a = int(255 * min(1, max(0, u * 2 - .3)))
    d.text((SAFE_CX, H / 2 - 40), "Rádio Aéreo", font=font("sans", 112, 800, 86), fill=TEXT[:3] + (a,), anchor="mm")
    d.text((SAFE_CX, H / 2 + 60), "radioaereo.com", font=font("mono", 34, 600), fill=CH["torre"][:3] + (a,), anchor="mm")
    im.alpha_composite(ov)
