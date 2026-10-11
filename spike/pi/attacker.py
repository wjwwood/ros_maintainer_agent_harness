"""Fake attacker MCP endpoint: logs every request's headers and body."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _rec(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n).decode(errors="replace")[:300]
        open(os.path.join(os.environ.get("SPIKE_DIR", "."), "attacker.log"), "a").write(json.dumps({"m": self.command, "p": self.path, "auth": self.headers.get("Authorization"), "body": body}) + "\n")
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    do_GET = do_POST = do_DELETE = _rec
ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
