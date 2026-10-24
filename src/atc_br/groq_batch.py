"""Groq Whisper in time-window batches: clips from every feed in the window are stitched into one request,
then words are mapped back to their clip by timestamp. Keys rotate; on failure we fall back to local MLX."""
import io
import os
import threading
import time
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

from .ingest import SR

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
GAP = 1.0           # seconds of silence between stitched clips
WINDOW = 15.0       # seconds per batch
MAX_AUDIO = 120.0   # cap per request


def load_keys(root: Path) -> list[str]:
    env = os.environ.get("GROQ_API_KEYS")
    if not env and (root / ".env").exists():
        for line in (root / ".env").read_text().splitlines():
            if line.startswith("GROQ_API_KEYS="):
                env = line.split("=", 1)[1]
    return [k.strip() for k in (env or "").split(",") if k.strip()]


class GroqBatcher:
    def __init__(self, keys, on_result, local_fallback=None, log=print, prompt_fn=lambda: ""):
        self.keys, self.on_result, self.local, self.log, self.prompt_fn = keys, on_result, local_fallback, log, prompt_fn
        self.buf, self.lock = [], threading.Lock()
        self.k = 0
        self.cool = {}            # key index -> unix time it may be used again
        self.stats = {"requests": 0, "clips": 0, "fallback": 0, "errors": 0}

    def add(self, item):
        """item: dict with 'clip' (float32 @16k) + any metadata; returned untouched with 'text'/'words' added."""
        with self.lock:
            self.buf.append(item)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        while True:
            time.sleep(WINDOW)
            with self.lock:
                batch, self.buf = self.buf, []
            while batch:
                part, total = [], 0.0
                while batch and total + len(batch[0]["clip"]) / SR <= MAX_AUDIO:
                    it = batch.pop(0)
                    part.append(it)
                    total += len(it["clip"]) / SR + GAP
                if not part:  # one oversized clip
                    part = [batch.pop(0)]
                self._send(part)

    def _key(self):
        now = time.time()
        for _ in range(len(self.keys)):
            self.k = (self.k + 1) % len(self.keys)
            if self.cool.get(self.k, 0) < now:
                return self.k
        return None

    def _send(self, items):
        gap = np.zeros(int(GAP * SR), dtype=np.float32)
        audio, bounds, t = [], [], 0.0
        for it in items:
            d = len(it["clip"]) / SR
            bounds.append((t, t + d))
            audio += [it["clip"], gap]
            t += d + GAP
        wav = io.BytesIO()
        sf.write(wav, np.concatenate(audio), SR, format="WAV", subtype="PCM_16")
        ki = self._key() if self.keys else None
        res = None
        if ki is not None:
            try:
                r = requests.post(URL, headers={"Authorization": f"Bearer {self.keys[ki]}"},
                                  files={"file": ("batch.wav", wav.getvalue(), "audio/wav")},
                                  data={"model": MODEL, "prompt": self.prompt_fn()[:880], "response_format": "verbose_json",
                                        "temperature": 0, "timestamp_granularities[]": ["word", "segment"]}, timeout=60)
                self.stats["requests"] += 1
                if r.status_code == 200:
                    res = r.json()
                else:
                    self.stats["errors"] += 1
                    if r.status_code == 429:
                        wait = float(r.headers.get("retry-after", 60))
                        self.cool[ki] = time.time() + wait
                        self.log(f"[groq] key {ki + 1} rate-limited for {wait:.0f}s")
                    else:
                        self.log(f"[groq] HTTP {r.status_code}: {r.text[:120]}")
            except Exception as e:
                self.stats["errors"] += 1
                self.log(f"[groq] {e}")
        if res is None:
            self.stats["fallback"] += len(items)
            for it in items:
                out = self.local(it["clip"]) if self.local else None
                if out:
                    self.on_result({**it, **out, "engine": "local"})
            return
        words = res.get("words") or []
        segs = res.get("segments") or []
        lang = res.get("language", "")
        lang = {"portuguese": "pt", "english": "en"}.get(lang, lang[:2])
        for it, (s0, s1) in zip(items, bounds):
            ws = [w for w in words if s0 - .3 <= (w["start"] + w["end"]) / 2 <= s1 + .3]
            if ws:
                text = " ".join(w["word"].strip() for w in ws)
            else:
                text = " ".join(s["text"].strip() for s in segs if s0 - .3 <= (s["start"] + s["end"]) / 2 <= s1 + .3)
            ss = [s for s in segs if s["end"] > s0 and s["start"] < s1]
            nsp = float(np.mean([s.get("no_speech_prob", 0) for s in ss])) if ss else 0
            alp = float(np.mean([s.get("avg_logprob", 0) for s in ss])) if ss else None
            self.stats["clips"] += 1
            self.on_result({**it, "text": text.strip(), "language": lang, "no_speech": nsp, "avg_logprob": alp, "engine": "groq"})
