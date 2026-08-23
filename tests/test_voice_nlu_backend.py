"""Voice NLU backend selection — metered API vs the subscription shim.

Voice is human-triggered one command at a time, which is the same property
that justified routing manual commodity analyses through the host's Claude
Code CLI. What these tests pin is the routing itself: which backend runs, that
`shim_then_api` really does fall back, and that a misconfiguration is loud
rather than quietly billing the API.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import Settings
from app.voice import nlu as nlu_mod
from app.voice.nlu import (
    BACKEND_API,
    BACKEND_SHIM,
    BACKEND_SHIM_THEN_API,
    resolve_nlu,
)

_PARSED = {"action": "BUY", "underlying": "NIFTY", "confidence": 0.97}


def _cfg(**over) -> Settings:
    base = dict(
        _env_file=None,
        SECRET_KEY="x" * 32,
        ANTHROPIC_API_KEY="sk-test-fake",
        COMMODITY_AGENT_SHIM_URL="http://host.docker.internal:8765",
        COMMODITY_AGENT_SHIM_TOKEN="shim-tok",
    )
    base.update(over)
    return Settings(**base)


# ── straight routing ────────────────────────────────────────────────────────

def test_api_backend_never_touches_the_shim():
    cfg = _cfg(VOICE_NLU_BACKEND="api")
    with patch.object(nlu_mod, "call_nlu", return_value=_PARSED) as api, \
         patch.object(nlu_mod, "call_nlu_shim") as shim:
        parsed, used = resolve_nlu("buy one lot NIFTY call", cfg)
    assert (parsed, used) == (_PARSED, BACKEND_API)
    assert api.called and not shim.called


def test_shim_backend_never_touches_the_api():
    """The whole point of the shim is not spending — a shim-only config that
    silently reached for the metered API would defeat it."""
    cfg = _cfg(VOICE_NLU_BACKEND="shim")
    with patch.object(nlu_mod, "call_nlu") as api, \
         patch.object(nlu_mod, "call_nlu_shim", return_value=_PARSED) as shim:
        parsed, used = resolve_nlu("buy one lot NIFTY call", cfg)
    assert (parsed, used) == (_PARSED, BACKEND_SHIM)
    assert shim.called and not api.called


def test_shim_is_called_without_web_search_and_with_the_cli_alias():
    """A CLI invocation with WebSearch enabled costs tokens and latency, and
    parsing an order needs no web. The model must be the CLI alias, not the
    API model id in VOICE_NLU_MODEL."""
    cfg = _cfg(VOICE_NLU_BACKEND="shim", VOICE_NLU_SHIM_MODEL="sonnet",
               VOICE_NLU_SHIM_TIMEOUT=45)
    with patch("app.commodity_agents.llm_shim.ShimClient") as Client:
        Client.return_value.run.return_value = _PARSED
        resolve_nlu("buy one lot NIFTY call", cfg)
    kwargs = Client.call_args.kwargs
    assert kwargs["enable_web_search"] is False
    assert kwargs["model"] == "sonnet"
    assert kwargs["timeout"] == 45
    assert kwargs["url"] == "http://host.docker.internal:8765"


# ── fallback ────────────────────────────────────────────────────────────────

def test_shim_then_api_prefers_the_shim():
    cfg = _cfg(VOICE_NLU_BACKEND="shim_then_api")
    with patch.object(nlu_mod, "call_nlu") as api, \
         patch.object(nlu_mod, "call_nlu_shim", return_value=_PARSED):
        parsed, used = resolve_nlu("buy NIFTY", cfg)
    assert used == BACKEND_SHIM
    assert not api.called


def test_shim_then_api_falls_back_when_the_shim_is_busy():
    """The shim serialises to one CLI run; a commodity debate holds it ~150s.
    A spoken order must not die because an analysis was running."""
    cfg = _cfg(VOICE_NLU_BACKEND="shim_then_api")
    busy = RuntimeError("shim returned HTTP 429: a claude run is already in flight")
    with patch.object(nlu_mod, "call_nlu", return_value=_PARSED) as api, \
         patch.object(nlu_mod, "call_nlu_shim", side_effect=busy):
        parsed, used = resolve_nlu("buy NIFTY", cfg)
    assert (parsed, used) == (_PARSED, BACKEND_API)
    assert api.called


def test_shim_then_api_falls_back_when_the_shim_is_unconfigured():
    """Turning the fallback policy on before the shim exists should degrade to
    the old behaviour, not 503."""
    cfg = _cfg(VOICE_NLU_BACKEND="shim_then_api",
               COMMODITY_AGENT_SHIM_URL="", COMMODITY_AGENT_SHIM_TOKEN="")
    with patch.object(nlu_mod, "call_nlu", return_value=_PARSED):
        parsed, used = resolve_nlu("buy NIFTY", cfg)
    assert used == BACKEND_API


def test_fallback_still_raises_when_both_backends_are_dead():
    cfg = _cfg(VOICE_NLU_BACKEND="shim_then_api", ANTHROPIC_API_KEY="")
    with patch.object(nlu_mod, "call_nlu_shim", side_effect=RuntimeError("down")):
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            resolve_nlu("buy NIFTY", cfg)


# ── misconfiguration is loud ────────────────────────────────────────────────

def test_unknown_backend_raises_rather_than_defaulting_to_the_api():
    """A typo'd .env value must not quietly route spend to the metered API."""
    cfg = _cfg(VOICE_NLU_BACKEND="shimm")
    with pytest.raises(ValueError, match="VOICE_NLU_BACKEND"):
        resolve_nlu("buy NIFTY", cfg)


def test_shim_only_with_no_shim_url_raises():
    cfg = _cfg(VOICE_NLU_BACKEND="shim", COMMODITY_AGENT_SHIM_URL="")
    with pytest.raises(ValueError, match="COMMODITY_AGENT_SHIM_URL"):
        resolve_nlu("buy NIFTY", cfg)


def test_backend_value_is_case_insensitive():
    cfg = _cfg(VOICE_NLU_BACKEND="SHIM")
    with patch.object(nlu_mod, "call_nlu_shim", return_value=_PARSED):
        _, used = resolve_nlu("buy NIFTY", cfg)
    assert used == BACKEND_SHIM


def test_default_backend_is_the_api():
    """Existing deployments must not silently switch backends on upgrade."""
    assert _cfg().VOICE_NLU_BACKEND == BACKEND_API
    assert BACKEND_SHIM_THEN_API == "shim_then_api"
