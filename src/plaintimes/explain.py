"""Explanations from the LP duals.

A TIMES solution is only half the answer; the other half is *why*.  The LP's
dual values hold that: every active process breaks even at shadow prices,
every unbuilt technology misses by its reduced cost.  These functions read
that off the solved LP and report it per unit of activity or capacity, in
undiscounted money of the period concerned.
"""
from __future__ import annotations

from typing import Any

from .results import Run, _clean

ROW_LABEL = {
    "BAL": "commodity", "CAPACT": "capacity_rent", "EMIS_CAP": "emission_cap", "EMIS_BUDGET": "emission_budget",
    "UC": "constraint", "PEAK": "peak_reserve", "BND_CAP": "capacity_bound", "BND_ACT": "activity_bound",
    "BND_CUM": "cumulative_bound", "STG_OUT": "capacity_rent", "STG_IN": "capacity_rent", "STG_ENERGY": "capacity_rent",
    "STG_CYCLE": "storage_cycle", "TRD_CAP": "capacity_rent",
}


def _columns(run: Run) -> dict[int, list[tuple[int, float]]]:
    lp = run.built.lp
    cache = getattr(lp, "_columns", None)
    if cache is None:
        cache = {}
        for i in range(lp.n_rows):
            for k in range(lp.row_start[i], lp.row_start[i + 1]):
                cache.setdefault(lp.col_idx[k], []).append((i, lp.values[k]))
        lp._columns = cache
    return cache


def _period_index(run: Run, period: int) -> int:
    M = run.model.periods.M
    if period not in M:
        raise KeyError(f"{period} is not a milestone year; choose from {M}")
    return M.index(period)


def explain_process(run: Run, process: str, region: str | None = None, period: int | None = None) -> dict[str, Any]:
    """Break-even account of a process: what it earns and pays at shadow prices."""
    m, lp, sol = run.model, run.built.lp, run.sol
    t = m.tech(process)
    if t is None:
        raise KeyError(f"unknown process '{process}'")
    region = region or t.regions[0]
    cols = _columns(run)
    P = m.periods
    out: dict[str, Any] = {"process": process, "region": region, "currency": m.currency, "operation": [], "investment": []}

    periods = [period] if period is not None else P.M
    for M in periods:
        i = _period_index(run, M)
        pv = run.built.pv[i]
        acc: dict[tuple[str, str], float] = {}
        own = rc = 0.0
        act_expr = run.built.activity.get((region, process, i), {})
        activity = sum(c * sol.col_value[j] for j, c in act_expr.items())
        for name, j in lp.var_index.items():
            head = name[0]
            if head in ("ACT", "FIN", "FOUT"):          # (head, region, process, vintage, period, timeslice[, commodity])
                if name[1] != region or name[2] != process or name[4] != M:
                    continue
            elif head in ("SIN", "SOUT"):               # (head, region, storage, period, timeslice)
                if name[1] != region or name[2] != process or name[3] != M:
                    continue
            elif head == "TRD":                         # (head, link, direction, period, timeslice)
                if name[1] != process or name[3] != M:
                    continue
            else:
                continue
            x = float(sol.col_value[j])
            if x <= 1e-9:
                continue
            own += x * lp.cost[j] / pv
            rc += x * float(sol.col_dual[j]) / pv
            for irow, a in cols.get(j, []):
                rname = lp.row_names[irow]
                y = float(sol.row_dual[irow])
                if y == 0.0:
                    continue
                head = rname[0]
                if head == "BAL":
                    key = ("revenue" if a > 0 else "input_cost", rname[2])
                elif head in ROW_LABEL:
                    detail = rname[1] if head == "UC" else (rname[2] if head.startswith("EMIS") else "")
                    key = (ROW_LABEL[head], str(detail))
                else:
                    continue                      # internal rows (flow definitions, shares) net out
                acc[key] = acc.get(key, 0.0) + x * y * a / pv
        rec: dict[str, Any] = {"period": M, "activity": _clean(activity), "unit": t.act_unit}
        if activity > 1e-9:
            lines = []
            for (kind, detail), v in sorted(acc.items()):
                # revenue is positive; everything else is shown as a cost (positive = the process pays)
                val = v / activity if kind == "revenue" else -v / activity
                lines.append({"item": kind, "detail": detail, "per_unit_activity": _clean(val, 6)})
            lines.append({"item": "variable_cost", "detail": "varom and taxes", "per_unit_activity": _clean(own / activity, 6)})
            rec["breakdown"] = lines
            rev = sum(l["per_unit_activity"] for l in lines if l["item"] == "revenue")
            cost = sum(l["per_unit_activity"] for l in lines if l["item"] != "revenue")
            rec["net_per_unit_activity"] = _clean(rev - cost, 6)
            rec["reading"] = ("revenue at shadow prices equals costs including the rent paid for scarce capacity; "
                              "a positive capacity_rent means the process is inframarginal and earns it back on its capacity.")
        else:
            rec["reading"] = "not operating in this period"
        out["operation"].append(rec)

    # investment view -------------------------------------------------------
    for v in run.built.vintages.get((region, process), []):
        if v.var is None or (period is not None and v.label != period):
            continue
        x = float(sol.col_value[v.var])
        cost = lp.cost[v.var]
        rcv = float(sol.col_dual[v.var])
        value: dict[str, float] = {}
        for irow, a in cols.get(v.var, []):
            rname = lp.row_names[irow]
            y = float(sol.row_dual[irow])
            if y == 0.0:
                continue
            label = ROW_LABEL.get(rname[0], rname[0].lower())
            if rname[0] == "UC":
                label = f"constraint {rname[1]}"
            value[label] = value.get(label, 0.0) + y * a
        rec = {
            "vintage": v.label, "new_capacity": _clean(x), "unit": t.cap_unit,
            "discounted_cost_per_unit": _clean(cost, 8),
            "discounted_value_per_unit": {k: _clean(val, 8) for k, val in sorted(value.items())},
            "reduced_cost": _clean(rcv, 8),
        }
        if x <= 1e-9 and cost > 0 and rcv > 1e-9:
            rec["reading"] = (f"not built: one unit costs {cost:.6g} (discounted investment + fixed O&M) but is worth only "
                              f"{cost - rcv:.6g} to the system; it would enter if its cost fell by {100 * rcv / cost:.3g}%.")
        elif x > 1e-9:
            rec["reading"] = "built: the discounted value of the capacity (rents it earns) equals its discounted cost."
        else:
            rec["reading"] = "at the margin: zero reduced cost, the model is indifferent to building it."
        out["investment"].append(rec)
    return out


def explain_price(run: Run, commodity: str, region: str | None = None, period: int | None = None) -> dict[str, Any]:
    """Shadow price of a commodity by timeslice, and who supplies it at what marginal cost."""
    m, lp, sol, b = run.model, run.built.lp, run.sol, run.built
    if commodity not in m.commodities:
        raise KeyError(f"unknown commodity '{commodity}'")
    com = m.commodities[commodity]
    region = region or m.regions[0]
    P, ts = m.periods, m.ts
    out: dict[str, Any] = {"commodity": commodity, "region": region, "price_unit": f"{m.currency}/{com.unit}", "periods": []}
    capact: dict[tuple, list] = {}
    for name in lp.row_names:
        if name[0] == "CAPACT":
            capact.setdefault((name[1], name[2], name[4]), []).append(name)
    for M in ([period] if period is not None else P.M):
        i = _period_index(run, M)
        pv = b.pv[i]
        slices = []
        for sc in ts.at_level(com.level):
            idx = lp.row_index.get(("BAL", region, commodity, M, sc))
            if idx is None:
                continue
            price = float(sol.row_dual[idx]) / pv
            suppliers = []
            for (r, tech, c, d, ii, s2), expr in b.flows.items():
                if r != region or c != commodity or d != "out" or ii != i or s2 != sc:
                    continue
                flow = sum(cf * sol.col_value[j] for j, cf in expr.items())
                t = m.tech(tech)
                rent = 0.0
                for name in capact.get((region, tech, M), []):
                    if ts.is_within(sc, name[5]) or ts.is_within(name[5], sc):
                        rent += -float(sol.row_dual[lp.row_index[name]]) / pv
                rcs = [float(sol.col_dual[j]) / pv for j in expr]
                rec: dict[str, Any] = {"process": tech, "flow": _clean(flow)}
                if flow > 1e-9:
                    rec["capacity_rent_per_unit_activity"] = _clean(rent, 6)
                    rec["role"] = "price-setting (no scarcity rent)" if rent <= 1e-7 else "inframarginal (capacity is scarce)"
                else:
                    rec["capacity_rent_per_unit_activity"] = None
                    rec["role"] = "idle"
                    rec["cost_above_price_per_unit_activity"] = _clean(max(rcs) if rcs else 0.0, 6)
                suppliers.append(rec)
            suppliers.sort(key=lambda s: -s["flow"])
            slices.append({"timeslice": sc, "price": _clean(price, 6),
                           "demand": _clean(b.demand.get((region, i, commodity, sc), 0.0)), "suppliers": suppliers})
        out["periods"].append({"period": M, "timeslices": slices})
    return out


def explain_infeasible(run_relaxed: Run) -> dict[str, Any]:
    """Summarise where a relaxed solve had to invent supply."""
    uns = run_relaxed.tables.get("unserved", [])
    by_com: dict[str, float] = {}
    for row in uns:
        by_com[row["commodity"]] = by_com.get(row["commodity"], 0.0) + row["unserved"]
    out: dict[str, Any] = {"status": run_relaxed.sol.status, "feasible_without_relaxation": not uns,
                           "unserved_by_commodity": {k: _clean(v) for k, v in sorted(by_com.items(), key=lambda kv: -kv[1])},
                           "unserved": uns[:100]}
    if run_relaxed.sol.status != "optimal":
        out["reading"] = ("still infeasible with slack on every commodity balance: the conflict is among bounds or user "
                          "constraints (e.g. a minimum above a maximum), not a missing supply option.")
    elif uns:
        first = uns[0]
        out["reading"] = (
            f"the model cannot supply {', '.join(by_com)}; the first shortfall is {first['commodity']} in "
            f"{first['region']}, {first['period']} ({first['timeslice']}). Typical causes: no (or too little) capacity "
            "allowed in that period, an emission cap or share constraint that excludes every supplier, or a missing "
            "fuel supply upstream.")
    else:
        out["reading"] = "the model is feasible; no slack was needed."
    return out
