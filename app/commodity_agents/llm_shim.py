"""LlmClient-compatible backend that runs the debate through the VM's Claude
Code CLI instead of the metered Anthropic API.

The CLI lives on the host (with its subscription OAuth credentials) while this
process runs in a container, so calls go over HTTP to `scripts/claude_shim.py`
— see that module for the host side and its deployment.

Interface note: `run(role, system, user) -> dict` is deliberately identical to
`LlmClient.run`, so debate.py and the orchestrator never branch on backend.
The difference that *does* matter is cost shape, not interface: each CLI
invocation carries ~18.9K tokens of fixed scaffolding, so this backend is only
sane when the caller uses `debate.run_full_debate` (one call, all four roles)
rather than the four-call per-role path.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from app.commodity_agents.llm_client import (
    ROLE_TREND,
    ROLE_VOL,
    LlmError,
    _parse_json_reply,
)

log = logging.getLogger(__name__)

# Roles that never need the web. Everything else (the event seat, and the
# combined seat that plays it) gets WebSearch enabled on the CLI side.
_OFFLINE_ROLES = {ROLE_TREND, ROLE_VOL}


class ShimClient:
    def __init__(
        self,
        url: str,
        token: str,
        model: str = "sonnet",
        enable_web_search: bool = True,
        timeout: int = 600,
    ) -> None:
        if not url:
            raise LlmError("COMMODITY_AGENT_SHIM_URL is empty")
        if not token:
            raise LlmError("COMMODITY_AGENT_SHIM_TOKEN is empty")
        self._url = url.rstrip("/") + "/run"
        self._token = token
        self._model = model
        self._web_search = enable_web_search
        self._timeout = timeout
        # Populated after each run so the caller can log/attribute what the
        # subscription actually spent — the API path gets this from usage
        # headers, here it comes back in the envelope.
        self.last_usage: dict = {}
        self.last_cost_usd: float | None = None

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            # +30s so the shim's own timeout fires first and returns a 504 with
            # a useful message, instead of us dropping the socket blind.
            with urllib.request.urlopen(req, timeout=self._timeout + 30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:400]
            raise LlmError(f"shim returned HTTP {exc.code}: {body}") from exc
        except Exception as exc:
            raise LlmError(f"shim call failed: {exc}") from exc

    _RETRY_SUFFIX = (
        "\n\nCRITICAL OUTPUT RULE: your previous reply could not be parsed. "
        "Return ONLY the JSON object — no markdown fences, no prose before or "
        "after — and keep every string value under 200 characters."
    )

    def run(self, role: str, system: str, user: str) -> dict:
        try:
            return self._run_once(role, system, user)
        except LlmError as first_exc:
            if "shim call failed" in str(first_exc) or "HTTP" in str(first_exc):
                raise                   # transport / CLI failure: don't burn a second run
            log.warning("[shim] %s reply unparseable, retrying once: %s",
                        role, str(first_exc)[:150])
            try:
                return self._run_once(role, system + self._RETRY_SUFFIX, user)
            except LlmError:
                raise first_exc

    def _run_once(self, role: str, system: str, user: str) -> dict:
        web_search = self._web_search and role not in _OFFLINE_ROLES
        out = self._post({
            "system": system,
            "user": user,
            "model": self._model,
            "web_search": web_search,
            "timeout": self._timeout,
        })
        self.last_usage = out.get("usage") or {}
        self.last_cost_usd = out.get("total_cost_usd")
        log.info("[shim] role=%s done in %sms (%s in / %s out tokens)",
                 role, out.get("duration_ms"),
                 self.last_usage.get("input_tokens"),
                 self.last_usage.get("output_tokens"))
        try:
            return _parse_json_reply(out.get("result") or "")
        except json.JSONDecodeError as exc:
            raise LlmError(f"non-JSON reply for role {role}: "
                           f"{str(out.get('result'))[:300]!r}") from exc


def shim_health(url: str, timeout: int = 15) -> dict:
    """GET /healthz — used by the UI to say whether manual (free) analysis is
    available before the user triggers a run that would fail."""
    try:
        with urllib.request.urlopen(
            url.rstrip("/") + "/healthz", timeout=timeout
        ) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)}
