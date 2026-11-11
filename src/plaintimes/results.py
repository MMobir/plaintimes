"""Solve a model and turn the LP solution into tidy tables.

Every table is a list of flat records (one fact per row) written as CSV, so
results can be diffed, grepped and loaded anywhere.  `summary.json` holds the
headline numbers an agent usually needs first; `manifest.json` records what
exactly was run (input hashes, versions, solver, timing).
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .kernel import Built, Expr, build_lp
from .lp import Solution
from .model import Model

TOL = 1e-7


@dataclass
class Run:
    model: Model
    built: Built
    sol: Solution
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.sol.status == "optimal"


def _val(sol: Solution, expr: Expr) -> float:
    cv = sol.col_value
    return float(sum(c * cv[j] for j, c in expr.items()))


def _clean(x: float, digits: int = 9) -> float:
    if abs(x) < TOL:
        return 0.0
    return float(f"{x:.{digits}g}")


def solve(model: Model, relax: bool = False, slack_penalty: float = 1e6, time_limit: float | None = None,
          method: str = "auto", log: bool = False, write_lp: str | None = None) -> Run:
    built = build_lp(model, relax=relax, slack_penalty=slack_penalty)
    if write_lp:
        built.lp.write(write_lp)
    sol = built.lp.solve(time_limit=time_limit, method=method, log=log)
    run = Run(model, built, sol)
    if sol.status == "optimal":
        extract(run)
    run.summary = make_summary(run)
    return run


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def extract(run: Run) -> None:
    m, b, sol, lp = run.model, run.built, run.sol, run.built.lp
    P, ts = m.periods, m.ts
    T = run.tables
    cv, cd = sol.col_value, sol.col_dual

    # capacity and new capacity -------------------------------------------
    cap_rows, ncap_rows = [], []
    cap_value: dict[tuple[str, str, int], float] = {}
    for (r, tech, i), (expr, const) in b.cap.items():
        v = _val(sol, expr) + const
        cap_value[(r, tech, i)] = v
        if v > TOL:
            cap_rows.append({"region": r, "process": tech, "period": P.M[i], "capacity": _clean(v),
                             "unit": m.tech(tech).cap_unit})
    for (r, tech), vints in b.vintages.items():
        for v in vints:
            if v.var is None:
                continue
            x = float(cv[v.var])
            rc = float(cd[v.var])
            rec = {"region": r, "process": tech, "period": v.label, "new_capacity": _clean(x),
                   "unit": m.tech(tech).cap_unit}
            # reduced cost: how much the discounted cost of one unit must fall before it is built
            cost = lp.cost[v.var]
            rec["reduced_cost"] = _clean(rc)
            rec["cost_gap_pct"] = _clean(100.0 * rc / cost, 4) if x <= TOL and cost > 0 else 0.0
            ncap_rows.append(rec)
    T["capacity"] = cap_rows
    T["new_capacity"] = ncap_rows

    # activity ---------------------------------------------------------------
    act_rows = []
    for (r, tech, i), expr in b.activity.items():
        a = _val(sol, expr)
        t = m.tech(tech)
        rec = {"region": r, "process": tech, "period": P.M[i], "activity": _clean(a), "unit": t.act_unit}
        if t.has_capacity:
            cap = cap_value.get((r, tech, i), 0.0)
            rec["utilization"] = _clean(a / (cap * t.cap2act), 5) if cap > TOL else 0.0
        if a > TOL or t.has_capacity:
            act_rows.append(rec)
    T["activity"] = act_rows

    # flows -----------------------------------------------------------------
    flow_rows = []
    prod: dict[tuple[str, int, str, str], float] = {}
    cons: dict[tuple[str, int, str, str], float] = {}
    for (r, tech, c, d, i, sc), expr in b.flows.items():
        v = _val(sol, expr)
        if abs(v) <= TOL:
            continue
        flow_rows.append({"region": r, "process": tech, "commodity": c, "direction": d, "period": P.M[i],
                          "timeslice": sc, "value": _clean(v), "unit": m.commodities[c].unit})
        target = prod if d == "out" else cons
        target[(r, i, c, sc)] = target.get((r, i, c, sc), 0.0) + v
    T["flows"] = flow_rows

    # balances and prices -----------------------------------------------------
    bal_rows = []
    for c, com in m.commodities.items():
        if com.kind == "emission":
            continue
        for r in m.regions:
            for i in range(P.n):
                for sc in ts.at_level(com.level):
                    idx = lp.row_index.get(("BAL", r, c, P.M[i], sc))
                    if idx is None:
                        continue
                    price = float(sol.row_dual[idx]) / b.pv[i]
                    bal_rows.append({
                        "region": r, "commodity": c, "period": P.M[i], "timeslice": sc,
                        "production": _clean(prod.get((r, i, c, sc), 0.0)),
                        "consumption": _clean(cons.get((r, i, c, sc), 0.0)),
                        "demand": _clean(b.demand.get((r, i, c, sc), 0.0)),
                        "price": _clean(price), "price_unit": f"{m.currency}/{com.unit}",
                    })
    T["balance"] = bal_rows

    # emissions -----------------------------------------------------------------
    em_rows = []
    for (r, tech, e, i), expr in b.emis.items():
        v = _val(sol, expr)
        if abs(v) > TOL:
            em_rows.append({"region": r, "emission": e, "process": tech, "period": P.M[i], "value": _clean(v),
                            "unit": m.commodities[e].unit})
    T["emissions"] = em_rows

    # costs: annual (undiscounted, average over the period) and discounted -----
    cost_rows = []
    keys = set(b.annual) | set(b.annual_const)
    for key in sorted(keys):
        cat, r, tech, i = key
        v = sum(c * cv[j] for j, c in b.annual.get(key, [])) + b.annual_const.get(key, 0.0)
        if abs(v) > TOL:
            cost_rows.append({"region": r, "process": tech, "period": P.M[i], "category": cat,
                              "annual_cost": _clean(float(v)), "unit": f"{m.currency}/yr"})
    T["costs"] = cost_rows
    obj_rows = []
    keys2 = set(lp.cost_terms) | set(lp.cost_consts)
    for key in sorted(keys2):
        cat, r, tech = key
        v = sum(c * cv[j] for j, c in lp.cost_terms.get(key, [])) + lp.cost_consts.get(key, 0.0)
        if abs(v) > TOL:
            obj_rows.append({"region": r, "process": tech, "category": cat, "discounted_cost": _clean(float(v)),
                             "unit": m.currency})
    T["objective"] = obj_rows

    # constraints (bounds, caps, user constraints, peak) ----------------------
    con_rows = []
    period_index = {M: i for i, M in enumerate(P.M)}
    for idx, name in enumerate(lp.row_names):
        head = name[0]
        if head not in ("UC", "EMIS_CAP", "EMIS_BUDGET", "PEAK", "BND_CAP", "BND_ACT", "BND_CUM"):
            continue
        sense, rhs = lp.row_sense_rhs(idx)
        level = float(sol.row_value[idx])
        dual = float(sol.row_dual[idx])
        per = next((x for x in name[1:] if isinstance(x, int) and x in period_index), None)
        undisc = dual / b.pv[period_index[per]] if per is not None else dual
        con_rows.append({
            "kind": head, "name": "/".join(str(x) for x in name[1:]), "sense": sense,
            "level": _clean(level), "rhs": _clean(rhs), "slack": _clean(abs(rhs - level)),
            "binding": bool(abs(rhs - level) <= 1e-6 * max(1.0, abs(rhs)) and abs(dual) > 1e-9),
            "shadow_price": _clean(undisc), "shadow_price_discounted": _clean(dual),
        })
    T["constraints"] = con_rows

    # slack (relaxed runs) ----------------------------------------------------
    if run.built.relax:
        sl_rows = []
        for name, j in lp.var_index.items():
            if name[0] == "SLACK" and cv[j] > 1e-6:
                _, r, c, M, sc = name
                sl_rows.append({"region": r, "commodity": c, "period": M, "timeslice": sc,
                                "unserved": _clean(float(cv[j])), "unit": m.commodities[c].unit})
        T["unserved"] = sl_rows


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
def make_summary(run: Run) -> dict[str, Any]:
    m, sol, b = run.model, run.sol, run.built
    P = m.periods
    s: dict[str, Any] = {
        "model": m.name,
        "scenarios": list(m.raw.scenarios),
        "status": sol.status,
        "objective": _clean(sol.objective, 12) if sol.objective is not None else None,
        "currency": m.currency,
        "discount_rate": m.discount_rate,
        "discount_year": m.discount_year,
        "periods": P.table(),
        "size": b.stats(),
        "solver": {"name": sol.solver, "version": sol.solver_version, "seconds": round(sol.seconds, 3),
                   "iterations": sol.iterations, "message": sol.message},
        "relaxed": b.relax,
    }
    if sol.status != "optimal":
        s["hint"] = ("The model has no feasible solution. Run `plaintimes solve --relax` to see which commodity "
                     "balances cannot be met, or `plaintimes explain infeasible`."
                     if "infeasible" in sol.status else
                     "The objective is unbounded: some activity earns money without limit. Look for negative costs "
                     "(varom, emission taxes on negative emissions) on a process with no capacity or bound.")
        return s
    T = run.tables
    periods = P.M

    def by_period(rows, key_fields, value_field, where=None):
        out: dict[str, dict[int, float]] = {}
        for row in rows:
            if where and not where(row):
                continue
            k = "/".join(str(row[f]) for f in key_fields)
            out.setdefault(k, {})
            out[k][row["period"]] = out[k].get(row["period"], 0.0) + row[value_field]
        return {k: {p: _clean(v.get(p, 0.0), 6) for p in periods} for k, v in sorted(out.items())}

    multi = len(m.regions) > 1
    kf = (["region"] if multi else [])
    s["capacity"] = by_period(T["capacity"], kf + ["process"], "capacity")
    s["new_capacity"] = by_period(T["new_capacity"], kf + ["process"], "new_capacity",
                                  where=lambda row: row["new_capacity"] > 0)
    s["emissions"] = by_period(T["emissions"], kf + ["emission"], "value")
    s["annual_cost_by_category"] = by_period(T["costs"], ["category"], "annual_cost")
    disc: dict[str, float] = {}
    for row in T["objective"]:
        disc[row["category"]] = disc.get(row["category"], 0.0) + row["discounted_cost"]
    s["discounted_cost_by_category"] = {k: _clean(v, 9) for k, v in sorted(disc.items())}
    # average annual price per commodity (weighted by production)
    prices: dict[str, dict[int, list[float]]] = {}
    for row in T["balance"]:
        k = "/".join(str(row[f]) for f in kf + ["commodity"])
        w = max(row["production"], 0.0)
        acc = prices.setdefault(k, {}).setdefault(row["period"], [0.0, 0.0, 0.0, 0])
        acc[0] += row["price"] * w
        acc[1] += w
        acc[2] += row["price"]
        acc[3] += 1
    s["prices"] = {
        k: {p: _clean((a[0] / a[1]) if a[1] > 0 else (a[2] / a[3] if a[3] else 0.0), 6)
            for p, a in sorted(v.items())}
        for k, v in sorted(prices.items())
    }
    s["binding_constraints"] = [
        {"name": f"{row['kind']}/{row['name']}", "shadow_price": row["shadow_price"]}
        for row in T["constraints"] if row["binding"]
    ]
    if b.relax:
        uns = T.get("unserved", [])
        s["unserved"] = uns[:50]
        s["feasible_without_relaxation"] = not uns
        if uns:
            s["hint"] = ("Some commodity balances could only be met with slack (listed under 'unserved'). "
                         "The model is infeasible without --relax; add supply options or relax bounds for those commodities.")
    return s


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def write_run(run: Run, out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    written = {}
    for name, rows in run.tables.items():
        path = os.path.join(out_dir, f"{name}.csv")
        fields: list[str] = []
        for row in rows:
            for k in row:
                if k not in fields:
                    fields.append(k)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        written[name] = path
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(run.summary, fh, indent=2)
    manifest = {
        "plaintimes_version": __version__,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "model_file": os.path.relpath(run.model.raw.model_file, out_dir),
        "scenarios": list(run.model.raw.scenarios),
        "input_fingerprint": run.model.raw.fingerprint(),
        "input_files": run.model.raw.file_hashes(),
        "solver": run.summary.get("solver"),
        "size": run.summary.get("size"),
        "status": run.sol.status,
        "objective": run.summary.get("objective"),
        "relaxed": run.built.relax,
        "tables": sorted(written),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return written


def read_table(run_dir: str, name: str) -> list[dict[str, Any]]:
    path = os.path.join(run_dir, f"{name}.csv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rec: dict[str, Any] = {}
            for k, v in row.items():
                if v is None or v == "":
                    rec[k] = v
                    continue
                try:
                    f = float(v)
                    rec[k] = int(f) if k == "period" and f == int(f) else f
                except ValueError:
                    rec[k] = {"True": True, "False": False}.get(v, v)
            out.append(rec)
    return out
