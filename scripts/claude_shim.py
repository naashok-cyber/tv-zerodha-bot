#!/usr/bin/env python3
"""Host-side bridge that lets the bot container reach the VM's Claude Code CLI.

Why this exists: the commodity debate agents normally call the Anthropic API,
which is metered. The VM already has Claude Code authenticated with a
subscription OAuth token (~/.claude/.credentials.json), so shelling out to
`claude -p` runs the same models against the subscription instead. But the bot
runs in Docker with only ./data mounted — the CLI and its credentials live on
the host. Rather than bind-mounting a refreshing OAuth token into a container,
this service stays on the host and exposes one narrow endpoint.

Deliberately stdlib-only: it runs under the host's system python3, outside the
project virtualenv, and must keep working after any image rebuild.

  POST /run    {"system": str, "user": str, "model": str, "web_search": bool}
               -> {"result": str, "usage": {...}, "duration_ms": int}
  GET  /healthz -> {"status": "ok", "claude": "<version>", "busy": bool}

Auth is a bearer token (CLAUDE_SHIM_TOKEN), required — the service refuses to
start without one, because reaching the container means binding something
wider than loopback.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("claude_shim")

BIND = os.environ.get("CLAUDE_SHIM_BIND", "0.0.0.0")
PORT = int(os.environ.get("CLAUDE_SHIM_PORT", "8765"))
TOKEN = os.environ.get("CLAUDE_SHIM_TOKEN", "")
CLAUDE_BIN = os.environ.get("CLAUDE_SHIM_BIN", "claude")
# One `claude` process at a time. Concurrent invocations would race for the
# same OAuth token refresh and multiply an already-heavy per-call cost.
_LOCK = threading.Lock()
_BUSY = threading.Event()

MAX_BODY = 1 << 20          # 1 MiB — a debate context is a few KB
DEFAULT_TIMEOUT = 600


def _run_claude(system: str, user: str, model: str, web_search: bool,
                timeout: int) -> dict:
    """One headless Claude Code turn. Runs in an empty scratch directory so no
    repo CLAUDE.md or project settings leak into the prompt."""
    cmd = [
        CLAUDE_BIN, "-p", user,
        "--output-format", "json",
        "--model", model,
        # Tools are off by default: the debate is pure reasoning over a context
        # the bot already computed, and every extra tool schema is tokens.
        "--allowed-tools", "WebSearch" if web_search else "",
    ]
    if system:
        cmd += ["--append-system-prompt", system]

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="claude-shim-") as workdir:
        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    duration_ms = int((time.time() - started) * 1000)

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude returned non-JSON envelope: "
                           f"{proc.stdout[:300]!r}") from exc
    if envelope.get("is_error"):
        raise RuntimeError(f"claude reported an error: "
                           f"{str(envelope.get('result'))[:400]}")

    return {
        "result": envelope.get("result", ""),
        "usage": envelope.get("usage", {}),
        "total_cost_usd": envelope.get("total_cost_usd"),
        "duration_ms": duration_ms,
        "session_id": envelope.get("session_id"),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-shim"

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        # hmac.compare_digest over str is fine here (both ASCII)
        import hmac
        return hmac.compare_digest(header, expected)

    def do_GET(self) -> None:                            # noqa: N802
        if self.path != "/healthz":
            self._send(404, {"error": "not found"})
            return
        try:
            version = subprocess.run(
                [CLAUDE_BIN, "--version"], capture_output=True, text=True,
                timeout=20, stdin=subprocess.DEVNULL,
            ).stdout.strip()
        except Exception as exc:
            self._send(503, {"status": "claude unavailable", "error": str(exc)})
            return
        self._send(200, {"status": "ok", "claude": version,
                         "busy": _BUSY.is_set()})

    def do_POST(self) -> None:                           # noqa: N802
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            return
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            self._send(400, {"error": f"bad JSON: {exc}"})
            return

        user = (req.get("user") or "").strip()
        if not user:
            self._send(400, {"error": "'user' is required"})
            return
        model = req.get("model") or "sonnet"
        timeout = min(int(req.get("timeout") or DEFAULT_TIMEOUT), 1800)

        # Serialize, but fail fast rather than queue: the caller has its own
        # timeout and a queued request would blow past it anyway.
        if not _LOCK.acquire(blocking=False):
            self._send(429, {"error": "a claude run is already in flight"})
            return
        _BUSY.set()
        try:
            out = _run_claude(
                system=req.get("system") or "",
                user=user,
                model=model,
                web_search=bool(req.get("web_search")),
                timeout=timeout,
            )
            log.info("run ok model=%s in %dms (%s tok out)", model,
                     out["duration_ms"], out.get("usage", {}).get("output_tokens"))
            self._send(200, out)
        except subprocess.TimeoutExpired:
            log.warning("run timed out after %ss", timeout)
            self._send(504, {"error": f"claude timed out after {timeout}s"})
        except Exception as exc:
            log.error("run failed: %s", exc)
            self._send(502, {"error": str(exc)})
        finally:
            _BUSY.clear()
            _LOCK.release()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not TOKEN:
        log.error("CLAUDE_SHIM_TOKEN is empty. This service binds beyond "
                  "loopback so the container can reach it; refusing to start "
                  "without a shared secret.")
        return 2
    if shutil.which(CLAUDE_BIN) is None and not os.path.isfile(CLAUDE_BIN):
        log.error("claude CLI not found at %r — set CLAUDE_SHIM_BIN", CLAUDE_BIN)
        return 2

    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log.info("claude-shim listening on %s:%d (bin=%s)", BIND, PORT, CLAUDE_BIN)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
