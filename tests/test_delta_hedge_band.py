"""Band sizing, δ-trend classification and hedge-lot solving for the NG delta hedge.

These cover the pure helpers only — the placement path needs a live kite mock
and is exercised through the existing integration fixtures.
"""
from __future__ import annotations

import pytest

from datetime import datetime

from app.config import IST, Settings
from app.delta_hedge import (
    _ADX_MULT_CHOPPY,
    _ADX_MULT_MILD,
    _blank_state,
    _build_specs,
    _compute_band,
    _delta_trend,
    _delta_velocity_confirms,
    _is_due,
    _lots_for,
    _notional_band_base,
    _matches_underlying,
    _spec_state,
)


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
    """Settings with NG and CRUDEOILM hedging both switched on."""
    return cfg.model_copy(update={
        "NG_DELTA_HEDGE_ENABLED": True,
        "CRUDEOILM_HEDGE_ENABLED": True,
    })


def _at(minute: int) -> datetime:
    return datetime(2026, 7, 20, 22, minute, tzinfo=IST)


def _hist(*deltas: float) -> list[dict]:
    return [{"at": f"2026-07-20T22:{i:02d}:00", "delta": d} for i, d in enumerate(deltas)]


# ── Band scaling with book size ──────────────────────────────────────────────

def test_band_matches_legacy_at_reference_size(cfg):
    """At REF_LOTS the trending band must still be the hand-tuned 350."""
    band, parts = _compute_band(cfg.NG_HEDGE_BAND_BASE, cfg.NG_HEDGE_BAND_REF_LOTS,
                                "UNKNOWN", 0, cfg)
    assert parts["size_mult"] == pytest.approx(1.0)
    assert band == pytest.approx(350.0)


def test_adx_multipliers_reproduce_legacy_bands(cfg):
    """CHOPPY/MILD multipliers must still land on the old 700/500 at ref size."""
    for mult, expected in ((_ADX_MULT_CHOPPY, 700.0), (_ADX_MULT_MILD, 500.0)):
        band, _ = _compute_band(cfg.NG_HEDGE_BAND_BASE * mult,
                                cfg.NG_HEDGE_BAND_REF_LOTS, "UNKNOWN", 0, cfg)
        assert band == pytest.approx(expected, rel=0.01)


def test_band_grows_sublinearly_with_lots(cfg):
    """3x the book must widen the band ~2.08x (3^0.667), not 1x and not 3x."""
    small, _ = _compute_band(350.0, 10, "UNKNOWN", 0, cfg)
    large, _ = _compute_band(350.0, 30, "UNKNOWN", 0, cfg)
    ratio = large / small
    assert 2.0 < ratio < 2.2, f"expected ~2.08x, got {ratio:.2f}x"
    assert ratio < 3.0, "linear scaling tolerates too much naked delta"


def test_larger_book_needs_bigger_move_to_trigger(cfg):
    """The regression that motivated this: at 15x15 lots a flat 350 band fires
    on a 23-paise NG move. Scaled, it should take ~2x that."""
    gamma_5, gamma_15 = 500.0, 1500.0  # mmBtu of δ per Rs 1 move
    band_5, _ = _compute_band(350.0, 10, "UNKNOWN", 0, cfg)
    band_15, _ = _compute_band(350.0, 30, "UNKNOWN", 0, cfg)

    move_5 = band_5 / gamma_5
    move_15 = band_15 / gamma_15
    flat_move_15 = 350.0 / gamma_15

    assert flat_move_15 == pytest.approx(0.233, abs=0.01)   # the old behaviour
    assert move_15 > 2 * flat_move_15                        # materially calmer
    assert move_15 < move_5                                  # but still tighter than small book


def test_band_is_clamped(cfg):
    tiny, parts = _compute_band(350.0, 1, "UNKNOWN", 0, cfg)
    assert tiny == cfg.NG_HEDGE_BAND_MIN and parts["clamped"]
    huge, parts = _compute_band(700.0, 500, "UNKNOWN", 0, cfg)
    assert huge == cfg.NG_HEDGE_BAND_MAX and parts["clamped"]


# ── Direction and escalation multipliers ─────────────────────────────────────

def test_expanding_delta_tightens_band(cfg):
    """δ drifting away from zero is the adverse case — react sooner."""
    neutral, _ = _compute_band(700.0, 20, "UNKNOWN", 0, cfg)
    adverse, _ = _compute_band(700.0, 20, "EXPANDING", 0, cfg)
    favourable, _ = _compute_band(700.0, 20, "CONTRACTING", 0, cfg)
    assert adverse < neutral < favourable


def test_escalation_widens_band_after_each_hedge(cfg):
    first, _ = _compute_band(700.0, 20, "UNKNOWN", 0, cfg)
    fourth, _ = _compute_band(700.0, 20, "UNKNOWN", 3, cfg)
    assert fourth == pytest.approx(first * 1.45, rel=0.01)


# ── δ trend / velocity ───────────────────────────────────────────────────────

def test_trend_expanding_and_contracting():
    assert _delta_trend(_hist(-800.0), -1000.0, 3) == "EXPANDING"
    assert _delta_trend(_hist(-1300.0), -900.0, 3) == "CONTRACTING"


def test_trend_unknown_without_history():
    assert _delta_trend([], -1000.0, 3) == "UNKNOWN"


def test_velocity_confirms_monotonic_drift_away_from_zero():
    assert _delta_velocity_confirms(_hist(-200.0, -520.0, -840.0), -1180.0, 3)


def test_velocity_rejects_reversion_and_chop():
    # Monotonic, but heading back toward zero — not a trend to hedge into.
    assert not _delta_velocity_confirms(_hist(-1180.0, -840.0, -520.0), -200.0, 3)
    # Oscillating.
    assert not _delta_velocity_confirms(_hist(-200.0, -600.0, -300.0), -700.0, 3)


def test_velocity_needs_full_sample_window():
    assert not _delta_velocity_confirms(_hist(-200.0), -800.0, 3)


# ── Hedge lot solving ────────────────────────────────────────────────────────

def test_lots_solve_breach_in_one_order(cfg):
    """15-lot book, band 728, δ=-2000: the old fixed sizing fired 1 lot and
    re-triggered twice. One order of 3 lots should clear it."""
    band = 728.0
    excess = 2000.0 - cfg.NG_HEDGE_RESIDUAL_RATIO * band   # ~1709
    lots = _lots_for(excess, 0.50, 1250, cfg.NG_HEDGE_MAX_LOTS_PER_TICK)
    assert lots == 3
    residual = 2000.0 - lots * 0.50 * 1250
    assert abs(residual) < band, "hedge must land inside the band"


def test_lots_respects_cap():
    assert _lots_for(50_000.0, 0.50, 1250, 6) == 6


def test_lots_floor_is_one():
    assert _lots_for(10.0, 0.50, 1250, 6) == 1
    assert _lots_for(-500.0, 0.50, 1250, 6) == 1


def test_lots_survives_zero_delta():
    """A degenerate δ must not divide by zero or size an absurd order."""
    assert _lots_for(1000.0, 0.0, 1250, 6) == 1


def test_deep_otm_leg_needs_more_lots_than_atm():
    """Same breach, weaker leg → more lots, which is why each fallback path
    re-solves its own size instead of reusing the primary one."""
    atm = _lots_for(1700.0, 0.50, 1250, 6)
    otm = _lots_for(1700.0, 0.15, 1250, 6)
    assert otm > atm


def test_futures_lot_absorbs_more_than_option_lot():
    assert _lots_for(1700.0, 1.0, 1250, 6) < _lots_for(1700.0, 0.50, 1250, 6)


# ── Per-underlying specs ─────────────────────────────────────────────────────

def test_specs_carry_their_own_contract_units(both):
    ng, crude = _build_specs(both)
    assert (ng.name, ng.lot_units) == ("NATURALGAS", 1250)   # mmBtu
    assert (crude.name, crude.lot_units) == ("CRUDEOILM", 10)  # barrels
    # Bands are denominated in those units — they must not be interchangeable.
    assert ng.band_base > 20 * crude.band_base


# Prices the crude band is calibrated against. If either leg re-rates
# materially these move, and the band means something different in risk terms —
# which is exactly the drift these tests exist to surface.
F_NG, F_CRUDE = 275.0, 7963.0
NG_CRUDE_VOL_RATIO = 3.5 / 2.0     # daily % range, gas vs crude


def test_crude_band_sits_between_its_two_anchors(both):
    """16 bbl is a deliberate midpoint, NOT notional parity.

    At F_ng=275 / F_crude=7963, equal rupee exposure is ~12.1 bbl; equal daily
    P&L swing (crude's % vol is lower) is ~21.2. Notional parity alone hedges
    crude too eagerly for how far it moves; vol parity alone takes too much
    absolute risk.
    """
    ng, crude = _build_specs(both)
    ng_base, _ = _notional_band_base(ng, F_NG, both)
    crude_base, _ = _notional_band_base(crude, F_CRUDE, both)

    notional_parity = ng_base * F_NG / F_CRUDE            # ~12.1
    vol_parity = notional_parity * NG_CRUDE_VOL_RATIO     # ~21.2

    assert notional_parity < crude_base < vol_parity
    assert crude_base == pytest.approx((notional_parity + vol_parity) / 2, rel=0.10)


# ── Live price-derived band base ─────────────────────────────────────────────

def test_notional_base_reproduces_the_calibration_points(both):
    """The rupee notional must yield exactly the hand-tuned bands at the prices
    it was calibrated against — 350 mmBtu for NG, 16.0 bbl for crude."""
    ng, crude = _build_specs(both)
    assert _notional_band_base(ng, F_NG, both)[0] == pytest.approx(350.0, abs=0.1)
    assert _notional_band_base(crude, F_CRUDE, both)[0] == pytest.approx(16.0, abs=0.1)


@pytest.mark.parametrize("f_ng", [200.0, 275.0, 400.0])
@pytest.mark.parametrize("f_crude", [5500.0, 7963.0, 9500.0])
def test_rupee_exposure_is_invariant_to_price(both, f_ng, f_crude):
    """The whole point: re-rating the underlying must not change how much money
    the book is allowed to be wrong by. A units-denominated constant fails this
    — crude moving 5500 -> 7963 shifted its real tolerance ~45%."""
    ng, crude = _build_specs(both)
    assert _notional_band_base(ng, f_ng, both)[0] * f_ng == pytest.approx(
        both.HEDGE_BAND_NOTIONAL_INR * ng.vol_factor
    )
    assert _notional_band_base(crude, f_crude, both)[0] * f_crude == pytest.approx(
        both.HEDGE_BAND_NOTIONAL_INR * crude.vol_factor
    )


def test_static_constant_would_have_drifted(both):
    """Guards the regression that motivated this: at the old crude price the
    correct band was ~23 bbl, so the static 16.0 was ~30% too tight in rupee
    terms — and nothing in the system flagged it."""
    _, crude = _build_specs(both)
    at_old_price, _ = _notional_band_base(crude, 5500.0, both)
    assert at_old_price == pytest.approx(23.1, abs=0.3)
    assert abs(at_old_price - crude.band_base) / at_old_price > 0.25


def test_bad_futures_price_falls_back_to_static(both):
    """A zero/garbage LTP must not produce an infinite or zero band."""
    _, crude = _build_specs(both)
    for bad_F in (0.0, -1.0):
        base, source = _notional_band_base(crude, bad_F, both)
        assert (base, source) == (crude.band_base, "STATIC")


def test_missing_notional_falls_back_to_static(both):
    _, crude = _build_specs(both)
    cfg = both.model_copy(update={"HEDGE_BAND_NOTIONAL_INR": 0.0})
    assert _notional_band_base(crude, F_CRUDE, cfg) == (crude.band_base, "STATIC")


def test_adx_multiplier_is_independent_of_price(both):
    """The ADX regime is cached for 10 min but the base is recomputed live, so
    a moving F must flow through immediately rather than being pinned by cache."""
    _, crude = _build_specs(both)
    lo, _ = _notional_band_base(crude, 7000.0, both)
    hi, _ = _notional_band_base(crude, 9000.0, both)
    assert lo > hi                      # higher price -> fewer barrels, same rupees
    for mult in (_ADX_MULT_CHOPPY, _ADX_MULT_MILD):
        assert (lo * mult) / (hi * mult) == pytest.approx(lo / hi)


def test_crude_is_more_granular_than_ng(both):
    """Granularity rule: the band must exceed the delta of one ATM lot, or the
    smallest possible hedge overshoots and the book ping-pongs."""
    ng, crude = _build_specs(both)
    ng_base, _ = _notional_band_base(ng, F_NG, both)
    crude_base, _ = _notional_band_base(crude, F_CRUDE, both)
    ng_ratio = ng_base / (0.50 * ng.lot_units)
    crude_ratio = crude_base / (0.50 * crude.lot_units)
    assert crude_ratio > 3.0            # crude lands comfortably inside its band
    assert crude_ratio > ng_ratio       # and is the finer instrument of the two


def test_crude_band_floor_clears_one_lot(both):
    """BAND_MIN must stay above one ATM lot's delta, else the clamp itself
    creates the ping-pong it is supposed to prevent."""
    _, crude = _build_specs(both)
    assert crude.band_min > 0.50 * crude.lot_units


def test_crude_floor_does_not_cancel_the_adverse_band(both):
    """The floor has a ceiling too: if BAND_MIN lands above base x ADVERSE_MULT,
    the clamp silently undoes the trend-tightening at reference size — the band
    reads 'adverse' in the logs but is numerically identical to neutral."""
    _, crude = _build_specs(both)
    crude_base, _ = _notional_band_base(crude, F_CRUDE, both)
    adverse = crude_base * both.NG_HEDGE_ADVERSE_MULT

    assert crude.band_min <= adverse, (
        f"BAND_MIN={crude.band_min} clamps the adverse band {adverse:.1f}"
    )
    # ...and the adverse band must still be tradeable.
    assert adverse >= 2 * (0.50 * crude.lot_units)


def test_adverse_band_reaches_its_intended_value(both):
    """End-to-end through _compute_band's real clamp.

    Asserting `adverse < neutral` is too weak — with BAND_MIN=12 the adverse
    band still tightened (12.0 vs 16.0), it just never reached the intended
    11.2. Pin the exact value so the floor clipping it shows up as a failure.
    """
    _, crude = _build_specs(both)
    crude_base, _ = _notional_band_base(crude, F_CRUDE, both)
    neutral, _ = _compute_band(crude_base, crude.ref_lots, "UNKNOWN", 0, both, crude)
    adverse, _ = _compute_band(crude_base, crude.ref_lots, "EXPANDING", 0, both, crude)

    assert adverse < neutral
    assert adverse == pytest.approx(crude_base * both.NG_HEDGE_ADVERSE_MULT), (
        "BAND_MIN clipped the adverse band short of its intended value"
    )


def test_specs_respect_enable_flags(cfg):
    assert _build_specs(cfg) == []
    ng_only = cfg.model_copy(update={"NG_DELTA_HEDGE_ENABLED": True})
    assert [s.name for s in _build_specs(ng_only)] == ["NATURALGAS"]


# ── Cadence / staggering ─────────────────────────────────────────────────────

def test_ng_fires_every_other_minute_on_odd(both):
    ng, _ = _build_specs(both)
    fires = [m for m in range(60) if _is_due(ng, _at(m))]
    assert fires == list(range(1, 60, 2))
    assert 0 not in fires, "NG must avoid :00, the most contended scheduler slot"


def test_crude_fires_five_minutely_from_two(both):
    _, crude = _build_specs(both)
    fires = [m for m in range(60) if _is_due(crude, _at(m))]
    assert fires == [2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57]


def test_collisions_are_bounded(both):
    """A 5-min cadence alternates parity, so some overlap with a 2-min cadence is
    unavoidable — it just has to stay rare enough for the arbitration to absorb."""
    ng, crude = _build_specs(both)
    both_due = [m for m in range(60) if _is_due(ng, _at(m)) and _is_due(crude, _at(m))]
    assert both_due == [7, 17, 27, 37, 47, 57]


def test_carryover_forces_an_off_cadence_run(both):
    _, crude = _build_specs(both)
    assert not _is_due(crude, _at(3))
    assert _is_due(crude, _at(3), carryover=True)


# ── State isolation ──────────────────────────────────────────────────────────

def test_state_is_keyed_per_underlying(both):
    ng, crude = _build_specs(both)
    all_state = {
        "date": "2026-07-20",
        "naturalgas": {"hedges_today": 4, "last_hedge_at": "2026-07-20T22:10:00+05:30"},
    }
    assert _spec_state(all_state, ng)["hedges_today"] == 4
    # Crude must not inherit NG's cooldown or escalation ladder.
    assert _spec_state(all_state, crude)["hedges_today"] == 0
    assert _spec_state(all_state, crude)["last_hedge_at"] is None


def test_spec_state_fills_missing_keys(both):
    ng, _ = _build_specs(both)
    filled = _spec_state({"date": "2026-07-20"}, ng)
    assert set(filled) == set(_blank_state("2026-07-20"))


# ── Underlying matching ──────────────────────────────────────────────────────

def test_crudeoil_prefix_does_not_swallow_crudeoilm():
    """The landmine: a substring test makes CRUDEOIL match every CRUDEOILM leg."""
    sym = "CRUDEOILM25JUL5900CE"
    assert "CRUDEOIL" in sym                      # what a naive check would see
    assert _matches_underlying(sym, "CRUDEOILM")
    assert not _matches_underlying(sym, "CRUDEOIL")


def test_matches_real_symbol_shapes():
    assert _matches_underlying("NATURALGAS25JUL300CE", "NATURALGAS")
    assert _matches_underlying("NATURALGAS25JULFUT", "NATURALGAS")
    assert _matches_underlying("CRUDEOIL25JUL5900PE", "CRUDEOIL")
    assert not _matches_underlying("NATGASMINI25JUL300CE", "NATURALGAS")
    assert not _matches_underlying("BANKNIFTY25JUL57000CE", "NATURALGAS")
    assert not _matches_underlying("", "NATURALGAS")


# ── Band scaling is unit-agnostic ────────────────────────────────────────────

def test_size_scaling_applies_to_crude_in_barrels(both):
    _, crude = _build_specs(both)
    crude_base, _ = _notional_band_base(crude, F_CRUDE, both)
    small, _ = _compute_band(crude_base, crude.ref_lots, "UNKNOWN", 0, both, crude)
    large, _ = _compute_band(crude_base, crude.ref_lots * 3, "UNKNOWN", 0, both, crude)
    assert small == pytest.approx(crude_base)
    assert 2.0 < large / small < 2.2          # same 2/3 law, barrels instead of mmBtu


def test_crude_uses_its_own_clamps_not_ngs(both):
    ng, crude = _build_specs(both)
    crude_base, _ = _notional_band_base(crude, F_CRUDE, both)
    huge, parts = _compute_band(crude_base, 5000, "UNKNOWN", 0, both, crude)
    assert huge == crude.band_max and parts["clamped"]
    assert huge < ng.band_min, "crude must never be clamped into NG's mmBtu range"


# ── /control toggle plumbing ─────────────────────────────────────────────────

def test_crude_toggle_flips_and_is_independent_of_ng():
    """The /control button drives these. They must be separate switches — the
    dispatcher builds specs from them, so a shared flag would start both books.
    """
    from app import state

    ng_before = state.is_ng_hedge_enabled(False)
    try:
        assert state.is_crude_hedge_enabled(False) is False
        assert state.toggle_crude_hedge_enabled(False) is True
        assert state.is_crude_hedge_enabled(False) is True
        # NG must be untouched by the crude toggle.
        assert state.is_ng_hedge_enabled(False) == ng_before
        assert state.toggle_crude_hedge_enabled(False) is False
    finally:
        state.set_crude_hedge_enabled(False)


def test_crude_toggle_overrides_the_env_default():
    """An explicit override must win over .env in both directions, so the
    dashboard can stop a hedger that .env has switched on."""
    from app import state

    try:
        state.set_crude_hedge_enabled(False)
        assert state.is_crude_hedge_enabled(True) is False   # env says on, override wins
        state.set_crude_hedge_enabled(True)
        assert state.is_crude_hedge_enabled(False) is True   # env says off, override wins
    finally:
        state.set_crude_hedge_enabled(False)


def test_toggled_spec_reaches_the_dispatcher(cfg):
    """End-to-end: flipping the toggle must change what _build_specs returns,
    since that is what actually gates order placement."""
    from app import state

    try:
        assert [s.name for s in _build_specs(cfg)] == []
        state.set_crude_hedge_enabled(True)
        assert [s.name for s in _build_specs(cfg)] == ["CRUDEOILM"]
    finally:
        state.set_crude_hedge_enabled(False)
