"""ATC-BR / Fonia live backend: every SBGR feed → VAD → Groq batches → parse → match → airport state → SSE.

    uv run atc-br                 # all enabled feeds from config/feeds.yaml
    uv run atc-br --feed torre    # only these feeds
"""
import argparse
import os
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf
import yaml

from . import airport as AP
from .adsb import Tracker
from .callsigns import match, spoken
from .groq_batch import GroqBatcher, load_keys
from .ingest import SR, stream_chunks
from .parse import parse
from .publisher import Publisher, load_env
from .routes import Routes
from .server import Hub
from .vad import transmissions

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
SBGR = (-23.4356, -46.4731)
PROMPT = ("Controle São Paulo, Torre Guarulhos, Solo Guarulhos. TAM, LATAM, Gol, Azul. Pista 10L, 10R, 28L, 28R. "
          "Livre táxi via Alfa, Bravo, Yankee. Livre pouso, autorizado decolagem, alinhe e mantenha.")
JUNK = re.compile(r"aeroescuta|obrigad[oa] por assistir|legendas? pela|inscreva-se|thanks for watching|^\W*$", re.I)
READBACK_S = 8      # an unmatched call this soon after a matched one on the same channel is the same exchange
ATIS_EVERY = 600    # ATIS loops forever; transcribe one clip per 10 min
KEEP = ("hex", "callsign", "reg", "type", "x", "y", "alt", "ground", "gs", "track", "vs", "squawk", "category", "kind", "phase", "route", "hold", "flight")


def log(msg):
    print(f"{datetime.now():%H:%M:%S} · {msg}", flush=True)


def looping(text):
    w = re.findall(r"\w+", text.lower())
    return len(w) > 12 and len(set(w)) / len(w) < .3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", action="append")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config" / "feeds.yaml").read_text())
    feeds = {k: f for k, f in cfg["airports"]["SBGR"]["feeds"].items()
             if (args.feed and k in args.feed) or (not args.feed and f.get("enabled"))}
    DATA.mkdir(exist_ok=True)

    tracker = Tracker(*SBGR, radius_nm=60, log_dir=DATA / "adsb", log=log).start()
    routes = Routes(log=log)
    if os.environ.get("GRU_BOARD", "1") == "1":   # needs headless Chromium (~250 MB RAM); off on small hosts
        from .gru_flights import GruFlights
        gru = GruFlights(log=log)
    else:
        gru = type("NoBoard", (), {"flights": [], "at": 0, "by_callsign": lambda self: {}})()
    apt = AP.Airport()
    hub = Hub(tracker, DATA, ROOT / "web")
    hub.channels = {f["ch"]: {"name": f["name"], "freq": f["freq"], "stream": f"{cfg['base']}:{f['path']}"} for f in feeds.values()}
    metar = {}

    def metar_loop():
        from urllib.request import Request, urlopen
        while True:
            try:
                with urlopen(Request("https://aviationweather.gov/api/data/metar?ids=SBGR&format=json", headers={"User-Agent": "atc-br/0.1"}), timeout=15) as r:
                    m = json.load(r)[0]
                metar.clear()
                metar.update({k: m.get(k) for k in ("rawOb", "wdir", "wspd", "wgst", "visib", "temp", "dewp", "altim", "clouds", "cover", "obsTime")})
            except Exception as e:
                log(f"[metar] {e}")
            time.sleep(600)
    threading.Thread(target=metar_loop, daemon=True).start()
    hub.serve(port=args.port, log=log)
    env = load_env(ROOT)
    if env.get("SUPABASE_URL") and env.get("SUPABASE_SERVICE_KEY"):
        pub = Publisher(env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"], DATA, log=log)
        pub.hello["channels"] = hub.channels
        local_publish = hub.publish

        def publish(event, payload):
            local_publish(event, payload)
            pub.event(event, payload)
        hub.publish = publish
    out = (DATA / "transcripts.jsonl").open("a")
    last_match, pending_rb, last_atis, seq = {}, {}, [0.0], [0]

    def prompt():
        ac = [a for a in tracker.snapshot(trails=False) if a.get("callsign")]
        d = lambda a: math.hypot(a["lat"] - SBGR[0], (a["lon"] - SBGR[1]) * math.cos(math.radians(SBGR[0])))
        return PROMPT + " " + ", ".join(spoken(a["callsign"]) for a in sorted(ac, key=d)[:20]) + "."

    def local(clip):
        try:
            from .transcribe import transcribe  # lazy: MLX Whisper (Mac only) loads only if Groq is unavailable
        except ImportError:
            return None
        return transcribe(clip, [spoken(c) for c in tracker.callsigns()[:15]], fast=True)

    def on_result(r):
        text = (r.get("text") or "").strip()
        if not text or JUNK.search(text) or looping(text) or r.get("no_speech", 0) > .6:
            return
        ch, start = r["ch"], r["start"]
        callsign, score = match(text, tracker.callsigns())
        inferred = False
        if callsign:
            last_match[ch] = (start + r["duration"], callsign)
        elif ch in last_match and start - last_match[ch][0] < READBACK_S:
            callsign, score, inferred = last_match[ch][1], .25, True
        it = parse(text)
        who = it["who"]
        if callsign:  # pilots end with their callsign; controllers lead with it
            num = re.sub(r"^[A-Z]+0*", "", callsign)
            digits = "".join(c for c in text if c.isdigit())
            pos = digits.rfind(num) if num else -1
            if inferred or (pos >= 0 and pos + len(num) >= len(digits) - 1 and pos > 0):
                who = "plt"
        seq[0] += 1
        rec = {"id": seq[0], "ts": datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="milliseconds"), "t": start,
               "feed": r["feed"], "ch": ch, "duration": round(r["duration"], 2), "text": text, "language": r.get("language"),
               "engine": r.get("engine"), "cs": callsign, "score": score, "inferred": inferred, "who": who,
               "chips": it["chips"], "intent": {k: it[k] for k in ("verb", "runway", "taxiways", "holds") if it.get(k)},
               "clip": r.get("path")}
        if callsign and who == "atc" and it["verb"]:
            pending_rb[callsign] = seq[0]
            apt.on_intent(callsign, {"verb": it["verb"], "runway": it["runway"]}, start)
            if it["verb"] == "TÁXI" and it.get("holds") and it["runway"]:
                apt.on_intent(callsign, {"verb": "HOLD", "runway": it["runway"]}, start)
        if who == "plt" and callsign in pending_rb:
            rec["readback_of"] = pending_rb.pop(callsign)
        if it["verb"] == "ARREMETA" and callsign:
            rec["alert"] = "ARREMETIDA"
            hub.publish("alert", {"kind": "ARREMETIDA", "cs": callsign, "rw": it.get("runway"), "t": start, "src": "radio"})
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        hub.publish("tx", rec)
        log(f"[{ch}|{r.get('engine')}|{who}] {('→ ' + callsign + ' ') if callsign else ''}{text}")

    batcher = GroqBatcher(load_keys(ROOT), on_result, local_fallback=local, log=log, prompt_fn=prompt).start()
    hub.stats = batcher.stats
    log(f"groq keys: {len(batcher.keys)}")

    def feed_worker(key, f):
        url = f"{cfg['base']}:{f['path']}"
        for clip in transmissions(stream_chunks(url, log=lambda m: log(f"[{key}] {m}"))):
            start = time.time() - len(clip) / SR
            if f["ch"] == "atis":
                if start - last_atis[0] < ATIS_EVERY:
                    continue
                last_atis[0] = start
            t = datetime.fromtimestamp(start, timezone.utc)
            rel = (Path("clips") / "SBGR" / key / f"{t:%Y-%m-%d}" / f"{t:%H%M%S_%f}"[:-3]).with_suffix(".ogg")
            (DATA / rel).parent.mkdir(parents=True, exist_ok=True)
            sf.write(DATA / rel, clip, SR, format="OGG", subtype="OPUS")
            hub.publish("vox", {"ch": f["ch"], "start": start, "duration": round(len(clip) / SR, 2)})
            batcher.add({"clip": clip, "feed": key, "ch": f["ch"], "start": start, "duration": len(clip) / SR, "path": str(rel)})

    for key, f in feeds.items():
        threading.Thread(target=feed_worker, args=(key, f), daemon=True).start()
        log(f"listening {f['name']} {f['freq']}")

    # ---------------------------------------------------------------- airport state loop
    alerted = {}
    on_ground, movements = {}, []   # hex -> bool ; [(t, kind)]

    def alert(key, every, payload):
        now = time.time()
        if now - alerted.get(key, 0) > every:
            alerted[key] = now
            hub.publish("alert", {**payload, "t": now, "src": "adsb"})
            log(f"⚠ {payload['kind']} {payload.get('cs')}")

    while True:
        now = time.time()
        acs = []
        sched = gru.by_callsign()
        if gru.at and gru.at != getattr(hub, "flights_at", 0):
            hub.flights, hub.flights_at = gru.flights, gru.at
            hub.publish("flights", gru.flights)
        for a in tracker.snapshot(trails=False):
            x, y = AP.xy(a["lat"], a["lon"])
            e = {**a, "x": x, "y": y}
            e["kind"], e["phase"] = apt.classify(e, now)
            r = routes.get(a.get("callsign"))
            if r:
                e["route"] = r
            fl = sched.get(a.get("callsign") or "")
            if fl:
                e["flight"] = {k: fl[k] for k in ("dir", "city", "gate", "terminal", "sched", "est", "status")}
            cs = a.get("callsign") or a["hex"]
            if apt.go_around(e, now):
                e["kind"], e["phase"] = "alert", "ARREMETEU"
                alert(("GA", a["hex"]), 300, {"kind": "ARREMETIDA", "cs": cs})
            sq = a.get("squawk")
            if sq in ("7500", "7600", "7700"):
                alert((sq, a["hex"]), 600, {"kind": {"7700": "EMERGÊNCIA", "7600": "FALHA DE RÁDIO", "7500": "INTERFERÊNCIA ILÍCITA"}[sq], "cs": cs})
            hold = apt.holds.get(a.get("callsign"))
            if hold:
                d, u = AP.seg(x, y, hold["rw"])
                clr = apt.clear.get(hold["rw"]) or {}
                if d < 25 and 0 < u < 1 and clr.get("cs") != a.get("callsign"):
                    alert(("INC", a["hex"]), 300, {"kind": "INCURSÃO", "cs": cs, "rw": hold["rw"]})
                if now - hold["since"] > 900:
                    apt.holds.pop(a.get("callsign"), None)
                elif (hp := AP.hold_near(x, y)):
                    e["hold"] = {"rw": hold["rw"], "x": hp[0], "y": hp[1]}
            g = bool(a.get("ground")) or ((a.get("alt") or 0) - AP.FIELD_FT < 60 and (a.get("gs") or 0) < 160)
            near = math.hypot(x, y) < 6000
            if near and a["hex"] in on_ground and on_ground[a["hex"]] != g:
                movements.append((now, "dep" if not g else "arr"))
            on_ground[a["hex"]] = g
            acs.append(e)
        movements[:] = [m for m in movements if now - m[0] < 3600]
        hub.state = {"t": now, "ac": [{k: e[k] for k in KEEP if k in e} for e in acs], "rwy": apt.runway_state(acs, now),
                     "stats": dict(batcher.stats), "metar": metar,
                     "movs": {"hour": len(movements), "dep": sum(1 for m in movements if m[1] == "dep"), "arr": sum(1 for m in movements if m[1] == "arr")}}
        hub.publish("state", hub.state)
        time.sleep(2)
