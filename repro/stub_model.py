"""A stub OpenAI-compatible model that calls one tool, so before_tool_call fires for real.

No credentials, no spend, and deterministic: it reads the tools the host offered, picks a
harmless one, and returns a tool_call for it. The second turn ends the conversation.
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFERRED = ["read", "session_status", "web_fetch", "exec"]
STATE = {"calls": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "stub", "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        offered = [t.get("function", {}).get("name") for t in (req.get("tools") or [])]
        offered = [n for n in offered if n]
        roles = [m.get("role") for m in (req.get("messages") or [])]
        print(f"[stub] turn {STATE['calls']}; {len(offered)} tools; roles={roles[-6:]}",
              file=sys.stderr, flush=True)

        STATE["calls"] += 1
        pick = next((p for p in PREFERRED if p in offered), offered[0] if offered else None)
        # Call a tool whenever this conversation has not produced a tool result yet, so the
        # stub works for any number of turns instead of only the first one ever.
        msgs = req.get("messages") or []
        last = msgs[-1].get("role") if msgs else None
        # One tool call per user turn: fire when the newest message is the user's, and finish
        # when the newest is the tool result we just caused.
        already = last != "user"

        base = {"id": "chatcmpl-stub", "object": "chat.completion",
                "created": int(time.time()), "model": req.get("model", "stub"),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

        # First turn calls a tool; every later turn ends the run.
        if STATE["calls"] > 40:
            pick = None
        if not already and pick:
            args = {"path": ".openclaw/tmp/probe.txt"} if pick == "read" else {}
            print(f"[stub] calling tool: {pick} {args}", file=sys.stderr, flush=True)
            base["choices"] = [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_probe", "type": "function", "function": {
                    "name": pick, "arguments": json.dumps(args)}}]}}]
        else:
            base["choices"] = [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "done"}}]
        self._json(200, base)


if __name__ == "__main__":
    print("[stub] listening on 127.0.0.1:8099", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8099), H).serve_forever()
