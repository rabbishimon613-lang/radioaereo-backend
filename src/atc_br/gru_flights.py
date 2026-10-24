"""Official GRU departures/arrivals (gru.com.br) via a headless browser — their WAF only answers real browsers.
Refreshes every 2 min; flights are keyed by ICAO callsign (e.g. AAL906) so they merge with ADS-B and radio."""
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

PAGE = "https://www.gru.com.br/pt/passageiro/voos"
JS = """async (tipo) => { const out = [];
  const r = await fetch('/pt/_layouts/15/WebSiteWebParts/WebServiceCustom.asmx/GetVoos', {method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'procura=&terminal=&tipo=' + tipo + '&pagina=0'});
  const j = JSON.parse(new DOMParser().parseFromString(await r.text(), 'text/xml').documentElement.textContent);
  return Array.isArray(j) ? j : []; }"""
BRT = timezone(timedelta(hours=-3))
EVERY = 120


def _clock(day, hhmm, now):
    """'01/10' + '23:15' → aware datetime (BRT), handling the year boundary."""
    try:
        d, m = (int(x) for x in day.split("/"))
        h, mi = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return None
    dt = datetime(now.year, m, d, h, mi, tzinfo=BRT)
    if (dt - now).days > 180:
        dt = dt.replace(year=now.year - 1)
    return dt


def _norm(x, kind, now):
    cias = x.get("Cias") or []
    sig = (cias[0].get("Sigla") if cias else "") or ""
    nums = [n.lstrip("0") or "0" for n in (x.get("NumVoo") or [])]
    sched = _clock(x["DataDiaMes"], x.get("Horario"), now)
    est = _clock(x["DataDiaMes"], x.get("HorarioConfirmado"), now)
    if sched and est and est < sched - timedelta(hours=12):
        est += timedelta(days=1)
    return {
        "dir": kind, "cs": f"{sig}{nums[0]}" if nums else sig, "airline": cias[0].get("Nome") if cias else None, "icao": sig,
        "codeshare": [f"{sig}{n}" for n in nums[1:]], "city": x.get("OrigemDestino"), "terminal": x.get("Terminal"),
        "gate": x.get("Portao"), "sched": sched.isoformat() if sched else None, "est": est.isoformat() if est else None,
        "status": (x.get("Observacao") or "").strip(), "updated": x.get("DataAtualizacao"),
    }


class GruFlights:
    def __init__(self, log=print):
        self.log, self.flights, self.at = log, [], 0
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def by_callsign(self):
        return {f["cs"]: f for f in self.flights}

    async def _run(self):
        from playwright.async_api import async_playwright
        while True:
            try:
                async with async_playwright() as p:
                    b = await p.chromium.launch(channel="chromium", args=["--disable-blink-features=AutomationControlled"])
                    pg = await b.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
                    await pg.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
                    while True:
                        now = datetime.now(BRT)
                        seen, out = set(), []
                        for tipo, kind in (("Partida", "dep"), ("Chegada", "arr")):
                            for x in await pg.evaluate(JS, tipo):
                                f = _norm(x, kind, now)
                                k = (kind, f["cs"], f["sched"])
                                if k in seen or not f["sched"]:
                                    continue
                                seen.add(k)
                                t = datetime.fromisoformat(f["est"] or f["sched"])
                                if timedelta(hours=-2) < t - now < timedelta(hours=6):
                                    out.append(f)
                        out.sort(key=lambda f: f["est"] or f["sched"])
                        self.flights, self.at = out, time.time()
                        self.log(f"[gru] {sum(f['dir'] == 'dep' for f in out)} partidas · {sum(f['dir'] == 'arr' for f in out)} chegadas")
                        await asyncio.sleep(EVERY)
                        await pg.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                self.log(f"[gru] {type(e).__name__}: {str(e)[:120]} — retrying in 3 min")
                await asyncio.sleep(180)
