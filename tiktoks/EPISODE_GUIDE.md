# Rádio Aéreo — episode guide (TikTok explainers from real Guarulhos ATC radio)

Visual template (layout, safe zones, fonts, fast-forward, end card): see `TEMPLATE.md` — the renderer already applies it;
don't fight it from episode.json (e.g. the camera `oy` is capped at 0.44 so the subject stays above the caption card).

Project root: `/Volumes/EOS_DIGITAL/atc-br` (run everything from there, Python is `.venv/bin/python`).
Brand: **Rádio Aéreo** · radioaereo.com (the renderer already draws the end card).

## Rules
- **Do not edit anything in `src/`** (the renderer `src/atc_br/reel.py` is shared). If the renderer can't do something, stop and report it.
- **Do not touch the live backend** (`run.sh`, the `atc-br` process, `data/live.log`). Read-only on `data/`.
- **Never post or upload anything.** Videos are staged locally only.
- Be honest: never invent what was said or why. If a reason is inferred from data, phrase it cautiously in the text ("provavelmente", "pelo que os dados mostram"). No personal names of passengers/crew on screen or in captions.
- **Do not trust the word "arremeta"/"arremete" in `data/transcripts.jsonl`** — the live speech model's prompt contained it, so it is often hallucinated (e.g. "alinhe e mantenha, arremeta"). Confirm go-arounds with ADS-B (aircraft descends on final, then climbs without touching down).

## Data
- `data/transcripts.jsonl` — one JSON per transmission: `ts` (UTC ISO), `t` (epoch), `feed` (`torre`, `solo1269`, `solo1217`, `final`, `trafego`, `atis`), `duration`, `text`, `cs` (guessed callsign, unreliable), `who` (`atc`/`plt`, unreliable), `clip` (path relative to `data/`).
- Audio clips: `data/clips/SBGR/<feed>/<YYYY-MM-DD>/<HHMMSS_mmm>.ogg` (UTC time in the filename). Radio exists for **Oct 2–3 and Oct 14–17, 2026** (gap Oct 14 20:04Z → Oct 15 00:55Z; down Oct 16 22:51Z → Oct 17 ~02:00Z).
- ADS-B: `data/adsb/<date>.jsonl.gz`, a snapshot every ~10–20 s. Helpers:
  ```python
  from atc_br.reel import load_tracks, at, load_tx, clip_time
  from atc_br.airport import xy, seg, RWY   # xy(lat,lon)->local meters (x east, y SOUTH); RWY ends for '10L','10R'
  tracks = load_tracks(t0, t1)   # hex -> [(t, x, y, track, gs, alt_ft, ground, callsign, type)]  (glitch-filtered)
  p = at(tracks[hex], T)         # interpolated point or None
  ```
  Field elevation 2459 ft. Runways: 10L/28R (north strip), 10R/28L (south strip); terminals are north of both, so planes landing on the south runway cross the north one.
  `data/routes.json` maps callsign -> airline/from/to.
- Times on screen are **Brasília time (UTC−3)**. Note: `data/live.log` timestamps are the Mac's local time (EDT), don't use them.

## Re-transcribe every clip you use
The live transcripts are rough. Re-run each chosen clip through Groq Whisper large-v3 with a **neutral** prompt:
```python
import requests
env = dict(l.split('=',1) for l in open('.env').read().splitlines() if '=' in l)
keys = [k.strip() for k in env['GROQ_API_KEYS'].split(',')]
r = requests.post('https://api.groq.com/openai/v1/audio/transcriptions',
    headers={'Authorization': f'Bearer {keys[i % len(keys)]}'},
    files={'file': open('data/' + clip, 'rb')},
    data={'model': 'whisper-large-v3', 'prompt': 'Aeroporto de Guarulhos, controle de tráfego aéreo.', 'temperature': 0}, timeout=60)
```
Rotate keys and back off (sleep 20–60 s) on HTTP 429 — other agents share these keys. You may add callsigns/airline names to the prompt, but never action words like "arremeta".
On screen, `text` must be faithful to the audio; cut off garbled parts with "…" rather than guessing.

## episode.json (one folder per episode: `tiktoks/staged/<YYYY-MM-DD>_<slug>/episode.json`)
Look at the three finished examples first:
`tiktoks/staged/2026-10-14_falha-controle-de-voo/`, `2026-10-14_decolagem-cancelada/`, `2026-10-02_arremetida-aeromexico/`.
```jsonc
{
  "kind": "arremetida",            // short type tag
  "status": "staged",
  "focus": ["GLO9640"],            // callsign(s) or hex; first = yellow, second = blue
  "measure": true, "measure_from": 0, "measure_until": 3,   // optional: live distance line between first two focus planes, for beats [from, until)
  "camera": {"follow": true, "vw": 1500, "vw_end": 1250, "oy": 0.5},
  //   follow=true tracks focus[0]; or {"follow": false, "center": [x, y] (world meters), ...}
  //   vw = view width in meters (ground ~1300–3300; airborne/approach 6000–12000); oy = where the target sits vertically (0..1)
  "kicker": "Gol · Boeing 737 · 14/10, 03:36",   // airline · aircraft · date, time (BRT)
  "title": "Pousou na segunda tentativa",          // short, ≤ ~40 chars
  "clips": [
    {"clip": "clips/SBGR/torre/2026-10-14/063637_980.ogg", "who": "atc",   // atc = controller, plt = pilot
     "step": "One plain-Portuguese sentence: what is happening right now.",
     "note": "Termo: explicação curta do jargão",     // optional, only when jargon appears
     "taxi": ["B"],                                    // optional: taxiway letters to light up
     "text": "Faithful transcript (original language)",
     "pt": "Same meaning in simple Brazilian Portuguese (big text)"},
    {"silent": 3.0, "span": 80, "t": 1792006685.4,     // silent beat: shows `span` real seconds in `silent` screen seconds, starting at epoch t
     "step": "What we see happening on the map."}
  ],
  "caption": "Post caption in Portuguese, ends with 🎧 Áudio real do rádio, DD/MM.",
  "hashtags": ["#aviação", "#guarulhos", "#aeroporto", "#radioaereo", "..."]   // + 1–3 specific (airline/aircraft/event); never #avgeek
}
```
**Caption text rule:** if the radio is in **Portuguese**, put only the verbatim transcript in `text` and **omit `pt`** — the big text is
then the real words, no simplified rewrite underneath. Only when the radio is in **English** (or another language) use `pt` for the
Portuguese translation (big) with the original in `text` (small underneath). Explaining jargon is the job of `step` and `note`.

Style: an **explainer for normal people**. One idea per beat, 4–7 beats, 25–50 s total — **hard cap 50 s including the end card**.
Use your own scratch subfolder (other agents run in parallel and share the scratchpad). Open on the key line or its setup. Plain Brazilian Portuguese everywhere on screen (the original transcript sits small under the Portuguese). Use `note` to decode jargon once (Solo, Torre, heavy, arremeter, milha, pista de táxi…).

## Render and check
```bash
cd /Volumes/EOS_DIGITAL/atc-br
.venv/bin/python -c "from atc_br.reel import render; render('tiktoks/staged/<slug>/episode.json')"
```
Writes `video.mp4` + `cover.jpg` in the episode folder (~1 min). Then **look at it**: extract frames
(`ffmpeg -v error -y -ss <sec> -i video.mp4 -frames:v 1 -vf scale=432:-1 /tmp/x.png`, a few per beat) and view them.
Check: focus plane visible and not hidden under the caption card or header, labels not colliding, story readable,
no data glitches (planes teleporting, wrong altitude). Adjust camera/beats and re-render until it reads well.
