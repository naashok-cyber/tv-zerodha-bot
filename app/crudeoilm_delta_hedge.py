"""CRUDEOILM delta hedge (band-based) — spec only.

The engine is `app/hedge_core.py`; the dispatcher is `app/delta_hedge.py`. This
module exists so crude's cadence, band bounds and contract units can be read and
retuned without scrolling past NG's, and so a change to one book cannot touch
the other.

Note there are **two** CRUDEOILM hedgers and they are independent: this
band-based one (|net delta| vs a notional-derived band, 5-min cadence) and the
F-move one in `app/crudeoilm_fmove_hedge.py` (fires on futures price movement).
They have separate enable toggles, separate state files and separate thresholds
in the `/control` registry (`crudeoilm_band` vs `crudeoilm_fmove`).

Cadence: interval 5, phase 2 -> :02 :07 :12 ... :57.
"""
from __future__ import annotations

from typing import Any

from app.hedge_core import HedgeSpec

_UNDERLYING = "CRUDEOILM"

SLUG = "crudeoilm_band"   # app/hedge_tunables.py registry key


def build_spec(settings: Any) -> HedgeSpec:
    """CRUDEOILM's spec for this tick, with live thresholds folded in.

    Band bounds are in **barrels** — they are dimensional and cannot be shared
    with NG's mmBtu. The floor must stay <= band base x NG_HEDGE_ADVERSE_MULT or
    the clamp clips the adverse band short of its intended value.
    """
    from app import hedge_tunables

    t = hedge_tunables.all_effective(settings, SLUG)
    return HedgeSpec(
        name=_UNDERLYING,
        slug=SLUG,
        exchange="MCX",
        lot_units=settings.MCX_LOT_UNITS["CRUDEOILM"],
        band_base=float(settings.CRUDEOILM_HEDGE_BAND_BASE),
        band_notional_inr=float(t["band_notional_inr"]),
        vol_factor=float(t["vol_factor"]),
        band_min=float(t["band_min"]),
        band_max=float(t["band_max"]),
        cooldown_min=int(t["cooldown_min"]),
        max_lots_per_tick=int(t["max_lots_per_tick"]),
        ref_lots=int(settings.CRUDEOILM_HEDGE_BAND_REF_LOTS),
        interval_min=int(settings.CRUDEOILM_HEDGE_INTERVAL_MIN),
        phase_min=int(settings.CRUDEOILM_HEDGE_PHASE_MIN),
        velocity_samples=int(settings.CRUDEOILM_HEDGE_VELOCITY_SAMPLES),
        adx_fallback=float(settings.CRUDEOILM_HEDGE_BAND_BASE),
    )
