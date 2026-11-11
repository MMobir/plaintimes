"""Expectations: assertions about results that live with the model.

    expect:
      - name: coal is gone by 2050 under the cap
        scenarios: [carbon_cap]
        table: capacity
        where: {process: COAL_PP, period: 2050}
        max: 0.01
      - name: reference case solves
        status: optimal
      - name: total cost in a sane range
        objective: {min: 1.0e4, max: 1.0e6}

Each check sums the value column of the matching rows of a result table and
compares it with min / max / equals.  `plaintimes test` runs them all; it is
the model's own regression suite.
"""
from __future__ import annotations

from typing import Any

from .api import load
from .diff import SPEC
from .results import solve

DEFAULT_FIELD = {name: field for name, (keys, field) in SPEC.items()}
DEFAULT_FIELD.update({"flows": "value", "constraints": "shadow_price", "unserved": "unserved"})


def run_expectations(model_path: str, only_scenarios: list[str] | None = None) -> dict[str, Any]:
    base, diags = load(model_path)
    if base is None:
        return {"ok": False, "error": "model has errors", "diagnostics": diags.to_list()}
    checks = base.expect
    results = []
    cache: dict[tuple, Any] = {}
    for n, chk in enumerate(checks):
        name = chk.get("name", f"check {n + 1}") if isinstance(chk, dict) else f"check {n + 1}"
        if not isinstance(chk, dict):
            results.append({"name": name, "ok": False, "message": "each expectation must be a mapping"})
            continue
        scen = chk.get("scenarios", [])
        scen = [scen] if isinstance(scen, str) else list(scen)
        if only_scenarios is not None and sorted(scen) != sorted(only_scenarios):
            continue
        key = tuple(scen)
        if key not in cache:
            model, d2 = load(model_path, scen)
            cache[key] = (solve(model) if model is not None else None, d2)
        run, d2 = cache[key]
        rec: dict[str, Any] = {"name": name, "scenarios": scen}
        if run is None:
            rec.update(ok=False, message="model has errors: " + "; ".join(d.message for d in d2.errors[:3]))
            results.append(rec)
            continue
        want_status = chk.get("status", "optimal")
        if run.sol.status != want_status:
            rec.update(ok=False, message=f"status is '{run.sol.status}', expected '{want_status}'")
            results.append(rec)
            continue
        if "status" in chk and len([k for k in chk if k not in ("name", "scenarios", "status")]) == 0:
            rec.update(ok=True, value=run.sol.status)
            results.append(rec)
            continue
        if "objective" in chk:
            value = run.sol.objective
            bounds = chk["objective"] if isinstance(chk["objective"], dict) else {"equals": chk["objective"]}
        elif "table" in chk:
            table = chk["table"]
            if table not in run.tables:
                rec.update(ok=False, message=f"unknown table '{table}'; available: {', '.join(sorted(run.tables))}")
                results.append(rec)
                continue
            field = chk.get("field", DEFAULT_FIELD.get(table))
            where = chk.get("where", {}) or {}
            rows = [r for r in run.tables[table]
                    if all(r.get(k) in (v if isinstance(v, list) else [v]) for k, v in where.items())]
            value = float(sum(float(r.get(field) or 0.0) for r in rows))
            rec["matched_rows"] = len(rows)
            bounds = chk
        else:
            rec.update(ok=False, message="expectation needs 'table', 'objective' or 'status'")
            results.append(rec)
            continue
        ok, msgs = True, []
        tol = float(chk.get("tolerance", 1e-6))
        if "min" in bounds and value < bounds["min"] - tol * max(1.0, abs(bounds["min"])):
            ok = False
            msgs.append(f"{value:.6g} is below min {bounds['min']:g}")
        if "max" in bounds and value > bounds["max"] + tol * max(1.0, abs(bounds["max"])):
            ok = False
            msgs.append(f"{value:.6g} is above max {bounds['max']:g}")
        if "equals" in bounds and abs(value - bounds["equals"]) > tol * max(1.0, abs(bounds["equals"])):
            ok = False
            msgs.append(f"{value:.6g} differs from {bounds['equals']:g}")
        if not any(k in bounds for k in ("min", "max", "equals")):
            ok = False
            msgs.append("no min / max / equals given")
        rec.update(ok=ok, value=value)
        if msgs:
            rec["message"] = "; ".join(msgs)
        results.append(rec)
    failed = [r for r in results if not r["ok"]]
    return {"ok": not failed, "passed": len(results) - len(failed), "failed": len(failed), "results": results}
