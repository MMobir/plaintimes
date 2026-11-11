"""Run the official ETSAP TIMES code on an exported model and compare.

This is the optional half of the GAMS bridge: if a GAMS executable and a copy
of the TIMES source (https://github.com/etsap-TIMES/TIMES_model) are
available, `plaintimes oracle` exports the model, solves it with the official
generator and reports how far the native kernel is from it.

Nothing here is needed for normal use of the native kernel.
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from typing import Any

from .ddexport import export_dd
from .model import Model
from .results import Run


class OracleUnavailable(RuntimeError):
    pass


def find_gams(gams: str | None = None) -> str:
    cand = gams or os.environ.get("PLAINTIMES_GAMS") or shutil.which("gams")
    if not cand or not os.path.exists(cand):
        raise OracleUnavailable(
            "no GAMS executable found. Pass --gams <path>, set PLAINTIMES_GAMS, or put `gams` on PATH. "
            "(The native kernel does not need GAMS; this is only for cross-checking against official TIMES.)")
    return cand


def find_times_source(src: str | None = None) -> str:
    cand = src or os.environ.get("PLAINTIMES_TIMES_SRC")
    if not cand or not os.path.exists(os.path.join(cand, "maindrv.mod")):
        raise OracleUnavailable(
            "TIMES source not found. Pass --times-src <dir> or set PLAINTIMES_TIMES_SRC to a checkout of "
            "https://github.com/etsap-TIMES/TIMES_model (the directory containing maindrv.mod).")
    return cand


def _dump(gdxdump: str, gdx: str, symbol: str) -> list[list[str]]:
    """Rows of a GDX symbol as lists of strings (index values..., value); header dropped."""
    res = subprocess.run([gdxdump, gdx, f"symb={symbol}", "format=csv"], capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    rows = list(csv.reader(io.StringIO(res.stdout)))[1:]
    special = {"Eps": "0", "Undf": "nan", "NA": "nan", "+Inf": "inf", "-Inf": "-inf"}
    for row in rows:
        if row and row[-1] in special:
            row[-1] = special[row[-1]]
    return rows


def run_times(model: Model, work_dir: str, gams: str | None = None, times_src: str | None = None,
              solver: str | None = None, timeout: int = 1800) -> dict[str, Any]:
    """Export, solve with official TIMES, and read back the main results."""
    gams = find_gams(gams)
    src = os.path.abspath(find_times_source(times_src))
    info = export_dd(model, work_dir, solver=solver)
    name = info["name"]
    cmd = [gams, f"{name}.run", "idir1=.", f"idir2={src}", "ps=0", f"gdx={name}", "lo=2", "filecase=4"]
    proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=timeout)
    log_path = os.path.join(work_dir, f"{name}.log")
    log = open(log_path, errors="replace").read() if os.path.exists(log_path) else proc.stdout
    out: dict[str, Any] = {"name": name, "work_dir": work_dir, "command": " ".join(cmd), "returncode": proc.returncode}
    qa = os.path.join(work_dir, "qa_check.log")
    if os.path.exists(qa):
        text = open(qa, errors="replace").read()
        out["qa_passed"] = "ALL QUALITY CHECKS PASSED" in text
        out["qa_log"] = text[-3000:] if not out["qa_passed"] else ""
    if "licensing error" in log or "demo license limits" in log:
        out["status"] = "license_limit"
        out["message"] = "the model exceeds the size limit of the GAMS licence in use"
        return out
    low = log.lower()
    if "compilation error" in low:
        out["status"] = "error"
        lines = [ln for ln in log.splitlines() if ln.startswith("***")]
        out["message"] = "\n".join(lines[-12:])
        return out
    if "times solve status: infeasible" in low or "proven infeasible" in low:
        out["status"] = "infeasible"
        return out
    if "unbounded" in low and "times solve status" in low and "optimal" not in low:
        out["status"] = "unbounded"
        return out
    if proc.returncode != 0:
        out["status"] = "error"
        lines = [ln for ln in log.splitlines() if ln.startswith("***")]
        out["message"] = "\n".join(lines[-12:]) or f"gams exited with code {proc.returncode}"
        return out
    gdx = os.path.join(work_dir, f"{name}.gdx")
    gdxdump = os.path.join(os.path.dirname(gams), "gdxdump")
    if not os.path.exists(gdxdump):
        gdxdump = shutil.which("gdxdump") or gdxdump
    obj = _dump(gdxdump, gdx, "OBJz")
    out["status"] = "optimal" if ("optimal" in low and obj) else "unknown"
    out["objective"] = float(obj[0][-1]) if obj else None
    M = set(model.periods.M)

    def table(symbol: str, keys: list[str], period_col: int | None = None) -> dict[tuple, float]:
        res: dict[tuple, float] = {}
        for vals in _dump(gdxdump, gdx, symbol):
            key = tuple(int(v) if v.lstrip("-").isdigit() else v for v in (vals[i] for i in range(len(keys))))
            res[key] = res.get(key, 0.0) + float(vals[-1])
        return res

    out["new_capacity"] = {k: v for k, v in table("PAR_NCAPL", ["r", "v", "p"]).items() if k[1] in M}
    cap = table("PAR_CAPL", ["r", "t", "p"])
    for vals in _dump(gdxdump, gdx, "PAR_PASTI"):       # existing stock is reported separately
        key = (vals[0], int(vals[1]), vals[2])
        cap[key] = cap.get(key, 0.0) + float(vals[-1])
    out["capacity"] = cap
    act: dict[tuple, float] = {}
    for vals in _dump(gdxdump, gdx, "PAR_ACTL"):
        key = (vals[0], int(vals[2]), vals[3])            # region, period, process (summed over vintage and timeslice)
        act[key] = act.get(key, 0.0) + float(vals[-1])
    out["activity"] = act
    out["prices"] = table("PAR_COMBALEM", ["r", "t", "c", "s"])
    return out


def compare(run: Run, times: dict[str, Any], rel_tol: float = 1e-4) -> dict[str, Any]:
    """Compare a native run with official TIMES results."""
    P = run.model.periods
    rep: dict[str, Any] = {"times_status": times.get("status"), "native_status": run.sol.status}
    if times.get("status") != "optimal" or run.sol.status != "optimal":
        rep["agree"] = times.get("status") == run.sol.status
        if times.get("message"):
            rep["times_message"] = times["message"]
        return rep
    o_n, o_t = run.sol.objective, times["objective"]
    rep["objective_native"] = o_n
    rep["objective_times"] = o_t
    rep["objective_rel_diff"] = abs(o_n - o_t) / max(1.0, abs(o_t))

    def diff_table(native_rows: list[dict], field: str, times_tab: dict[tuple, float]) -> dict[str, Any]:
        nat: dict[tuple, float] = {}
        for row in native_rows:
            key = (row["region"], row["period"], row["process"])
            nat[key] = nat.get(key, 0.0) + row[field]
        tim = {(k[0], k[1], k[2]): v for k, v in times_tab.items()}
        worst, worst_key, n = 0.0, None, 0
        scale = max([abs(v) for v in list(nat.values()) + list(tim.values())] + [1e-9])
        for key in set(nat) | set(tim):
            a, b = nat.get(key, 0.0), tim.get(key, 0.0)
            d = abs(a - b) / max(scale * 1e-3, abs(b), abs(a))
            n += 1
            if d > worst:
                worst, worst_key = d, key
        res: dict[str, Any] = {"compared": n, "max_rel_diff": worst}
        if worst_key is not None and worst > rel_tol:
            res["worst"] = {"key": list(worst_key), "native": nat.get(worst_key, 0.0), "times": tim.get(worst_key, 0.0)}
        return res

    rep["new_capacity"] = diff_table(run.tables["new_capacity"], "new_capacity", times["new_capacity"])
    rep["capacity"] = diff_table(run.tables["capacity"], "capacity", times["capacity"])
    rep["activity"] = diff_table(run.tables["activity"], "activity", times["activity"])
    rep["agree"] = rep["objective_rel_diff"] <= rel_tol
    rep["solution_agrees"] = all(rep[k]["max_rel_diff"] <= 1e-3 for k in ("new_capacity", "capacity", "activity"))
    if rep["agree"] and not rep["solution_agrees"]:
        rep["note"] = ("objectives agree but some quantities differ: the LP has alternative optima "
                       "(equal-cost solutions), which is expected in degenerate models.")
    rep["qa_passed"] = times.get("qa_passed")
    return rep
