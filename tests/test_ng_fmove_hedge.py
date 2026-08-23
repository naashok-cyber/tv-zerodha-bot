"""NG F-move hedge — registry wiring, toggle, and the granularity guard.

The engine body is a line-for-line mirror of app/crudeoilm_fmove_hedge.py, so
these tests target what the mirror could plausibly get wrong: the registry
entry, the enable toggle, the underlying matcher, and the NG-specific
min-|delta| floor that stops one 1250 mmBtu lot from overshooting.
"""
from __future__ import annotations

import pytest

from app import hedge_tunables, ng_fmove_hedge, state
from app.config import Settings


@pytest.fixture
def cfg() -> Settings:
    return Settings(SECRET_KEY="x" * 32)


# ── registry ────────────────────────────────────────────────────────────────

def test_ng_fmove_is_registered_with_five_knobs(cfg):
    eng = hedge_tunables.get_engine("ng_fmove")
    assert eng is not None
    assert {t.key for t in eng.tunables} == {
        "trigger_inr", "min_delta_units", "max_lots_per_tick",
        "cooldown_min", "max_hedges_per_day",
    }


def test_slug_constant_matches_the_registry():
    assert ng_fmove_hedge.SLUG == "ng_fmove"
    assert "ng_fmove" in hedge_tunables.engine_slugs()


def test_defaults_resolve_from_env(cfg):
    tun = hedge_tunables.all_effective(cfg, "ng_fmove")
    assert tun["trigger_inr"] == pytest.approx(3.0)
    assert tun["min_delta_units"] == pytest.approx(625.0)
    assert tun["max_lots_per_tick"] == 3
    assert tun["cooldown_min"] == 5
    assert tun["max_hedges_per_day"] == 20


def test_every_env_attr_exists_on_settings(cfg):
    for tun in hedge_tunables.get_engine("ng_fmove").tunables:
        assert hasattr(cfg, tun.env_attr), tun.env_attr


def test_min_delta_default_is_one_atm_lot(cfg):
    """625 mmBtu = 0.5 delta x 1250 units. If the NG lot size ever changes this
    default has to move with it, or the engine starts overshooting again."""
    lot_units = cfg.MCX_LOT_UNITS["NATURALGAS"]
    assert cfg.NG_FMOVE_HEDGE_MIN_DELTA_UNITS == pytest.approx(0.5 * lot_units)


# ── overrides ───────────────────────────────────────────────────────────────

def test_override_beats_env_and_clears_on_default(cfg):
    state.set_hedge_tunable("ng_fmove", "trigger_inr", 8.5)
    assert hedge_tunables.effective(cfg, "ng_fmove", "trigger_inr") == pytest.approx(8.5)
    state.set_hedge_tunable("ng_fmove", "trigger_inr", None)
    assert hedge_tunables.effective(cfg, "ng_fmove", "trigger_inr") == pytest.approx(3.0)


def test_override_is_clamped_on_read(cfg):
    state.set_hedge_tunable("ng_fmove", "trigger_inr", 99_999)
    assert hedge_tunables.effective(cfg, "ng_fmove", "trigger_inr") == pytest.approx(200.0)
    state.set_hedge_tunable("ng_fmove", "trigger_inr", 0.0001)
    assert hedge_tunables.effective(cfg, "ng_fmove", "trigger_inr") == pytest.approx(0.5)


def test_ng_fmove_and_ng_band_hold_independent_slots(cfg):
    """Both engines hedge NATURALGAS but must never share a tunable slot."""
    state.set_hedge_tunable("ng_fmove", "cooldown_min", 42)
    assert hedge_tunables.effective(cfg, "ng_fmove", "cooldown_min") == 42
    assert hedge_tunables.effective(cfg, "ng", "cooldown_min") == 10


# ── toggle ──────────────────────────────────────────────────────────────────

def test_toggle_is_independent_of_the_band_engine(cfg):
    assert state.is_ng_fmove_hedge_enabled(False) is False
    state.set_ng_fmove_hedge_enabled(True)
    assert state.is_ng_fmove_hedge_enabled(False) is True
    # Flipping the F-move engine must not disturb the band engine's toggle,
    # which also gates half-exit / BNF SL / straddle ladder.
    assert state.is_ng_hedge_enabled(True) is True
    state.set_ng_fmove_hedge_enabled(None)
    assert state.is_ng_fmove_hedge_enabled(False) is False


def test_toggle_flips_from_env_default(cfg):
    assert state.toggle_ng_fmove_hedge_enabled(False) is True
    assert state.toggle_ng_fmove_hedge_enabled(False) is False


def test_disabled_engine_is_a_no_op(cfg, monkeypatch):
    """The guard must return before touching kite — the cron fires every minute
    whether or not the engine is on."""
    def _boom():
        raise AssertionError("get_session_manager called while disabled")

    monkeypatch.setattr("app.kite_session.get_session_manager", _boom)
    state.set_ng_fmove_hedge_enabled(False)
    ng_fmove_hedge.run_ng_fmove_hedge_job(cfg, None)  # must not raise


# ── underlying matcher ──────────────────────────────────────────────────────

@pytest.mark.parametrize("sym,expected", [
    ("NATURALGAS25AUG300CE", True),
    ("NATURALGAS25AUGFUT", True),
    ("NATURALGAS", False),             # no expiry digit
    ("NATURALGASX25AUG300CE", False),  # prefix matches, next char is not a digit
    ("NATGASMINI25AUG300CE", False),   # different contract entirely
    ("CRUDEOILM25AUG5900CE", False),
    ("", False),
])
def test_matches_requires_an_expiry_digit(sym, expected):
    assert ng_fmove_hedge._matches(sym) is expected


# ── state file ──────────────────────────────────────────────────────────────

def test_state_path_is_separate_from_the_band_engine(cfg):
    """Shared state would let one engine's cooldown gate the other's."""
    assert ng_fmove_hedge._state_path(cfg) != cfg.NG_HEDGE_STATE_PATH
    assert ng_fmove_hedge._state_path(cfg).endswith("ng_fmove_hedge_state.json")


def test_state_resets_on_date_rollover(cfg, tmp_path):
    import json

    path = tmp_path / "ng_fmove_hedge_state.json"
    path.write_text(json.dumps({
        "date": "2020-01-01", "F_anchor": 275.0, "delta_anchor": 900.0,
        "hedges_today": 7, "last_hedge_at": "2020-01-01T10:00:00+05:30",
    }))
    cfg2 = cfg.model_copy(update={"NG_FMOVE_HEDGE_STATE_PATH": str(path)})
    s = ng_fmove_hedge._load_state(cfg2)
    assert s["F_anchor"] is None
    assert s["hedges_today"] == 0


def test_state_round_trips(cfg, tmp_path):
    path = tmp_path / "ng_fmove_hedge_state.json"
    cfg2 = cfg.model_copy(update={"NG_FMOVE_HEDGE_STATE_PATH": str(path)})
    s = ng_fmove_hedge._load_state(cfg2)
    s["F_anchor"] = 281.25
    s["hedges_today"] = 2
    ng_fmove_hedge._save_state(cfg2, s)
    assert ng_fmove_hedge._load_state(cfg2)["F_anchor"] == pytest.approx(281.25)
    assert ng_fmove_hedge._load_state(cfg2)["hedges_today"] == 2


def test_unreadable_state_starts_fresh_rather_than_raising(cfg, tmp_path):
    path = tmp_path / "ng_fmove_hedge_state.json"
    path.write_text("{ not json")
    cfg2 = cfg.model_copy(update={"NG_FMOVE_HEDGE_STATE_PATH": str(path)})
    assert ng_fmove_hedge._load_state(cfg2)["F_anchor"] is None


# ── /control ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(cfg):
    from fastapi.testclient import TestClient

    from app.main import _auth_guard, app, get_current_settings

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
    from unittest.mock import patch

    with patch("app.main.get_session_manager") as mock_factory:
        mock_factory.return_value.get_token_info.return_value = {
            "is_valid": False, "age_hours": None, "reason": "no token",
        }
        resp = client.get("/control")
    assert resp.status_code == 200
    return resp.text


def test_control_shows_the_row_and_the_tunables_card(client):
    html = _control_html(client)
    assert "NG F-move Hedge" in html
    assert "/control/ng-fmove-hedge/toggle" in html
    assert "/control/hedge/ng_fmove" in html
    assert "mmBtu F move" in html


def test_double_hedging_warning_appears_only_when_both_ng_engines_run(client, cfg):
    state.set_ng_hedge_enabled(True)
    state.set_ng_fmove_hedge_enabled(False)
    assert "DOUBLE-HEDGING" not in _control_html(client)

    state.set_ng_fmove_hedge_enabled(True)
    html = _control_html(client)
    assert "DOUBLE-HEDGING" in html
    assert "same book hedged twice" in html

    # Band engine off, F-move on — one engine, so no warning.
    state.set_ng_hedge_enabled(False)
    assert "DOUBLE-HEDGING" not in _control_html(client)


def test_toggle_route_flips_only_the_fmove_engine(client, cfg):
    state.set_ng_hedge_enabled(True)
    r = client.post("/control/ng-fmove-hedge/toggle", follow_redirects=False)
    assert r.status_code == 302
    assert state.is_ng_fmove_hedge_enabled(cfg.NG_FMOVE_HEDGE_ENABLED) is True
    assert state.is_ng_hedge_enabled(cfg.NG_DELTA_HEDGE_ENABLED) is True

    client.post("/control/ng-fmove-hedge/toggle", follow_redirects=False)
    assert state.is_ng_fmove_hedge_enabled(cfg.NG_FMOVE_HEDGE_ENABLED) is False


def test_post_retunes_the_trigger(client, cfg):
    r = client.post("/control/hedge/ng_fmove", data={"trigger_inr": "5.5"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert hedge_tunables.effective(cfg, "ng_fmove", "trigger_inr") == pytest.approx(5.5)
