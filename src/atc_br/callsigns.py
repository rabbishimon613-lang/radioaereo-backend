"""Spoken callsigns ↔ ICAO callsigns, and matching a transcript to an aircraft in the sky."""
import re

# ICAO prefix → spoken telephony (first is what we put in the Whisper prompt)
TELEPHONY = {
    "TAM": ["TAM", "LATAM"], "GLO": ["Gol"], "AZU": ["Azul"], "PTB": ["Passaredo", "Voepass"],
    "LAN": ["LAN", "LATAM"], "LPE": ["LATAM Peru", "LANPERU"], "LXP": ["LANCARGO"],
    "TTL": ["Total"], "SID": ["Sideral"], "MWM": ["Modern"], "ABJ": ["Abaeté"],
    "AAL": ["American"], "UAL": ["United"], "DAL": ["Delta"], "UAE": ["Emirates"], "AFR": ["Air France"],
    "KLM": ["KLM"], "DLH": ["Lufthansa"], "IBE": ["Iberia"], "TAP": ["Air Portugal", "TAP"],
    "ARG": ["Argentina"], "CMP": ["Copa"], "AVA": ["Avianca"], "KAL": ["Korean Air"], "QTR": ["Qatari"],
    "THY": ["Turkish"], "ETH": ["Ethiopian"], "BAW": ["Speedbird"], "SWR": ["Swiss"], "ITY": ["Itarrow"],
    "SKU": ["Sky Chile", "Aerosky"], "JAT": ["JetSMART"], "AEA": ["Europa"], "SAA": ["Springbok"],
    "RAM": ["Royal Air Maroc"], "BOV": ["Boliviana"], "DTA": ["DTA", "Angola"], "AMX": ["Aeromexico"],
    "ACA": ["Air Canada"], "CCA": ["Air China"], "GTI": ["Giant"], "FDX": ["FedEx"], "UPS": ["UPS"],
    "CLX": ["Cargolux"], "FAB": ["Força Aérea", "Brazilian Air Force"],
}

_PT_DIGITS = {"zero": "0", "uno": "1", "dois": "2", "três": "3", "tres": "3",  # not "um": it's also "a"
              "quatro": "4", "cinco": "5", "meia": "6", "seis": "6", "sete": "7", "oito": "8", "nove": "9",
              "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
              "eight": "8", "nine": "9", "niner": "9", "tree": "3", "fife": "5"}


def spoken(callsign: str) -> str:
    """'TAM3297' → 'TAM 3297' for the Whisper prompt."""
    m = re.match(r"([A-Z]{3})(\w+)", callsign)
    if not m:
        return callsign
    names = TELEPHONY.get(m[1])
    return f"{names[0]} {m[2]}" if names else callsign


def digit_runs(text: str) -> list[str]:
    """Collapse spoken digits and Whisper's '3.297' / '32 97' / 'três dois' into digit runs."""
    t = text.lower()
    for w, d in _PT_DIGITS.items():
        t = re.sub(rf"\b{w}\b", d, t)
    # digit groups separated only by spaces/punctuation are candidates to glue ("38, 08" → "3808")
    runs = []
    for chain in re.findall(r"\d+(?:[\s.,-]+\d+)*", t):
        parts = re.findall(r"\d+", chain)
        for i in range(len(parts)):
            for j in range(i + 1, min(i + 4, len(parts) + 1)):
                runs.append("".join(parts[i:j]))
    return runs


def _one_edit(a: str, b: str) -> bool:
    """True if a and b differ by exactly one dropped, added or swapped digit (Whisper's usual number slip)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def match(text: str, callsigns: list[str]) -> tuple[str | None, float]:
    """Best aircraft for a transmission → (callsign, score 0..1). None when ambiguous or absent."""
    runs = digit_runs(text)
    low = text.lower()
    scored = []
    for cs in callsigns:
        m = re.match(r"([A-Z]{3})0*(\d+[A-Z]?)$", cs)
        if not m:
            continue
        prefix, num = m[1], m[2].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        names = [n.lower() for n in TELEPHONY.get(prefix, [prefix])]
        airline = any(n in low for n in names)
        if num in runs:
            s = 1.0 if airline else 0.75
        elif len(num) >= 3 and any(num in r for r in runs):
            s = 0.55 if airline else 0.4
        elif len(num) >= 4 and any(r == num[-3:] for r in runs):  # ATC abbreviates to last 3 digits
            s = 0.45 if airline else 0.3
        elif airline and len(num) >= 3 and any(len(r) >= 3 and _one_edit(r, num) for r in runs):
            s = 0.35  # "TAM 805" for TAM8105
        else:
            continue
        scored.append((s, runs.count(num), cs))  # tie-break: callsign repeated more often
    if not scored:
        return None, 0.0
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None, 0.0
    return scored[0][2], scored[0][0]
