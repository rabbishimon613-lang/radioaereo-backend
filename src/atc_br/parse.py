"""Rule-based ATC phraseology parser (PT + EN) → intent: verb, runway, taxiways, numbers, speaker."""
import re

from .callsigns import digit_runs

NATO = {"alfa": "A", "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "eco": "E", "foxtrot": "F", "golf": "G",
        "hotel": "H", "india": "I", "juliett": "J", "juliet": "J", "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O",
        "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W",
        "x-ray": "X", "yankee": "Y", "zulu": "Z"}
VALID_TW = {"A", "B", "BB", "C", "CC", "D", "DD", "E", "FF", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "S", "T", "U", "V", "Y"}

VERBS = [  # (regex, verb) — first match wins, ordered by priority
    (r"\barremet|go[ -]?around", "ARREMETA"),
    (r"autorizad[oa] (para )?decola|cleared for take ?off|autorizado decolagem", "DECOLAGEM"),
    (r"livre (para )?pouso|cleared to land|autorizado pouso", "LIVRE POUSO"),
    (r"alinh[ea]|line up", "ALINHE"),
    (r"cruz[ae] (a )?pista|cross runway|cruzando (a )?pista", "CRUZE"),
    (r"livre t[aá]xi|taxi via|t[aá]xi via|prossiga via", "TÁXI"),
    (r"mantenha (posição )?antes|hold short|aguarde antes|ponto de espera", "HOLD"),
    (r"push|partida aprovad|start ?up", "PUSHBACK"),
    (r"desç|desc[ea] |descend", "DESÇA"),
    (r"\bsuba\b|climb", "SUBA"),
    (r"\bproa\b|heading", "PROA"),
    (r"reduz|reduce speed|mantenha .* n[oó]s|maintain .* knots", "VELOCIDADE"),
    (r"\bils\b|aproximação|approach", "APROXIMAÇÃO"),
    (r"chame|contate|contact|monitore", "CHAME"),
    (r"informação [a-z]+|information [a-z]+", "ATIS"),
]


def _norm(t):
    t = t.lower()
    t = re.sub(r"(\d)\.(\d{3})\b", r"\1\2", t)  # 5.200 -> 5200
    return t


def runway(t):
    m = re.search(r"\b(?:pista|runway|rwy)\s*(?:é\s*)?(1\s*0|2\s*8)\s*(l|r|left|right|esquerda|direita)\b", t)
    if not m:
        m = re.search(r"\b(10|28)\s*(l|r)\b", t)
    if not m:
        return None
    n = m.group(1).replace(" ", "")
    side = m.group(2)[0]
    side = {"e": "l", "d": "r"}.get(side, side)
    return n + side.upper()


def taxiways(t):
    m = re.search(r"\bvia\s+(.+?)(?:,?\s*(?:mantenha|hold|até|to|ponto|posição|position|cruz|e chame|$))", t)
    if not m:
        return []
    out = []
    for tok in re.split(r"[,\s]+(?:e\s+)?", m.group(1)):
        tok = tok.strip(" .")
        if not tok:
            continue
        if tok in NATO:
            if out and len(out[-1]) == 1 and out[-1] == NATO[tok]:   # "bravo bravo" -> BB
                out[-1] = out[-1] * 2
            else:
                out.append(NATO[tok])
        elif re.fullmatch(r"[a-z]{1,2}\d?", tok) and tok.upper().rstrip("0123456789") in VALID_TW:
            out.append(tok.upper())
        elif re.fullmatch(r"\d", tok) and out:
            out[-1] += tok
        elif tok in ("whiskey", "echo") and out and out[-1][:1] == "Y":
            out[-1] += tok[0].upper()
    return [x for x in out if x.rstrip("0123456789EW") in VALID_TW][:6]


def numbers(t):
    out = {}
    m = re.search(r"(?:desç\w*|desc\w*|suba\w*|climb\w*|mantenha|maintain)\s*(?:e mantenha\s*)?(?:para\s*|ao?\s*|to\s*)?(?:nível\s*|flight level\s*|fl\s*)?(\d{2,5})\s*(pés|ft|feet)?", t)
    if m:
        v = int(m.group(1))
        out["alt"] = f"FL{v:03d}" if v < 600 and ("nível" in m.group(0) or "level" in m.group(0) or "fl" in m.group(0)) else f"{v} FT"
    m = re.search(r"(?:proa|heading)\s*(\d{3})", t)
    if m:
        out["hdg"] = m.group(1)
    m = re.search(r"(\d{3})\s*(?:nós|knots|kt)\b", t)
    if m:
        out["spd"] = m.group(1) + " KT"
    m = re.search(r"\b(1[1-3]\d)[.,](\d{1,3})\b", t)
    if m:
        out["freq"] = f"{m.group(1)}.{m.group(2).ljust(3, '0')}"
    m = re.search(r"(?:posição|position|stand|box)\s*(\d{3}[lr]?)", t)
    if m:
        out["stand"] = m.group(1).upper()
    return out


def parse(text, callsign_spoken=None):
    """→ {verb, runway, taxiways, alt, hdg, spd, freq, stand, chips, who}"""
    t = _norm(text)
    verb = next((v for rx, v in VERBS if re.search(rx, t)), None)
    rw = runway(t)
    tws = taxiways(t) if verb in ("TÁXI", "HOLD", "CRUZE", None) else []
    nums = numbers(t)
    # speaker: pilots end with their callsign, controllers start with it
    who = "atc"
    if callsign_spoken:
        tail = t[-40:]
        if callsign_spoken in tail and not t.startswith(callsign_spoken[:4]):
            who = "plt"
    if re.match(r"^(ciente|copiado|roger|wilco|obrigad|thank)", t):
        who = "plt"
    chips = []
    if verb:
        chips.append(verb)
    holds = bool(re.search(r"mantenha (posição )?antes|hold short|aguarde antes|ponto de espera", t))
    chips += tws
    if rw:
        chips.append(("→ H" + rw) if verb == "TÁXI" and holds else ("HOLD " + rw) if verb == "HOLD" else rw)
    for k in ("alt", "hdg", "spd", "freq", "stand"):
        if k in nums:
            chips.append({"hdg": "PROA " + nums.get("hdg", ""), "stand": "POS " + nums.get("stand", ""), "freq": "→ " + nums.get("freq", "")}.get(k, nums[k]))
    return {"verb": verb, "holds": holds, "runway": rw, "taxiways": tws, **nums, "chips": chips, "who": who}
