"""The GAMS bridge: write a model as .dd files for the official ETSAP TIMES code.

The same YAML model that the native kernel solves is exported as
  <name>_ts.dd   timeslice declarations (must be read first)
  <name>.dd      all sets and parameters
  <name>.run     a run file for the TIMES source (maindrv.mod)
so that the official TIMES generator can be used as a validation oracle, or
as the production engine when a model needs extensions the native kernel
does not have.

Mapping (model key -> TIMES attribute) is listed in ATTRIBUTE_MAP and printed
by `plaintimes schema --times-map`.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

from .model import Model, Tech
from .params import Param
from .timebase import ANNUAL, TIMES_LEVEL

ATTRIBUTE_MAP = [
    ("periods", "MILESTONYR, B, E", "period definitions"),
    ("timeslices", "ALL_TS, TS_GROUP, TS_MAP, G_YRFR", "timeslice tree and year fractions"),
    ("discount_rate / discount_year", "G_DRATE / G_DYEAR", ""),
    ("mid_year_discounting", "$SET MID_YEAR YES", "run-file switch"),
    ("commodities.<c>.kind", "COM_TMAP (NRG, MAT, DEM, ENV)", ""),
    ("commodities.<c>.timeslice", "COM_TSL", ""),
    ("commodities.<c>.balance", "COM_LIM", "'>=' -> LO, '=' -> FX"),
    ("commodities.<c>.demand", "COM_PROJ", ""),
    ("commodities.<c>.load_shape", "COM_FR", ""),
    ("commodities.<c>.efficiency", "COM_IE", ""),
    ("commodities.<c>.peak_reserve", "COM_PEAK, COM_PKRSV", ""),
    ("commodities.<c>.peak_timeslices", "COM_PKTS", ""),
    ("commodities.<e>.cap", "COM_BNDNET (UP)", ""),
    ("commodities.<e>.budget", "COM_CUMNET (UP)", ""),
    ("commodities.<e>.tax", "COM_TAXNET", ""),
    ("processes.<p>.inputs / outputs", "TOP, PRC_ACTUNT, PRC_ACTFLO, ACT_FLO", "first output is the primary group"),
    ("processes.<p>.efficiency", "ACT_FLO (inputs) / ACT_EFF (flexible group)", ""),
    ("processes.<p>.flexible_inputs / flexible_outputs", "COM_GMAP, ACT_EFF, FLO_SHAR", ""),
    ("processes.<p>.timeslice", "PRC_TSL", ""),
    ("processes.<p>.capacity_unit / cap2act", "PRC_CAPACT", ""),
    ("processes.<p>.lifetime", "NCAP_TLIFE", ""),
    ("processes.<p>.economic_life", "NCAP_ELIFE", ""),
    ("processes.<p>.hurdle_rate", "NCAP_DRATE", ""),
    ("processes.<p>.invcost", "NCAP_COST", "absent -> NCAP_BND UP 0 (no investment)"),
    ("processes.<p>.fixom", "NCAP_FOM", ""),
    ("processes.<p>.varom", "ACT_COST", ""),
    ("processes.<p>.availability / _min / _fix", "NCAP_AF (UP / LO / FX)", ""),
    ("processes.<p>.capacity_factor / _min", "NCAP_AFA, NCAP_AFS (UP / LO)", ""),
    ("processes.<p>.existing", "NCAP_PASTI", ""),
    ("processes.<p>.residual", "PRC_RESID", ""),
    ("processes.<p>.vintaged", "PRC_VINT", ""),
    ("processes.<p>.emissions", "FLO_EMIS", ""),
    ("processes.<p>.peak_contribution", "NCAP_PKCNT", ""),
    ("processes.<p>.bounds.new_capacity", "NCAP_BND", ""),
    ("processes.<p>.bounds.capacity", "CAP_BND", ""),
    ("processes.<p>.bounds.activity", "ACT_BND (ANNUAL)", ""),
    ("processes.<p>.bounds.cumulative_activity", "ACT_CUM", ""),
    ("constraints.<u>", "UC_N, UC_R_EACH/SUM, UC_T_EACH/SUM, UC_FLO, UC_COMPRD, UC_COMCON, UC_COMNET, UC_CAP, UC_NCAP, UC_ACT, UC_RHS*", ""),
    ("storage.<s>", "(not exported yet)", "native kernel only"),
    ("trade.<l>", "(not exported yet)", "native kernel only"),
]

BD = {"max": "UP", "min": "LO", "fix": "FX"}
LIM = {"<=": "UP", ">=": "LO", "=": "FX"}


def _num(x: float) -> str:
    if x in (float("inf"), float("-inf")):
        return "INF" if x > 0 else "-INF"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.12g}"


def _unit(u: str) -> str:
    """A unit label that is safe as a GAMS set element ('PJ/yr' -> 'PJa')."""
    u = u.strip()
    for suffix in ("/yr", "/y", "/a"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)] + "a"
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in u) or "UNIT"


class _DD:
    def __init__(self):
        self.sets: dict[str, list[str]] = {}
        self.params: dict[str, list[str]] = {}

    def s(self, name: str, *key: Any, text: str | None = None) -> None:
        line = ".".join(str(k) for k in key)
        if text:
            line += f" '{text}'"
        rows = self.sets.setdefault(name, [])
        if line not in rows:
            rows.append(line)

    def p(self, name: str, key: Iterable[Any], value: float) -> None:
        self.params.setdefault(name, []).append(".".join(str(k) for k in key) + " " + _num(value))

    # GAMS needs a set to be filled before its subsets, so sets are written in this order
    SET_ORDER = [
        "ALL_REG", "REG", "CUR", "DATAYEAR", "MODLYEAR", "PASTYEAR", "TS_GROUP", "TS_MAP",
        "UNITS", "UNITS_COM", "UNITS_CAP", "UNITS_ACT", "UNITS_MONY",
        "COM_GRP", "COM", "COM_GMAP", "COM_TMAP", "COM_TSL", "COM_LIM", "COM_UNIT", "COM_PEAK", "COM_PKTS",
        "PRC", "PRC_MAP", "PRC_TSL", "PRC_ACTUNT", "PRC_VINT", "TOP",
        "UC_N", "UC_R_EACH", "UC_R_SUM", "UC_T_SUM",
    ]

    def render(self, header: list[str]) -> str:
        out = list(header)
        ordered = [n for n in self.SET_ORDER if n in self.sets] + [n for n in self.sets if n not in self.SET_ORDER]
        for name in ordered:
            rows = self.sets[name]
            out += [f"SET {name}", "/"] + rows + ["/", ""]
        for name, rows in self.params.items():
            out += ["PARAMETER", f"{name} ' '/"] + rows + ["/", ""]
        return "\n".join(out) + "\n"


def unsupported(model: Model) -> list[str]:
    """Features of this model that the bridge cannot export yet."""
    out = []
    for name in model.storage:
        out.append(f"storage.{name}: storage is not exported yet")
    for name in model.trade:
        out.append(f"trade.{name}: trade links are not exported yet")
    for uc in model.constraints.values():
        for term in uc.terms:
            for p in term.processes:
                if p in model.storage or p in model.trade:
                    out.append(f"constraints.{uc.name}: refers to storage/trade '{p}'")
    return out


def export_dd(model: Model, out_dir: str, name: str | None = None, solver: str | None = None,
              allow_partial: bool = False) -> dict[str, Any]:
    """Write <name>_ts.dd, <name>.dd and <name>.run into out_dir."""
    # GAMS on Linux is case-sensitive and the TIMES source uses lower-case file names throughout
    name = (name or model.name).replace(" ", "_").lower()
    missing = unsupported(model)
    if missing and not allow_partial:
        raise NotImplementedError("the model uses features the GAMS bridge cannot export yet:\n  " + "\n  ".join(missing))
    os.makedirs(out_dir, exist_ok=True)
    P, ts, cur = model.periods, model.ts, model.currency
    dd = _DD()
    regions = model.regions
    milestones = P.M

    # years -------------------------------------------------------------
    past_years = sorted({y for t in model.processes.values() for ex in t.existing.values() for y in ex})
    model_years = past_years + milestones

    def dense(param: Param, r: str, years: Iterable[int], second: str | None = None):
        """Values at the given years (explicit, so TIMES interpolation defaults never matter)."""
        for y in years:
            v = param.at(r, y, second)
            if v is not None:
                yield y, v

    def points(param: Param, r: str, second: str | None = None):
        """Original data points (for cost series, which TIMES interpolates year by year)."""
        s = param.series(r, second)
        if s is None:
            return
        if s.scalar:
            yield milestones[0], s.points[0][1]
        else:
            yield from s.points

    # global sets ---------------------------------------------------------
    for r in regions:
        dd.s("ALL_REG", r)
        dd.s("REG", r)
    dd.s("CUR", cur)
    data_years = set(model_years)
    for y in model_years:
        dd.s("MODLYEAR", y)
    for y in past_years:
        dd.s("PASTYEAR", y)
    dd.sets.setdefault("PASTYEAR", [])
    for r in regions:
        for s in ts.names:
            lvl = TIMES_LEVEL[ts.levels[ts.level_of[s]]]
            dd.s("TS_GROUP", r, lvl, s)
            par = ts.parent[s]
            if par and par != ANNUAL:
                dd.s("TS_MAP", r, par, s)
            if s != ANNUAL:
                dd.p("G_YRFR", (r, s), ts.frac[s])
        dd.p("G_DRATE", (r, milestones[0], cur), model.discount_rate)
    dd.sets.setdefault("TS_MAP", [])
    dd.s("UNITS_MONY", cur)
    dd.s("UNITS", cur)
    dd.s("UNITS", "UNIT")
    dd.s("UNITS_ACT", "UNIT")

    # emissions that some process removes (negative factor): their net total may go below zero
    removals = set()
    for t in model.processes.values():
        for e, bases in t.emissions.items():
            for factor in bases.values():
                if any(v < 0 for series in factor.table.values() for _, v in series.points):
                    removals.add(e)

    # commodities ---------------------------------------------------------
    type_of = {"energy": "NRG", "material": "MAT", "demand": "DEM", "emission": "ENV"}
    for c, com in model.commodities.items():
        dd.s("COM_GRP", c)
        dd.s("COM", c)
        if com.unit:
            dd.s("UNITS", _unit(com.unit))
            dd.s("UNITS_COM", _unit(com.unit))
        for r in regions:
            dd.s("COM_TMAP", r, type_of[com.kind], c)
            if com.unit:
                dd.s("COM_UNIT", r, c, _unit(com.unit))
            if com.level:
                dd.s("COM_TSL", r, c, TIMES_LEVEL[ts.levels[com.level]])
            default_sense = "=" if com.kind == "material" else ">="
            if com.sense != default_sense:
                dd.s("COM_LIM", r, c, "FX" if com.sense == "=" else "LO")
            if com.demand:
                for y, v in dense(com.demand, r, milestones):
                    dd.p("COM_PROJ", (r, y, c), v)
            if com.load_shape:
                for s, v in com.load_shape.items():
                    dd.p("COM_FR", (r, milestones[0], c, s), v)
            if com.efficiency:
                for y, v in dense(com.efficiency, r, milestones):
                    dd.p("COM_IE", (r, y, c, ANNUAL), v)
            if com.peak_reserve:
                dd.s("COM_PEAK", r, c)
                for y, v in dense(com.peak_reserve, r, milestones):
                    dd.p("COM_PKRSV", (r, y, c), v)
                for s in com.peak_slices or []:
                    dd.s("COM_PKTS", r, c, s)
            if com.cap:
                for y, v in dense(com.cap, r, milestones):
                    dd.p("COM_BNDNET", (r, y, c, ANNUAL, "UP"), v)
            if com.kind == "emission" and c in removals:
                # TIMES keeps net emission variables non-negative unless told otherwise
                for y in milestones:
                    dd.p("COM_BNDNET", (r, y, c, ANNUAL, "LO"), float("-inf"))
            budget = com.budget.get(r, com.budget.get(None))
            if budget is not None:
                dd.p("COM_CUMNET", (r, P.first_year, P.last_year, c, "UP"), budget)
            if com.tax:
                for y, v in points(com.tax, r):
                    dd.p("COM_TAXNET", (r, y, c, ANNUAL, cur), v)
                    data_years.add(y)

    # processes -----------------------------------------------------------
    for p, t in model.processes.items():
        dd.s("PRC", p)
        prim = t.primary_outputs
        is_dmd = any(model.commodities[c].kind == "demand" for c in prim)
        pcg = prim[0] if not t.flex_out else f"{p}_FO"
        fi = f"{p}_FI"
        if t.flex_out:
            dd.s("COM_GRP", pcg)
        if t.flex_in:
            dd.s("COM_GRP", fi)
        if t.act_unit:
            dd.s("UNITS", _unit(t.act_unit))
            dd.s("UNITS_ACT", _unit(t.act_unit))
        if t.has_capacity and t.cap_unit:
            dd.s("UNITS", _unit(t.cap_unit))
            dd.s("UNITS_CAP", _unit(t.cap_unit))
        for r in t.regions:
            years = list(milestones)
            if t.vintaged:
                # vintage-indexed data is also needed for old stock; residual stock is treated by
                # TIMES as one vintage commissioned the year before the horizon
                extra = set(t.existing.get(r, {}))
                if t.residual and t.residual.has(r):
                    extra.add(P.first_year - 1)
                years = sorted(extra | set(milestones))
            dd.s("PRC_MAP", r, "DMD" if is_dmd else "PRE", p)
            dd.s("PRC_TSL", r, p, TIMES_LEVEL[ts.levels[t.level]])
            dd.s("PRC_ACTUNT", r, p, pcg, _unit(t.act_unit) if t.act_unit else "UNIT")
            if t.vintaged:
                dd.s("PRC_VINT", r, p)
            for c in t.inputs:
                dd.s("TOP", r, p, c, "IN")
            for c in t.outputs:
                dd.s("TOP", r, p, c, "OUT")
            for c, mem in t.flex_in.items():
                dd.s("TOP", r, p, c, "IN")
                dd.s("COM_GMAP", r, fi, c)
            for c in t.flex_out:
                dd.s("TOP", r, p, c, "OUT")
                dd.s("COM_GMAP", r, pcg, c)
            for e in t.emissions:
                dd.s("TOP", r, p, e, "OUT")
            # transformation -------------------------------------------------
            for y in years:
                eff = t.efficiency.at(r, y, default=1.0) if t.efficiency else 1.0
                for k, (c, coef) in enumerate(t.outputs.items()):
                    v = coef.at(r, y, default=1.0)
                    if k == 0 and not t.flex_out:
                        if v != 1.0:
                            dd.p("PRC_ACTFLO", (r, y, p, c), v)
                    else:
                        dd.p("ACT_FLO", (r, y, p, c, ANNUAL), v)
                for c, coef in t.inputs.items():
                    dd.p("ACT_FLO", (r, y, p, c, ANNUAL), coef.at(r, y, default=1.0) / eff)
                if t.flex_in:
                    dd.p("ACT_EFF", (r, y, p, fi, ANNUAL), 1.0)
                    for c, mem in t.flex_in.items():
                        e_c = mem.efficiency.at(r, y, default=eff) if mem.efficiency else eff
                        dd.p("ACT_EFF", (r, y, p, c, ANNUAL), e_c)
                for grp, gname in ((t.flex_in, fi), (t.flex_out, pcg)):
                    for c, mem in grp.items():
                        for param, bd in ((mem.max_share, "UP"), (mem.min_share, "LO")):
                            v = param.at(r, y) if param else None
                            if v is not None:
                                dd.p("FLO_SHAR", (r, y, p, c, gname, ANNUAL, bd), v)
                for e, bases in t.emissions.items():
                    for basis, factor in bases.items():
                        v = factor.at(r, y, default=0.0)
                        dd.p("FLO_EMIS", (r, y, p, "ACT" if basis == "activity" else basis, e, ANNUAL), v)
            # costs (original points; TIMES interpolates costs for every year) --
            for param, attr in ((t.invcost, "NCAP_COST"), (t.fixom, "NCAP_FOM"), (t.varom, "ACT_COST")):
                for y, v in points(param, r):
                    dd.p(attr, (r, y, p, cur), v)
                    data_years.add(y)
            if not t.has_capacity:
                continue
            # capacity ---------------------------------------------------------
            dd.p("PRC_CAPACT", (r, p), t.cap2act)
            cap_years = sorted(set(t.existing.get(r, {})) | set(milestones))
            for param, attr in ((t.lifetime, "NCAP_TLIFE"), (t.economic_life, "NCAP_ELIFE"), (t.hurdle_rate, "NCAP_DRATE")):
                if param:
                    for y, v in dense(param, r, cap_years):
                        dd.p(attr, (r, y, p), v)
            if t.peak_contribution:
                for y, v in dense(t.peak_contribution, r, cap_years):
                    dd.p("NCAP_PKCNT", (r, y, p, ANNUAL), v)
            for y, v in sorted(t.existing.get(r, {}).items()):
                dd.p("NCAP_PASTI", (r, y, p), v)
            if t.residual and t.residual.has(r):
                # every milestone gets a value: TIMES would otherwise let a single point decay
                # linearly over the lifetime instead of ending where the profile ends
                for y in milestones:
                    dd.p("PRC_RESID", (r, y, p), t.residual.at(r, y) or 0.0)
            if not t.can_invest:
                for y in milestones:
                    dd.p("NCAP_BND", (r, y, p, "UP"), 0.0)
            # availability
            slices = ts.at_level(t.level)

            def af_value(param: Param, s: str, y: int):
                for a in ts.ancestors(s):
                    if a == ANNUAL:
                        break
                    if param.has(r, a):
                        return param.at(r, y, a)
                return param.at(r, y, None) if param.has(r, None) else None

            for y in years:
                for s in slices:
                    if t.af_fix and af_value(t.af_fix, s, y) is not None:
                        dd.p("NCAP_AF", (r, y, p, s, "FX"), af_value(t.af_fix, s, y))
                        continue
                    v = af_value(t.af_max, s, y) if t.af_max else None
                    dd.p("NCAP_AF", (r, y, p, s, "UP"), 1.0 if v is None else v)
                    if t.af_min:
                        lo = af_value(t.af_min, s, y)
                        if lo:
                            dd.p("NCAP_AF", (r, y, p, s, "LO"), lo)
                for param, bd in ((t.cf_max, "UP"), (t.cf_min, "LO")):
                    if not param:
                        continue
                    if param.has(r, None):
                        dd.p("NCAP_AFA", (r, y, p, bd), param.at(r, y, None))
                    for g in param.seconds():
                        if param.has(r, g):
                            dd.p("NCAP_AFS", (r, y, p, g, bd), param.at(r, y, g))
            # bounds
            for (what, kind), param in t.bounds.items():
                for y, v in dense(param, r, milestones):
                    if what == "new_capacity":
                        dd.p("NCAP_BND", (r, y, p, BD[kind]), v)
                    elif what == "capacity":
                        dd.p("CAP_BND", (r, y, p, BD[kind]), v)
        # bounds that also apply to capacity-free processes
        for r in t.regions:
            for (what, kind), param in t.bounds.items():
                if what == "activity":
                    for y, v in dense(param, r, milestones):
                        dd.p("ACT_BND", (r, y, p, ANNUAL, BD[kind]), v)
            for kind, table in t.cumulative_activity.items():
                v = table.get(r, table.get(None))
                if v is not None:
                    dd.p("ACT_CUM", (r, p, P.first_year, P.last_year, BD[kind]), v)

    # user constraints ----------------------------------------------------
    for uc in model.constraints.values():
        _export_uc(dd, model, uc)

    for y in sorted(data_years):
        dd.s("DATAYEAR", y)
    for i, M in enumerate(milestones):
        dd.p("B", (M,), P.B[i])
        dd.p("E", (M,), P.E[i])
    dd.params["G_DYEAR"] = [str(model.discount_year)]

    header = ["$ONEMPTY", "$ONEPS", "$ONWARNING", f"$SET RUN_NAME '{name}'", "$SET SCENARIO_NAME 'plaintimes'", ""]
    ts_file = os.path.join(out_dir, f"{name}_ts.dd")
    with open(ts_file, "w") as fh:
        fh.write("* timeslices must be declared first so that their order is right\nSET ALL_TS\n/\n")
        fh.write("\n".join(f" {s}" for s in ts.names) + "\n/\n")
    dd_file = os.path.join(out_dir, f"{name}.dd")
    with open(dd_file, "w") as fh:
        fh.write(dd.render(header))
    run_file = os.path.join(out_dir, f"{name}.run")
    botime = min([1960] + [y - 1 for y in past_years])
    with open(run_file, "w") as fh:
        fh.write(_run_file(name, milestones, botime, model.mid_year, solver))
    with open(os.path.join(out_dir, "README.txt"), "w") as fh:
        fh.write(_readme(name, missing))
    return {"dd": dd_file, "ts": ts_file, "run": run_file, "name": name, "not_exported": missing,
            "sets": {k: len(v) for k, v in dd.sets.items()}, "parameters": {k: len(v) for k, v in dd.params.items()}}


def _export_uc(dd: _DD, model: Model, uc) -> None:
    P = model.periods
    name = uc.name
    dd.s("UC_N", name, text=uc.description[:60].replace("'", "") if uc.description else None)
    each_r = uc.region_mode == "each"
    each_t = uc.period_mode == "each"
    for r in uc.regions:
        dd.s("UC_R_EACH" if each_r else "UC_R_SUM", r, name)
    active = []
    for i, M in enumerate(P.M):
        if (uc.year_from and P.E[i] < uc.year_from) or (uc.year_until and P.B[i] > uc.year_until):
            continue
        if each_t and all(uc.rhs.at(r if each_r else None, M) is None for r in uc.regions):
            continue
        active.append(i)
    for i in active:
        M = P.M[i]
        if not each_t:
            for r in uc.regions:
                dd.s("UC_T_SUM", r, name, M)
        # (for constraints summed over periods TIMES itself weights flow, activity and
        #  commodity terms by the period length, exactly as the native kernel does)
        for term in uc.terms:
            for r in uc.regions:
                coef = term.coef.at(r, M, default=0.0)
                if not coef:
                    continue
                if term.kind == "flow":
                    for p in term.processes:
                        dd.p("UC_FLO", (name, "LHS", r, M, p, term.commodity, ANNUAL), coef)
                elif term.kind == "production":
                    dd.p("UC_COMPRD", (name, "LHS", r, M, term.commodity, ANNUAL), coef)
                elif term.kind == "consumption":
                    dd.p("UC_COMCON", (name, "LHS", r, M, term.commodity, ANNUAL), coef)
                elif term.kind == "emission":
                    dd.p("UC_COMNET", (name, "LHS", r, M, term.commodity, ANNUAL), coef)
                else:
                    attr = {"capacity": "UC_CAP", "new_capacity": "UC_NCAP", "activity": "UC_ACT"}[term.kind]
                    for p in term.processes:
                        if r not in model.tech(p).regions:
                            continue
                        key = (name, "LHS", r, M, p) + ((ANNUAL,) if attr == "UC_ACT" else ())
                        dd.p(attr, key, coef)
        if each_t:
            if each_r:
                for r in uc.regions:
                    v = uc.rhs.at(r, M)
                    if v is not None:
                        dd.p("UC_RHSRT", (r, name, M, LIM[uc.sense]), v)
            else:
                dd.p("UC_RHST", (name, M, LIM[uc.sense]), uc.rhs.at(None, M))
    if not each_t:
        last = P.M[-1]
        if each_r:
            for r in uc.regions:
                v = uc.rhs.at(r, last)
                dd.p("UC_RHSR", (r, name, LIM[uc.sense]), v if v is not None else (uc.rhs.at(r, P.M[0]) or 0.0))
        else:
            v = uc.rhs.at(None, last)
            dd.p("UC_RHS", (name, LIM[uc.sense]), v if v is not None else (uc.rhs.at(None, P.M[0]) or 0.0))


def _run_file(name: str, milestones: list[int], botime: int, mid_year: bool, solver: str | None) -> str:
    lines = [
        "$TITLE  TIMES run generated by plaintimes",
        "OPTION RESLIM=50000, PROFILE=1, SOLVEOPT=REPLACE;",
        "OPTION ITERLIM=999999, LIMROW=0, LIMCOL=0, SOLPRINT=OFF;",
        "$OFFLISTING",
        f"option LP={solver};" if solver else "* option LP=<your solver>;   (GAMS default LP solver is used otherwise)",
        "$SET REDUCE   YES",
        "$SET DSCAUTO  YES",
        "$SET VDA      YES",
        "$SET DEBUG    NO",
        "$SET DUMPSOL  NO",
        "$SET SOLVE_NOW YES",
        "$SET XTQA     YES",
        "$SET VAR_UC   YES",
        "* objective conventions matched by the plaintimes native kernel:",
        "$SET OBJ      AUTO",
        "$SET OBLONG   YES",
    ]
    if mid_year:
        lines.append("$SET MID_YEAR YES")
    lines += [
        "$SET SOLVEDA  YES",
        "$SET DATAGDX  YES",
        "OPTION BRATIO=1;",
        "$ONMULTI",
        "* the timeslices must come first to ensure their ordering",
        f"$BATINCLUDE {name}_ts.dd",
        f"$SET BOTIME {botime}",
        "$BATINCLUDE initsys.mod",
        "$BATINCLUDE initmty.mod",
        f"$BATINCLUDE {name}.dd",
        "SET MILESTONYR / " + ", ".join(str(y) for y in milestones) + " /;",
        f"$SET RUN_NAME {name}",
        "$SET VEDAVDD YES",
        "$BATINCLUDE maindrv.mod mod",
        "",
    ]
    return "\n".join(lines)


def _readme(name: str, missing: list[str]) -> str:
    text = f"""TIMES input files generated by plaintimes
=========================================

  {name}_ts.dd   timeslices
  {name}.dd      sets and parameters
  {name}.run     run file

To solve with the official ETSAP TIMES code (https://github.com/etsap-TIMES/TIMES_model):

  gams {name}.run idir1=. idir2=<path to TIMES_model> ps=0 gdx={name}

or let plaintimes do it and compare with the native kernel:

  plaintimes oracle <model dir> --times-src <path to TIMES_model>
"""
    if missing:
        text += "\nNOT exported (native-kernel-only features):\n" + "\n".join(f"  - {m}" for m in missing) + "\n"
    return text
