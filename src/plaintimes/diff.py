"""Compare two runs table by table and report the largest changes."""
from __future__ import annotations

import json
import os
from typing import Any

from .results import read_table

# table -> (key fields, value field)
SPEC = {
    "capacity": (["region", "process", "period"], "capacity"),
    "new_capacity": (["region", "process", "period"], "new_capacity"),
    "activity": (["region", "process", "period"], "activity"),
    "emissions": (["region", "emission", "period"], "value"),
    "balance": (["region", "commodity", "period", "timeslice"], "price"),
    "costs": (["region", "category", "period"], "annual_cost"),
    "objective": (["category"], "discounted_cost"),
}


def _load_summary(run_dir: str) -> dict[str, Any]:
    path = os.path.join(run_dir, "summary.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{run_dir} is not a run directory (no summary.json)")
    with open(path) as fh:
        return json.load(fh)


def diff_runs(a_dir: str, b_dir: str, top: int = 15, min_change: float = 1e-6) -> dict[str, Any]:
    sa, sb = _load_summary(a_dir), _load_summary(b_dir)
    out: dict[str, Any] = {
        "a": {"dir": a_dir, "scenarios": sa.get("scenarios"), "status": sa.get("status"), "objective": sa.get("objective")},
        "b": {"dir": b_dir, "scenarios": sb.get("scenarios"), "status": sb.get("status"), "objective": sb.get("objective")},
    }
    if sa.get("objective") is not None and sb.get("objective") is not None:
        d = sb["objective"] - sa["objective"]
        out["objective_change"] = d
        out["objective_change_pct"] = 100.0 * d / abs(sa["objective"]) if sa["objective"] else None
    tables: dict[str, Any] = {}
    for name, (keys, field) in SPEC.items():
        ta, tb = read_table(a_dir, name), read_table(b_dir, name)
        agg_a: dict[tuple, float] = {}
        agg_b: dict[tuple, float] = {}
        for rows, agg in ((ta, agg_a), (tb, agg_b)):
            for row in rows:
                k = tuple(row.get(f) for f in keys)
                agg[k] = agg.get(k, 0.0) + float(row.get(field) or 0.0)
        changes = []
        for k in set(agg_a) | set(agg_b):
            va, vb = agg_a.get(k, 0.0), agg_b.get(k, 0.0)
            if abs(vb - va) > min_change * max(1.0, abs(va), abs(vb)):
                rec = dict(zip(keys, k))
                rec.update({"a": va, "b": vb, "change": vb - va})
                changes.append(rec)
        changes.sort(key=lambda r: -abs(r["change"]))
        tables[name] = {"field": field, "changed_rows": len(changes), "largest": changes[:top]}
    out["tables"] = tables
    return out
