"""The resolved model: typed objects built from the raw YAML tree.

`build_model` turns a RawModel into a Model, reporting every problem it finds
as a Diagnostic (it keeps going after the first error so that one `check`
shows everything that is wrong).
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from . import FORMAT_VERSION
from .diagnostics import Diagnostics
from .params import Param, parse_param, is_number, suggest
from .timebase import ANNUAL, Discounting, Periods, Timeslices, cap2act as unit_cap2act
from .yamlio import RawModel, TOP_LEVEL

COMMODITY_KINDS = ("energy", "material", "demand", "emission")
SENSES = {">=": ">=", "<=": "<=", "=": "=", "==": "=", "ge": ">=", "le": "<=", "eq": "="}
BOUND_KEYS = ("max", "min", "fix")

COMMODITY_KEYS = {
    "kind", "unit", "timeslice", "description", "demand", "load_shape", "peak_reserve",
    "peak_timeslices", "efficiency", "balance", "cap", "budget", "tax", "sources",
}
CAPACITY_KEYS = {
    "capacity_unit", "cap2act", "lifetime", "economic_life", "hurdle_rate", "invcost", "fixom",
    "existing", "residual", "availability", "availability_min", "availability_fix",
    "capacity_factor", "capacity_factor_min", "peak_contribution",
}
PROCESS_KEYS = CAPACITY_KEYS | {
    "description", "tags", "regions", "inputs", "outputs", "flexible_inputs", "flexible_outputs",
    "efficiency", "timeslice", "activity_unit", "varom", "vintaged", "emissions", "bounds", "sources",
}
STORAGE_KEYS = {
    "description", "tags", "regions", "commodity", "cycle", "efficiency", "hours", "capacity_unit",
    "cap2act", "lifetime", "economic_life", "hurdle_rate", "invcost", "fixom", "varom", "existing",
    "residual", "availability", "peak_contribution", "bounds", "sources",
}
TRADE_KEYS = {
    "description", "tags", "commodity", "from", "to", "bidirectional", "efficiency", "varom",
    "capacity_unit", "cap2act", "lifetime", "economic_life", "hurdle_rate", "invcost", "fixom",
    "existing", "residual", "availability", "bounds", "sources",
}
CONSTRAINT_KEYS = {"description", "regions", "periods", "from", "until", "terms", "sense", "rhs"}
TERM_KINDS = ("flow", "production", "consumption", "emission", "capacity", "new_capacity", "activity")


@dataclass
class Commodity:
    name: str
    kind: str
    unit: str
    level: int
    description: str = ""
    sense: str = ">="
    demand: Param = field(default_factory=Param)
    load_shape: dict[str, float] | None = None
    peak_reserve: Param = field(default_factory=Param)
    peak_slices: list[str] | None = None
    efficiency: Param = field(default_factory=Param)
    cap: Param = field(default_factory=lambda: Param("bound"))
    budget: dict[str | None, float] = field(default_factory=dict)
    tax: Param = field(default_factory=Param)


@dataclass
class FlexMember:
    commodity: str
    efficiency: Param = field(default_factory=Param)
    max_share: Param = field(default_factory=Param)
    min_share: Param = field(default_factory=Param)


@dataclass
class Tech:
    """A process, a storage or a trade link (they share all capacity logic)."""

    name: str
    kind: str                      # process | storage | trade
    description: str = ""
    tags: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    # topology (process)
    inputs: dict[str, Param] = field(default_factory=dict)
    outputs: dict[str, Param] = field(default_factory=dict)
    flex_in: dict[str, FlexMember] = field(default_factory=dict)
    flex_out: dict[str, FlexMember] = field(default_factory=dict)
    efficiency: Param = field(default_factory=Param)
    level: int = 0
    # storage / trade
    commodity: str | None = None
    cycle_level: int = 0
    hours: Param = field(default_factory=Param)
    region_from: str | None = None
    region_to: str | None = None
    bidirectional: bool = False
    # capacity
    has_capacity: bool = False
    can_invest: bool = False
    cap_unit: str = ""
    act_unit: str = ""
    cap2act: float = 1.0
    lifetime: Param = field(default_factory=Param)
    economic_life: Param = field(default_factory=Param)
    hurdle_rate: Param = field(default_factory=Param)
    invcost: Param = field(default_factory=Param)
    fixom: Param = field(default_factory=Param)
    varom: Param = field(default_factory=Param)
    af_max: Param = field(default_factory=Param)
    af_min: Param = field(default_factory=Param)
    af_fix: Param = field(default_factory=Param)
    cf_max: Param = field(default_factory=Param)
    cf_min: Param = field(default_factory=Param)
    existing: dict[str, dict[int, float]] = field(default_factory=dict)
    residual: Param = field(default_factory=lambda: Param("bound"))
    vintaged: bool = False
    emissions: dict[str, dict[str, Param]] = field(default_factory=dict)
    peak_contribution: Param = field(default_factory=Param)
    bounds: dict[tuple[str, str], Param] = field(default_factory=dict)
    cumulative_activity: dict[str, dict[str | None, float]] = field(default_factory=dict)

    @property
    def primary_outputs(self) -> list[str]:
        """Commodities that define the activity (the TIMES primary group)."""
        if self.flex_out:
            return list(self.flex_out)
        return list(self.outputs)[:1]

    def commodities_in(self) -> list[str]:
        return list(self.inputs) + list(self.flex_in)

    def commodities_out(self) -> list[str]:
        return list(self.outputs) + list(self.flex_out) + list(self.emissions)


@dataclass
class Term:
    kind: str
    processes: list[str]
    commodity: str | None
    coef: Param


@dataclass
class Constraint:
    name: str
    description: str
    sense: str
    rhs: Param
    region_mode: str            # each | sum
    regions: list[str]
    period_mode: str            # each | sum
    year_from: int | None
    year_until: int | None
    terms: list[Term]


@dataclass
class Model:
    name: str
    description: str
    currency: str
    discount_rate: float
    discount_year: int
    mid_year: bool
    periods: Periods
    ts: Timeslices
    regions: list[str]
    units: dict[str, str]
    commodities: dict[str, Commodity]
    processes: dict[str, Tech]
    storage: dict[str, Tech]
    trade: dict[str, Tech]
    constraints: dict[str, Constraint]
    emission_factors: dict[str, dict[str, Param]]
    expect: list[dict[str, Any]]
    raw: RawModel

    @property
    def discounting(self) -> Discounting:
        return Discounting(self.discount_rate, self.discount_year, self.periods)

    def techs(self):
        yield from self.processes.values()
        yield from self.storage.values()
        yield from self.trade.values()

    def tech(self, name: str) -> Tech | None:
        return self.processes.get(name) or self.storage.get(name) or self.trade.get(name)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _check_keys(entry: dict, allowed: set[str], path: tuple, diags: Diagnostics, what: str) -> None:
    for key in entry:
        if key not in allowed:
            diags.error(
                "PT040", f"unknown key '{key}' in {what}", path + (key,),
                hint=suggest(key, sorted(allowed)) or f"allowed keys: {', '.join(sorted(allowed))}",
            )


def _name_ok(name: Any, path: tuple, diags: Diagnostics, what: str) -> bool:
    s = str(name)
    if not isinstance(name, str) or not s or not all(ch.isalnum() or ch in "_-" for ch in s) or len(s) > 60:
        diags.error("PT041", f"{what} name {name!r} must be a string of letters, digits, '_' or '-' (max 60 chars)", path)
        return False
    return True


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _bound_params(raw: Any, path: tuple, regions: list[str], diags: Diagnostics, what: str) -> dict[str, Param]:
    out: dict[str, Param] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict) or not raw or not set(raw) <= set(BOUND_KEYS):
        diags.error("PT042", f"{what} must be a mapping with keys from: max, min, fix", path,
                    hint="e.g. capacity: {max: {2030: 10, 2050: 25}}")
        return out
    for key, val in raw.items():
        p = parse_param(val, kind="bound", regions=regions, second=None, path=path + (key,), diags=diags, lo=0.0)
        if p:
            out[key] = p
    return out


def _level_from_name(name: Any, ts: Timeslices, path: tuple, diags: Diagnostics) -> int | None:
    idx = ts.level_index(str(name))
    if idx is None:
        diags.error("PT044", f"unknown timeslice level '{name}'", path,
                    hint=f"levels defined in this model: {', '.join(ts.levels)}")
    return idx


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build_model(raw: RawModel, diags: Diagnostics) -> Model | None:
    d = raw.data
    for key in d:
        if key not in TOP_LEVEL:
            diags.error("PT040", f"unknown top-level key '{key}'", (key,),
                        hint=suggest(key, list(TOP_LEVEL)) or f"allowed: {', '.join(TOP_LEVEL)}")
    version = d.get("plaintimes")
    if version is None:
        diags.warn("PT050", "model.yaml does not state its format version", (),
                   hint=f"add 'plaintimes: {FORMAT_VERSION}' at the top of model.yaml")
    elif version != FORMAT_VERSION:
        diags.error("PT051", f"model format version {version!r} is not supported (this tool reads version {FORMAT_VERSION})",
                    ("plaintimes",))

    # -- regions ----------------------------------------------------------
    regions = d.get("regions")
    if regions is None:
        diags.error("PT052", "model has no 'regions'", (), hint="add e.g.  regions: [R1]")
        regions = []
    elif not isinstance(regions, list) or not regions or len(set(regions)) != len(regions):
        diags.error("PT053", "regions must be a non-empty list of unique names", ("regions",))
        regions = []
    else:
        for i, r in enumerate(regions):
            _name_ok(r, ("regions", i), diags, "region")
        regions = [str(r) for r in regions]

    # -- time ---------------------------------------------------------------
    periods = Periods.from_raw(d.get("periods"), diags) if "periods" in d else None
    if "periods" not in d:
        diags.error("PT054", "model has no 'periods'", (),
                    hint="add e.g.  periods: {start: 2025, lengths: [5, 5, 10, 10]}")
    ts = Timeslices.from_raw(d.get("timeslices"), d.get("timeslice_levels"), diags)

    clash = sorted(set(regions) & set(ts.names))
    if clash:
        diags.error("PT059", f"name(s) {clash} are used for both a region and a timeslice", ("regions",),
                    hint="parameters keyed by name could not tell them apart; rename one of them.")

    rate = d.get("discount_rate")
    if not is_number(rate) or not (0 <= rate < 1):
        diags.error("PT055", "discount_rate must be a number between 0 and 1 (e.g. 0.05)", ("discount_rate",)
                    if "discount_rate" in d else ())
        rate = 0.05
    dyear = d.get("discount_year", periods.first_year if periods else 0)
    if not isinstance(dyear, int):
        diags.error("PT056", "discount_year must be a year", ("discount_year",))
        dyear = periods.first_year if periods else 0
    mid_year = d.get("mid_year_discounting", False)
    if not isinstance(mid_year, bool):
        diags.error("PT057", "mid_year_discounting must be true or false", ("mid_year_discounting",))
        mid_year = False
    units = d.get("units") or {}
    if not isinstance(units, dict):
        diags.error("PT058", "units must be a mapping, e.g. {energy: PJ, capacity: GW}", ("units",))
        units = {}

    if periods is None or diags.has_errors and not regions:
        return None

    # -- commodities ------------------------------------------------------
    commodities: dict[str, Commodity] = {}
    raw_coms = d.get("commodities") or {}
    if not isinstance(raw_coms, dict):
        diags.error("PT006", "'commodities' must be a mapping of name -> definition", ("commodities",))
        raw_coms = {}
    slice_names = set(ts.names)
    for name, entry in raw_coms.items():
        path = ("commodities", name)
        if not _name_ok(name, path, diags, "commodity"):
            continue
        entry = entry or {}
        if not isinstance(entry, dict):
            diags.error("PT006", f"commodity '{name}' must be a mapping", path)
            continue
        _check_keys(entry, COMMODITY_KEYS, path, diags, f"commodity '{name}'")
        kind = entry.get("kind")
        if kind not in COMMODITY_KINDS:
            diags.error("PT060", f"commodity '{name}' needs kind: one of {', '.join(COMMODITY_KINDS)}", path + (("kind",) if "kind" in entry else ()),
                        hint=suggest(kind, list(COMMODITY_KINDS)) if kind else None)
            kind = "energy"
        level = 0
        if "timeslice" in entry:
            level = _level_from_name(entry["timeslice"], ts, path + ("timeslice",), diags) or 0
        if kind == "emission" and level != 0:
            diags.error("PT061", "emission commodities are tracked annually; remove 'timeslice'", path + ("timeslice",))
            level = 0
        sense = ">=" if kind != "material" else "="
        if "balance" in entry:
            if entry["balance"] not in (">=", "=", "=="):
                diags.error("PT062", "balance must be '>=' (surplus allowed) or '=' (exact)", path + ("balance",))
            else:
                sense = SENSES[entry["balance"]]
        com = Commodity(name, kind, str(entry.get("unit", units.get("energy", ""))), level,
                        str(entry.get("description", "")), sense)
        # demand
        if "demand" in entry:
            if kind != "demand":
                diags.error("PT063", f"'demand' given for commodity '{name}' of kind '{kind}'", path + ("demand",),
                            hint="set kind: demand, or remove the demand projection.")
            com.demand = parse_param(entry["demand"], kind="data", regions=regions, second=None,
                                     path=path + ("demand",), diags=diags, lo=0.0)
        elif kind == "demand":
            diags.error("PT064", f"demand commodity '{name}' has no 'demand' projection", path,
                        hint="add e.g.  demand: {2025: 100, 2050: 140}")
        # load shape
        if "load_shape" in entry:
            shape = entry["load_shape"]
            slices = ts.at_level(level)
            if level == 0:
                diags.error("PT065", f"load_shape given but commodity '{name}' is annual", path + ("load_shape",),
                            hint="set 'timeslice:' on the commodity to the level of the load shape.")
            elif not isinstance(shape, dict) or set(map(str, shape)) != set(slices):
                diags.error("PT066", f"load_shape must give a share for exactly these timeslices: {', '.join(slices)}",
                            path + ("load_shape",))
            elif not all(is_number(v) and v >= 0 for v in shape.values()):
                diags.error("PT067", "load_shape shares must be non-negative numbers", path + ("load_shape",))
            else:
                total = float(sum(shape.values()))
                if abs(total - 1.0) > 1e-6:
                    diags.error("PT068", f"load_shape shares sum to {total:.6g}, not 1", path + ("load_shape",),
                                hint="shares are fractions of annual demand falling in each timeslice.")
                else:
                    com.load_shape = {str(k): float(v) / total for k, v in shape.items()}
        # peak
        if "peak_reserve" in entry:
            com.peak_reserve = parse_param(entry["peak_reserve"], kind="data", regions=regions, second=None,
                                           path=path + ("peak_reserve",), diags=diags, lo=0.0)
            if level == 0:
                diags.warn("PT069", f"peak_reserve on annual commodity '{name}' only constrains annual capacity adequacy",
                           path + ("peak_reserve",))
        if "peak_timeslices" in entry:
            pk = [str(s) for s in _as_list(entry["peak_timeslices"])]
            bad = [s for s in pk if s not in ts.at_level(level)]
            if bad:
                diags.error("PT070", f"peak_timeslices {bad} are not timeslices at this commodity's level",
                            path + ("peak_timeslices",), hint=f"choose from: {', '.join(ts.at_level(level))}")
            else:
                com.peak_slices = pk
        if "efficiency" in entry:
            com.efficiency = parse_param(entry["efficiency"], kind="data", regions=regions, second=None,
                                         path=path + ("efficiency",), diags=diags, lo=1e-6, hi=1.0)
        for key in ("cap", "tax"):
            if key in entry:
                setattr(com, key, parse_param(entry[key], kind="bound" if key == "cap" else "data", regions=regions,
                                              second=None, path=path + (key,), diags=diags,
                                              lo=None if key == "tax" else None))
        if "budget" in entry:
            b = entry["budget"]
            if is_number(b):
                com.budget = {None: float(b)}
            elif isinstance(b, dict) and b and set(b) <= set(regions) and all(is_number(v) for v in b.values()):
                com.budget = {str(k): float(v) for k, v in b.items()}
            else:
                diags.error("PT071", "budget must be a number (per region) or a mapping region -> number",
                            path + ("budget",))
        if kind != "emission" and any(k in entry for k in ("cap", "budget", "tax")):
            diags.error("PT072", f"cap/budget/tax apply to emission commodities; '{name}' is {kind}", path)
        commodities[name] = com

    # -- emission factors -------------------------------------------------
    emission_factors: dict[str, dict[str, Param]] = {}
    raw_ef = d.get("emission_factors") or {}
    if not isinstance(raw_ef, dict):
        diags.error("PT006", "'emission_factors' must be a mapping fuel -> {emission: factor}", ("emission_factors",))
        raw_ef = {}
    for fuel, efs in raw_ef.items():
        path = ("emission_factors", fuel)
        if fuel not in commodities:
            diags.error("PT080", f"emission_factors refers to unknown commodity '{fuel}'", path,
                        hint=suggest(fuel, list(commodities)))
            continue
        if not isinstance(efs, dict):
            diags.error("PT081", "expected a mapping emission -> factor", path)
            continue
        for em, val in efs.items():
            if em not in commodities or commodities[em].kind != "emission":
                diags.error("PT082", f"'{em}' is not an emission commodity", path + (em,),
                            hint=suggest(em, [c for c, o in commodities.items() if o.kind == "emission"]))
                continue
            emission_factors.setdefault(fuel, {})[em] = parse_param(
                val, kind="data", regions=regions, second=None, path=path + (em,), diags=diags)

    ctx = _Ctx(regions, ts, periods, commodities, emission_factors, units, diags)

    # -- processes, storage, trade ---------------------------------------
    processes: dict[str, Tech] = {}
    for name, entry in _section(d, "processes", diags).items():
        t = _build_process(name, entry, ctx)
        if t:
            processes[name] = t
    storage: dict[str, Tech] = {}
    for name, entry in _section(d, "storage", diags).items():
        t = _build_storage(name, entry, ctx)
        if t:
            storage[name] = t
    trade: dict[str, Tech] = {}
    for name, entry in _section(d, "trade", diags).items():
        t = _build_trade(name, entry, ctx)
        if t:
            trade[name] = t
    seen: dict[str, str] = {}
    for sect, group in (("processes", processes), ("storage", storage), ("trade", trade)):
        for name in group:
            if name in seen:
                diags.error("PT090", f"name '{name}' is used in both '{seen[name]}' and '{sect}'", (sect, name),
                            hint="process, storage and trade names share one namespace.")
            seen[name] = sect

    # -- constraints ------------------------------------------------------
    all_techs = {**processes, **storage, **trade}
    constraints: dict[str, Constraint] = {}
    for name, entry in _section(d, "constraints", diags).items():
        c = _build_constraint(name, entry, ctx, all_techs)
        if c:
            constraints[name] = c

    expect = d.get("expect") or []
    if not isinstance(expect, list):
        diags.error("PT100", "'expect' must be a list of checks", ("expect",))
        expect = []

    return Model(
        name=str(d.get("name", "model")), description=str(d.get("description", "")),
        currency=str(d.get("currency", "CUR")), discount_rate=float(rate), discount_year=dyear,
        mid_year=mid_year, periods=periods, ts=ts, regions=regions, units={str(k): str(v) for k, v in units.items()},
        commodities=commodities, processes=processes, storage=storage, trade=trade,
        constraints=constraints, emission_factors=emission_factors, expect=expect, raw=raw,
    )


@dataclass
class _Ctx:
    regions: list[str]
    ts: Timeslices
    periods: Periods
    commodities: dict[str, Commodity]
    emission_factors: dict[str, dict[str, Param]]
    units: dict[str, Any]
    diags: Diagnostics


def _section(d: dict, key: str, diags: Diagnostics) -> dict:
    sec = d.get(key) or {}
    if not isinstance(sec, dict):
        diags.error("PT006", f"'{key}' must be a mapping of name -> definition", (key,))
        return {}
    return sec


def _commodity_ref(name: Any, ctx: _Ctx, path: tuple, kinds: tuple[str, ...] | None = None) -> bool:
    if name not in ctx.commodities:
        ctx.diags.error("PT110", f"unknown commodity '{name}'", path,
                        hint=suggest(name, list(ctx.commodities)) or "define it under 'commodities:' or correct the name.")
        return False
    if kinds and ctx.commodities[name].kind not in kinds:
        ctx.diags.error("PT111", f"commodity '{name}' is of kind '{ctx.commodities[name].kind}'; expected {' or '.join(kinds)}", path)
        return False
    return True


def _parse_regions(entry: dict, ctx: _Ctx, path: tuple) -> list[str]:
    if "regions" not in entry:
        return list(ctx.regions)
    regs = [str(r) for r in _as_list(entry["regions"])]
    bad = [r for r in regs if r not in ctx.regions]
    if bad or not regs:
        ctx.diags.error("PT112", f"unknown region(s) {bad}" if bad else "regions is empty", path + ("regions",),
                        hint=suggest(bad[0], ctx.regions) if bad else None)
        return [r for r in regs if r in ctx.regions]
    return regs


def _parse_capacity(t: Tech, entry: dict, ctx: _Ctx, path: tuple, default_act_unit: str) -> None:
    """Capacity, lifetime, cost and availability attributes shared by all techs."""
    diags, regs = ctx.diags, t.regions
    pp = lambda key, **kw: parse_param(entry.get(key), kind=kw.pop("kind", "data"), regions=regs,
                                       second=kw.pop("second", None), path=path + (key,), diags=diags, **kw)
    t.lifetime = pp("lifetime", lo=0.5, hi=200)
    t.economic_life = pp("economic_life", lo=0.5, hi=200)
    t.hurdle_rate = pp("hurdle_rate", lo=0.0, hi=1.0)
    t.invcost = pp("invcost", lo=0.0)
    t.fixom = pp("fixom", lo=0.0)
    t.varom = pp("varom")
    t.peak_contribution = pp("peak_contribution", lo=0.0, hi=1.0)
    slices = [s for s in ctx.ts.names if s != ANNUAL]
    for key, attr in (("availability", "af_max"), ("availability_min", "af_min"), ("availability_fix", "af_fix")):
        if key in entry:
            setattr(t, attr, pp(key, second=slices, second_label="timeslice", lo=0.0, hi=1.0))
    for key, attr in (("capacity_factor", "cf_max"), ("capacity_factor_min", "cf_min")):
        if key in entry:
            setattr(t, attr, pp(key, second=slices, second_label="timeslice", lo=0.0, hi=1.0))
    if t.af_fix and (t.af_max or t.af_min):
        diags.error("PT120", "availability_fix cannot be combined with availability / availability_min", path)

    # existing stock, by vintage
    if "existing" in entry:
        ex = entry["existing"]
        by_region: dict[str, Any] = {}
        if isinstance(ex, dict) and ex and all(k in ctx.regions for k in ex):
            by_region = ex
        elif isinstance(ex, dict) and ex:
            by_region = {r: ex for r in regs}
        else:
            diags.error("PT121", "existing must map commissioning year -> capacity, e.g. {2005: 3.0, 2015: 1.5}",
                        path + ("existing",))
        for r, series in by_region.items():
            p = path + ("existing",) + ((r,) if by_region is ex else ())
            if r not in regs:
                diags.error("PT122", f"existing capacity given for region '{r}' where '{t.name}' is not defined", p)
                continue
            if not (isinstance(series, dict) and series and all(isinstance(y, int) and is_number(v) and v >= 0
                                                                 for y, v in series.items())):
                diags.error("PT121", "existing must map commissioning year -> capacity (non-negative numbers)", p)
                continue
            late = [y for y in series if y >= ctx.periods.first_year]
            if late:
                diags.error("PT123", f"existing capacity vintage {late[0]} is not before the first model year {ctx.periods.first_year}",
                            p + (late[0],), hint="capacity built inside the horizon is a model decision; use bounds.new_capacity to force it.")
                continue
            t.existing[r] = {int(y): float(v) for y, v in series.items() if v > 0}
    if "residual" in entry:
        t.residual = parse_param(entry["residual"], kind="bound", regions=regs, second=None,
                                 path=path + ("residual",), diags=diags, lo=0.0)
        if t.existing:
            diags.error("PT124", "give either 'existing' (vintages + lifetime) or 'residual' (a stock profile), not both", path)

    t.has_capacity = any(k in entry for k in CAPACITY_KEYS - {"peak_contribution"}) or \
        any(k in (entry.get("bounds") or {}) for k in ("capacity", "new_capacity"))
    t.can_invest = "invcost" in entry
    if t.has_capacity:
        if (t.can_invest or t.existing) and not t.lifetime:
            diags.error("PT125", f"'{t.name}' needs a lifetime (years)", path,
                        hint="add e.g.  lifetime: 30")
        if not t.can_invest and not t.existing and not t.residual:
            diags.error("PT126", f"'{t.name}' has capacity attributes but can never have any capacity", path,
                        hint="add 'invcost' (use 0 for free capacity) to allow investment, or 'existing'/'residual' stock. "
                             "Remove lifetime/availability/fixom to make it a capacity-free process.")
        t.cap_unit = str(entry.get("capacity_unit", ctx.units.get("capacity", "")))
        t.act_unit = default_act_unit
        if "cap2act" in entry:
            if not is_number(entry["cap2act"]) or entry["cap2act"] <= 0:
                diags.error("PT127", "cap2act must be a positive number", path + ("cap2act",))
            else:
                t.cap2act = float(entry["cap2act"])
        else:
            c2a = unit_cap2act(t.cap_unit, t.act_unit)
            if c2a is None:
                diags.error(
                    "PT128", f"cannot derive capacity-to-activity factor from capacity unit '{t.cap_unit}' and activity unit '{t.act_unit}'",
                    path, hint="give 'cap2act:' explicitly (activity per year from one unit of capacity at 100% use), "
                               "or use units the tool knows (GW/MW with PJ/TWh/..., or '<unit>/yr').")
            else:
                t.cap2act = c2a
    elif "invcost" in entry:  # pragma: no cover - has_capacity is true whenever invcost is given
        pass

    # bounds
    b = entry.get("bounds")
    if b is not None:
        allowed = {"capacity", "new_capacity", "activity", "cumulative_activity"}
        if not isinstance(b, dict):
            diags.error("PT129", "bounds must be a mapping", path + ("bounds",))
        else:
            _check_keys(b, allowed, path + ("bounds",), diags, "bounds")
            for what in ("capacity", "new_capacity", "activity"):
                if what in b:
                    for key, p in _bound_params(b[what], path + ("bounds", what), regs, diags, f"bounds.{what}").items():
                        t.bounds[(what, key)] = p
            if "cumulative_activity" in b:
                ca = b["cumulative_activity"]
                if not isinstance(ca, dict) or not set(ca) <= {"max", "min"}:
                    diags.error("PT130", "bounds.cumulative_activity must be {max: x} and/or {min: x}",
                                path + ("bounds", "cumulative_activity"))
                else:
                    for key, val in ca.items():
                        if is_number(val):
                            t.cumulative_activity[key] = {None: float(val)}
                        elif isinstance(val, dict) and set(val) <= set(regs) and all(is_number(v) for v in val.values()):
                            t.cumulative_activity[key] = {str(k): float(v) for k, v in val.items()}
                        else:
                            diags.error("PT130", "cumulative bound must be a number or a mapping region -> number",
                                        path + ("bounds", "cumulative_activity", key))
            if any(k in b for k in ("capacity", "new_capacity")) and not t.has_capacity:
                diags.error("PT131", "capacity bounds given for a process without capacity", path + ("bounds",))
            if "new_capacity" in b and not t.can_invest:
                diags.error("PT132", "bounds.new_capacity given but the process has no 'invcost' (investment is not allowed)",
                            path + ("bounds", "new_capacity"))


def _coef_map(raw: Any, ctx: _Ctx, path: tuple, regs: list[str], what: str) -> dict[str, Param]:
    """inputs / outputs: a name, a list of names, or a mapping name -> coefficient."""
    out: dict[str, Param] = {}
    if raw is None:
        return out
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        for i, c in enumerate(raw):
            if _commodity_ref(c, ctx, path + (i,)):
                if c in out:
                    ctx.diags.error("PT140", f"commodity '{c}' listed twice in {what}", path + (i,))
                out[c] = Param.constant(1.0)
        return out
    if isinstance(raw, dict):
        for c, val in raw.items():
            if not _commodity_ref(c, ctx, path + (c,)):
                continue
            p = parse_param(1.0 if val is None else val, kind="data", regions=regs, second=None,
                            path=path + (c,), diags=ctx.diags, lo=0.0)
            if p:
                out[c] = p
        return out
    ctx.diags.error("PT141", f"{what} must be a commodity name, a list of names, or a mapping name -> amount per unit activity", path)
    return out


def _flex_map(raw: Any, ctx: _Ctx, path: tuple, regs: list[str], is_input: bool) -> dict[str, FlexMember]:
    out: dict[str, FlexMember] = {}
    if raw is None:
        return out
    if isinstance(raw, list):
        raw = {c: None for c in raw}
    if not isinstance(raw, dict) or len(raw) < 2:
        ctx.diags.error("PT142", "a flexible group needs at least two commodities (name -> {max_share, min_share"
                        + (", efficiency" if is_input else "") + "})", path,
                        hint="for a single commodity use 'inputs'/'outputs'.")
        return out
    allowed = {"max_share", "min_share"} | ({"efficiency"} if is_input else set())
    for c, spec in raw.items():
        if not _commodity_ref(c, ctx, path + (c,), ("energy", "material", "demand")):
            continue
        spec = spec or {}
        if not isinstance(spec, dict):
            ctx.diags.error("PT143", f"expected a mapping with keys from {sorted(allowed)}", path + (c,))
            continue
        _check_keys(spec, allowed, path + (c,), ctx.diags, "flexible group member")
        m = FlexMember(c)
        for key in ("max_share", "min_share"):
            if key in spec:
                setattr(m, key, parse_param(spec[key], kind="data", regions=regs, second=None,
                                            path=path + (c, key), diags=ctx.diags, lo=0.0, hi=1.0))
        if "efficiency" in spec and is_input:
            m.efficiency = parse_param(spec["efficiency"], kind="data", regions=regs, second=None,
                                       path=path + (c, "efficiency"), diags=ctx.diags, lo=1e-9)
        out[c] = m
    return out


def _build_process(name: Any, entry: Any, ctx: _Ctx) -> Tech | None:
    path, diags = ("processes", name), ctx.diags
    if not _name_ok(name, path, diags, "process"):
        return None
    if not isinstance(entry, dict):
        diags.error("PT006", f"process '{name}' must be a mapping", path)
        return None
    _check_keys(entry, PROCESS_KEYS, path, diags, f"process '{name}'")
    t = Tech(name, "process", str(entry.get("description", "")), [str(x) for x in _as_list(entry.get("tags"))])
    t.regions = _parse_regions(entry, ctx, path)
    regs = t.regions
    t.inputs = _coef_map(entry.get("inputs"), ctx, path + ("inputs",), regs, "inputs")
    t.outputs = _coef_map(entry.get("outputs"), ctx, path + ("outputs",), regs, "outputs")
    t.flex_in = _flex_map(entry.get("flexible_inputs"), ctx, path + ("flexible_inputs",), regs, True)
    t.flex_out = _flex_map(entry.get("flexible_outputs"), ctx, path + ("flexible_outputs",), regs, False)
    for c in list(t.inputs) + list(t.flex_in):
        if ctx.commodities[c].kind == "emission":
            diags.error("PT150", f"emission commodity '{c}' cannot be an input", path + ("inputs",),
                        hint="model removals as a negative factor under 'emissions:'.")
    for c in list(t.outputs):
        if ctx.commodities[c].kind == "emission":
            diags.error("PT151", f"emission commodity '{c}' listed under outputs", path + ("outputs", c),
                        hint=f"write it as  emissions: {{{c}: {{activity: <factor>}}}}  or per input fuel.")
    dup = (set(t.inputs) & set(t.flex_in)) | (set(t.outputs) & set(t.flex_out))
    if dup:
        diags.error("PT152", f"commodity {sorted(dup)} appears in both the fixed and the flexible list", path)
    both = (set(t.inputs) | set(t.flex_in)) & (set(t.outputs) | set(t.flex_out))
    if both:
        diags.error("PT153", f"commodity {sorted(both)} is both an input and an output of '{name}'", path,
                    hint="model own-use or losses through the efficiency instead.")
    if not t.outputs and not t.flex_out:
        diags.error("PT154", f"process '{name}' has no outputs", path,
                    hint="every process needs at least one output commodity; a sink for exports can output a dummy material.")
    t.efficiency = parse_param(entry.get("efficiency"), kind="data", regions=regs, second=None,
                               path=path + ("efficiency",), diags=diags, lo=1e-9)
    if t.efficiency and not t.inputs and not t.flex_in:
        diags.error("PT155", "efficiency given but the process has no inputs", path + ("efficiency",))

    # timeslice level
    touched = [c for c in list(t.inputs) + list(t.outputs) + list(t.flex_in) + list(t.flex_out) if c in ctx.commodities]
    t.level = max((ctx.commodities[c].level for c in touched), default=0)
    if "timeslice" in entry:
        lv = _level_from_name(entry["timeslice"], ctx.ts, path + ("timeslice",), diags)
        if lv is not None:
            t.level = lv
    prim = t.primary_outputs
    act_unit = str(entry.get("activity_unit") or (ctx.commodities[prim[0]].unit if prim and prim[0] in ctx.commodities else ""))
    _parse_capacity(t, entry, ctx, path, act_unit)
    t.act_unit = act_unit

    t.vintaged = entry.get("vintaged", False)
    if not isinstance(t.vintaged, bool):
        diags.error("PT156", "vintaged must be true or false", path + ("vintaged",))
        t.vintaged = False
    if t.vintaged and not t.has_capacity:
        diags.error("PT157", "vintaged: true needs a process with capacity", path + ("vintaged",))
        t.vintaged = False

    # check availability keys against process level
    for attr, key in (("af_max", "availability"), ("af_min", "availability_min"), ("af_fix", "availability_fix")):
        for s in getattr(t, attr).seconds():
            if ctx.ts.level_of[s] > t.level:
                diags.error("PT158", f"{key} given for timeslice '{s}', finer than the process operates ({ctx.ts.levels[t.level]})",
                            path + (key,), hint="set 'timeslice:' on the process to the finer level.")
    for attr, key in (("cf_max", "capacity_factor"), ("cf_min", "capacity_factor_min")):
        for s in getattr(t, attr).seconds():
            if ctx.ts.level_of[s] >= t.level:
                diags.error("PT159", f"{key} for timeslice '{s}' must be coarser than the process level; use 'availability' for per-timeslice limits",
                            path + (key,))

    # emissions
    em = entry.get("emissions")
    if em is not None:
        if em == "combustion":
            hit = False
            for c in list(t.inputs) + list(t.flex_in):
                for e, factor in ctx.emission_factors.get(c, {}).items():
                    t.emissions.setdefault(e, {})[c] = factor
                    hit = True
            if not hit:
                diags.warn("PT160", f"'emissions: combustion' on '{name}' but none of its inputs has an entry in emission_factors",
                           path + ("emissions",))
        elif isinstance(em, dict):
            for e, spec in em.items():
                if not _commodity_ref(e, ctx, path + ("emissions", e), ("emission",)):
                    continue
                if is_number(spec):
                    spec = {"activity": spec}
                if not isinstance(spec, dict) or not spec:
                    diags.error("PT161", "expected a factor per unit activity, or a mapping {<input or output commodity> | activity: factor}",
                                path + ("emissions", e))
                    continue
                for basis, val in spec.items():
                    if basis != "activity" and basis not in t.inputs and basis not in t.outputs \
                            and basis not in t.flex_in and basis not in t.flex_out:
                        diags.error("PT162", f"emission basis '{basis}' is neither 'activity' nor a commodity of this process",
                                    path + ("emissions", e, basis),
                                    hint=suggest(basis, ["activity"] + t.commodities_in() + list(t.outputs)))
                        continue
                    t.emissions.setdefault(e, {})[basis] = parse_param(
                        val, kind="data", regions=regs, second=None, path=path + ("emissions", e, basis), diags=diags)
        else:
            diags.error("PT163", "emissions must be 'combustion' or a mapping emission -> factors", path + ("emissions",))
    return t


def _build_storage(name: Any, entry: Any, ctx: _Ctx) -> Tech | None:
    path, diags = ("storage", name), ctx.diags
    if not _name_ok(name, path, diags, "storage"):
        return None
    if not isinstance(entry, dict):
        diags.error("PT006", f"storage '{name}' must be a mapping", path)
        return None
    _check_keys(entry, STORAGE_KEYS, path, diags, f"storage '{name}'")
    t = Tech(name, "storage", str(entry.get("description", "")), [str(x) for x in _as_list(entry.get("tags"))])
    t.regions = _parse_regions(entry, ctx, path)
    c = entry.get("commodity")
    if c is None or not _commodity_ref(c, ctx, path + ("commodity",), ("energy", "material")):
        if c is None:
            diags.error("PT170", f"storage '{name}' needs 'commodity:'", path)
        return None
    t.commodity = c
    com = ctx.commodities[c]
    t.level = com.level
    if com.level == 0:
        diags.error("PT171", f"storage '{name}' stores '{c}', which is tracked annually; storage shifts energy between timeslices",
                    path + ("commodity",), hint=f"give commodity '{c}' a 'timeslice:' level.")
        return None
    t.cycle_level = com.level - 1
    if "cycle" in entry:
        lv = _level_from_name(entry["cycle"], ctx.ts, path + ("cycle",), diags)
        if lv is not None:
            if lv >= com.level:
                diags.error("PT172", "cycle must be a coarser timeslice level than the stored commodity's", path + ("cycle",))
            else:
                t.cycle_level = lv
    t.efficiency = parse_param(entry.get("efficiency", 1.0), kind="data", regions=t.regions, second=None,
                               path=path + ("efficiency",), diags=diags, lo=1e-6, hi=1.0)
    t.hours = parse_param(entry.get("hours"), kind="data", regions=t.regions, second=None,
                          path=path + ("hours",), diags=diags, lo=1e-6)
    _parse_capacity(t, entry, ctx, path, com.unit)
    if not t.has_capacity:
        diags.error("PT173", f"storage '{name}' needs capacity attributes (lifetime and invcost and/or existing)", path)
    return t


def _build_trade(name: Any, entry: Any, ctx: _Ctx) -> Tech | None:
    path, diags = ("trade", name), ctx.diags
    if not _name_ok(name, path, diags, "trade link"):
        return None
    if not isinstance(entry, dict):
        diags.error("PT006", f"trade link '{name}' must be a mapping", path)
        return None
    _check_keys(entry, TRADE_KEYS, path, diags, f"trade link '{name}'")
    t = Tech(name, "trade", str(entry.get("description", "")), [str(x) for x in _as_list(entry.get("tags"))])
    c = entry.get("commodity")
    if c is None or not _commodity_ref(c, ctx, path + ("commodity",), ("energy", "material")):
        if c is None:
            diags.error("PT180", f"trade link '{name}' needs 'commodity:'", path)
        return None
    ok = True
    for key in ("from", "to"):
        if entry.get(key) not in ctx.regions:
            diags.error("PT181", f"trade link '{name}': '{key}' must be one of the model regions", path + ((key,) if key in entry else ()),
                        hint=suggest(entry.get(key), ctx.regions))
            ok = False
    if not ok:
        return None
    if entry["from"] == entry["to"]:
        diags.error("PT182", "trade link must connect two different regions", path)
        return None
    t.commodity, t.region_from, t.region_to = c, entry["from"], entry["to"]
    t.regions = [t.region_from]
    t.bidirectional = entry.get("bidirectional", False)
    if not isinstance(t.bidirectional, bool):
        diags.error("PT183", "bidirectional must be true or false", path + ("bidirectional",))
        t.bidirectional = False
    t.level = ctx.commodities[c].level
    t.efficiency = parse_param(entry.get("efficiency", 1.0), kind="data", regions=t.regions, second=None,
                               path=path + ("efficiency",), diags=diags, lo=1e-6, hi=1.0)
    _parse_capacity(t, entry, ctx, path, ctx.commodities[c].unit)
    return t


def select_processes(selector: Any, techs: dict[str, Tech]) -> tuple[list[str], list[str]]:
    """Resolve names, globs and 'tag:xyz' selectors. Returns (matches, unmatched selectors)."""
    out: list[str] = []
    missing: list[str] = []
    for sel in _as_list(selector):
        sel = str(sel)
        if sel.startswith("tag:"):
            hits = [n for n, t in techs.items() if sel[4:] in t.tags]
        elif any(ch in sel for ch in "*?["):
            hits = [n for n in techs if fnmatch.fnmatchcase(n, sel)]
        else:
            hits = [sel] if sel in techs else []
        if not hits:
            missing.append(sel)
        out.extend(h for h in hits if h not in out)
    return out, missing


def _build_constraint(name: Any, entry: Any, ctx: _Ctx, techs: dict[str, Tech]) -> Constraint | None:
    path, diags = ("constraints", name), ctx.diags
    if not _name_ok(name, path, diags, "constraint"):
        return None
    if not isinstance(entry, dict):
        diags.error("PT006", f"constraint '{name}' must be a mapping", path)
        return None
    _check_keys(entry, CONSTRAINT_KEYS, path, diags, f"constraint '{name}'")
    sense = SENSES.get(str(entry.get("sense", "")))
    if sense is None:
        diags.error("PT190", "constraint needs sense: one of '<=', '>=', '='", path + (("sense",) if "sense" in entry else ()))
        return None
    reg_spec = entry.get("regions", "each")
    region_mode, regs = "each", list(ctx.regions)
    if reg_spec in ("each", "sum"):
        region_mode = reg_spec
    else:
        regs = [str(r) for r in _as_list(reg_spec)]
        bad = [r for r in regs if r not in ctx.regions]
        if bad:
            diags.error("PT191", f"unknown region(s) {bad}", path + ("regions",),
                        hint="use 'each' (one constraint per region), 'sum' (one constraint over all regions) or a list of regions.")
            return None
    period_mode = entry.get("periods", "each")
    if period_mode not in ("each", "sum"):
        diags.error("PT192", "periods must be 'each' (one constraint per period) or 'sum' (cumulative over the horizon)",
                    path + ("periods",))
        return None
    for key in ("from", "until"):
        if key in entry and not isinstance(entry[key], int):
            diags.error("PT193", f"'{key}' must be a year", path + (key,))
            return None
    rhs = parse_param(entry.get("rhs", 0), kind="bound", regions=ctx.regions, second=None,
                      path=path + ("rhs",), diags=diags)
    raw_terms = entry.get("terms")
    if not isinstance(raw_terms, list) or not raw_terms:
        diags.error("PT194", "constraint needs a non-empty list of 'terms'", path,
                    hint="e.g. terms: [{flow: ELC, of: ['tag:renewable'], coef: 1}, {production: ELC, coef: -0.4}]")
        return None
    terms: list[Term] = []
    for i, rt in enumerate(raw_terms):
        tp = path + ("terms", i)
        if not isinstance(rt, dict):
            diags.error("PT195", "each term must be a mapping", tp)
            continue
        kinds = [k for k in TERM_KINDS if k in rt]
        if len(kinds) != 1:
            diags.error("PT196", f"each term needs exactly one of: {', '.join(TERM_KINDS)}", tp)
            continue
        kind = kinds[0]
        _check_keys(rt, {kind, "of", "coef"}, tp, diags, "constraint term")
        coef = parse_param(rt.get("coef", 1), kind="data", regions=ctx.regions, second=None, path=tp + ("coef",), diags=diags)
        commodity, procs = None, []
        if kind in ("flow", "production", "consumption", "emission"):
            commodity = rt[kind]
            want = ("emission",) if kind == "emission" else ("energy", "material", "demand")
            if not _commodity_ref(commodity, ctx, tp + (kind,), want):
                continue
            if kind == "flow":
                if "of" not in rt:
                    diags.error("PT197", "a 'flow' term needs 'of:' (which processes); use 'production'/'consumption' for all processes", tp)
                    continue
                procs, missing = select_processes(rt["of"], techs)
            elif "of" in rt:
                diags.error("PT198", f"'of' is not used with '{kind}' terms", tp + ("of",))
                continue
            else:
                missing = []
        else:
            if "of" in rt:
                diags.error("PT198", f"for '{kind}' terms list the processes directly: {kind}: [A, B]", tp + ("of",))
                continue
            procs, missing = select_processes(rt[kind], techs)
        if missing:
            diags.error("PT199", f"selector(s) {missing} match no process", tp,
                        hint=suggest(missing[0], list(techs)) or "selectors are names, globs like 'WIND*', or 'tag:<tag>'.")
            continue
        if kind == "flow":
            for p in procs:
                t = techs[p]
                if t.kind == "process" and commodity not in t.commodities_in() and commodity not in t.commodities_out():
                    diags.error("PT200", f"process '{p}' has no flow of '{commodity}'", tp)
                elif t.kind != "process" and t.commodity != commodity:
                    diags.error("PT200", f"'{p}' does not handle '{commodity}'", tp)
        terms.append(Term(kind, procs, commodity, coef))
    if not terms:
        return None
    return Constraint(str(name), str(entry.get("description", "")), sense, rhs, region_mode, regs, period_mode,
                      entry.get("from"), entry.get("until"), terms)
