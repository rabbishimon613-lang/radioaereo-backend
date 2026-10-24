"""Hourly archive to a Hugging Face dataset: one tar per finished UTC hour (clips + transcripts + ADS-B for that hour).

Layout in the dataset:  <YYYY-MM-DD>/<HH>.tar  →  clips/SBGR/<feed>/<date>/<HHMMSS_mmm>.ogg, transcripts.jsonl, adsb.jsonl
Cloud disks are ephemeral, so uploaded clips older than KEEP_H hours are deleted locally.
"""
import gzip
import io
import json
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "zerocool69/radioaereo-archive"
KEEP_H = 3


def _hour_tar(data: Path, h: datetime, dest: Path) -> int:
    """Pack hour h (UTC, aligned) into dest; returns how many items went in."""
    t0, t1 = h.timestamp(), (h + timedelta(hours=1)).timestamp()
    day, hh = f"{h:%Y-%m-%d}", f"{h:%H}"
    clips = sorted(data.glob(f"clips/SBGR/*/{day}/{hh}*.ogg"))
    tx = []
    if (data / "transcripts.jsonl").exists():
        with (data / "transcripts.jsonl").open() as f:
            tx = [l for l in f if t0 <= json.loads(l).get("t", 0) < t1]
    adsb = []
    if (data / "adsb" / f"{day}.jsonl.gz").exists():
        try:
            with gzip.open(data / "adsb" / f"{day}.jsonl.gz", "rt") as f:
                adsb = [l for l in f if t0 <= json.loads(l)["t"] < t1]
        except (EOFError, OSError, ValueError):   # last member may be mid-write; keep what was read
            pass
    with tarfile.open(dest, "w") as tar:
        for p in clips:
            tar.add(p, arcname=str(p.relative_to(data)))
        for name, lines in (("transcripts.jsonl", tx), ("adsb.jsonl", adsb)):
            b = "".join(lines).encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime = len(b), int(t1)
            tar.addfile(ti, io.BytesIO(b))
    return len(clips) + len(tx) + len(adsb)


class Archiver:
    def __init__(self, token: str, data: Path, log=print, repo: str = REPO):
        from huggingface_hub import HfApi
        self.api, self.data, self.log, self.repo = HfApi(token=token), data, log, repo
        self.done: set[str] = set()
        self.since = time.time()   # hours that ended before this process started have nothing local

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        try:
            self.done = {f[:-4] for f in self.api.list_repo_files(self.repo, repo_type="dataset") if f.endswith(".tar")}
        except Exception as e:
            self.log(f"[archive] list failed: {e}")
        while True:
            try:
                self.tick()
            except Exception as e:
                self.log(f"[archive] {e}")
            time.sleep(300)

    def tick(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for back in range(24, 0, -1):   # every finished hour of the last day not yet uploaded
            h = now - timedelta(hours=back)
            key = f"{h:%Y-%m-%d}/{h:%H}"
            if key in self.done or (h + timedelta(hours=1)).timestamp() <= self.since:
                continue
            tmp = self.data / "archive.tar"
            if not _hour_tar(self.data, h, tmp):
                continue   # nothing recorded that hour (e.g. before this container started)
            self.api.upload_file(path_or_fileobj=str(tmp), path_in_repo=key + ".tar", repo_id=self.repo,
                                 repo_type="dataset", commit_message=f"archive {key}")
            self.done.add(key)
            self.log(f"[archive] {key} uploaded ({tmp.stat().st_size // 1024} KB)")
            tmp.unlink()
        cut = time.time() - KEEP_H * 3600
        for p in self.data.glob("clips/SBGR/*/*/*.ogg"):   # free the ephemeral disk once archived
            h = datetime.strptime(f"{p.parent.name} {p.stem[:2]}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
            if f"{h:%Y-%m-%d}/{h:%H}" in self.done and p.stat().st_mtime < cut:
                p.unlink()
