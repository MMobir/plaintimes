"""Static checks on a built model.

These catch, before any solve, the mistakes that otherwise surface as an
infeasible LP, a silently idle process or a wrong answer: commodities with no
source, dead-end outputs, share bounds that cannot hold, stock that is retired
before the model starts, demands that are silently extrapolated, and so on.
"""
from __future__ import annotations

from .diagnostics import Diagnostics
from .model import Model, Tech
from .timebase import ANNUAL


def topology(model: Model) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Per region: commodity -> {'producers': [...], 'consumers': [...]}."""
    out: dict[str, dict[str, dict[str, list[str]]]] = {
        r: {c: {"producers": [], "consumers": []} for c in model.commodities} for r in model.regions
    }
    for t in model.processes.values():
        for r in t.regions:
            for c in t.commodities_in():
                out[r][c]["consumers"].append(t.name)
            for c in t.commodities_out():
                out[r][c]["producers"].append(t.name)
    for t in model.storage.values():
        for r in t.regions:
            out[r][t.commodity]["consumers"].append(t.name)
            out[r][t.commodity]["producers"].append(t.name)
    for t in model.trade.values():
        pairs = [(t.region_from, t.region_to)] + ([(t.region_to, t.region_from)] if t.bidirectional else [])
        for src, dst in pairs:
            if t.name not in out[src][t.commodity]["consumers"]:
                out[src][t.commodity]["consumers"].append(t.name)
            if t.name not in out[dst][t.commodity]["producers"]:
                out[dst][t.commodity]["producers"].append(t.name)
    return out


def lint_model(model: Model, diags: Diagnostics) -> None:
    P, ts = model.periods, model.ts
    topo = topology(model)
    storages = set(model.storage)

    # -- commodities --------------------------------------------------------
    for c, com in model.commodities.items():
        used_anywhere = False
        for r in model.regions:
            node = topo[r][c]
            real_prod = [p for p in node["producers"] if p not in storages]
            real_cons = [p for p in node["consumers"] if p not in storages]
            used_anywhere |= bool(node["producers"] or node["consumers"])
            has_demand = bool(com.demand) and any((com.demand.at(r, M, default=0.0) or 0.0) > 0 for M in P.M)
            if has_demand and not real_prod:
                diags.error(
                    "PT300", f"demand '{c}' in region '{r}' has no process that can supply it", ("commodities", c),
                    hint=f"add a process with '{c}' in its outputs" + (f" and '{r}' in its regions." if len(model.regions) > 1 else "."))
            if real_cons and not real_prod and com.kind != "emission":
                diags.warn(
                    "PT301", f"commodity '{c}' is consumed in region '{r}' by {real_cons} but nothing produces it there",
                    ("commodities", c),
                    hint="those processes can never run. Add a supply process (an import or extraction with a 'varom' cost) or a trade link.")
            if real_prod and not real_cons and com.kind in ("energy", "material"):
                diags.warn(
                    "PT302", f"commodity '{c}' is produced in region '{r}' by {real_prod} but never consumed",
                    ("commodities", c), hint="is a consuming process missing, or should this be a demand?")
        if not used_anywhere:
            diags.warn("PT303", f"commodity '{c}' is not used by any process", ("commodities", c))
        if com.kind == "demand" and com.demand:
            for r in model.regions:
                s = com.demand.series(r)
                if s is not None and not s.scalar and s.points[-1][0] < P.M[-1]:
                    diags.warn(
                        "PT305", f"demand '{c}' is given only up to {s.points[-1][0]}; later periods use that last value",
                        ("commodities", c, "demand"),
                        hint=f"add a value for {P.M[-1]} (the last milestone) if demand should keep changing.")
                    break
            if com.level > 0 and not com.load_shape:
                diags.info("PT313", f"demand '{c}' is tracked by timeslice but has no load_shape; it is spread by hours",
                           ("commodities", c))
        if com.kind == "emission" and (com.cap or com.budget or com.tax):
            if not any(c in t.emissions for t in model.processes.values()):
                diags.warn("PT314", f"emission '{c}' has a cap, budget or tax but no process emits it", ("commodities", c),
                           hint="add 'emissions:' to the emitting processes (e.g. 'emissions: combustion').")
        if com.peak_reserve:
            firm = [t.name for t in model.processes.values() if t.has_capacity and c in t.primary_outputs]
            if not firm:
                diags.warn("PT315", f"'{c}' has a peak_reserve but no process with capacity has it as its main output",
                           ("commodities", c, "peak_reserve"))

    # -- technologies -------------------------------------------------------
    for t in model.techs():
        section = {"process": "processes", "storage": "storage", "trade": "trade"}[t.kind]
        path = (section, t.name)
        for r in t.regions:
            # stock that is gone before the model starts
            for year in list(t.existing.get(r, {})):
                life = t.lifetime.at(r, year, default=0.0) or 0.0
                if year + life <= P.first_year:
                    diags.warn(
                        "PT306", f"existing capacity of '{t.name}' built in {year} (lifetime {life:g}) is retired before {P.first_year}",
                        path + ("existing",), hint="it contributes nothing; remove it or check the vintage year and lifetime.")
            if t.can_invest:
                from .kernel import Builder

                bld = Builder(model)
                for i, M in enumerate(P.M):
                    life = t.lifetime.at(r, M, default=0.0) or 0.0
                    if life > 0 and bld.uncharged_builds(life, i):
                        diags.warn(
                            "PT321", f"the first build of '{t.name}' in period {M} is not charged: its lifetime ({life:g} y) is "
                                     f"shorter than the period, and the period starts in {P.B[i]}, before its milestone year",
                            path + ("lifetime",),
                            hint="official TIMES leaves this build out of its cost equations, and plaintimes reproduces "
                                 "that so the two agree, but it makes the technology look cheaper than it is. Avoid it by "
                                 "starting the first period at its milestone year (drop periods.start) or by using "
                                 "periods: {start, lengths}.")
                        break
                for i, M in enumerate(P.M):
                    life = t.lifetime.at(r, M, default=0.0) or 0.0
                    if 0 < life < P.D[i]:
                        diags.info(
                            "PT307", f"lifetime of '{t.name}' ({life:g} y) is shorter than period {M} ({P.D[i]} y): "
                                     "the investment is repeated within the period", path + ("lifetime",))
                        break
            if t.has_capacity and t.af_max:
                vals = []
                for s in ts.at_level(t.level):
                    v = None
                    for a in ts.ancestors(s):
                        if a != ANNUAL and t.af_max.has(r, a):
                            v = t.af_max.at(r, P.M[0], a)
                            break
                    if v is None and t.af_max.has(r, None):
                        v = t.af_max.at(r, P.M[0], None)
                    vals.append(1.0 if v is None else v)
                if vals and max(vals) <= 0:
                    diags.warn("PT309", f"'{t.name}' has zero availability in every timeslice", path + ("availability",))
            # a capacity ceiling below stock that already exists cannot be met
            if ("capacity", "max") in t.bounds or ("capacity", "fix") in t.bounds:
                from .kernel import Builder

                b = Builder(model)
                for i, M in enumerate(P.M):
                    old = sum(cap * b.cpt(year, year + (t.lifetime.at(r, year, default=30.0) or 30.0), i)
                              for year, cap in t.existing.get(r, {}).items())
                    old += (t.residual.at(r, M) or 0.0) if t.residual else 0.0
                    for kind in ("max", "fix"):
                        p = t.bounds.get(("capacity", kind))
                        lim = p.at(r, M) if p else None
                        if lim is not None and old > lim + 1e-9:
                            diags.error(
                                "PT320", f"capacity {kind} of '{t.name}' in {M} ({lim:g}) is below the existing stock still alive then ({old:.4g})",
                                path + ("bounds", "capacity"),
                                hint="existing stock cannot be retired early, so this bound makes the model infeasible. "
                                     "(Official TIMES silently relaxes such a bound; here you must fix the data.)")
        if t.kind != "process":
            continue
        _lint_process(model, t, diags, path)

    # -- constraints --------------------------------------------------------
    for name, uc in model.constraints.items():
        if uc.period_mode == "each":
            reg = uc.regions[0] if uc.region_mode == "each" else None
            active = [M for i, M in enumerate(P.M) if uc.rhs.at(reg, M) is not None
                      and not (uc.year_from and P.E[i] < uc.year_from) and not (uc.year_until and P.B[i] > uc.year_until)]
            if not active:
                diags.warn(
                    "PT318", f"constraint '{name}' is never active: its rhs has no value at any milestone year {P.M}",
                    ("constraints", name, "rhs"),
                    hint="rhs year series apply only between their first and last year; use a plain number for 'every period'.")


def _lint_process(model: Model, t: Tech, diags: Diagnostics, path: tuple) -> None:
    P = model.periods
    coms = model.commodities
    r0 = t.regions[0] if t.regions else None
    if r0 is None:
        return
    # efficiency above one between energy carriers
    energy_in = [c for c in t.commodities_in() if coms[c].kind == "energy"]
    energy_out = [c for c in list(t.outputs) + list(t.flex_out) if coms[c].kind in ("energy", "demand")]
    if energy_in and energy_out and len(t.commodities_in()) == len(energy_in):
        for M in (P.M[0], P.M[-1]):
            eff = t.efficiency.at(r0, M, default=1.0) if t.efficiency else 1.0
            total_in = sum(p.at(r0, M, default=1.0) for p in t.inputs.values()) / eff if t.inputs else 0.0
            if t.flex_in:
                effs = [(m.efficiency.at(r0, M, default=eff) if m.efficiency else eff) for m in t.flex_in.values()]
                total_in += 1.0 / max(effs)
            total_out = sum(p.at(r0, M, default=1.0) for p in t.outputs.values()) + (1.0 if t.flex_out else 0.0)
            same_units = len({coms[c].unit for c in energy_in + energy_out}) == 1
            if same_units and total_in > 0 and total_out / total_in > 1.0 + 1e-9:
                diags.warn(
                    "PT304", f"process '{t.name}' turns 1 unit of input into {total_out / total_in:.3g} units of output ({M})",
                    path, hint="fine for heat pumps (ambient heat not modelled); otherwise check efficiency and coefficients.")
                break
    # share bounds that cannot hold
    for grp, key in ((t.flex_in, "flexible_inputs"), (t.flex_out, "flexible_outputs")):
        if not grp:
            continue
        for M in P.M:
            lo = sum((m.min_share.at(r0, M, default=0.0) if m.min_share else 0.0) for m in grp.values())
            hi = sum((m.max_share.at(r0, M, default=1.0) if m.max_share else 1.0) for m in grp.values())
            if lo > 1.0 + 1e-9:
                diags.error("PT310", f"min_share values in {key} of '{t.name}' add up to {lo:.3g} > 1 ({M})", path + (key,))
                break
            if hi < 1.0 - 1e-9:
                diags.error("PT310", f"max_share values in {key} of '{t.name}' add up to {hi:.3g} < 1 ({M}); the group can never be filled",
                            path + (key,))
                break
    # money for nothing
    if t.varom and not t.has_capacity and ("activity", "max") not in t.bounds and ("activity", "fix") not in t.bounds \
            and "max" not in t.cumulative_activity:
        for M in P.M:
            if (t.varom.at(r0, M, default=0.0) or 0.0) < 0 and not t.commodities_in():
                diags.warn("PT317", f"'{t.name}' earns money (negative varom) without inputs, capacity or an activity bound",
                           path + ("varom",), hint="the model may be unbounded; add bounds.activity.max.")
                break
    if t.can_invest and t.invcost and not t.fixom and all((t.invcost.at(r0, M, default=0.0) or 0.0) == 0 for M in P.M):
        diags.info("PT319", f"'{t.name}' can be built for free (invcost 0, no fixom)", path + ("invcost",))
