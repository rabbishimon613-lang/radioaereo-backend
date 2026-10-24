"""Pull a live MP3 stream through ffmpeg as 16 kHz mono float32, reconnecting forever."""
import subprocess
import time
from collections.abc import Iterator

import numpy as np

SR = 16000
CHUNK = 512  # Silero VAD needs exactly 512 samples at 16 kHz


def stream_chunks(url: str, log=print) -> Iterator[np.ndarray]:
    backoff = 2
    while True:
        proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "quiet", "-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "10", "-rw_timeout", "20000000", "-i", url, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            stdout=subprocess.PIPE,
        )
        got_audio = False
        try:
            while True:
                raw = proc.stdout.read(CHUNK * 2)
                if len(raw) < CHUNK * 2:
                    break
                got_audio = True
                backoff = 2
                yield np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        finally:
            proc.kill()
            proc.wait()
        log(f"stream dropped ({'after audio' if got_audio else 'no audio'}), retrying in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
