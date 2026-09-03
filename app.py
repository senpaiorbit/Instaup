import os
import json
import threading
import queue
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

PORT = int(os.environ.get("PORT", 8080))
_running = False
_log_queues: list[queue.Queue] = []
_log_lock = threading.Lock()
_log_history: list[str] = []
_MAX_HISTORY = 500

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_FILE = PROJECT_ROOT / "data" / "logs" / "bot.log"

class BroadcastHandler:
    @staticmethod
    def emit(msg: str):
        line = msg.rstrip()
        if not line:
            return
        with _log_lock:
            _log_history.append(line)
            if len(_log_history) > _MAX_HISTORY:
                _log_history.pop(0)
            for q in list(_log_queues):
                try:
                    q.put_nowait(line)
                except Exception:
                    pass

import logging
class QueueLogHandler(logging.Handler):
    def emit(self, record):
        try:
            BroadcastHandler.emit(self.format(record))
        except Exception:
            pass

try:
    _qh = QueueLogHandler()
    _qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger("instabot").addHandler(_qh)
    logging.getLogger("instabot").setLevel(logging.INFO)
except Exception:
    pass

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _running
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        # /upload and /run - support ?stream=1 for raw SSE and custom query overrides
        if self.path == "/upload" or self.path == "/run" or self.path.startswith("/upload?") or self.path.startswith("/run?"):
            # parse query overrides for custom run without recommit: ?src=accounts&account=@a,@b&cover=cover/2.jpg etc
            query = {}
            if "?" in self.path:
                try:
                    qs = parse_qs(urlparse(self.path).query)
                    # flatten: take first value, keep list for account
                    for k, v in qs.items():
                        if k == "stream":
                            continue
                        # handle comma-separated and multiple values
                        vals = []
                        for val in v:
                            # split on comma and also handle @ prefix
                            for part in val.split(","):
                                part = unquote(part).strip()
                                if part:
                                    vals.append(part)
                        query[k] = vals if len(vals) > 1 else (vals[0] if vals else "")
                    # special: account param may be like ?account=@xyz&account=@abcd or ?account=@xyz,@abcd or ?account=@xyz@{abcd} (legacy)
                    if "account" in query:
                        # already handled
                        pass
                    # also support ?src=reels, ?src=accounts, ?src=feed
                    # also support ?cover=cover/2.jpg, ?caption_mode=custom etc
                    if query:
                        BroadcastHandler.emit(f"Query override: {query}")
                except Exception as e:
                    BroadcastHandler.emit(f"Query parse error: {e}")
            if "stream=1" in self.path or "text/event-stream" in self.headers.get("Accept", ""):
                if not _running:
                    threading.Thread(target=_run_job, args=(query,), daemon=True).start()
                    time.sleep(0.5)
                self._stream_logs()
                return
            # browser HTML with live EventSource
            if not _running:
                threading.Thread(target=_run_job, args=(query,), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            html = b"""<!doctype html><html><head><meta charset="utf-8"><title>InstaUp Live</title>
<style>body{font-family:monospace;background:#0f0f0f;color:#0f0;padding:16px}pre{white-space:pre-wrap;word-break:break-all}#log{border:1px solid #333;padding:12px;height:70vh;overflow:auto;background:#000}</style></head>
<body><h2>InstaUp - Live log</h2><pre id="log">Connecting...</pre><script>
const log=document.getElementById('log');
log.textContent='';
const es=new EventSource('/logs/stream');
es.onmessage=e=>{log.textContent+=e.data+"\\n";log.scrollTop=log.scrollHeight};
es.onerror=()=>{es.close()};
</script><p><a href="/logs" style="color:#0ff">raw logs</a> | <a href="/health" style="color:#0ff">health</a> | <a href="/upload?stream=1">plain stream</a></p></body></html>"""
            self.wfile.write(html)
            return

        if self.path == "/logs":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            with _log_lock:
                for line in _log_history:
                    self.wfile.write((line + "\n").encode())
            if LOG_FILE.exists():
                try:
                    with open(LOG_FILE, "r") as f:
                        for l in f.readlines()[-200:]:
                            if l.strip() not in _log_history:
                                self.wfile.write(l.encode())
                except Exception:
                    pass
            return

        if self.path == "/logs/stream":
            self._stream_logs()
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"not found")

    def _stream_logs(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        with _log_lock:
            _log_queues.append(q)
            for line in _log_history:
                try:
                    self.wfile.write(f"data: {line}\n\n".encode())
                except Exception:
                    break
        try:
            if LOG_FILE.exists():
                try:
                    with open(LOG_FILE, "r") as f:
                        for line in f.readlines()[-30:]:
                            line=line.strip()
                            if line and line not in _log_history:
                                self.wfile.write(f"data: {line}\n\n".encode())
                except Exception:
                    pass
            self.wfile.flush()
            start = time.time()
            while True:
                try:
                    line = q.get(timeout=10)
                    self.wfile.write(f"data: {line}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    if not _running and time.time() - start > 30:
                        break
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                if not _running and q.empty():
                    time.sleep(1)
                    if q.empty():
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _log_lock:
                if q in _log_queues:
                    _log_queues.remove(q)

    def do_POST(self):
        global _running
        # parse query for POST as well
        query = {}
        if "?" in self.path:
            try:
                qs = parse_qs(urlparse(self.path).query)
                for k, v in qs.items():
                    if k == "stream":
                        continue
                    vals = []
                    for val in v:
                        for part in val.split(","):
                            part = unquote(part).strip()
                            if part:
                                vals.append(part)
                    query[k] = vals if len(vals) > 1 else (vals[0] if vals else "")
            except Exception:
                pass
        if self.path in ("/upload", "/run") or self.path.startswith("/upload?") or self.path.startswith("/run?"):
            if "stream=1" in self.path:
                if not _running:
                    threading.Thread(target=_run_job, args=(query,), daemon=True).start()
                    time.sleep(0.5)
                self._stream_logs()
                return
            self.do_GET()
        elif self.path == "/logs/stream":
            self._stream_logs()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, format, *args):
        pass

def _run_job(query: dict | None = None):
    global _running
    _running = True
    if query:
        BroadcastHandler.emit(f"Query override: {query}")
        try:
            os.environ["CONFIG_OVERRIDE"] = json.dumps(query)
        except Exception:
            pass
    BroadcastHandler.emit("=== Upload job started ===")
    BroadcastHandler.emit(f"Config: {PROJECT_ROOT / 'config.json'}")
    os.environ["FROM_APP"] = "1"
    try:
        BroadcastHandler.emit("Loading config and session...")
        from main import main
        import sys
        sys.argv = ["main.py"]
        BroadcastHandler.emit(f"Calling main() - fetching feed... src={query.get('src') if query else 'config'}")
        ret = main()
        BroadcastHandler.emit(f"main() returned {ret}")
        BroadcastHandler.emit("=== Upload job finished ===")
    except Exception as e:
        import traceback
        BroadcastHandler.emit(f"Job error: {e}")
        BroadcastHandler.emit(traceback.format_exc())
        print(f"Job error: {e}")
        traceback.print_exc()
    finally:
        _running = False
        os.environ.pop("FROM_APP", None)

if __name__ == "__main__":
    print(f"InstaUp listening on 0.0.0.0:{PORT}  /health  /upload  /logs  /logs/stream")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
