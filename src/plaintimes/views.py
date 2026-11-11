"""Read-only views of a model: what one entity resolves to, and the whole RES."""
from __future__ import annotations

import os
from typing import Any

from .kernel import Builder
from .lint import topology
from .model import Model, Tech
from .timebase import ANNUAL


def _r(x: float | None, digits: int = 6) -> float | None:
    return None if x is None else float(f"{x:.{digits}g}")


def _where(model: Model, path: tuple) -> str | None:
    loc = model.raw.locs.get(path)
    if not loc:
        return None
    return f"{os.path.relpath(loc[0], model.raw.root)}:{loc[1]}"


def show(model: Model, name: str) -> dict[str, Any]:
    """Everything the model knows about one commodity, process, storage, trade link or constraint."""
    if name in model.commodities:
        return _show_commodity(model, name)
    t = model.tech(name)
    if t is not None:
        return _show_tech(model, t)
    if name in model.constraints:
        uc = model.constraints[name]
        P = model.periods
        return {
            "name": name, "type": "constraint", "defined_at": _where(model, ("constraints", name)),
            "description": uc.description, "sense": uc.sense, "regions": uc.region_mode, "periods": uc.period_mode,
            "terms": [{"kind": term.kind, "commodity": term.commodity, "processes": term.processes,
                       "coef": {M: _r(term.coef.at(model.regions[0], M, default=0.0)) for M in P.M}} for term in uc.terms],
            "rhs": {M: _r(uc.rhs.at(uc.regions[0] if uc.region_mode == "each" else None, M)) for M in P.M},
        }
    import difflib

    names = list(model.commodities) + [t.name for t in model.techs()] + list(model.constraints)
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
    raise KeyError(f"nothing called '{name}' in the model" + (f"; did you mean {', '.join(close)}?" if close else ""))


def _show_commodity(model: Model, name: str) -> dict[str, Any]:
    com, P, ts = model.commodities[name], model.periods, model.ts
    topo = topology(model)
    out: dict[str, Any] = {
        "name": name, "type": "commodity", "kind": com.kind, "unit": com.unit,
        "defined_at": _where(model, ("commodities", name)), "description": com.description,
        "timeslice_level": ts.levels[com.level], "balance": com.sense, "regions": {},
    }
    if com.load_shape:
        out["load_shape"] = {k: _r(v) for k, v in com.load_shape.items()}
    for r in model.regions:
        rec: dict[str, Any] = {"producers": topo[r][name]["producers"], "consumers": topo[r][name]["consumers"]}
        if com.demand:
            rec["demand"] = {M: _r(com.demand.at(r, M, default=0.0)) for M in P.M}
        if com.cap:
            rec["cap"] = {M: _r(com.cap.at(r, M)) for M in P.M}
        if com.tax:
            rec["tax"] = {M: _r(com.tax.at(r, M, default=0.0)) for M in P.M}
        if com.budget:
            rec["budget"] = com.budget.get(r, com.budget.get(None))
        if com.peak_reserve:
            rec["peak_reserve"] = {M: _r(com.peak_reserve.at(r, M)) for M in P.M}
        if com.efficiency:
            rec["efficiency"] = {M: _r(com.efficiency.at(r, M, default=1.0)) for M in P.M}
        out["regions"][r] = rec
    return out


def _show_tech(model: Model, t: Tech) -> dict[str, Any]:
    P, ts = model.periods, model.ts
    b = Builder(model)
    section = {"process": "processes", "storage": "storage", "trade": "trade"}[t.kind]
    topo = topology(model)
    out: dict[str, Any] = {
        "name": t.name, "type": t.kind, "defined_at": _where(model, (section, t.name)),
        "description": t.description, "tags": t.tags, "timeslice_level": ts.levels[t.level],
        "has_capacity": t.has_capacity, "can_invest": t.can_invest, "vintaged": t.vintaged,
    }
    if t.kind == "trade":
        out.update({"commodity": t.commodity, "from": t.region_from, "to": t.region_to, "bidirectional": t.bidirectional})
    if t.kind == "storage":
        out.update({"commodity": t.commodity, "cycle_level": ts.levels[t.cycle_level]})
    if t.has_capacity:
        out["capacity_unit"] = t.cap_unit
        out["activity_unit"] = t.act_unit
        out["cap2act"] = _r(t.cap2act)
    out["regions"] = {}
    for r in t.regions:
        rows = []
        for i, M in enumerate(P.M):
            rec: dict[str, Any] = {"period": M, "years": f"{P.B[i]}-{P.E[i]}"}
            eff = t.efficiency.at(r, M, default=1.0) if t.efficiency else 1.0
            if t.kind == "process":
                rec["per_unit_activity"] = {
                    "inputs": {c: _r(p.at(r, M, default=1.0) / eff) for c, p in t.inputs.items()},
                    "outputs": {c: _r(p.at(r, M, default=1.0)) for c, p in t.outputs.items()},
                }
                if t.flex_in:
                    rec["flexible_inputs"] = {
                        c: {"efficiency": _r(mem.efficiency.at(r, M, default=eff) if mem.efficiency else eff),
                            "max_share": _r(mem.max_share.at(r, M)) if mem.max_share else None,
                            "min_share": _r(mem.min_share.at(r, M)) if mem.min_share else None}
                        for c, mem in t.flex_in.items()}
                if t.flex_out:
                    rec["flexible_outputs"] = {
                        c: {"max_share": _r(mem.max_share.at(r, M)) if mem.max_share else None,
                            "min_share": _r(mem.min_share.at(r, M)) if mem.min_share else None}
                        for c, mem in t.flex_out.items()}
                if t.emissions:
                    rec["emission_factors"] = {e: {basis: _r(f.at(r, M, default=0.0)) for basis, f in bases.items()}
                                               for e, bases in t.emissions.items()}
            else:
                rec["efficiency"] = _r(eff)
            if t.varom:
                rec["varom"] = _r(b.year_cost(t.varom, r, i)[1])
            if t.has_capacity:
                life = t.lifetime.at(r, M, default=None) if t.lifetime else None
                rec["lifetime"] = _r(life)
                if t.can_invest:
                    steps = b.ipd(i)
                    rec["invcost"] = _r(b.window_cost(t.invcost, r, P.B[i], steps))
                    if t.fixom:
                        rec["fixom"] = _r(b.window_cost(t.fixom, r, P.B[i], steps))
                elif t.fixom:
                    rec["fixom"] = _r(t.fixom.at(r, M, default=0.0))
                old = 0.0
                for year, cap in t.existing.get(r, {}).items():
                    lf = t.lifetime.at(r, year, default=30.0)
                    old += cap * b.cpt(year, year + lf, i)
                res = t.residual.at(r, M) if t.residual else None
                rec["existing_capacity"] = _r(old + (res or 0.0))
                av = {}
                for s in ts.at_level(t.level):
                    v = b.af(t.af_fix, r, s, M) if t.af_fix else None
                    if v is None:
                        v = b.af(t.af_max, r, s, M) if t.af_max else None
                    av[s] = _r(1.0 if v is None else v)
                rec["availability"] = av if len(av) > 1 else av.get(ANNUAL, 1.0)
                if t.cf_max:
                    rec["capacity_factor_max"] = {g or "annual": _r(t.cf_max.at(r, M, g)) for g in [None] + t.cf_max.seconds()
                                                  if t.cf_max.has(r, g)}
                bnds = {f"{what}.{kind}": _r(p.at(r, M)) for (what, kind), p in t.bounds.items() if p.at(r, M) is not None}
                if bnds:
                    rec["bounds"] = bnds
            rows.append(rec)
        reg: dict[str, Any] = {"periods": rows}
        if t.kind == "process":
            reg["upstream"] = {c: [p for p in topo[r][c]["producers"] if p != t.name] for c in t.commodities_in()}
            reg["downstream"] = {c: [p for p in topo[r][c]["consumers"] if p != t.name]
                                 for c in list(t.outputs) + list(t.flex_out)}
        if t.existing.get(r):
            reg["existing_by_vintage"] = t.existing[r]
        out["regions"][r] = reg
    return out


def res(model: Model, region: str | None = None) -> dict[str, Any]:
    """The reference energy system as adjacency lists plus a Mermaid diagram."""
    topo = topology(model)
    regions = [region] if region else model.regions
    out: dict[str, Any] = {"regions": {}}
    for r in regions:
        if r not in topo:
            raise KeyError(f"unknown region '{r}'")
        out["regions"][r] = {c: v for c, v in topo[r].items() if v["producers"] or v["consumers"]}
    r0 = regions[0]
    lines = ["flowchart LR"]
    for c, node in topo[r0].items():
        if not (node["producers"] or node["consumers"]):
            continue
        shape = {"demand": f'{c}[["{c}"]]', "emission": f'{c}{{{{"{c}"}}}}'}.get(model.commodities[c].kind, f'{c}(("{c}"))')
        lines.append(f"  {shape}")
        for p in node["producers"]:
            lines.append(f"  {p}[{p}] --> {c}")
        for p in node["consumers"]:
            if p not in model.storage:
                lines.append(f"  {c} --> {p}[{p}]")
    out["mermaid"] = "\n".join(dict.fromkeys(lines))
    out["mermaid_region"] = r0
    return out


def overview(model: Model) -> dict[str, Any]:
    P, ts = model.periods, model.ts
    kinds: dict[str, int] = {}
    for c in model.commodities.values():
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    return {
        "name": model.name, "scenarios": list(model.raw.scenarios), "regions": model.regions,
        "periods": P.table(), "period_mode": P.mode,
        "timeslices": {lvl: ts.at_level(i) for i, lvl in enumerate(ts.levels) if i > 0},
        "discount_rate": model.discount_rate, "discount_year": model.discount_year,
        "mid_year_discounting": model.mid_year, "currency": model.currency,
        "counts": {"commodities": kinds, "processes": len(model.processes), "storage": len(model.storage),
                   "trade": len(model.trade), "constraints": len(model.constraints), "expectations": len(model.expect)},
        "files": sorted(os.path.relpath(f, model.raw.root) for f in model.raw.files),
    }
