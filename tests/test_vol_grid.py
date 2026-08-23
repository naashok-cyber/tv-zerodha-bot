"""Markets panel — one volatility tile per configured scrip, verdict folded in.

The chart engine itself is canvas JS and can't be asserted here; what these
tests pin is the contract between the server-rendered skeleton and
_VOL_GRID_JS: one tile per COMMODITY_AGENTS_COMMODITIES entry, every data-r
hook the script writes into, and the fact that Approve/Reject exist only
inside the expanded-only block.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _auth_guard, _VOL_LABELS, app, get_current_settings


@pytest.fixture
def cfg() -> Settings:
    return Settings(SECRET_KEY="x" * 32)


@pytest.fixture
def client(cfg):
    app.dependency_overrides[get_current_settings] = lambda: cfg
    app.dependency_overrides[_auth_guard] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _control_html(client) -> str:
    """GET /control with a concrete token payload.

    conftest's blanket MagicMock session manager returns a MagicMock for
    age_hours, which blows up the f-string in the session-status card.
    """
    with patch("app.main.get_session_manager") as mock_factory:
        mock_factory.return_value.get_token_info.return_value = {
            "is_valid": False, "age_hours": None, "reason": "no token",
        }
        resp = client.get("/control")
    assert resp.status_code == 200
    return resp.text


# ── tiles ───────────────────────────────────────────────────────────────────

def test_one_tile_per_configured_commodity(client, cfg):
    html = _control_html(client)
    assert html.count("class='vgt'") == len(cfg.COMMODITY_AGENTS_COMMODITIES)
    for c in cfg.COMMODITY_AGENTS_COMMODITIES:
        assert f"data-c='{c}'" in html


def test_tiles_follow_the_config_rather_than_a_literal_list(cfg):
    """Adding or removing a scrip in .env must move the grid with it — the old
    monitor's dropdown was a hardcoded six and could silently disagree."""
    trimmed = cfg.model_copy(
        update={"COMMODITY_AGENTS_COMMODITIES": ["GOLDM", "SILVERM"]})
    app.dependency_overrides[get_current_settings] = lambda: trimmed
    app.dependency_overrides[_auth_guard] = lambda: None
    try:
        with TestClient(app) as c:
            html = _control_html(c)
    finally:
        app.dependency_overrides.clear()
    assert html.count("class='vgt'") == 2
    assert "data-c='GOLDM'" in html
    assert "data-c='NATURALGAS'" not in html


def test_unlabelled_scrip_falls_back_to_its_raw_key(cfg):
    """A scrip added to the config without a _VOL_LABELS entry must still
    render, unprettified — not crash the whole page."""
    odd = cfg.model_copy(update={"COMMODITY_AGENTS_COMMODITIES": ["COPPERM"]})
    assert "COPPERM" not in _VOL_LABELS
    app.dependency_overrides[get_current_settings] = lambda: odd
    app.dependency_overrides[_auth_guard] = lambda: None
    try:
        with TestClient(app) as c:
            html = _control_html(c)
    finally:
        app.dependency_overrides.clear()
    assert "data-c='COPPERM'" in html
    assert "<b>COPPERM</b>" in html


def test_every_hook_the_script_writes_into_exists(client):
    """_VOL_GRID_JS looks these up per tile; a renamed hook fails silently in
    the browser (the `if(e)` guards swallow it), so pin them here."""
    html = _control_html(client)
    for hook in ("badge", "iv", "state", "open", "rangev", "rv", "vrp",
                 "confk", "conf", "sizek", "size", "sum", "btns", "cv", "tip"):
        assert f"data-r='{hook}'" in html, hook


# ── merged commodity intelligence ───────────────────────────────────────────

def test_standalone_commodity_intelligence_card_is_gone(client):
    html = _control_html(client)
    assert "Commodity Intelligence" not in html
    assert "id='ca-grid'" not in html
    assert "class='cagrid'" not in html


def test_live_script_no_longer_fetches_recommendations(client):
    """The verdicts ride on the tiles now. If _CONTROL_LIVE_JS also polled
    /recommendations we'd be back to two fetches of the same rows."""
    html = _control_html(client)
    assert "loadCommodities" not in html
    # count the call, not the word — a comment mentioning the endpoint is fine
    assert html.count("'/recommendations'") == 1   # the grid script's, only
    assert "iv-history?limit=40" not in html       # the old duplicate pull


def test_action_buttons_live_only_in_the_expanded_block(client):
    """Approve/Reject are injected into [data-r=btns], which sits inside
    .vgt-x — display:none until the tile is expanded. A money button must
    never be one stray tap away from a sparkline."""
    html = _control_html(client)
    body = html[html.index("class='vg-grid'"):]
    tile = body[:body.index("</article>")]
    btns = tile.index("data-r='btns'")
    assert tile.index("class='vgt-x'") < btns
    # and the skeleton ships empty — nothing clickable before a verdict loads
    assert "data-r='btns'></div>" in tile


def test_verdict_pill_starts_neutral(client):
    """Tiles paint before /recommendations lands; the placeholder must not
    imply a direction."""
    html = _control_html(client)
    assert "<span class='pill pm' data-r='badge'>" in html


# ── shared controls ─────────────────────────────────────────────────────────

def test_range_control_is_shared_not_per_tile(client, cfg):
    html = _control_html(client)
    assert html.count("id='vg-range'") == 1
    assert html.count("id='vg-collapse'") == 1
    # the old per-chart scrip dropdown is gone — every scrip is drawn now
    assert "id='vm-scrip'" not in html
    assert "id='vm-chart'" not in html
