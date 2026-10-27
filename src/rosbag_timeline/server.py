"""Loopback-only viewer. No ROS, Node.js, npm, CDN, or application framework."""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files


def serve(analysis, port=8765, open_browser=True):
    allowed_assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/style.css": ("style.css", "text/css")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def allowed(self):
            return self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")

        def send(self, code, content, mime="application/json"):
            if not isinstance(content, bytes):
                content = json.dumps(content, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "loopback host required"})
            if self.path in allowed_assets:
                name, mime = allowed_assets[self.path]
                return self.send(200, files("rosbag_timeline").joinpath("web", name).read_bytes(), mime)
            if self.path == "/api/meta":
                return self.send(200, analysis.catalog())
            if self.path == "/api/progress":
                return self.send(200, analysis.progress)
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected_origin = "http://" + self.headers.get("Host", "")
            if not self.allowed() or self.headers.get("X-Timeline") != "1" or (origin and origin != expected_origin):
                return self.send(403, {"error": "same-origin requests required"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 65536:
                    raise ValueError("invalid request length")
                body = json.loads(self.rfile.read(length))
                start = int(body["start"])
                end = int(body["end"])
                if not 0 <= start < end <= 2**63 - 1:
                    raise ValueError("invalid time range")
                tids = analysis.select([int(x) for x in body.get("topics", analysis.topics)], node=body.get("node", ""))
                if self.path == "/api/view":
                    result = analysis.view(start, end, tids, metric=body.get("metric", "record_stamp"),
                        selection=body.get("selection"), selection_topic=body.get("selection_topic"),
                        bins=max(1, min(2000, int(body.get("bins", 600)))), limit=20000)
                elif self.path == "/api/stats":
                    result = analysis.statistics(start, end, tids, bins=80)
                else:
                    return self.send(404, {"error": "not found"})
                self.send(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception as exc:
                self.send(500, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Open {url}\nBag times stay on this computer. Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
