"""Nightly episode maker: last 24 h from the HF archive → interesting moments → episode.json → render → check → (post).

    python -m atc_br.nightly                    # window = the 24 h ending at the last 04:00 BRT
    python -m atc_br.nightly --end 2026-10-15T07:00Z --max 3 --no-post

Runs in GitHub Actions (free, no Mac). Free models only: Groq (Whisper + gpt-oss-120b), Cerebras / OpenRouter as fallback.
The LLM only writes words (picks, steps, notes, title, caption); camera, hashtags, timings and every check are code,
so a video that breaks the template is fixed and re-rendered here, never posted broken and never silently dropped.
"""
import argparse
import gzip
import json
import math
import os
import re
import subprocess
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .archive import REPO
from .callsigns import match, spoken
from .publisher import load_env

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = ROOT / "episodes"
BRT = timezone(timedelta(hours=-3))
SBGR = (-23.4356, -46.4731)
FIELD = 2459
NEUTRAL = "Aeroporto de Guarulhos, controle de tráfego aéreo."
BASE_TAGS = ["#aviação", "#guarulhos", "#aeroporto", "#radioaereo"]
BANNED_TAGS = {"#avgeek"}
GUIDE = ROOT / "tiktoks" / "EPISODE_GUIDE.md"
EXAMPLES = ["2026-10-14_decolagem-cancelada"]   # one full example fits the free models' token budget
KEYWORDS = re.compile(
    r"arremet|go.?around|emerg|mayday|pan.?pan|m[ée]dic|ambul[âa]n|bombeir|passando mal|p[áa]ssar|\bave[s]?\b|bird|pipa|bal[ãa]o|"
    r"drone|cancel|abort|interromp|rejeit|falha|\bpane\b|problema|desvi|alternad|retorn|combust[íi]vel|fuel|tempestade|\bcb\b|"
    r"windshear|tesoura|cortante|ve[íi]culo|inspe[çc]|objeto|fod\b|expedit|imediatamente|urgente|negativo|corrig|aten[çc][ãa]o|"
    r"cuidado|tcas|resolution|espera|holding|[óo]rbita|militar|for[çc]a a[ée]rea|presiden|helic[óo]ptero|incurs|invadi|"
    r"descontinu|vento de cauda|tailwind|bloque|pista fechada|fechad|sem luz|luzes|stand|posi[çc][ãa]o ocupada|reboque|"
    r"tripula|jornada|duty|crew|nunca|absurd|obrigado pela paci|desculp|sorry|unable|n[ãa]o consigo", re.I)
RARE_TYPES = {"B748", "B744", "B742", "A346", "A343", "A342", "A124", "AN12", "AN26", "IL76", "C130", "C30J", "KC39",
              "E3TF", "MD11", "DC10", "C17", "A400", "K35R", "B52", "C5M", "A3ST", "BLCF", "B703", "CONC"}   # not GRU daily widebodies (A380 comes every day)
CHANNELS = {"torre": "Torre de Guarulhos (pistas, pousos e decolagens)", "final": "Controle São Paulo / aproximação (não é a torre)",
            "solo1269": "Solo de Guarulhos (táxi)", "solo1217": "Solo de Guarulhos (táxi)",
            "trafego": "Tráfego / autorização de plano de voo", "atis": "ATIS"}
log = lambda m: print(f"{datetime.now():%H:%M:%S} · {m}", flush=True)


# ---------------------------------------------------------------- keys + free models
ENV = load_env(ROOT)
for k in ("CEREBRAS_API_KEYS", "OPENROUTER_API_KEYS"):
    ENV.setdefault(k, os.environ.get(k, ""))
KEYS = {p: [k.strip() for k in ENV.get(f"{p.upper()}_API_KEYS", "").split(",") if k.strip()] for p in ("groq", "cerebras", "openrouter")}
LLMS = [("groq", "https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),   # 8k tokens/min per key
        ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "nvidia/nemotron-3-ultra-550b-a55b:free"),
        ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "google/gemma-4-31b-it:free"),
        ("groq", "https://api.groq.com/openai/v1/chat/completions", "qwen/qwen3.8-27b")]   # Cerebras free tier is gone (402)
_turn = {"n": 0}


def _key(p):
    _turn["n"] += 1
    return KEYS[p][_turn["n"] % len(KEYS[p])]


WRITER_FIRST = [LLMS[1], LLMS[0], LLMS[2], LLMS[3]]   # Nemotron Ultra writes the best stories; gpt-oss then reviews them


def chat(system, user, tries=8, order=None):
    """JSON answer from the first free model that responds; rotates keys and backs off on 429."""
    last = []
    for i in range(tries):
        last = []
        for p, url, model in order or LLMS:
            if not KEYS[p]:
                continue
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {_key(p)}"}, timeout=180, json={
                    "model": model, "temperature": .4, "max_tokens": 4000,
                    **({"response_format": {"type": "json_object"}} if p == "groq" else {}),
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
                if r.status_code == 200:
                    txt = r.json()["choices"][0]["message"]["content"]
                    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
                last.append(f"{p} {r.status_code} {r.text[:160]}")
            except Exception as e:
                last.append(f"{p} {e}")
        log(f"[llm] retry {i + 1}: {last}")
        time.sleep(min(60, 10 * (i + 1)))
    raise RuntimeError(f"no model answered: {last}")


RT = {}   # (clip, model, prompt) -> re-transcription (shared across attempts and episodes)


def phrase_prompt(cs, go_around=False):
    """Standard Guarulhos phraseology (no numbers, so nothing gets invented) + the plane's spoken callsign. Whisper hears
    'autorizado pouso 28 esquerda' instead of 'autorizado por um desequilíbrio'. 'Arremeter' only when ADS-B confirmed one."""
    return (f"Torre Guarulhos, Controle São Paulo, Solo Guarulhos. {spoken(cs)}. Pista 28 esquerda, 28 direita, 10 esquerda, "
            "10 direita. Autorizado pouso, autorizado decolagem, vento, graus, nós, mantenha, suba, curva, aproximação, solo, "
            "estabilizado no ILS, localizador, aproximação final."
            + (" Arremeter." if go_around else ""))


def retranscribe(clip, hint="", model="whisper-large-v3", prompt=None):
    prompt = prompt or (NEUTRAL + " " + hint).strip()
    if (clip, model, prompt) in RT:
        return RT[(clip, model, prompt)]
    for i in range(12):
        try:
            with open(DATA / clip, "rb") as f:
                r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=90,
                                  headers={"Authorization": f"Bearer {_key('groq')}"}, files={"file": f},
                                  data={"model": model, "prompt": prompt,
                                        "temperature": 0, "response_format": "verbose_json"})
            if r.status_code == 200:
                j = r.json()
                text = j.get("text", "").strip()
                if "controle de tráfego aéreo" in text.lower() or "aeroporto de guarulhos" in text.lower() \
                        or "solo guarulhos. " in text.lower() or "vento, graus, nós" in text.lower():
                    text = ""          # Whisper repeating its prompt = it heard nothing usable
                RT[(clip, model, prompt)] = {"text": text, "language": j.get("language", ""), "duration": j.get("duration", 0)}
                return RT[(clip, model, prompt)]
            if r.status_code != 429:
                log(f"[whisper] {r.status_code} {r.text[:120]}")
        except Exception as e:
            log(f"[whisper] {e}")
        time.sleep(20 + 10 * i)
    return None


# ---------------------------------------------------------------- data from the archive
def fetch(t0, t1):
    """Download every archived hour in [t0, t1) and lay it out like the Mac's data/ folder."""
    from huggingface_hub import HfApi, hf_hub_download
    api = HfApi(token=ENV.get("HF_TOKEN"))
    have = sorted(f for f in api.list_repo_files(REPO, repo_type="dataset") if f.endswith(".tar"))
    hour = lambda f: datetime.strptime(f[:-4], "%Y-%m-%d/%H").replace(tzinfo=timezone.utc).timestamp()
    want = [f for f in have if t0 - 3600 < hour(f) < t1]   # every hour that overlaps the window
    (DATA / "adsb").mkdir(parents=True, exist_ok=True)
    tx = (DATA / "transcripts.jsonl").open("a")
    for f in want:
        p = hf_hub_download(REPO, f, repo_type="dataset", token=ENV.get("HF_TOKEN"))
        with tarfile.open(p) as tar:
            for m in tar.getmembers():
                if m.name.startswith("clips/"):
                    tar.extract(m, DATA, filter="data")
                elif m.name == "transcripts.jsonl":
                    tx.write(tar.extractfile(m).read().decode())
                elif m.name == "adsb.jsonl":
                    lines = tar.extractfile(m).read().decode().splitlines(keepends=True)
                    if lines:
                        day = datetime.fromtimestamp(json.loads(lines[0])["t"], timezone.utc).strftime("%Y-%m-%d")
                        with gzip.open(DATA / "adsb" / f"{day}.jsonl.gz", "at") as g:
                            g.writelines(lines)
    tx.close()
    log(f"fetched {len(want)} archived hours")
    return len(want)


def dist_km(lat, lon):
    return math.hypot(lat - SBGR[0], (lon - SBGR[1]) * math.cos(math.radians(SBGR[0]))) * 111.2


def raw_tracks(t0, t1):
    """hex -> [(t, lat, lon, alt, gs, ground, callsign, type, squawk, reg)] straight from the log (no smoothing)."""
    tr = {}
    d = datetime.fromtimestamp(t0, timezone.utc).date()
    while d <= datetime.fromtimestamp(t1, timezone.utc).date():
        p = DATA / "adsb" / f"{d}.jsonl.gz"
        if p.exists():
            try:
                for line in gzip.open(p, "rt"):
                    s = json.loads(line)
                    if t0 <= s["t"] < t1:
                        for a in s["ac"]:
                            if a.get("lat") is not None:
                                tr.setdefault(a["hex"], []).append((s["t"], a["lat"], a["lon"], a.get("alt") or 0, a.get("gs") or 0,
                                                                    bool(a.get("ground")), (a.get("callsign") or "").strip(),
                                                                    a.get("type") or "", a.get("squawk"), a.get("reg") or ""))
            except (EOFError, OSError):
                pass
        d += timedelta(days=1)
    for v in tr.values():
        v.sort()
    return tr


def adsb_events(tracks):
    """Go-arounds (low on final, then climbing with no touchdown), emergency squawks, rare types near the field."""
    ev = []
    for hx, v in tracks.items():
        cs = next((p[6] for p in v if p[6]), hx)
        typ = next((p[7] for p in v if p[7]), "")
        for i, p in enumerate(v):
            if p[5] or dist_km(p[1], p[2]) > 12 or not 0 < p[3] - FIELD < 1500:
                continue
            before = [q for q in v[:i] if p[0] - 240 <= q[0] < p[0]]
            if not any(q[3] - p[3] > 400 and dist_km(q[1], q[2]) > dist_km(p[1], p[2]) for q in before) or any(q[5] for q in before):
                continue   # must be descending towards the field (not a departure climbing out)
            low = p[3]
            for q in v[i + 1:]:
                if q[0] - p[0] > 300 or q[5]:
                    break
                low = min(low, q[3])
                if q[3] - low > 800 and dist_km(q[1], q[2]) < 20:
                    ev.append({"t": p[0], "hex": hx, "cs": cs, "type": typ, "what":
                               f"ADS-B: {cs} ({typ}) desceu a {low - FIELD:.0f} ft sobre o campo a {dist_km(p[1], p[2]):.1f} km "
                               f"e voltou a subir pra {q[3] - FIELD:.0f} ft sem tocar a pista (provável arremetida)"})
                    break
            else:
                continue
            break
        sq = next((p for p in v if p[8] in ("7500", "7600", "7700")), None)
        if sq:
            ev.append({"t": sq[0], "hex": hx, "cs": cs, "type": typ, "what": f"ADS-B: {cs} ({typ}) transponder {sq[8]}"})
        if typ in RARE_TYPES:
            near = next((p for p in v if dist_km(p[1], p[2]) < 8), None)
            if near:
                ev.append({"t": near[0], "hex": hx, "cs": cs, "type": typ, "what": f"ADS-B: {typ} incomum ({cs}) perto do campo",
                           "weak": True})
    # merge duplicates of the same aircraft within 10 min
    ev.sort(key=lambda e: e["t"])
    out = []
    for e in ev:
        if any(o["hex"] == e["hex"] and abs(o["t"] - e["t"]) < 600 for o in out):
            continue
        out.append(e)
    return out


def candidates(t0, t1, tracks):
    tx = [d for d in map(json.loads, open(DATA / "transcripts.jsonl")) if t0 <= d.get("t", 0) < t1 and d.get("clip")]
    tx = [d for d in tx if d["feed"] != "atis" and (DATA / d["clip"]).exists()]
    tx.sort(key=lambda d: d["t"])
    seeds = [{"t": d["t"], "why": f"palavra-chave: {KEYWORDS.search(d['text']).group(0)}"} for d in tx
             if d.get("duration", 0) >= 1.5 and KEYWORDS.search(d["text"] or "")]
    seeds += [{"t": e["t"], "why": e["what"], "hex": e["hex"], "weak": e.get("weak")} for e in adsb_events(tracks)]
    seeds.sort(key=lambda s: s["t"])
    groups = []
    for s in seeds:
        if groups and s["t"] - groups[-1]["t1"] < 240:
            g = groups[-1]
            g["t1"] = s["t"]
            g["seeds"].append(s["t"])
            g["why"].append(s["why"])
            g["hex"] = g.get("hex") or s.get("hex")
            g["weak"] = bool(g["weak"] and s.get("weak"))
        else:
            groups.append({"t0": s["t"], "t1": s["t"], "seeds": [s["t"]], "why": [s["why"]], "hex": s.get("hex"),
                           "weak": bool(s.get("weak"))})
    out = []
    for g in groups:
        near = lambda d: min(abs(d["t"] - t) for t in g["seeds"])
        lines = sorted(sorted((d for d in tx if g["t0"] - 180 <= d["t"] <= g["t1"] + 180), key=near)[:40], key=lambda d: d["t"])
        if len(lines) < 2:
            continue
        g["lines"] = lines
        g["shown"] = sorted(sorted(lines, key=near)[:14], key=lambda d: d["t"])   # what the picker reads: closest to the signal
        g["pool"] = [d for d in tx if g["t0"] - 720 <= d["t"] <= g["t1"] + 720]  # whole conversation, for the writer
        out.append(g)
    out.sort(key=lambda g: (g["weak"], -len(set(g["why"]))))
    return out[:60]


def disk_clips(t0, t1, known):
    """Clips on disk in [t0, t1] that the live pipeline never logged (it drops transcripts that looked like junk)."""
    import soundfile as sf
    out = []
    for feed in ("torre", "final", "solo1269", "solo1217"):
        for day in {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in (t0, t1)}:
            for f in (DATA / "clips" / "SBGR" / feed / day).glob("*.ogg"):
                rel = str(f.relative_to(DATA))
                t = _clip_t(rel)
                if t0 <= t <= t1 and rel not in known:
                    try:
                        dur = sf.info(f).duration
                    except Exception:
                        continue
                    out.append({"id": -abs(hash(rel)) % 10**9, "t": t, "feed": feed, "duration": dur, "text": "", "clip": rel,
                                "who": None, "cs": None})
    return out


def fmt_line(d):
    return (f"[{d['id']}] {datetime.fromtimestamp(d['t'], BRT):%H:%M:%S} {d['feed']} {d.get('who') or '?'} "
            f"{d.get('cs') or ''} ({d['duration']:.1f}s): {d['text']}")


LANDING = re.compile(r"pouso autorizado|livre pouso|autorizad[oa] (?:o )?pouso|clear(?:ed)? to land", re.I)


def catalog_event(t, focus, story, clips, tracks):
    """A moment from the dig catalogs (tiktoks/CANDIDATES_*.md): its time, aircraft and key clips."""
    tx = [d for d in map(json.loads, open(DATA / "transcripts.jsonl")) if abs(d.get("t", 0) - t) <= 720 and d.get("clip")
          and (DATA / d["clip"]).exists()]
    pool = sorted(tx + disk_clips(t - 720, t + 720, {d["clip"] for d in tx}), key=lambda d: d["t"])
    full = []
    for c in clips:
        c = c if c.startswith("clips/") else "clips/SBGR/" + c
        c = c if c.endswith(".ogg") else c + ".ogg"
        full.append(c)
    lines = [d for d in pool if d["clip"] in full]
    log(f"catalog event: {len(lines)}/{len(full)} key clips found, pool {len(pool)}")
    # catalog entries were verified against ADS-B when they were dug; re-check the go-around from the track anyway
    why = ["catálogo: " + story] + [e["what"] for e in adsb_events({focus: tracks.get(focus, [])}) if abs(e["t"] - t) < 900]
    if re.search(r"arremet", story, re.I) and not any("arremetida" in w for w in why[1:]):
        why.append("arremetida confirmada no catálogo (ADS-B verificado na pesquisa)")
    log("signals: " + " | ".join(why[1:]))
    cand = {"t0": t, "t1": t, "seeds": [t], "why": why, "hex": focus, "weak": False,
            "lines": lines or pool[:20], "pool": pool, "shown": lines}
    return cand, {"candidate": 0, "score": 10, "story": story, "focus_hex_or_callsign": focus, "clips": full}


def normal_landing(t0, t1, tracks):
    """The first plain arrival of the window: cleared to land on torre and ADS-B shows it on the ground minutes later."""
    tx = sorted((d for d in map(json.loads, open(DATA / "transcripts.jsonl")) if t0 <= d.get("t", 0) < t1 and d.get("clip")
                 and (DATA / d["clip"]).exists()), key=lambda d: d["t"])
    by_cs = {}
    for h, v in tracks.items():
        for q in v:
            if q[6]:
                by_cs.setdefault(q[6], h)
    for d in tx:
        if d["feed"] != "torre" or not d.get("cs") or not LANDING.search(d["text"]) or d.get("duration", 0) < 2.5:
            continue
        h = by_cs.get(d["cs"])
        v = tracks.get(h or "", [])
        if not any(q[5] and d["t"] < q[0] < d["t"] + 400 and dist_km(q[1], q[2]) < 4 for q in v):
            continue
        lines = [x for x in tx if x.get("cs") == d["cs"] and d["t"] - 240 <= x["t"] <= d["t"] + 240]
        if len(lines) < 2:
            continue
        cand = {"t0": lines[0]["t"], "t1": lines[-1]["t"], "why": ["pouso normal"], "hex": h, "weak": False, "lines": lines}
        return cand, {"candidate": 0, "score": 10, "story": f"Um pouso de rotina em Guarulhos: o {d['cs']} chama a torre, "
                      "recebe a autorização e pousa", "focus_hex_or_callsign": h, "line_ids": [x["id"] for x in lines][:6]}
    return None, None


# ---------------------------------------------------------------- picking + writing
BRIEF = """Você escolhe momentos de rádio real do Aeroporto de Guarulhos (torre, solo, aproximação) para vídeos curtos
explicativos no TikTok (@radioaereo) para brasileiros comuns. Interessante = arremetidas (confirmadas por ADS-B), decolagens
canceladas/abortadas, emergências, médico a bordo, pássaros/pipa/balão/drone, falhas técnicas, desvios e retornos, espera por
tempestade/vento, inspeção de pista/objeto/veículo, mudança de pista, correções do controlador, readback errado, callsigns
trocados, frequência bloqueada, conversas tensas ou engraçadas, personalidade do controlador, aviões raros (militar, cargueiro,
A380, 747, VIP, helicóptero), tripulação estrangeira com dificuldade, separação apertada, "expedite", espera longa sem posição,
limite de jornada, tráfego recorde. Chato = autorização de rotina, táxi normal, troca de frequência, ATIS.
Só escolha o que renderia um vídeo de 25–50 s com começo, meio e fim que alguém assistiria até o fim. Atenção:
transcrições ao vivo têm erros; a palavra "arremeta" nelas não é confiável sem ADS-B. Clipes < 2 s costumam ser alucinação."""


def pick(cands, posted, per_call=5):
    """Score candidates in small batches (free tiers cap ~8k tokens a minute per key)."""
    picks = []
    for b in range(0, len(cands), per_call):
        blocks = []
        for i in range(b, min(b + per_call, len(cands))):
            g = cands[i]
            blocks.append(f"## candidato {i}\nsinais: {'; '.join(dict.fromkeys(g['why']))[:300]}\n"
                          + "\n".join(("★ " if any(abs(d["t"] - t) < 1 for t in g["seeds"]) else "") + fmt_line(d)[:190]
                                      for d in g["shown"]))
        user = (f"Já postados (não repetir a mesma história): {', '.join(posted[-30:]) or 'nenhum'}\n\n" + "\n\n".join(blocks) +
                '\n\nResponda JSON: {"picks": [{"candidate": n, "score": 1-10, "story": "uma frase", '
                '"focus_hex_or_callsign": "...", "line_ids": [ids das transmissões que contam a história, em ordem, 3–7]}]}. '
                'Inclua só score >= 7. Pode ser lista vazia.')
        try:
            picks += chat(BRIEF, user).get("picks", [])
        except Exception as e:
            log(f"[pick] batch {b}: {e}")
    return sorted(picks, key=lambda p: -p.get("score", 0) if isinstance(p, dict) else 0)


def facts(hexes, t0, t1, tracks):
    """Plain-text ADS-B summary of the focus aircraft for the writer (sampled every ~30 s)."""
    out = []
    for hx in hexes:
        v = [p for p in tracks.get(hx, []) if t0 - 120 <= p[0] <= t1 + 300]
        if not v:
            out.append(f"{hx}: sem ADS-B nesse horário")
            continue
        cs = next((p[6] for p in v if p[6]), hx)
        out.append(f"{hx} callsign {cs} tipo {next((p[7] for p in v if p[7]), '?')} matrícula {next((p[9] for p in v if p[9]), '?')}")
        last = 0
        for p in v:
            if p[0] - last >= 30:
                last = p[0]
                out.append(f"  {datetime.fromtimestamp(p[0], BRT):%H:%M:%S} {'SOLO' if p[5] else f'{p[3] - FIELD:.0f}ft acima do campo'} "
                           f"{p[4]:.0f}kt a {dist_km(p[1], p[2]):.1f}km do aeroporto")
    return "\n".join(out)


def flight_phase(hx, t0, t1, tracks):
    """What ADS-B says the focus plane really did around the story (trusted over any transcript or guess)."""
    v = [p for p in tracks.get(hx, []) if t0 - 900 <= p[0] <= t1 + 1200]
    if not v:
        return {"ev": [], "text": "sem ADS-B"}
    ev = [(e["t"], "arremeteu") for e in adsb_events({hx: tracks[hx]}) if "arremetida" in e["what"] and t0 - 900 <= e["t"] <= t1 + 1200]
    for a, b in zip(v, v[1:]):
        if a[5] != b[5] and (b[0] - a[0]) < 120:
            ev.append((b[0], "pousou" if b[5] else "decolou"))
    ev.sort()
    first = v[0]
    start = "no solo" if first[5] else f"no ar a {first[3] - FIELD:.0f} ft, a {dist_km(first[1], first[2]):.0f} km"
    kinds = {k for _, k in ev}
    role = ("CHEGANDO" if "pousou" in kinds or "arremeteu" in kinds or (not first[5] and v[-1][3] < first[3] - 300)
            else "PARTINDO" if "decolou" in kinds else "no solo (táxi)" if first[5] else "sobrevoando")
    txt = f"o avião estava {role} (começa {start}); " + ("; ".join(f"{k.upper()} às {datetime.fromtimestamp(t, BRT):%H:%M:%S}"
                                                            for t, k in ev) or "nenhum pouso/decolagem nesse período")
    if "decolou" not in kinds:
        txt += ". Ele NÃO decolou neste período"
    if "arremeteu" in kinds:
        txt += ". Arremeter = desistir do POUSO e subir de novo (não é abortar decolagem)"
    return {"ev": ev, "text": txt, "role": role}


def route(cs):
    try:
        fr = requests.get(f"https://api.adsbdb.com/v0/callsign/{cs}", timeout=10).json().get("response", {}).get("flightroute")
        return f"{(fr.get('airline') or {}).get('name')} {fr['origin']['municipality']} → {fr['destination']['municipality']}" if fr else ""
    except Exception:
        return ""


WRITER = """Você escreve o roteiro (episode.json) de um vídeo do Rádio Aéreo: um EXPLICADOR calmo para gente comum, nada
agitado. Regras do template (tiktoks/EPISODE_GUIDE.md + TEMPLATE.md):
- Abra direto na fala-chave (ou na preparação imediata dela). Uma ideia por beat. 4–7 beats, fala total ≤ 40 s
  (o vídeo inteiro tem limite de 50 s com o cartão final, que o renderizador já põe).
- Use SOMENTE as transmissões fornecidas (campo "clip" exato) e os fatos de ADS-B. Nunca invente o que foi dito nem o
  motivo; se for inferido, diga "provavelmente" / "pelo que os dados mostram". Sem nomes de passageiros/tripulantes.
- Legenda: rádio em português → "text" = transcrição literal, SEM "pt". O Whisper às vezes inventa palavras no começo/fim de
  áudio ruim (saudações em outra língua, "obrigado por assistir"…): corte essas partes com "…" — nunca traduza lixo.
  Rádio em inglês → "pt" = tradução simples em português (texto grande) e "text" = original (pequeno).
- "who": "atc" (controlador) ou "plt" (piloto). "step": uma frase curta (máx 90 caracteres), português simples, o que está
  acontecendo agora. Texto curto = cartão pequeno = o mapa e o avião ficam visíveis (regra do template).
- "note" (máx 75 caracteres) só quando aparece jargão, uma vez por termo, formato "Termo: explicação curta" (Solo, Torre, heavy, arremeter,
  milha, pista de táxi, alinhe e mantenha…). "taxi": letras de taxiway citadas, se houver.
- Beat "silent" no fim só se o ADS-B mostrar o desfecho: {"silent": 3.0, "span": segundos reais, "t": epoch, "step": "..."}.
- "kind": etiqueta curta. "kicker": "Companhia · Avião · DD/MM, HH:MM" (horário de Brasília). "title": ≤ 40 caracteres, gancho.
- "caption": 1–2 frases que contam a história e termina com "🎧 Áudio real do rádio, DD/MM."
- "hashtags": 1–3 específicas (companhia, avião, evento). Nunca #avgeek. "focus": hex do avião principal (e do segundo, se houver).
- Chame cada canal pelo nome certo (Torre, Controle São Paulo, Solo, Tráfego) — "final" é o Controle São Paulo, não a torre.
- Todas as transmissões têm que ser da MESMA história e do avião em foco (ou de quem fala com ele). Escolha 3–6 transmissões
  que contam essa história do começo ao fim. Só se o material não tiver história nenhuma, responda {"skip": "<explique por quê>"}.
- Não escreva "camera". Responda só o JSON do episódio."""


def write(pick_, cand, tracks, fix=None, prev=None):
    ids = set(pick_.get("line_ids") or [])
    pool = cand.get("pool") or cand["lines"]
    picked = [d for d in pool if d["id"] in ids or d["clip"] in (pick_.get("clips") or [])] or cand["lines"][:7]
    fh0 = (pick_.get("focus_hex_or_callsign") or cand.get("hex") or "").strip()
    cs0 = next((q[6] for q in tracks.get(fh0.lower(), []) if q[6]), fh0.upper())
    # the live transcripts are too rough to find the plane's own calls: re-hear every clip around the event (turbo model,
    # neutral prompt + the plane's spoken callsign) and keep the ones that name it
    mid = sum(cand.get("seeds") or [picked[0]["t"]]) / len(cand.get("seeds") or [1])
    pp = phrase_prompt(cs0, go_around=any("arremetida" in w for w in cand["why"]))
    if not cand.get("_swept"):
        pool += disk_clips(mid - 420, mid + 420, {d["clip"] for d in pool})
        pool.sort(key=lambda d: d["t"])
        cand["pool"] = pool
        for d in pool:
            if abs(d["t"] - mid) <= 420 and d.get("duration", 0) >= 1.5 and d["feed"] in ("torre", "final", "solo1269", "solo1217"):
                r = retranscribe(d["clip"], prompt=pp)   # turbo echoes the prompt on clipped radio; large-v3 hears it
                if r:
                    d["_heard"] = r["text"]
        cand["_swept"] = True
    talk = [d for d in pool if d.get("_heard") and match(d["_heard"], [cs0])[1] >= .4]
    lines = sorted({d["id"]: d for d in picked + talk}.values(), key=lambda d: d["t"])
    if len(lines) > 16:
        lines = sorted(sorted(lines, key=lambda d: (d not in talk, abs(d["t"] - mid)))[:16], key=lambda d: d["t"])
    log(f"  writer gets {len(lines)} lines ({len(talk)} name {cs0})")
    hint = spoken(cs0)
    rt = []
    for d in lines:
        r = d.get("_rt") or retranscribe(d["clip"], prompt=pp)
        d["_rt"] = r
        if not r:
            log(f"  [whisper] gave up on {d['clip']}")
        if r and r["text"] and r["duration"] >= 1.2 and r["language"].lower()[:2] in ("pt", "po", "en"):
            off = d["t"] - mid
            rt.append(f"clip {d['clip']} | {datetime.fromtimestamp(d['t'], BRT):%H:%M:%S} (evento {'+' if off >= 0 else '−'}"
                      f"{int(abs(off)) // 60}:{int(abs(off)) % 60:02d}) | canal {CHANNELS.get(d['feed'], d['feed'])} | quem(chute) "
                      f"{d.get('who')} | idioma {r['language']} | {r['duration']:.1f}s | {r['text']}")
    fh = pick_.get("focus_hex_or_callsign") or cand.get("hex") or ""
    hexes = [h for h in tracks if h == fh.lower()] or [h for h, v in tracks.items() if any(p[6] == fh.upper() for p in v)]
    if not hexes and cand.get("hex"):
        hexes = [cand["hex"]]
    t0, t1 = lines[0]["t"], lines[-1]["t"]
    cs = next((p[6] for h in hexes for p in tracks[h] if p[6]), fh)
    ex = "\n\n".join((ROOT / "tiktoks" / "staged" / e / "episode.json").read_text() for e in EXAMPLES
                     if (ROOT / "tiktoks" / "staged" / e / "episode.json").exists())
    user = (f"EXEMPLO (episódio já postado, siga o estilo):\n{ex}\n\nPALPITE (de transcrições ao vivo ruins, pode estar errado; conte a história que as transmissões "
            f"retranscritas abaixo e o ADS-B realmente mostram): {pick_.get('story')}\nSINAIS: {'; '.join(cand['why'])}\n"
            f"EVENTO por volta de {datetime.fromtimestamp(mid, BRT):%H:%M:%S}: os beats têm que mostrar o que leva a ele, o momento em si "
            f"(a fala-chave) e o que vem logo depois.\n"
            f"FATOS DO ADS-B (confiáveis, valem mais que o palpite e que qualquer transcrição): "
            f"{flight_phase(hexes[0], t0, t1, tracks)['text'] if hexes else 'sem ADS-B'}\n"
            f"ROTA: {route(cs)}\nFOCO: {', '.join(hexes) or fh}\nADS-B:\n{chr(10).join(facts(hexes, t0, t1, tracks).splitlines()[:30])}\n\n"
            f"TRANSMISSÕES (retranscritas):\n" + "\n".join(rt))
    if fix:
        user += f"\n\nSUA VERSÃO ANTERIOR:\n{json.dumps(prev, ensure_ascii=False)}\n\nCORRIJA ESTES PROBLEMAS:\n- " + "\n- ".join(fix)
    spec = chat(WRITER, user, order=WRITER_FIRST)
    spec["_input"] = user
    spec["focus"] = hexes[:2] + [f for f in spec.get("focus", []) if f not in hexes][:max(0, 2 - len(hexes))] if hexes else spec.get("focus", [])
    return spec, {d["clip"]: d["_rt"] for d in lines if d.get("_rt")}


# ---------------------------------------------------------------- template + honesty checks (code, not the model)
def norm(s):
    return re.findall(r"\w+", (s or "").lower())


def camera(spec, tracks):
    """Deterministic camera that keeps the template: follow focus[0], zoom from altitude / spread of the two focus planes."""
    ts = [c.get("t") or _clip_t(c["clip"]) for c in spec["clips"]]
    hexes = [f for f in spec.get("focus", []) if f in tracks]
    if not hexes:
        return {"follow": False, "vw": 3300, "vw_end": 3000, "oy": .42}
    v = [p for p in tracks[hexes[0]] if min(ts) - 30 <= p[0] <= max(ts) + 120]
    air = [p[3] - FIELD for p in v if not p[5]]
    vw = 1500 if not air or max(air) < 200 else (4500 if max(air) < 2500 else 9000)
    if len(hexes) > 1:
        w = [p for p in tracks[hexes[1]] if min(ts) - 30 <= p[0] <= max(ts) + 120]
        spread = max((math.hypot((p[1] - q[1]) * 111200, (p[2] - q[2]) * 102000) for p in v[::3] for q in w[::3]
                      if abs(p[0] - q[0]) < 15), default=0)
        vw = max(vw, min(12000, spread * 1.7))
    return {"follow": True, "vw": round(vw), "vw_end": round(vw * .85), "oy": .42 if vw < 3000 else .44}


def _clip_t(rel):
    parts = rel.split("/")
    hms, ms = Path(parts[-1]).stem.split("_")
    return datetime.strptime(parts[-2] + hms, "%Y-%m-%d%H%M%S").replace(tzinfo=timezone.utc).timestamp() + int(ms) / 1000


def problems(spec, rts, tracks):
    p = []
    clips = [c for c in spec.get("clips", []) if c.get("clip")]
    if not 4 <= len(spec.get("clips", [])) <= 7:
        p.append(f"tem {len(spec.get('clips', []))} beats; precisa de 4 a 7")
    if len(clips) < 2:
        p.append("precisa de pelo menos 2 transmissões de rádio")
    speech = sum((rts.get(c["clip"]) or {}).get("duration", 0) for c in clips)
    if speech > 40:
        p.append(f"fala soma {speech:.0f}s; tire transmissões até ficar ≤ 40 s")
    if len(spec.get("title", "")) > 42:
        p.append(f"título com {len(spec['title'])} caracteres; máximo 40")
    for c in spec.get("clips", []):
        if not c.get("step"):
            p.append(f"beat sem step: {c.get('clip') or 'silent'}")
        elif len(c["step"]) > 90:
            p.append(f"step longo demais ({len(c['step'])} caracteres), máx 90: {c['step']}")
        if c.get("note") and len(c["note"]) > 75:
            p.append(f"note longa demais ({len(c['note'])} caracteres), máx 75: {c['note']}")
        if c.get("silent") is not None:
            first = min((_clip_t(x["clip"]) for x in clips), default=0)
            last = max((_clip_t(x["clip"]) for x in clips), default=0)
            if not c.get("t") or not last - 5 <= c["t"] <= last + 900 or not 10 <= c.get("span", 0) <= 930:
                p.append(f"beat silent precisa de 't' entre o último clip ({last:.0f}) e +15 min, e 'span' 10–930 s")
            elif c["t"] < first:
                p.append("beat silent antes do primeiro clip")
        if not c.get("clip"):
            continue
        if not (DATA / c["clip"]).exists() or c["clip"] not in rts:
            p.append(f"clip inexistente ou não fornecido: {c['clip']}")
            continue
        r = rts[c["clip"]]
        said, real = norm(c.get("text")), set(norm(r["text"]))
        if not said or sum(w in real for w in said) / len(said) < .8:
            p.append(f"'text' de {c['clip']} não bate com o áudio; use a transcrição literal: {r['text']}")
        en = r["language"].lower().startswith(("en", "english"))
        if en and not c.get("pt"):
            p.append(f"{c['clip']} é em inglês: falta 'pt' com a tradução")
        if not en and c.get("pt"):
            c.pop("pt")          # Portuguese radio: big text is the verbatim transcript only
        if c.get("who") not in ("atc", "plt"):
            c["who"] = "atc"
        st = (c.get("step") or "").lower()          # the sentence must not put the words in the wrong mouth
        says_atc = re.match(r"(a |o )?(torre|controle|controlador|solo|aproxima)", st)
        says_plt = re.match(r"(o |a )?(piloto|tripula|comandante)", st)
        if not c.pop("_sure", False) and (says_atc or says_plt):   # only a callsign-position guess: trust the writer's sentence
            c["who"] = "atc" if says_atc else "plt"
        if c["who"] == "plt" and says_atc:
            p.append(f"beat {spec['clips'].index(c) + 1}: quem fala é o PILOTO ({r['text'][:60]}…), mas o step diz que é a torre/controle")
        if c["who"] == "atc" and says_plt:
            p.append(f"beat {spec['clips'].index(c) + 1}: quem fala é o CONTROLADOR ({r['text'][:60]}…), mas o step diz que é o piloto")
    if not re.search(r"🎧 Áudio real do rádio, \d\d/\d\d\.$", spec.get("caption", "").strip()):
        p.append("caption precisa terminar com '🎧 Áudio real do rádio, DD/MM.'")
    said_ga = any(re.search(r"arremet|go.?around", (r or {}).get("text", ""), re.I) for r in rts.values())
    if re.search(r"arremet", json.dumps({k: spec.get(k) for k in ("title", "caption", "clips")}, ensure_ascii=False), re.I) \
            and not said_ga and not any("arremetida" in w for w in spec.get("_why", [])):
        p.append("o roteiro fala em arremetida mas nem o áudio nem o ADS-B confirmam; diga só o que o rádio mostra")
    focus = [f for f in spec.get("focus", []) if f in tracks]
    if focus and clips:
        ts = [_clip_t(c["clip"]) for c in clips]
        ph = flight_phase(focus[0], min(ts), max(ts), tracks)
        story = " ".join([spec.get("title", ""), spec.get("caption", "")] + [c.get("step", "") for c in spec["clips"]]).lower()
        kinds = {k for _, k in ph["ev"]}
        if "decolou" not in kinds and re.search(r"decol\w*|partida|abort\w*", story) and ph.get("role") != "PARTINDO":
            p.append(f"FATO DO ADS-B: {ph['text']}. A história não pode dizer que ele decolou/abortou a decolagem")
        if "pousou" not in kinds and "arremeteu" not in kinds and re.search(r"\bpous(ou|a)\b(?! autoriz)", story) \
                and ph.get("role") == "PARTINDO":
            p.append(f"FATO DO ADS-B: {ph['text']}. Ele estava partindo; não diga que pousou")
    try:
        from . import reel                    # same window maths as the renderer, before spending a render on it
        c0 = next((c for c in clips), None)
        hh = reel._header_layer(spec, _clip_t(c0["clip"]) if c0 else None)[1]
        need = 200 if len(focus) > 1 else 80
        for i, c in enumerate(spec.get("clips", [])):
            lo = reel._step_bottom(c, hh) + reel.PLANE_CLEAR_UP
            hi = (reel._card_layout(c)[0] if c.get("clip") else reel.SAFE_B - 60) - reel.PLANE_CLEAR_DOWN
            if hi - lo < need:
                p.append(f"beat {i + 1}: step + cartão altos demais pro avião aparecer — encurte o step (≤ 70 caracteres), "
                         "tire a note desse beat ou corte o text/pt com '…'")
    except Exception as e:
        log(f"[layout precheck] {e}")
    if not focus:
        p.append("foco sem ADS-B: escolha o hex de um avião da história que aparece nos dados")
    else:
        cs = next((q[6] for q in tracks[focus[0]] if q[6]), "")
        num = re.sub(r"^\D+0*", "", cs)
        hits = sum(1 for c in clips if num and num in re.sub(r"\D", "", (rts.get(c["clip"]) or {}).get("text", "")))
        if num and hits < min(2, len(clips)):
            p.append(f"o avião em foco ({cs}) quase não aparece nas falas; use as transmissões dele ou troque o foco")
    return p


def outcome(hexes, after, tracks):
    """First touchdown or lift-off of the focus aircraft within 15 min after `after` (epoch), or None."""
    v = [q for q in tracks.get(hexes[0], [])] if hexes else []
    for a, b in zip(v, v[1:]):
        if after <= b[0] <= after + 900 and a[5] != b[5]:
            return b[0], "pouso" if b[5] else "decolagem"
    return None


PILOT_SAYS = re.compile(r"\b(estabilizad\w*|ciente|copiad\w*|aguardando|iniciando|solicit\w*|pronto|nivelad\w*|mantendo|subindo|"
                        r"descendo|chamando|nós estamos|estamos|arremetendo|we have|we are|request\w*|ready|with you|established|"
                        r"climbing|descending|going around|wilco|roger|copied)\b")
ATC_SAYS = re.compile(r"\b(autorizad\w*|livre|mantenha|suba|desça|desca|curva|chame|reporte|aguarde|contate|arremeta|cruze|alinhe|"
                      r"prossiga|cleared|climb|descend|turn|contact|maintain|hold short|line up|go around|expect|report)\b")


def repair(spec, rts, tracks):
    """Things code gets right every time: the closing silent beat comes from ADS-B, English lines get a Portuguese line."""
    clips = [c for c in spec.get("clips", []) if c.get("clip")]
    sil = [c for c in spec.get("clips", []) if not c.get("clip")]
    spec["clips"] = list(clips)
    if clips:
        last = max(clips, key=lambda c: _clip_t(c["clip"]))
        end = _clip_t(last["clip"]) + (rts.get(last["clip"]) or {}).get("duration", 3)
        ev = outcome([f for f in spec.get("focus", []) if f in tracks], end, tracks)
        if ev:
            step = (sil[-1].get("step") if sil else None) or ("O avião pousa." if ev[1] == "pouso" else "O avião decola.")
            spec["clips"].append({"silent": 3.0, "span": round(max(10, ev[0] - end + 15)), "t": end, "step": step})
    cs = next((q[6] for f in spec.get("focus", []) if f in tracks for q in tracks[f] if q[6]), "")
    num = re.sub(r"^\D+0*", "", cs)
    for c in clips:
        r = rts.get(c["clip"]) or {}
        w = re.findall(r"\w+", r.get("text", ""))
        pos = [i for i, x in enumerate(w) if num and num in x]
        said = (r.get("text") or "").lower()
        pl, at = bool(PILOT_SAYS.search(said)), bool(ATC_SAYS.search(said))
        c["_sure"] = pl != at                # wording decided it (an order vs a report)
        if pl and not at:                    # pilots report and read back; controllers give orders
            c["who"] = "plt"
        elif at and not pl:
            c["who"] = "atc"
        elif pos and c["clip"].split("/")[2] != "atis":   # radio convention: controllers lead with the callsign, pilots end with it
            if pos[-1] >= len(w) - 2 and pos[0] > 1:
                c["who"] = "plt"
            elif pos[0] <= 1 and pos[-1] < len(w) - 2:
                c["who"] = "atc"
        if c.get("note") and len(c["note"]) > 75:
            c.pop("note")
    from .reel import KICKER_DATE
    spec["kicker"] = KICKER_DATE.sub("", spec.get("kicker") or "")   # the video's date line comes from the clip time
    if clips:
        day = datetime.fromtimestamp(min(_clip_t(c["clip"]) for c in clips), BRT).strftime("%d/%m")
        cap = re.sub(r"\s*🎧.*$", "", (spec.get("caption") or "").strip())
        spec["caption"] = f"{cap} 🎧 Áudio real do rádio, {day}."
        if r.get("language", "").lower().startswith(("en", "english")) and not c.get("pt"):
            try:
                c["pt"] = chat("Traduza a fala de rádio de aviação para português do Brasil simples. JSON {\"pt\": \"...\"}",
                               c.get("text") or r["text"]).get("pt", "")
            except Exception:
                pass
    return spec


def finish(spec):
    tags = [t if t.startswith("#") else "#" + t for t in spec.get("hashtags", [])]
    extra = [t for t in dict.fromkeys(t.lower() for t in tags) if t not in BANNED_TAGS and t not in BASE_TAGS][:3]
    spec["hashtags"] = BASE_TAGS[:2] + extra + BASE_TAGS[2:]
    spec["status"] = "staged"
    return spec


def video_checks(mp4, spec, tracks):
    """After render: hard length cap and the focus plane on screen for every radio beat."""
    p = []
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
                               capture_output=True, text=True).stdout or 0)
    if not 20 <= dur <= 50.05:
        p.append(f"vídeo com {dur:.1f}s; precisa 20–50 s")
    lay = mp4.parent / "layout.json"           # written by the renderer: was the plane ever under the text?
    if lay.exists():
        L = json.loads(lay.read_text())
        if len(L.get("covered_seconds", [])) > 6 or L.get("tight_seconds"):
            p.append("o avião fica embaixo do texto (cartão/nota/step altos demais): encurte step, note e pt desses beats "
                     f"(segundos {sorted(set(L.get('covered_seconds', []) + L.get('tight_seconds', [])))[:6]})")
    hx = next((f for f in spec["focus"] if f in tracks), None)
    if hx:
        ts = [p_[0] for p_ in tracks[hx]]
        for c in spec["clips"]:
            t = c.get("t") or _clip_t(c["clip"])
            if not any(abs(t - x) < 300 for x in ts):
                p.append(f"o avião em foco não aparece no ADS-B perto de {datetime.fromtimestamp(t, BRT):%H:%M:%S}; "
                         "tire esse beat ou troque o foco")
                break
    return p, dur


def contact_sheet(mp4, png):
    chk = ROOT / "tools" / "framing" / "check.py"
    if chk.exists():
        subprocess.run(["python3", str(chk), str(mp4), str(png), "--surface", "tiktok"], capture_output=True)


# ---------------------------------------------------------------- main
CRITIC = """Você revisa roteiros do Rádio Aéreo antes de postar. Seja exigente. "text" é a transcrição LITERAL do rádio real:
não critique a fraseologia, erros de digitação ou números dentro dele (é o que foi dito/ouvido). Julgue os passos ("step"),
notas, título, legenda e a história. Responda JSON
{"score": 1-10, "problems": ["..."]}: score = quanto um brasileiro comum entenderia e acharia interessante assistir até o fim.
Você recebe também os FATOS DO ADS-B (confiáveis). Se o roteiro contradiz esses fatos (ex.: diz que decolou/abortou a
decolagem quando o avião estava chegando e arremeteu), ou conta algo que as falas não mostram, marque "fatal": true.
Responda {"score": 1-10, "fatal": true/false, "problems": [...]}.
Penalize: tradução ("pt") que acrescenta sentido que o original ("text") não tem (ex.: "clear out" virar "arremeta"),
palavras inventadas pelo Whisper deixadas no texto (saudações estranhas, outra língua), história confusa, beats que não se ligam, passos que não batem com as falas, canal errado (final = Controle São Paulo,
não torre), afirmações sem prova, título/legenda exagerados ou falsos, jargão sem explicação."""


def review(spec, rts, tracks=None):
    view = {k: spec.get(k) for k in ("kicker", "title", "caption")}
    focus = [f for f in spec.get("focus", []) if tracks and f in tracks]
    clips = [c for c in spec.get("clips", []) if c.get("clip")]
    if focus and clips:
        ts = [_clip_t(c["clip"]) for c in clips]
        view["FATOS_DO_ADSB"] = flight_phase(focus[0], min(ts), max(ts), tracks)["text"]
    view["beats"] = [{k: c.get(k) for k in ("step", "note", "text", "pt", "who", "silent")} | {"canal": CHANNELS.get(c["clip"].split("/")[2], "")}
                     if c.get("clip") else {"silent": c.get("silent"), "step": c.get("step")} for c in spec["clips"]]
    try:
        r = chat(CRITIC, json.dumps(view, ensure_ascii=False))
        score = int(r.get("score", 0))
        if r.get("fatal"):
            score = min(score, 3)
        return score, r.get("problems", [])
    except Exception as e:
        log(f"[critic] {e}")
        return 7, []


def focus_hexes(pick_, cand, tracks):
    fh = (pick_.get("focus_hex_or_callsign") or cand.get("hex") or "").strip()
    hexes = [h for h in tracks if h == fh.lower()] or [h for h, v in tracks.items() if any(p[6] == fh.upper() for p in v)]
    return hexes or ([cand["hex"]] if cand.get("hex") in tracks else [])


def make(pick_, cand, tracks, day_dir, tries=6):
    from .reel import render
    spec, rts, fix = None, {}, None
    if not focus_hexes(pick_, cand, tracks):
        log("  skip: the story's plane isn't on ADS-B (nothing to show on the map)")
        return None
    for attempt in range(tries):
        new, r = write(pick_, cand, tracks, fix, spec)
        rts.update(r)
        dbg = day_dir / "_debug"
        dbg.mkdir(parents=True, exist_ok=True)
        tag = re.sub(r"\W+", "-", (pick_.get("story") or "x")[:30])
        (dbg / f"{tag}_{attempt}_input.txt").write_text(new.pop("_input", ""))
        if new.get("skip"):
            log(f"  writer skipped: {new['skip']}")
            if attempt or len(new["skip"]) < 25:      # a lazy one-word skip gets one more try
                fix = ["Você pulou sem motivo real. Escreva o episódio com as transmissões dadas."]
                if attempt:
                    return None
                continue
            return None
        spec = repair(new, rts, tracks)
        spec["_why"] = cand["why"]
        fix = problems(spec, rts, tracks)
        (dbg / f"{tag}_{attempt}_spec.json").write_text(json.dumps({"spec": spec, "problems": fix}, ensure_ascii=False, indent=1))
        if not fix:
            score, crit = review(spec, rts, tracks)
            log(f"  critic {score}/10 {crit[:2]}")
            if score < 7 or (score == 7 and crit and attempt < tries - 1):   # 8+ passes; a 7 only without open problems
                fix = crit or ["história fraca ou confusa; deixe mais clara"]
        if fix:
            log(f"  attempt {attempt + 1}: {len(fix)} problems → rewrite: {fix[:3]}")
            continue
        spec = finish(spec)
        spec["camera"] = camera(spec, tracks)
        slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"[^\w\s-]", "", spec["title"].lower()
                      .translate(str.maketrans("áàâãéêíóôõúç", "aaaaeeiooouc")))).strip("-")[:40]
        ep = day_dir / f"{datetime.fromtimestamp(_clip_t(spec['clips'][0].get('clip') or cand['lines'][0]['clip']), BRT):%Y-%m-%d}_{slug}"
        ep.mkdir(parents=True, exist_ok=True)
        why = spec.pop("_why")
        spec["_event"] = {"hex": (spec.get("focus") or [""])[0], "t": round(_clip_t(spec["clips"][0]["clip"]))}
        (ep / "episode.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
        try:
            render(ep / "episode.json", log=log)
        except Exception as e:
            fix = [f"o renderizador falhou: {e}"]
            spec["_why"] = why
            continue
        fix, dur = video_checks(ep / "video.mp4", spec, tracks)
        if fix:
            spec["_why"] = why
            log(f"  attempt {attempt + 1}: video problems → {fix}")
            continue
        contact_sheet(ep / "video.mp4", ep / "check.png")
        log(f"  ✓ {ep.name} ({dur:.1f}s)")
        return ep
    log(f"  ✗ gave up after {tries} rewrites: {fix}")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", help="window end, UTC ISO or 'now' (default: the current hour; the archive has up to the last one)")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max", type=int, default=0, help="cap episodes (0 = every pick)")
    ap.add_argument("--no-fetch", action="store_true", help="use data/ as is (Mac)")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--normal-landing", action="store_true", help="make just the first plain landing of the window")
    ap.add_argument("--reuse-picks", action="store_true", help="debug: reuse episodes/<day>/picks.json")
    ap.add_argument("--event", help="catalog mode: event time, UTC ISO (window = ±1 h around it)")
    ap.add_argument("--focus", default="", help="catalog mode: focus hex")
    ap.add_argument("--story", default="", help="catalog mode: what happened")
    ap.add_argument("--clips", default="", help="catalog mode: key clips, comma separated (feed/date/HHMMSS_mmm)")
    ap.add_argument("--only", type=int, help="debug: just this pick index")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    if a.event:
        ev = datetime.fromisoformat(a.event.replace("Z", "+00:00"))
        a.end, a.hours = (ev + timedelta(hours=1)).isoformat(), 2
    end = now.replace(minute=0, second=0, microsecond=0) if a.end == "now" else \
        datetime.fromisoformat(a.end.replace("Z", "+00:00")) if a.end else \
        now.replace(minute=0, second=0, microsecond=0)
    t1, t0 = end.timestamp(), (end - timedelta(hours=a.hours)).timestamp()
    log(f"window {datetime.fromtimestamp(t0, BRT):%d/%m %H:%M} → {datetime.fromtimestamp(t1, BRT):%d/%m %H:%M} BRT")
    if not a.no_fetch and not fetch(t0, t1):
        log("nothing archived in the window")
        return
    tracks = raw_tracks(t0 - 600, t1 + 600)
    state = OUT / "posted.json"
    posted = pull_state(state)
    if a.event:
        cand, p = catalog_event(ev.timestamp(), a.focus.lower(), a.story, [c.strip() for c in a.clips.split(",") if c.strip()], tracks)
        cands, picks = [cand], [p]
    elif a.normal_landing:
        cand, p = normal_landing(t0, t1, tracks)
        cands, picks = ([cand], [p]) if cand else ([], [])
    else:
        cands = candidates(t0, t1, tracks)
        log(f"{len(cands)} candidate moments, {len(tracks)} aircraft")
        cache = OUT / f"{end:%Y-%m-%d}" / "picks.json"
        if a.reuse_picks and cache.exists():
            picks = json.loads(cache.read_text())
        else:
            picks = pick(cands, [p["title"] for p in posted]) if cands else []
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(picks, ensure_ascii=False, indent=1))
    picks = [p for p in picks if isinstance(p, dict) and isinstance(p.get("candidate"), int)
             and 0 <= p["candidate"] < len(cands) and p.get("score", 0) >= 7]
    log(f"{len(picks)} picks: " + " | ".join(f"{p['score']} {p.get('story')}" for p in picks))
    day_dir = OUT / f"{end:%Y-%m-%d}"
    made = []
    kept = []
    done = [(e.get("hex"), e.get("t", 0)) for e in posted if e.get("hex")]
    for p in sorted(picks, key=lambda p: -p.get("score", 0)):
        hx = (focus_hexes(p, cands[p["candidate"]], tracks) or [None])[0]
        t = cands[p["candidate"]]["t0"]
        p["_hex"] = hx
        if any(q["candidate"] == p["candidate"] or (hx and q["_hex"] == hx and abs(cands[q["candidate"]]["t0"] - t) < 1800) for q in kept):
            continue
        if hx and any(h == hx and abs(t0_ - t) < 3600 for h, t0_ in done):
            log(f"  already posted: {p.get('story')}")
            continue
        kept.append(p)
    picks = kept
    if a.only is not None:
        picks = picks[a.only:a.only + 1]
    for p in picks[:a.max or None]:
        log(f"episode: {p.get('story')}")
        ep = make(p, cands[p["candidate"]], tracks, day_dir)
        if ep:
            made.append(ep)
    (day_dir / "made.json").parent.mkdir(parents=True, exist_ok=True)
    (day_dir / "made.json").write_text(json.dumps([e.name for e in made]))
    log(f"made {len(made)} episodes")
    if a.post and made:
        from .post_tiktok import post_all
        post_all(made, state)
    if not a.no_fetch:
        push_episodes(day_dir, state)


def pull_state(state):
    """posted.json lives in the archive dataset so every nightly run knows what is already up."""
    if not ENV.get("HF_TOKEN"):
        return json.loads(state.read_text()) if state.exists() else []
    from huggingface_hub import hf_hub_download
    try:
        p = hf_hub_download(REPO, "episodes/posted.json", repo_type="dataset", token=ENV["HF_TOKEN"])
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(Path(p).read_text())
    except Exception:
        pass
    return json.loads(state.read_text()) if state.exists() else []


def push_episodes(day_dir, state):
    from huggingface_hub import HfApi
    api = HfApi(token=ENV.get("HF_TOKEN"))
    if day_dir.exists():
        api.upload_folder(folder_path=str(day_dir), path_in_repo=f"episodes/{day_dir.name}", repo_id=REPO, repo_type="dataset",
                          allow_patterns=["*/episode.json", "*/video.mp4", "*/cover.jpg", "*/check.png", "made.json"],
                          commit_message=f"episodes {day_dir.name}")
    if state.exists():
        api.upload_file(path_or_fileobj=str(state), path_in_repo="episodes/posted.json", repo_id=REPO, repo_type="dataset",
                        commit_message="posted.json")


if __name__ == "__main__":
    main()
