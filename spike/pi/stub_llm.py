"""Stub OpenAI-compatible chat server for the Pi spike. Logs requests; scripted tool call."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import os
SPIKE_DIR = os.environ.get("SPIKE_DIR", ".")
LOG = open(os.path.join(SPIKE_DIR, "stub.log"), "a", buffering=1)
TARGET_TOOL = sys.argv[3] if len(sys.argv) > 3 else "get_workspace_status"


def log(**kw):
    LOG.write(json.dumps(kw) + "\n")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        log(method="GET", path=self.path, auth=self.headers.get("Authorization"))
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
        self._send(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        msgs = req.get("messages", [])
        tools = [t.get("function", {}).get("name") for t in req.get("tools", [])]
        log(method="POST", path=self.path, auth=self.headers.get("Authorization"), stream=req.get("stream"),
            model=req.get("model"), n_msgs=len(msgs), tools=tools,
            last_role=msgs[-1]["role"] if msgs else None)
        for t in req.get("tools", []):
            if t.get("function", {}).get("name") == "codemode":
                open(os.path.join(SPIKE_DIR, "codemode_schema.json"), "w").write(json.dumps(t, indent=1))
        last = msgs[-1] if msgs else {}
        text = "STUB: hello from the stub model"
        call = None
        if last.get("role") == "user":
            content = last.get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if "CALLTOOL" in (content or ""):
                if "codemode" in tools:
                    call = ("codemode", {"code": open(os.path.join(SPIKE_DIR, "code.js")).read()})
        elif last.get("role") == "tool":
            text = "STUB: tool result was: " + str(last.get("content"))[:3000]
        message = {"role": "assistant", "content": None if call else text}
        if call:
            message["tool_calls"] = [{"id": "call_1", "type": "function",
                                      "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
        finish = "tool_calls" if call else "stop"
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(obj):
                b = ("data: " + json.dumps(obj) + "\n\n").encode()
                self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
            base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub-model"}
            delta = {"role": "assistant"}
            if call:
                delta["tool_calls"] = [{"index": 0, "id": "call_1", "type": "function",
                                        "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
            else:
                delta["content"] = text
            chunk({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n0\r\n\r\n")
            return
        self._send(200, {"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": "stub-model",
                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
