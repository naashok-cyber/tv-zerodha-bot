"""Claude API wrapper with per-role model routing.

Routing lives in Settings (COMMODITY_AGENT_MODEL_*) so the model behind each
role can be swapped via .env without touching code — e.g. a cheaper model for
debate agents, a stronger one for the Judge's synthesis.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

ROLE_TREND = "trend"
ROLE_EVENT = "event"
ROLE_VOL = "vol"
ROLE_JUDGE = "judge"


class LlmError(Exception):
    pass


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def _parse_json_reply(raw: str) -> dict:
    """Parse a model reply that should be one JSON object. Tolerates code
    fences, leading/trailing prose, and picks out the outermost {...} span.
    Raises json.JSONDecodeError when nothing parseable is found."""
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


class LlmClient:
    def __init__(
        self,
        api_key: str,
        role_models: dict[str, str],
        enable_web_search: bool = True,
        max_tokens: int = 3000,     # web-search replies got truncated at 1500
    ) -> None:
        if not api_key:
            raise LlmError("ANTHROPIC_API_KEY is empty")
        self._api_key = api_key
        self._role_models = role_models
        self._web_search = enable_web_search
        self._max_tokens = max_tokens
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic  # runtime import, same pattern as voice NLU
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    _MAX_TURNS = 5      # pause_turn continuation cap (web-search rounds)

    def run(self, role: str, system: str, user: str) -> dict:
        """Call the role's model, parse its JSON reply. Raises LlmError on failure."""
        model = self._role_models.get(role)
        if not model:
            raise LlmError(f"no model configured for role {role!r}")

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "system": system,
        }
        # Event/gap-risk agent gets web search so it can catch breaking news
        # (geopolitical shocks, cold snaps) that no static calendar contains.
        if role == ROLE_EVENT and self._web_search:
            kwargs["tools"] = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2,
            }]

        # Server-side tools (web search) can pause mid-turn: the API returns
        # stop_reason="pause_turn" with only tool blocks and expects the
        # partial content echoed back as an assistant message to continue.
        # Without this loop those responses have no text block at all.
        messages: list[dict] = [{"role": "user", "content": user}]
        msg = None
        try:
            for _ in range(self._MAX_TURNS):
                msg = self._get_client().messages.create(messages=messages, **kwargs)
                if getattr(msg, "stop_reason", None) != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": msg.content})
        except Exception as exc:
            raise LlmError(f"API call failed for role {role}: {exc}") from exc

        # last text block carries the final JSON (earlier blocks may be
        # search results / intermediate text when tools are used)
        text_blocks = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        if not text_blocks:
            raise LlmError(f"no text content in response for role {role} "
                           f"(stop_reason={getattr(msg, 'stop_reason', None)})")
        try:
            return _parse_json_reply(text_blocks[-1])
        except json.JSONDecodeError as exc:
            raise LlmError(
                f"non-JSON reply for role {role}: {text_blocks[-1][:300]!r}"
            ) from exc
