"""Live-editable rebalance thresholds: precedence, clamping and per-tick freshness.

The point of this layer is that retuning a hedge trigger no longer needs a
rebuild — so the tests that matter are the ones proving (a) an override actually
reaches the engine on the *next* tick, (b) it cannot reach it with a value that
would make the engine misbehave, and (c) two engines sharing one .env default
still move independently.
"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from app import hedge_tunables as ht
from app import state
from app.config import Settings
from app.delta_hedge import _build_specs
from app.main import _auth_guard, app, get_current_settings


@pytest.fixture
def cfg() -> Settings:
    return Settings(
        SECRET_KEY="x" * 32,
        KITE_API_KEY="k",
        KITE_API_SECRET="s",
        VOICE_AUTH_TOKEN="v",
    )


@pytest.fixture
def both(cfg) -> Settings:
    return cfg.model_copy(update={
        "NG_DELTA_HEDGE_ENABLED": True,
        "CRUDEOILM_HEDGE_ENABLED": True,
    })


# ── Registry shape ───────────────────────────────────────────────────────────

def test_every_engine_points_at_real_settings_fields(cfg):
    """A typo in `env_attr` would only surface at 22:00 on a live tick."""
    for eng in ht.ENGINES:
        for t in eng.tunables:
            assert hasattr(cfg, t.env_attr), f"{eng.slug}.{t.key} → missing {t.env_attr}"


def test_env_defaults_sit_inside_their_own_clamps(cfg):
    """If a default were outside [lo, hi] the clamp would silently retune the
    bot the moment this layer shipped — the opposite of a no-op default."""
    for eng in ht.ENGINES:
        for t in eng.tunables:
            raw = float(getattr(cfg, t.env_attr))
            assert t.lo <= raw <= t.hi, f"{eng.slug}.{t.key}={raw} outside [{t.lo}, {t.hi}]"
            assert ht.effective(cfg, eng.slug, t.key) == pytest.approx(raw)


def test_slugs_are_unique():
    assert len(ht.engine_slugs()) == len(set(ht.engine_slugs()))


# ── Precedence ───────────────────────────────────────────────────────────────

def test_override_wins_over_env(cfg):
    assert ht.effective(cfg, "goldm", "trigger_inr") == cfg.GOLDM_HEDGE_TRIGGER_INR
    state.set_hedge_tunable("goldm", "trigger_inr", 750)
    assert ht.effective(cfg, "goldm", "trigger_inr") == 750.0
    assert ht.is_overridden("goldm", "trigger_inr")


def test_clearing_falls_back_to_env(cfg):
    state.set_hedge_tunable("goldm", "trigger_inr", 750)
    state.set_hedge_tunable("goldm", "trigger_inr", None)
    assert not ht.is_overridden("goldm", "trigger_inr")
    assert ht.effective(cfg, "goldm", "trigger_inr") == cfg.GOLDM_HEDGE_TRIGGER_INR


def test_clear_is_per_engine(cfg):
    state.set_hedge_tunable("goldm", "trigger_inr", 750)
    state.set_hedge_tunable("silverm", "trigger_inr", 4000)
    state.clear_hedge_tunables("goldm")
    assert not ht.is_overridden("goldm", "trigger_inr")
    assert ht.is_overridden("silverm", "trigger_inr")


def test_shared_env_default_still_gives_independent_slots(both):
    """NG and CRUDEOILM both default their band notional to the same .env field.

    Retuning one must not drag the other along — that coupling is exactly what
    made crude's band move whenever NG was retuned.
    """
    ng_before = ht.effective(both, "ng", "band_notional_inr")
    crude_before = ht.effective(both, "crudeoilm_band", "band_notional_inr")
    assert ng_before == crude_before          # same default…

    state.set_hedge_tunable("ng", "band_notional_inr", 250_000)
    assert ht.effective(both, "ng", "band_notional_inr") == 250_000.0
    assert ht.effective(both, "crudeoilm_band", "band_notional_inr") == crude_before


# ── Clamping — the safety rail ───────────────────────────────────────────────

def test_values_are_clamped_at_both_ends(cfg):
    """A zero-width band breaches every tick and drains the quote bucket and the
    margin pool; an absurd one silently disables hedging. Neither may be stored."""
    band = ht.get_engine("banknifty").tunable("band_units")
    assert ht.coerce("banknifty", "band_units", -50) == band.lo
    assert ht.coerce("banknifty", "band_units", 10 ** 9) == band.hi


def test_clamp_also_applies_on_read_not_just_on_write(cfg):
    """data/state_overrides.json is hand-editable, so a bad value can appear
    without ever passing through `coerce` on the way in."""
    state.set_hedge_tunable("banknifty", "band_units", 25)
    state._HEDGE_TUNABLE_OVERRIDES["banknifty.band_units"] = -999   # hand-edited file
    assert ht.effective(cfg, "banknifty", "band_units") == ht.get_engine(
        "banknifty").tunable("band_units").lo


def test_int_tunables_stay_ints(cfg):
    assert ht.coerce("ng", "max_lots_per_tick", "3.7") == 4
    assert isinstance(ht.coerce("ng", "max_lots_per_tick", "3.7"), int)
    assert isinstance(ht.effective(cfg, "ng", "cooldown_min"), int)


def test_band_notional_zero_is_preserved_as_the_static_sentinel(cfg):
    """0 means "ignore the formula, use the static band base" — clamping it up
    to a floor would quietly turn the fallback back on."""
    assert ht.coerce("ng", "band_notional_inr", 0) == 0.0


def test_unknown_engine_or_key_is_rejected(cfg):
    assert ht.get_engine("nifty_typo") is None
    assert ht.coerce("nifty_typo", "band_units", 5) is None
    assert ht.coerce("ng", "not_a_knob", 5) is None
    assert ht.coerce("ng", "cooldown_min", "abc") is None
    with pytest.raises(KeyError):
        ht.effective(cfg, "ng", "not_a_knob")


# ── The whole point: it reaches the engine on the next tick ──────────────────

def test_override_reaches_the_ng_spec_without_a_restart(both):
    """`_build_specs` runs per wake-up, so the next tick sees the new value."""
    before = _build_specs(both)[0]
    state.set_hedge_tunable("ng", "band_notional_inr", 250_000)
    state.set_hedge_tunable("ng", "max_lots_per_tick", 2)
    after = _build_specs(both)[0]

    assert before.band_notional_inr != 250_000.0
    assert after.band_notional_inr == 250_000.0
    assert after.max_lots_per_tick == 2
    assert after.name == "NATURALGAS"


def test_override_reaches_the_crude_spec_and_not_ngs(both):
    state.set_hedge_tunable("crudeoilm_band", "band_max", 40)
    ng, crude = _build_specs(both)
    assert crude.band_max == 40.0
    assert ng.band_max == both.NG_HEDGE_BAND_MAX


def test_spec_slugs_match_the_registry(both):
    ng, crude = _build_specs(both)
    assert (ng.slug, crude.slug) == ("ng", "crudeoilm_band")
    assert ht.get_engine(ng.slug) is not None
    assert ht.get_engine(crude.slug) is not None


# ── Persistence — overrides must survive the rebuild they exist to avoid ─────

def test_overrides_round_trip_through_the_overrides_file(cfg, tmp_path, monkeypatch):
    path = tmp_path / "state_overrides.json"
    monkeypatch.setattr(state, "_OVERRIDES_PATH", str(path))

    state.set_hedge_tunable("silverm", "trigger_inr", 4_000)
    assert path.exists(), "override was never written — a rebuild would lose it"

    # Simulate the restart by dropping the in-memory copy only. Going through
    # clear_hedge_tunables() here would persist the clear and wipe the very file
    # under test.
    state._HEDGE_TUNABLE_OVERRIDES.clear()
    assert not ht.is_overridden("silverm", "trigger_inr")

    state.load_overrides_from_disk()
    assert ht.effective(cfg, "silverm", "trigger_inr") == 4_000.0


def test_garbage_in_the_overrides_file_is_ignored(cfg, tmp_path, monkeypatch):
    """Hand-edited file with a string where a number belongs must not take the
    hedge down at 22:00 — drop the bad entry, keep the rest."""
    path = tmp_path / "state_overrides.json"
    path.write_text(
        '{"hedge_tunables": {"silverm.trigger_inr": "loads", '
        '"goldm.trigger_inr": 900, "banknifty.band_units": true}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "_OVERRIDES_PATH", str(path))
    state._HEDGE_TUNABLE_OVERRIDES.clear()   # in-memory only; keep the file intact
    state.load_overrides_from_disk()

    assert not ht.is_overridden("silverm", "trigger_inr")
    assert not ht.is_overridden("banknifty", "band_units")   # bool is not a number
    assert ht.effective(cfg, "goldm", "trigger_inr") == 900.0


# ── The /control form ────────────────────────────────────────────────────────

@pytest.fixture
def client(cfg):
    app.dependency_overrides[get_current_settings] = lambda: cfg
    app.dependency_overrides[_auth_guard] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_post_applies_an_override(client, cfg):
    r = client.post("/control/hedge/goldm", data={"trigger_inr": "800"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert ht.effective(cfg, "goldm", "trigger_inr") == 800.0


def test_post_clamps_instead_of_rejecting(client, cfg):
    """A hedge left unretuned because the form 400'd is worse than one retuned
    to the nearest sane value, so out-of-range input clamps and still applies."""
    hi = ht.get_engine("goldm").tunable("trigger_inr").hi
    client.post("/control/hedge/goldm", data={"trigger_inr": "99999999"},
                follow_redirects=False)
    assert ht.effective(cfg, "goldm", "trigger_inr") == hi


def test_blank_and_junk_fields_are_left_alone(client, cfg):
    client.post("/control/hedge/goldm", data={"trigger_inr": "800"}, follow_redirects=False)
    client.post("/control/hedge/goldm",
                data={"trigger_inr": "", "min_delta_units": "not a number"},
                follow_redirects=False)
    assert ht.effective(cfg, "goldm", "trigger_inr") == 800.0          # untouched
    assert not ht.is_overridden("goldm", "min_delta_units")


def test_posting_the_env_default_clears_the_override(client, cfg):
    """Typing the default back must return the row to "from .env", not pin a
    duplicate — otherwise /control shows an orange dot forever."""
    client.post("/control/hedge/goldm", data={"trigger_inr": "800"}, follow_redirects=False)
    assert ht.is_overridden("goldm", "trigger_inr")
    client.post("/control/hedge/goldm",
                data={"trigger_inr": str(cfg.GOLDM_HEDGE_TRIGGER_INR)},
                follow_redirects=False)
    assert not ht.is_overridden("goldm", "trigger_inr")


def test_reset_clears_only_that_engine(client, cfg):
    client.post("/control/hedge/goldm", data={"trigger_inr": "800"}, follow_redirects=False)
    client.post("/control/hedge/silverm", data={"trigger_inr": "4000"}, follow_redirects=False)
    client.post("/control/hedge/goldm", data={"reset": "1"}, follow_redirects=False)
    assert not ht.is_overridden("goldm", "trigger_inr")
    assert ht.is_overridden("silverm", "trigger_inr")


def test_unknown_engine_is_404(client):
    r = client.post("/control/hedge/nifty", data={"band_units": "5"},
                    follow_redirects=False)
    assert r.status_code == 404
