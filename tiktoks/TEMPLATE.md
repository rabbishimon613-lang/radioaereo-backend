# Rádio Aéreo — video template (v2, 2026-10-24)

The locked look for every Rádio Aéreo explainer. Implemented in `src/atc_br/reel.py` (constants at the top of the file);
episode content rules live in `EPISODE_GUIDE.md`. Change this document and the constants together — and copy reel.py to the
cloud repo (radioaereo-backend) in the same change, so manual and nightly videos are identical.

**v2 (2026-10-24) — why:** checked on Pedro's phone (tall iPhone, TikTok app). TikTok scales the frame to ~0.94 and crops
~47 px off each side; the status bar + search bar cover the top ~220 px; the like/comment rail starts at x ≈ 900. v1 put the
header under the search bar, the caption card under the heart, and let the note + card grow over the plane. Never again:

## Frame
- 1080 × 1920, 30 fps, H.264 CRF 18 + AAC 192k, `+faststart`. Audio = the real radio clips, peak-normalised to −1 dBFS.
- Background = the airport/radar map, full-bleed, runways rotated vertical (east at the top), dimmed (ink overlay α 95).
- Header gradient: black α 240 → 0 over the top 900 px. Footer gradient: 0 → α 245 over the bottom 900 px.

## Safe zones (measured on a real phone, TikTok ∪ Reels)
| | px | covered by |
|---|---|---|
| top | 240 | status bar + TikTok search bar / tabs |
| bottom | 484 | caption, handle, sound bar |
| left | 100 | side crop on tall phones (~47 px) + margin |
| right | 220 | like / comment / share rail (starts x ≈ 900) + crop |

**Golden box: x 100–860, y 240–1436** (`SAFE_L, SAFE_R, SAFE_T, SAFE_B`). Every piece of text, tag, pill and card is inside it.
Only the map runs under the red zones. Horizontal centre for anything centred = **x 480** (`SAFE_CX`), not 540.

## The plane is never under text (protected window)
- Per beat the renderer computes a free window: from the bottom of the step text + 90 px to the top of the caption card
  (or note pill) − 80 px. The camera puts the focus aircraft in the middle of that window (eased across beat changes);
  spec `camera.oy` is ignored. Fast-forward pill sits low (y ≈ 1176), below the window, where no card is shown.
- Every render writes `layout.json` (`covered_seconds`, `tight_seconds`). Both must be (near) empty — if not, shorten that
  beat's step / note / pt (step ≤ 90 chars, note ≤ 75) and re-render. The nightly pipeline enforces this automatically.
- Check any render also with `python3 tools/framing/check.py video.mp4 out.png --surface tiktok`.

## Layout, top to bottom
| element | position | font |
|---|---|---|
| live dot + "RÁDIO REAL · GUARULHOS" (or "DADOS REAIS" when there's no radio) / clock BRT right-aligned at x 860 | y 262 | Geist Mono 24/600 |
| kicker: AIRLINE · AIRCRAFT · DD/MM, HH:MM (amber) | y 302 | Geist Mono 26/700 |
| title (1–2 lines) | from y ≈ 352, 80 px leading | Archivo 76 / wght 830 / wdth 80 |
| step: "n/N" chip + one plain sentence, fades/slides in per beat | under the title | Geist Mono 26 chip, Archivo 38/560 |
| map window: focus aircraft in the **protected window** (see above), centred on x 480 | — | — |
| jargon note (amber pill), only when a term appears | directly above the card | Geist Mono 24/800 term + Archivo 32/520 |
| caption card, **bottom edge on y 1436**, x 100–860 | grows upward | channel/freq/speaker Geist Mono 26; big text Archivo 50/720/92 (62 px leading) (karaoke highlight in channel colour); original transcript Archivo 29/500 (dim) |

## Caption text rule
- Portuguese radio → big text = **verbatim transcript only** (no `pt`).
- English/other radio → big = Portuguese translation (`pt`), small underneath = original (`text`).

## Map overlays
- Focus aircraft: amber icon 64 px + amber ring + 4-min trail + callsign label (Geist Mono 28/700) and type · altitude · speed.
  Second focus aircraft in torre-blue, label on the left. All other aircraft: faint grey 34 px icons, no labels.
- Taxiway highlight when mentioned (`taxi`): amber glow + "B · BRAVO" tag (Geist Mono 30/800), placed ~300 px from the focus plane.
- Distance tag (`measure`): white dashed line + "4,3 km / 2,3 milhas" box, clamped inside the golden box.

## Movement between position reports
- Ground gaps are reconstructed, not glided: `src/atc_br/taxiroute.py` finds the shortest route along SBGR's
  taxiways/taxilanes/runways between two known fixes and the plane drives it (stopped at the first fix → waits, then taxis
  at 8–22 kt and arrives on time; moving → taxis, then waits at the second fix). Short hops that stay on pavement keep
  the straight line; landing/takeoff rolls stay on the runway. Every real fix is kept; repeated frozen copies of one fix
  count once. Check: nothing should leave the pavement (`drift_diag`).

## Fast-forward (long real-time jump between clips, ≥ 40 s)
- Screen time `FF` = 2.4 s; the map advances through the whole real gap.
- **Pill in your face**: amber filled rounded box at the centre (x 480, y ≈ 1176, below the plane window), "» N MIN" Geist Mono 92/800 + "DEPOIS" 34/700, dark text; pops in/out in 12 % of the gap.
- **Radar-only VCR effect** (never on text): vertical smear ±10 px, faint scanlines every 5 px, a fast light-blue tracking band;
  strength sin-shaped over the gap (full), half strength on fast silent beats (rate ≥ 8×).

## Timing
- `INTRO` 0.05 s — the video opens on the first word. `GAP` 1.1 s between clips (reading time). `OUTRO` 2.4 s.
- 4–7 beats, **hard cap 50 s** including the end card (`MAX_LEN`). If an episode runs long, the renderer automatically
  squeezes pauses (down to 0.45 s), fast-forwards (down to 1.4 s), silent beats (down to 1.6 s) and the end card (down to 1.8 s) —
  never the speech. If it is still over 50 s after that, cut a clip.

## End card
Dark fade, "Rádio Aéreo" (Archivo 112/800/86) + "radioaereo.com" (Geist Mono 34/600, torre-blue), centred on x 480. Nothing else.

## Colours (from the site, oklch)
ink .135/.012/255 · text .94/.006/85 · dim .68/.012/255 · faint .46/.012/255 · amber #FFC452 ·
torre .80/.105/245 · solo .80/.105/155 · controle SP (final) .80/.105/290 · tráfego .80/.105/200 · ATIS .72/.02/85.
