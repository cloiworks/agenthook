#!/usr/bin/env python3
"""
agenthook.py — a Hermes-style inbound webhook gateway that runs headless agents.

Mirrors NousResearch hermes-agent's webhook adapter
(website/docs/user-guide/messaging/webhooks.md). A webhook simply triggers a
headless Claude Code agent run. Stdlib only — no pip installs.

Design parity with Hermes:
  * POST /webhooks/<route>           (JSON only, 1 MB body cap)
  * per-route HMAC verification      (GitHub / GitLab / generic headers)
  * event filtering via route.events
  * prompt templates with {dot.notation} field access and {__raw__}
  * idempotency cache (X-GitHub-Delivery / X-Request-ID, 1h TTL)
  * per-route rate limit (default 30/min)
  * GET /health -> {"status":"ok"}

Difference from Hermes (by request): the HTTP response is a *one-way ack*.
We return 202 immediately and run the agent in a background thread, instead
of blocking until the agent finishes and then delivering synchronously.

This service is fully standalone — it has NO dependency on cokacdir. A webhook
simply runs a headless Claude Code agent (`claude -p <prompt>`); whatever the
agent does (call an API, run a script, write a file) is the result. There is
no Telegram delivery and no shared bot credentials.

Run modes (per route, field "mode", default "agent"):
  * "agent" -> run the rendered prompt as a headless agent:
               claude -p <prompt> --dangerously-skip-permissions [--model ...]
               [--add-dir ...] [--append-system-prompt ...]
               stdout/stderr are captured to a per-delivery run log.
  * "log"   -> just logs the rendered prompt (dry-run / testing); no agent run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.environ.get(
    "AGENTHOOK_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json"),
)

# Headless agent runner (Claude Code CLI). Fully independent of cokacdir.
DEFAULT_AGENT_BIN = os.environ.get(
    "CLAUDE_BIN",
    os.path.expanduser("~/.local/bin/claude"),
)
DEFAULT_AGENT_TIMEOUT = 1800          # seconds (30 min) per agent run
DEFAULT_AGENT_WORKDIR = os.path.expanduser("~")
# Directory where each agent run's stdout/stderr is captured.
RUN_LOG_DIR = os.environ.get(
    "WEBHOOK_RUN_LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"),
)

MAX_BODY_BYTES = 1 * 1024 * 1024      # 1 MB, like Hermes
IDEMPOTENCY_TTL = 3600                # 1 hour
RATE_WINDOW = 60                      # seconds
DEFAULT_RATE_LIMIT = 30              # requests / window / route


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if "routes" not in cfg or not isinstance(cfg["routes"], dict):
        raise ValueError("config must contain a 'routes' object")
    return cfg


# --------------------------------------------------------------------------
# Prompt template interpolation  (Hermes-compatible)
# --------------------------------------------------------------------------

def _resolve_dotted(payload: dict, dotted: str):
    """Walk payload['a']['b']['c'] for 'a.b.c'. Returns None if missing."""
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def render_prompt(template: str, payload: dict) -> str:
    """
    Replace {dot.notation} tokens. {__raw__} -> full payload JSON (4000 cap).
    Missing keys are left as the literal {token}. Nested dict/list values are
    JSON-serialized and truncated at 2000 chars — same rules as Hermes.
    """
    if template is None:
        # Hermes default: render the full JSON payload when no template given.
        return json.dumps(payload, indent=2, ensure_ascii=False)

    out = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            end = template.find("}", i)
            if end == -1:
                out.append(template[i:])
                break
            token = template[i + 1:end]
            if token == "__raw__":
                raw = json.dumps(payload, indent=2, ensure_ascii=False)
                out.append(raw[:4000])
            else:
                val = _resolve_dotted(payload, token)
                if val is None:
                    out.append("{" + token + "}")          # literal, key missing
                elif isinstance(val, (dict, list)):
                    out.append(json.dumps(val, ensure_ascii=False)[:2000])
                else:
                    out.append(str(val))
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# HMAC signature verification  (Hermes-compatible)
# --------------------------------------------------------------------------

def verify_signature(secret: str, raw_body: bytes, headers) -> bool:
    """
    Returns True if the request is authorized.

    * secret == "INSECURE_NO_AUTH"  -> skip (testing only)
    * X-Hub-Signature-256: sha256=<hex>   (GitHub)
    * X-Gitlab-Token: <token>             (GitLab, plain compare)
    * X-Webhook-Signature: <hex>          (generic raw HMAC-SHA256)
    """
    if secret == "INSECURE_NO_AUTH":
        return True

    secret_b = secret.encode("utf-8")

    gh = headers.get("X-Hub-Signature-256")
    if gh:
        expected = "sha256=" + hmac.new(secret_b, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, gh)

    gl = headers.get("X-Gitlab-Token")
    if gl:
        return hmac.compare_digest(secret, gl)

    generic = headers.get("X-Webhook-Signature")
    if generic:
        expected = hmac.new(secret_b, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, generic)

    # A secret is configured but the request carried no signature header.
    return False


def extract_event(headers, payload: dict):
    return (
        headers.get("X-GitHub-Event")
        or headers.get("X-GitLab-Event")
        or (payload.get("event_type") if isinstance(payload, dict) else None)
    )


# --------------------------------------------------------------------------
# Dispatch -> headless agent run
# --------------------------------------------------------------------------

def _agent_cmd(route: dict, cfg: dict, prompt: str) -> list[str]:
    """Build the headless `claude -p` command from global + per-route config."""
    agent = {**cfg.get("agent", {}), **route.get("agent", {})}
    bin_ = agent.get("bin", DEFAULT_AGENT_BIN)

    cmd = [bin_, "-p", prompt]

    # default to skipping permission prompts unless explicitly overridden
    extra_args = agent.get("extra_args", ["--dangerously-skip-permissions"])
    cmd += list(extra_args)

    if agent.get("model"):
        cmd += ["--model", str(agent["model"])]
    if agent.get("effort"):
        cmd += ["--effort", str(agent["effort"])]

    add_dir = agent.get("add_dir") or []
    for d in add_dir:
        cmd += ["--add-dir", os.path.expanduser(str(d))]

    tools = agent.get("tools")
    if tools:
        cmd += ["--tools", *[str(t) for t in tools]]

    asp = agent.get("append_system_prompt")
    if asp:
        cmd += ["--append-system-prompt", str(asp)]

    return cmd


def dispatch(route_name: str, route: dict, cfg: dict, prompt: str, delivery_id: str):
    """Run in a background thread. Never raises into the HTTP path."""
    mode = route.get("mode", "agent")
    tag = f"[{route_name}/{delivery_id[:8]}]"

    if mode == "log":
        log(f"{tag} (log) prompt:\n{prompt}")
        return

    if mode != "agent":
        log(f"{tag} unknown mode '{mode}', dropping")
        return

    agent = {**cfg.get("agent", {}), **route.get("agent", {})}
    workdir = os.path.expanduser(agent.get("workdir", DEFAULT_AGENT_WORKDIR))
    timeout = int(agent.get("timeout", DEFAULT_AGENT_TIMEOUT))
    cmd = _agent_cmd(route, cfg, prompt)

    os.makedirs(RUN_LOG_DIR, exist_ok=True)
    run_log = os.path.join(RUN_LOG_DIR, f"{route_name}-{delivery_id[:8]}.log")

    log(f"{tag} agent start (workdir={workdir}, timeout={timeout}s) -> {run_log}")
    started = time.time()
    try:
        with open(run_log, "w", encoding="utf-8") as fh:
            fh.write(f"# {tag} cmd: {cmd[:2]} ...(prompt elided)\n")
            fh.flush()
            p = subprocess.run(
                cmd,
                cwd=workdir,
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        dur = time.time() - started
        log(f"{tag} agent done rc={p.returncode} in {dur:.0f}s")
    except subprocess.TimeoutExpired:
        log(f"{tag} agent TIMEOUT after {timeout}s (see {run_log})")
    except Exception as exc:                                   # noqa: BLE001
        log(f"{tag} agent error: {exc}")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

_LOCK = threading.Lock()
_SEEN: dict[str, float] = {}                 # delivery_id -> first-seen ts
_RATE: dict[str, deque] = {}                 # route -> deque[ts]


def log(msg: str):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _seen_before(delivery_id: str) -> bool:
    now = time.time()
    with _LOCK:
        # prune
        for k, ts in list(_SEEN.items()):
            if now - ts > IDEMPOTENCY_TTL:
                del _SEEN[k]
        if delivery_id in _SEEN:
            return True
        _SEEN[delivery_id] = now
        return False


def _rate_ok(route_name: str, limit: int) -> bool:
    now = time.time()
    with _LOCK:
        dq = _RATE.setdefault(route_name, deque())
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "agenthook/1.0"

    # silence default noisy logging; we log ourselves
    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._json(200, {"status": "ok", "platform": "agenthook"})
        return self._json(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        cfg = self.server.cfg                     # type: ignore[attr-defined]
        routes = cfg["routes"]

        if not self.path.startswith("/webhooks/"):
            return self._json(404, {"status": "error", "message": "unknown path"})
        route_name = self.path[len("/webhooks/"):].strip("/").split("?")[0]
        route = routes.get(route_name)
        if route is None:
            return self._json(404, {"status": "error", "message": "unknown route"})

        # body size guard
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._json(413, {"status": "error", "message": "payload too large"})
        raw = self.rfile.read(length) if length else b""

        # rate limit
        limit = int(route.get("rate_limit", cfg.get("rate_limit", DEFAULT_RATE_LIMIT)))
        if not _rate_ok(route_name, limit):
            return self._json(429, {"status": "error", "message": "rate limited"})

        # parse JSON
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except Exception:                                      # noqa: BLE001
            return self._json(400, {"status": "error", "message": "malformed JSON"})

        # auth
        secret = route.get("secret") or cfg.get("secret") or ""
        if not secret:
            return self._json(401, {"status": "error", "message": "no secret configured"})
        if not verify_signature(secret, raw, self.headers):
            return self._json(401, {"status": "error", "message": "invalid signature"})

        # event filter
        events = route.get("events") or []
        if events:
            ev = extract_event(self.headers, payload)
            if ev not in events:
                return self._json(200, {"status": "ignored", "reason": "event not matched",
                                        "event": ev})

        # idempotency
        delivery_id = (self.headers.get("X-GitHub-Delivery")
                       or self.headers.get("X-Request-ID")
                       or str(uuid.uuid4()))
        if _seen_before(delivery_id):
            return self._json(200, {"status": "duplicate", "delivery_id": delivery_id})

        # render + dispatch in background (one-way ack)
        prompt = render_prompt(route.get("prompt"), payload)
        threading.Thread(
            target=dispatch,
            args=(route_name, route, cfg, prompt, delivery_id),
            daemon=True,
        ).start()

        return self._json(202, {"status": "accepted", "route": route_name,
                                "delivery_id": delivery_id})


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = load_config(path)
    port = int(os.environ.get("WEBHOOK_PORT", cfg.get("port", 8644)))
    host = os.environ.get("WEBHOOK_HOST", cfg.get("host", "127.0.0.1"))

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.cfg = cfg                               # type: ignore[attr-defined]
    log(f"agenthook listening on http://{host}:{port}  "
        f"routes={list(cfg['routes'])}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
