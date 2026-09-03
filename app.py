import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))
_run_lock = threading.Lock()
_running = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # health for Render - keeps service alive
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        # upload trigger - runs main.py (alias /run and /upload for compat)
        if self.path in ("/run", "/upload"):
            global _running
            if _running:
                self.send_response(429)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"already running")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"started")
            threading.Thread(target=_run_job, daemon=True).start()
            return
        # 404 for unknown
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"not found")

    def do_POST(self):
        # allow POST to /upload as well
        if self.path in ("/upload", "/run"):
            self.do_GET()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, format, *args):
        pass

def _run_job():
    global _running
    _running = True
    # tell main.py not to start its own healthcheck (PORT already used)
    os.environ["FROM_APP"] = "1"
    try:
        from main import main
        import sys
        sys.argv = ["main.py"]
        main()
    except Exception as e:
        print(f"Job error: {e}")
    finally:
        _running = False
        os.environ.pop("FROM_APP", None)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
