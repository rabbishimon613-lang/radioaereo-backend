"""Local Whisper (MLX, Apple Silicon) with ATC prompting and hallucination filtering."""
import re

import mlx_whisper
import numpy as np

MODEL = "mlx-community/whisper-large-v3-turbo"
MAX_HINTS = 15  # nearest aircraft fed to Whisper; more slows decoding with little gain

PROMPT = ("Controle São Paulo, Torre Guarulhos, Solo Guarulhos. TAM, LATAM, Gol, Azul, Passaredo. "
          "Pista 10L, 28R, 10R, 28L. Nível, mantenha, livre pouso, autorizado decolagem, arremeter, "
          "cleared to land, line up and wait, go around.")

# Stream jingles and classic Whisper hallucinations on noise
JUNK = re.compile(r"aeroescuta|obrigad[oa] por assistir|legendas? pela|inscreva-se|thanks for watching|"
                  r"^\W*$", re.I)


def _looping(r: dict) -> bool:
    words = re.findall(r"\w+", r["text"].lower())
    seg_loop = any(s.get("compression_ratio", 0) > 2.4 for s in r.get("segments") or [])
    return seg_loop or (len(words) > 12 and len(set(words)) / len(words) < 0.3)


def transcribe(clip: np.ndarray, live_callsigns: list[str] = (), fast: bool = False) -> dict | None:
    """fast=True skips the retry passes — used when the live queue is backing up."""
    prompt = PROMPT + (" " + ", ".join(live_callsigns[:MAX_HINTS]) + "." if live_callsigns else "")
    kw = dict(path_or_hf_repo=MODEL, initial_prompt=prompt, condition_on_previous_text=False, temperature=0.0)
    r = mlx_whisper.transcribe(clip, **kw)
    if not fast and r.get("language") not in ("pt", "en"):  # short noisy clips get mis-detected; ATC here is PT or EN
        r = mlx_whisper.transcribe(clip, language="pt", **kw)
    if _looping(r):  # model parroting the prompt — retry blind (unless we're behind)
        if fast:
            return None
        r = mlx_whisper.transcribe(clip, **{**kw, "initial_prompt": None, "language": r["language"]})
        if _looping(r):
            return None

    text = r["text"].strip()
    segs = r.get("segments") or []
    if not text or JUNK.search(text):
        return None
    if segs and np.mean([s["no_speech_prob"] for s in segs]) > 0.7:
        return None
    return {
        "text": text,
        "language": r["language"],
        "avg_logprob": round(float(np.mean([s["avg_logprob"] for s in segs])), 3) if segs else None,
    }
