"""Web server: map page, aircraft + transcript SSE stream, clip playback. Binds 127.0.0.1 unless HOST is set (cloud)."""
import json
import os
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".ogg": "audio/ogg", ".json": "application/json"}


class Hub:
    def __init__(self, tracker, data_dir: Path, web_dir: Path):
        self.tracker, self.data, self.web = tracker, data_dir.resolve(), web_dir.resolve()
        self.recent: deque = deque(maxlen=60)
        self.alerts: deque = deque(maxlen=20)
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.state, self.channels, self.stats = None, {}, {}

    def publish(self, event: str, payload) -> None:
        if event == "tx":
            self.recent.append(payload)
        if event == "alert":
            self.alerts.append(payload)
        msg = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
        with self.lock:
            for q in self.subs:
                q.put(msg)

    def serve(self, port: int = 8787, log=print):
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _file(self, base: Path, rel: str):
                p = (base / rel).resolve()
                if not p.is_relative_to(base) or not p.is_file():  # no path traversal
                    return self._send(404, b"not found", "text/plain")
                self._send(200, p.read_bytes(), TYPES.get(p.suffix, "application/octet-stream"))

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/app"):
                    return self._file(hub.web, "app.html")
                if path == "/sbgr.json":
                    return self._file(hub.web, "sbgr.json")
                if path == "/health":
                    st = hub.state or {}
                    return self._send(200, json.dumps({"ok": bool(st), "aircraft": len(st.get("ac", [])), "age": round(time.time() - st.get("t", 0), 1) if st else None,
                                                       "stats": st.get("stats")}).encode())
                if path == "/api/hello":
                    return self._send(200, json.dumps({"channels": hub.channels, "state": hub.state, "recent": list(hub.recent),
                                                       "alerts": list(hub.alerts), "flights": getattr(hub, "flights", [])}, ensure_ascii=False).encode())
                if path == "/api/aircraft":
                    return self._send(200, json.dumps(hub.tracker.snapshot()).encode())
                if path == "/api/recent":
                    return self._send(200, json.dumps(list(hub.recent), ensure_ascii=False).encode())
                if path.startswith("/clips/"):
                    return self._file(hub.data / "clips", path[len("/clips/"):])
                if path.startswith("/static/"):
                    return self._file(hub.web, path[len("/static/"):])
                if path == "/api/stream":
                    q: queue.Queue = queue.Queue()
                    with hub.lock:
                        hub.subs.append(q)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        while True:
                            try:
                                self.wfile.write(q.get(timeout=15))
                            except queue.Empty:
                                self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        with hub.lock:
                            hub.subs.remove(q)
                    return
                self._send(404, b"not found", "text/plain")

        srv = ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"map → http://127.0.0.1:{port}")
