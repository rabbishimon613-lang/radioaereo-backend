"""Cut a chunk stream into individual radio transmissions with Silero VAD."""
from collections import deque
from collections.abc import Iterator

from pathlib import Path

import numpy as np
import onnxruntime as ort

from .ingest import CHUNK, SR

START_P = 0.5        # speech starts above this probability
END_P = 0.35         # ...and is considered over below this
END_SILENCE = 0.35   # seconds below END_P that close a transmission
PRE_ROLL = 0.2       # seconds kept before speech start
MIN_LEN = 0.6        # drop clicks / squelch tails
MAX_LEN = 25.0       # force a cut on stuck carriers


class SileroVAD:
    """Silero VAD v5 on onnxruntime + numpy (no torch: fits a 512 MB container)."""
    CONTEXT = 64

    def __init__(self):
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(str(Path(__file__).with_name("models") / "silero_vad.onnx"), opts, providers=["CPUExecutionProvider"])
        self.sr = np.array(SR, dtype=np.int64)
        self.reset_states()

    def reset_states(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self.ctx, chunk.reshape(1, -1).astype(np.float32)], axis=1)
        out, self.state = self.sess.run(None, {"input": x, "state": self.state, "sr": self.sr})
        self.ctx = x[:, -self.CONTEXT:]
        return float(out[0, 0])


def transmissions(chunks: Iterator[np.ndarray]) -> Iterator[np.ndarray]:
    model = SileroVAD()
    pre = deque(maxlen=int(PRE_ROLL * SR / CHUNK))
    buf: list[np.ndarray] = []
    quiet = 0
    end_chunks = int(END_SILENCE * SR / CHUNK)

    for chunk in chunks:
        p = model(chunk)
        if not buf:
            if p >= START_P:
                buf = [*pre, chunk]
                quiet = 0
            else:
                pre.append(chunk)
            continue

        buf.append(chunk)
        quiet = quiet + 1 if p < END_P else 0
        if quiet >= end_chunks or len(buf) * CHUNK >= MAX_LEN * SR:
            clip = np.concatenate(buf[: len(buf) - quiet + 2] if quiet else buf)
            buf, quiet = [], 0
            pre.clear()
            model.reset_states()
            if len(clip) >= MIN_LEN * SR:
                yield clip
