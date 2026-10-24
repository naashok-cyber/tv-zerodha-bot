"""Live-editable rebalance thresholds for every delta-hedge engine.

The problem this solves: retuning a hedge trigger used to mean editing `.env`,
rebuilding the image and restarting the container. Every engine already re-reads
its knobs *inside* the tick (`_build_specs` runs per wake-up; the F-move jobs
read `settings.X` inline), so the only missing piece was somewhere to put a
runtime value. That is `data/state_overrides.json`, which lives on the `./data`
volume and therefore survives a rebuild.

Layout: a registry of `Engine`s, each owning a tuple of `Tunable`s. Everything
downstream is generated from it — the `/control` cards, the POST route, the
reset action. Adding an underlying is one `Engine` entry, not new UI code.

Resolution order for any knob:

    state override (data/state_overrides.json)  →  Settings attr (.env)  →  literal

and the result is **clamped to the registry's [lo, hi]** on the way out. The
clamp is the safety rail: a band of 0 would breach on every tick and drain both
the quote bucket and the margin pool, and a hand-edited overrides file is not a
trusted input.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Tunable", "Engine", "ENGINES", "get_engine", "engine_slugs",
    "effective", "all_effective", "coerce",
]


@dataclass(frozen=True)
class Tunable:
    """One editable number on one engine.

    `env_attr` is the `Settings` field holding the default. Two engines may
    point at the same one (NG and CRUDEOILM both default their band notional to
    `HEDGE_BAND_NOTIONAL_INR`) — they still get independent override slots, so
    retuning NG no longer drags crude along with it.
    """
    key: str
    label: str
    unit: str            # rendered as the row suffix — units differ per engine
    env_attr: str
    kind: str = "float"  # "float" | "int"
    lo: float = 0.0
    hi: float = 1e9
    step: float = 1.0
    note: str = ""       # optional one-liner under the label

    def coerce(self, raw: Any) -> float | int:
        """Parse + clamp a value into this tunable's domain."""
        val = max(float(self.lo), min(float(self.hi), float(raw)))
        return int(round(val)) if self.kind == "int" else float(val)


@dataclass(frozen=True)
class Engine:
    """One hedge engine's editable surface.

    `slug` namespaces the override keys and forms the POST path, so it must be
    stable — renaming one orphans any saved override. CRUDEOILM deliberately
    appears twice: the band engine and the F-move engine are separate hedgers
    on the same book and are tuned independently.
    """
    slug: str
    title: str
    subtitle: str
    tunables: tuple[Tunable, ...]

    def tunable(self, key: str) -> Tunable | None:
        return next((t for t in self.tunables if t.key == key), None)


# ── The registry ─────────────────────────────────────────────────────────────
# Units are NOT interchangeable across engines: NG is mmBtu, CRUDEOILM barrels,
# SILVERM kg, BANKNIFTY index units, and the F-move triggers are rupees. Every
# row carries its unit so a crude ₹ trigger cannot be typed into an NG band.
#
# The band notional floors at 0 rather than at some sane minimum on purpose: 0 is
# the documented sentinel for "ignore the formula, use the static band base", and
# anything small-but-nonzero is caught downstream by the band floor anyway, so a
# floor here would only break the sentinel.

_BAND_NOTIONAL = Tunable(
    key="band_notional_inr", label="Band notional", unit="&#8377;",
    env_attr="HEDGE_BAND_NOTIONAL_INR", kind="float",
    lo=0, hi=2_000_000, step=1_000,
    note="&#8377; of delta-notional the book may be wrong by; band = this / F &times; vol. 0 = fall back to the static band base",
)

ENGINES: tuple[Engine, ...] = (
    Engine(
        slug="ng",
        title="NATURALGAS &middot; &delta; band",
        subtitle="1-min dispatcher &middot; band = &#8377;notional / F &times; vol &times; ADX &times; size &times; direction",
        tunables=(
            _BAND_NOTIONAL,
            Tunable("vol_factor", "Vol factor", "&times;", "NG_HEDGE_VOL_FACTOR",
                    "float", 0.1, 10.0, 0.01,
                    note="1.0 = the calibration reference"),
            Tunable("band_min", "Band floor", "mmBtu", "NG_HEDGE_BAND_MIN",
                    "float", 10, 100_000, 10,
                    note="keep &le; notional band &times; adverse mult, or the clamp clips it"),
            Tunable("band_max", "Band ceiling", "mmBtu", "NG_HEDGE_BAND_MAX",
                    "float", 10, 1_000_000, 50),
            Tunable("cooldown_min", "Cooldown", "min", "NG_HEDGE_COOLDOWN_MIN",
                    "int", 0, 240, 1),
            Tunable("max_lots_per_tick", "Max lots / hedge", "lots",
                    "NG_HEDGE_MAX_LOTS_PER_TICK", "int", 1, 50, 1),
            Tunable("skew_max_units", "Supertrend skew cap", "mmBtu",
                    "NG_HEDGE_ST_SKEW_MAX_UNITS", "float", 0, 5_000, 25,
                    note="post-hedge &delta; lands on the Supertrend side, up to this; 0 = off"),
        ),
    ),
    Engine(
        slug="ng_fmove",
        title="NATURALGAS &middot; F-move",
        subtitle="1-min &middot; SELLs ATM when F moves from anchor AND |&delta;| is growing",
        tunables=(
            Tunable("trigger_inr", "F-move trigger", "&#8377;/mmBtu",
                    "NG_FMOVE_HEDGE_TRIGGER_INR", "float", 0.5, 200, 0.5),
            Tunable("min_delta_units", "Min |&delta;| to act", "mmBtu",
                    "NG_FMOVE_HEDGE_MIN_DELTA_UNITS", "float", 0, 50_000, 25,
                    note="625 = one ATM lot; below that a single lot overshoots"),
            Tunable("max_lots_per_tick", "Max lots / hedge", "lots",
                    "NG_FMOVE_HEDGE_MAX_LOTS_PER_TICK", "int", 1, 20, 1),
            Tunable("cooldown_min", "Cooldown", "min",
                    "NG_FMOVE_HEDGE_COOLDOWN_MIN", "int", 0, 240, 1),
            Tunable("max_hedges_per_day", "Daily cap", "hedges",
                    "NG_FMOVE_HEDGE_MAX_HEDGES_PER_DAY", "int", 0, 200, 1,
                    note="0 disables the cap"),
        ),
    ),
    Engine(
        slug="crudeoilm_band",
        title="CRUDEOILM &middot; &delta; band",
        subtitle="1-min dispatcher &middot; same formula as NG, independent knobs",
        tunables=(
            _BAND_NOTIONAL,
            Tunable("vol_factor", "Vol factor", "&times;", "CRUDEOILM_HEDGE_VOL_FACTOR",
                    "float", 0.1, 10.0, 0.01,
                    note="1.0 = notional parity, 1.75 = vol parity with NG"),
            Tunable("band_min", "Band floor", "bbl", "CRUDEOILM_HEDGE_BAND_MIN",
                    "float", 1, 10_000, 1),
            Tunable("band_max", "Band ceiling", "bbl", "CRUDEOILM_HEDGE_BAND_MAX",
                    "float", 1, 100_000, 5),
            Tunable("cooldown_min", "Cooldown", "min", "NG_HEDGE_COOLDOWN_MIN",
                    "int", 0, 240, 1),
            Tunable("max_lots_per_tick", "Max lots / hedge", "lots",
                    "NG_HEDGE_MAX_LOTS_PER_TICK", "int", 1, 50, 1),
            Tunable("skew_max_units", "Supertrend skew cap", "bbl",
                    "CRUDEOILM_HEDGE_ST_SKEW_MAX_UNITS", "float", 0, 500, 1,
                    note="post-hedge &delta; lands on the Supertrend side, up to this; 0 = off"),
        ),
    ),
    Engine(
        slug="crudeoilm_fmove",
        title="CRUDEOILM &middot; F-move",
        subtitle="1-min &middot; SELLs ATM when F moves from anchor AND |&delta;| is growing",
        tunables=(
            Tunable("trigger_inr", "F-move trigger", "&#8377;/bbl",
                    "CRUDEOILM_FMOVE_HEDGE_TRIGGER_INR", "float", 5, 5_000, 5),
            Tunable("min_delta_units", "Min |&delta;| to act", "bbl",
                    "CRUDEOILM_FMOVE_HEDGE_MIN_DELTA_UNITS", "float", 0, 1_000, 1,
                    note="below this the anchor advances with no order"),
            Tunable("max_lots_per_tick", "Max lots / hedge", "lots",
                    "CRUDEOILM_FMOVE_HEDGE_MAX_LOTS_PER_TICK", "int", 1, 20, 1),
            Tunable("cooldown_min", "Cooldown", "min",
                    "CRUDEOILM_FMOVE_HEDGE_COOLDOWN_MIN", "int", 0, 240, 1),
            Tunable("max_hedges_per_day", "Daily cap", "hedges",
                    "CRUDEOILM_FMOVE_HEDGE_MAX_HEDGES_PER_DAY", "int", 0, 200, 1,
                    note="0 disables the cap"),
        ),
    ),
    Engine(
        slug="banknifty",
        title="BANKNIFTY &middot; &delta; band",
        subtitle="1-min &middot; NSE hours only &middot; flat band, no notional formula",
        tunables=(
            Tunable("band_units", "&delta; band", "units", "BNF_HEDGE_BAND_UNITS",
                    "float", 1, 10_000, 1,
                    note="NFO positions() reports units, not lots"),
            Tunable("cooldown_min", "Cooldown", "min", "BNF_HEDGE_COOLDOWN_MIN",
                    "int", 0, 240, 1),
            Tunable("max_hedges_per_day", "Daily cap", "hedges",
                    "BNF_HEDGE_MAX_LOTS_PER_DAY", "int", 0, 200, 1),
            Tunable("freeze_before_min", "Freeze before close", "min",
                    "BNF_HEDGE_FREEZE_BEFORE_MIN", "int", 0, 180, 1),
        ),
    ),
    Engine(
        slug="goldm",
        title="GOLDM &middot; F-move",
        subtitle="5-min &middot; rebalances with front-month FUT, not options",
        tunables=(
            Tunable("trigger_inr", "F-move trigger", "&#8377;",
                    "GOLDM_HEDGE_TRIGGER_INR", "float", 10, 20_000, 10),
            Tunable("min_delta_units", "Min |&delta;| to act", "units",
                    "GOLDM_HEDGE_MIN_DELTA_UNITS", "float", 0, 1_000, 1),
            Tunable("max_lots_per_tick", "Max lots / hedge", "lots",
                    "GOLDM_HEDGE_MAX_LOTS_PER_TICK", "int", 1, 20, 1),
            Tunable("max_hedges_per_day", "Daily cap", "hedges",
                    "GOLDM_HEDGE_MAX_HEDGES_PER_DAY", "int", 0, 200, 1),
            Tunable("freeze_before_min", "Freeze before close", "min",
                    "GOLDM_HEDGE_FREEZE_BEFORE_MIN", "int", 0, 180, 1),
        ),
    ),
    Engine(
        slug="silverm",
        title="SILVERM &middot; F-move",
        subtitle="1-min &middot; BUYs OTM wings (long &gamma;), never sells more premium",
        tunables=(
            Tunable("trigger_inr", "F-move trigger", "&#8377;/kg",
                    "SILVERM_HEDGE_TRIGGER_INR", "float", 50, 50_000, 50),
            Tunable("max_residual_kg", "Target residual |&delta;|", "kg",
                    "SILVERM_HEDGE_MAX_RESIDUAL_KG", "float", 0.1, 50.0, 0.1,
                    note="wing is sized to land the book inside this"),
            Tunable("max_lots_per_step", "Max lots / hedge", "lots",
                    "SILVERM_HEDGE_MAX_LOTS_PER_STEP", "int", 1, 20, 1),
            Tunable("cooldown_min", "Cooldown", "min", "SILVERM_HEDGE_COOLDOWN_MIN",
                    "int", 0, 240, 1),
            Tunable("max_hedges_per_day", "Daily cap", "hedges",
                    "SILVERM_HEDGE_MAX_HEDGES_PER_DAY", "int", 0, 200, 1),
        ),
    ),
)

_BY_SLUG = {e.slug: e for e in ENGINES}


def get_engine(slug: str) -> Engine | None:
    return _BY_SLUG.get(slug)


def engine_slugs() -> tuple[str, ...]:
    return tuple(e.slug for e in ENGINES)


def coerce(slug: str, key: str, raw: Any) -> float | int | None:
    """Parse + clamp `raw` for one tunable. None if unknown or unparseable."""
    eng = _BY_SLUG.get(slug)
    tun = eng.tunable(key) if eng else None
    if tun is None:
        return None
    try:
        return tun.coerce(raw)
    except (TypeError, ValueError):
        return None


def effective(settings: Any, slug: str, key: str) -> float | int:
    """Current value of one tunable: override if set, else the .env default.

    Call this *inside* the tick, never at scheduler-registration time — that is
    what makes a /control edit apply from the next rebalance.
    """
    from app import state

    eng = _BY_SLUG[slug]
    tun = eng.tunable(key)
    if tun is None:
        raise KeyError(f"{slug} has no tunable {key!r}")
    env_default = getattr(settings, tun.env_attr)
    return tun.coerce(state.get_hedge_tunable(slug, key, env_default))


def all_effective(settings: Any, slug: str) -> dict[str, float | int]:
    """Every tunable for one engine, resolved in one pass.

    The band engines resolve once per tick and thread the result through, so a
    single hedge decision can never be computed from two different generations
    of the config.
    """
    return {t.key: effective(settings, slug, t.key) for t in _BY_SLUG[slug].tunables}


def is_overridden(slug: str, key: str) -> bool:
    from app import state
    return state.get_hedge_tunable(slug, key, None) is not None
