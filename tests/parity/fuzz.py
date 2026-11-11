"""Randomized differential test: native kernel versus official TIMES.

Composes small random models from the features the format supports, solves
each with the native kernel and with the official ETSAP TIMES code, and
reports every case where the two objectives differ.

    PLAINTIMES_GAMS=/path/to/gams PLAINTIMES_TIMES_SRC=/path/to/TIMES_model \\
        python tests/parity/fuzz.py 0 200            # seeds 0..199
        python tests/parity/fuzz.py 0 200 --keep out # keep the generated models in out/

The models are kept small enough for the free GAMS demo licence (2,000 rows
or columns); a model that exceeds it is counted as TOOBIG and skipped.
Emission caps, budgets and capacity constraints are sized from a first
unconstrained solve so that they bind without making the model infeasible.

This is how most of the accounting conventions listed in docs/MATH.md were
found.  A seed that fails is a reproducible bug report: `--keep` the model,
then strip features from it until the difference disappears.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from plaintimes.api import load  # noqa: E402
from plaintimes.oracle import compare, run_times  # noqa: E402
from plaintimes.results import solve  # noqa: E402

TOL = 1e-7


def series(rng, lo, hi, y0, y1, p_const=0.4):
    """A constant or a two- or three-point year series."""
    a = round(rng.uniform(lo, hi), 3)
    if rng.random() < p_const:
        return a
    out = {y0: a, y1: round(rng.uniform(lo, hi), 3)}
    if rng.random() < 0.3:
        out[rng.randint(y0, y1)] = round(rng.uniform(lo, hi), 3)
    return out


def shares(rng, names, lo=0.5, hi=1.5):
    w = [rng.uniform(lo, hi) for _ in names]
    total = sum(w)
    sh = [round(x / total, 4) for x in w]
    sh[-1] = round(1 - sum(sh[:-1]), 4)
    return dict(zip(names, sh))


def generate(seed: int) -> tuple[dict, str]:
    """Return (model as a plain dict, emission policy to add after the first solve)."""
    rng = random.Random(seed)
    m: dict = {"plaintimes": 1, "name": f"fz{seed}", "currency": "MUSD",
               "discount_rate": rng.choice([0.03, 0.05, 0.08]),
               "regions": ["R1"] if rng.random() < 0.7 else ["R1", "R2"],
               "units": {"energy": "PJ", "capacity": "GW"}}
    # -- periods ------------------------------------------------------------
    if rng.random() < 0.5:
        lengths = [rng.choice([1, 2, 5, 5, 10]) for _ in range(rng.randint(2, 4))]
        m["periods"] = {"start": 2025, "lengths": lengths}
        y1 = 2025 + sum(lengths) - 1
    else:
        ms = [2025]
        for _ in range(rng.randint(1, 3)):
            ms.append(ms[-1] + rng.choice([3, 5, 5, 10]))
        m["periods"] = {"milestones": ms}
        if rng.random() < 0.3:
            m["periods"]["start"] = 2025 - rng.randint(1, 3)
        if rng.random() < 0.3:
            m["periods"]["end"] = ms[-1] + rng.randint(0, 8)
        y1 = ms[-1]
    if rng.random() < 0.3:
        m["mid_year_discounting"] = True
    if rng.random() < 0.3:
        m["discount_year"] = 2025 + rng.randint(-2, 6)
    # -- timeslices ---------------------------------------------------------
    tsmode = rng.choice(["none", "two", "four", "four", "eight"])
    if tsmode == "two":
        m["timeslices"] = {"D": 0.55, "N": 0.45}
        leaf, seas, elvl = ["D", "N"], ["D", "N"], "season"
    elif tsmode == "four":
        m["timeslices"] = {"W": {"D": 0.3, "N": 0.2}, "S": {"D": 0.27, "N": 0.23}}
        leaf, seas, elvl = ["WD", "WN", "SD", "SN"], ["W", "S"], "daynite"
    elif tsmode == "eight":
        m["timeslices"] = {"W": {"K": {"D": 0.2, "N": 0.15}, "E": {"D": 0.08, "N": 0.07}},
                           "S": {"K": {"D": 0.19, "N": 0.16}, "E": {"D": 0.08, "N": 0.07}}}
        leaf = [s + k + d for s in "WS" for k in "KE" for d in "DN"]
        seas, elvl = ["W", "S"], "daynite"
    else:
        leaf, seas, elvl = [], [], None
    regs = m["regions"]

    def by_region(f):
        if len(regs) > 1 and rng.random() < 0.4:
            return {r: f() for r in regs}
        return f()

    # -- commodities --------------------------------------------------------
    C: dict = {"GAS": {"kind": "energy"}, "COAL": {"kind": "energy"}, "CO2": {"kind": "emission", "unit": "kt"}}
    elc: dict = {"kind": "energy"}
    if elvl:
        elc["timeslice"] = elvl
    if rng.random() < 0.4:
        elc["efficiency"] = round(rng.uniform(0.85, 0.98), 3)
    if rng.random() < 0.4:
        elc["peak_reserve"] = round(rng.uniform(0.05, 0.3), 2)
        if leaf and rng.random() < 0.3:
            elc["peak_timeslices"] = [rng.choice(leaf)]
    if rng.random() < 0.2:
        elc["balance"] = "="
    C["ELC"] = elc
    d1: dict = {"kind": "demand", "demand": by_region(lambda: series(rng, 50, 150, 2025, y1))}
    if elvl:
        d1["timeslice"] = elvl
        if rng.random() < 0.7:
            d1["load_shape"] = shares(rng, leaf)
    C["LIGHT"] = d1
    d2: dict = {"kind": "demand", "demand": series(rng, 20, 80, 2025, y1)}
    if seas and rng.random() < 0.6:
        d2["timeslice"] = "season"
        d2["load_shape"] = shares(rng, seas, 0.3, 1.7)
    C["HEAT"] = d2
    policy = rng.choice(["none", "cap", "tax", "budget", "none"])
    if policy == "tax":
        C["CO2"]["tax"] = series(rng, 0.0, 0.1, 2025, y1)
    m["commodities"] = C
    m["emission_factors"] = {"GAS": {"CO2": 56.1}, "COAL": {"CO2": 94.6}}

    # -- processes ----------------------------------------------------------
    P: dict = {"GAS_IMP": {"outputs": ["GAS"], "varom": by_region(lambda: series(rng, 4, 10, 2025, y1))},
               "COAL_IMP": {"outputs": ["COAL"], "varom": series(rng, 2, 4, 2025, y1)},
               "LIGHT_BACKSTOP": {"outputs": ["LIGHT"], "varom": 400},
               "HEAT_BACKSTOP": {"outputs": ["HEAT"], "varom": 300}}
    if rng.random() < 0.3:
        P["COAL_IMP"]["bounds"] = {"cumulative_activity": {"max": rng.randint(800, 4000)}}
    if rng.random() < 0.15:
        P["GAS_IMP"].setdefault("bounds", {})["activity"] = {"max": {y1: rng.randint(150, 400)}}

    def plant(fuel):
        p: dict = {"outputs": ["ELC"], "lifetime": rng.choice([4, 8, 15, 25, 30, 40]),
                   "invcost": series(rng, 500, 2500, 2025, y1), "fixom": series(rng, 5, 50, 2025, y1),
                   "tags": ["thermal" if fuel else "renewable"]}
        if fuel:
            p["inputs"] = [fuel]
            p["efficiency"] = series(rng, 0.3, 0.6, 2025, y1)
            p["emissions"] = "combustion"
            p["availability"] = series(rng, 0.7, 0.95, 2025, y1) if rng.random() < 0.7 else 0.9
            if rng.random() < 0.4:
                p["varom"] = series(rng, 0, 2, 2025, y1)
            r = rng.random()
            if r < 0.3:
                p["capacity_factor"] = round(rng.uniform(0.4, 0.7), 2)
            elif r < 0.4 and len(seas) == 2 and elvl == "daynite":
                p["capacity_factor"] = {seas[0]: round(rng.uniform(0.5, 0.8), 2), seas[1]: round(rng.uniform(0.3, 0.6), 2)}
            if rng.random() < 0.2 and leaf:
                p["availability_min"] = round(rng.uniform(0.05, 0.2), 2)
            if rng.random() < 0.1:
                p["capacity_factor_min"] = round(rng.uniform(0.05, 0.15), 2)
        else:
            key = "availability_fix" if rng.random() < 0.15 else "availability"
            p[key] = {s: round(rng.uniform(0.0, 0.5), 2) for s in leaf} if leaf else round(rng.uniform(0.2, 0.45), 2)
            p["peak_contribution"] = round(rng.uniform(0.0, 0.4), 2)
        if rng.random() < 0.35:
            p["vintaged"] = True
        r = rng.random()
        if r < 0.3:
            p["existing"] = {rng.randint(1990, 2021): round(rng.uniform(0.2, 3), 2),
                             rng.randint(2000, 2021): round(rng.uniform(0.2, 2), 2)}
        elif r < 0.45:
            p["residual"] = {2025: round(rng.uniform(0.5, 3), 2), 2025 + rng.randint(3, 15): 0}
        if rng.random() < 0.25:
            p["economic_life"] = max(1, p["lifetime"] - rng.randint(0, 3))
        if rng.random() < 0.25:
            p["hurdle_rate"] = round(rng.uniform(0.04, 0.12), 3)
        if rng.random() < 0.2:
            p["bounds"] = {"capacity": {"max": {y1: round(rng.uniform(3, 12), 1)}}}
        if rng.random() < 0.15:
            p.setdefault("bounds", {})["new_capacity"] = {"max": round(rng.uniform(1, 5), 1)}
        if rng.random() < 0.1 and "existing" not in p and "residual" not in p:
            p.setdefault("bounds", {}).setdefault("new_capacity", {})["min"] = {2025 + rng.randint(0, 6): round(rng.uniform(0.1, 0.5), 2)}
        if len(regs) > 1 and rng.random() < 0.2:
            p["regions"] = [rng.choice(regs)]
        return p

    P["GAS_PP"], P["COAL_PP"], P["WIND"], P["SOLAR"] = plant("GAS"), plant("COAL"), plant(None), plant(None)
    lamp: dict = {"inputs": ["ELC"], "outputs": ["LIGHT"]}
    if rng.random() < 0.5:
        lamp.update({"efficiency": round(rng.uniform(0.7, 1.0), 2), "lifetime": rng.choice([5, 10, 15]),
                     "invcost": series(rng, 5, 60, 2025, y1), "capacity_unit": "PJ/yr"})
        if elvl and rng.random() < 0.5:
            lamp["timeslice"] = rng.choice(["annual", "season"])
    P["LAMP"] = lamp
    kind = rng.choice(["boiler", "flex", "elec"])
    if kind == "boiler":
        P["HEATER"] = {"inputs": ["GAS"], "outputs": ["HEAT"], "efficiency": series(rng, 0.7, 0.95, 2025, y1),
                       "lifetime": 15, "invcost": 100, "emissions": "combustion", "capacity_unit": "PJ/yr"}
    elif kind == "flex":
        P["HEATER"] = {"flexible_inputs": {"GAS": {"efficiency": 0.9},
                                           "ELC": {"efficiency": 1.0, "max_share": round(rng.uniform(0.3, 0.9), 2)},
                                           "COAL": {"efficiency": 0.7, "max_share": 0.5}},
                       "outputs": ["HEAT"], "lifetime": 15, "invcost": 150, "emissions": "combustion",
                       "capacity_unit": "PJ/yr"}
        if rng.random() < 0.3:
            P["HEATER"]["flexible_inputs"]["GAS"]["min_share"] = 0.1
    else:
        P["HEATER"] = {"inputs": ["ELC"], "outputs": ["HEAT"], "efficiency": 0.98, "lifetime": 12, "invcost": 120,
                       "capacity_unit": "PJ/yr"}
    if elvl and rng.random() < 0.6:
        P["HEATER"]["timeslice"] = rng.choice(["annual", "season"])
    if rng.random() < 0.3:
        P["HEATER"]["vintaged"] = True
    if rng.random() < 0.3:
        P["CHP"] = {"inputs": {"GAS": 1}, "outputs": {"ELC": 1, "HEAT": round(rng.uniform(0.5, 1.5), 2)},
                    "efficiency": 0.35, "lifetime": 20, "invcost": 1200, "fixom": 30, "availability": 0.8,
                    "emissions": "combustion"}
    if rng.random() < 0.2:
        # a refinery-like process with a flexible output mix serving two more demands
        C["OIL"] = {"kind": "energy"}
        C["PETROL"] = {"kind": "demand", "demand": series(rng, 10, 40, 2025, y1)}
        C["DIESEL"] = {"kind": "demand", "demand": series(rng, 10, 40, 2025, y1)}
        m["emission_factors"]["OIL"] = {"CO2": 73.3}
        P["OIL_IMP"] = {"outputs": ["OIL"], "varom": series(rng, 8, 14, 2025, y1)}
        P["REFINERY"] = {"inputs": {"OIL": 1.0, "ELC": 0.01}, "efficiency": 0.92,
                         "flexible_outputs": {"PETROL": {"max_share": 0.7}, "DIESEL": {"max_share": 0.8, "min_share": 0.2}},
                         "lifetime": 30, "invcost": 60, "fixom": 2, "capacity_unit": "PJ/yr",
                         "emissions": {"CO2": {"OIL": 5.0, "activity": 1.5}}}
        P["PETROL_BACKSTOP"] = {"outputs": ["PETROL"], "varom": 200}
        P["DIESEL_BACKSTOP"] = {"outputs": ["DIESEL"], "varom": 200}
    m["processes"] = P

    # -- user constraints ---------------------------------------------------
    ucs: dict = {}
    if rng.random() < 0.4:
        ucs["RPS"] = {"terms": [{"flow": "ELC", "of": ["tag:renewable"], "coef": 1},
                                {"production": "ELC", "coef": -round(rng.uniform(0.1, 0.5), 2)}],
                      "sense": ">=", "rhs": 0, "regions": rng.choice(["each", "sum"])}
    if rng.random() < 0.3:
        ucs["CUMEM"] = {"periods": "sum", "terms": [{"emission": "CO2", "coef": 1},
                                                    {"new_capacity": ["WIND", "SOLAR"], "coef": -50}],
                        "sense": "<=", "rhs": 0}
    if rng.random() < 0.3:
        ucs["FLEET"] = {"terms": [{"capacity": ["GAS_PP", "COAL_PP"], "coef": 1}], "sense": "<=", "rhs": 0}
    if rng.random() < 0.15:
        ucs["COALUSE"] = {"terms": [{"consumption": "COAL", "coef": 1}, {"activity": ["GAS_IMP"], "coef": -0.8}],
                          "sense": "<=", "rhs": 5}
    if ucs:
        m["constraints"] = ucs
    return m, policy


def run(seed: int, work: str, gams: str | None = None, times_src: str | None = None) -> tuple[str, str]:
    """Generate, size the policies, solve both ways. Returns (status, detail)."""
    m, policy = generate(seed)
    d = os.path.join(work, f"fz{seed}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)

    def dump():
        with open(os.path.join(d, "model.yaml"), "w") as fh:
            yaml.safe_dump(m, fh, sort_keys=False)

    uc = m.get("constraints", {})
    held = {k: uc.pop(k) for k in ("CUMEM", "FLEET") if k in uc}
    if "constraints" in m and not m["constraints"]:
        del m["constraints"]
    dump()
    model, diags = load(d)
    if model is None:
        return "MODELERR", diags.errors[0].message
    base = solve(model)
    if base.sol.status != "optimal":
        return "SKIP", f"unconstrained model is {base.sol.status}"
    P = model.periods
    em: dict = {}
    for row in base.tables["emissions"]:
        key = (row["region"], row["period"])
        em[key] = em.get(key, 0.0) + row["value"]
    by_region = [sum(v * P.D[P.M.index(k[1])] for k, v in em.items() if k[0] == r) for r in model.regions]
    last = [v for k, v in em.items() if k[1] == P.M[-1]] or [1.0]
    if policy == "cap":
        m["commodities"]["CO2"]["cap"] = {P.M[-1]: round(0.8 * max(1.0, max(last)), 1)}
    if policy == "budget":
        m["commodities"]["CO2"]["budget"] = round(0.9 * max(by_region + [1.0]) + 1, 1)
    if held:
        m.setdefault("constraints", {})
        if "CUMEM" in held:
            held["CUMEM"]["rhs"] = round(0.95 * sum(by_region) / len(model.regions) + 10, 1)
            m["constraints"]["CUMEM"] = held["CUMEM"]
        if "FLEET" in held:
            caps: dict = {}
            for row in base.tables["capacity"]:
                if row["process"] in ("GAS_PP", "COAL_PP"):
                    key = (row["region"], row["period"])
                    caps[key] = caps.get(key, 0.0) + row["capacity"]
            held["FLEET"]["rhs"] = round(max(list(caps.values()) + [1.0]) * 1.02 + 0.5, 2)
            m["constraints"]["FLEET"] = held["FLEET"]
    dump()
    model, diags = load(d)
    if model is None:
        return "MODELERR", diags.errors[0].message
    native = solve(model)
    times = run_times(model, os.path.join(d, "dd"), gams=gams, times_src=times_src)
    if times.get("status") == "license_limit":
        return "TOOBIG", ""
    rep = compare(native, times)
    if rep.get("native_status") != "optimal" or rep.get("times_status") != "optimal":
        same = rep.get("native_status") == rep.get("times_status")
        return ("SAME" if same else "STATUSDIFF"), f"native {rep.get('native_status')}, official {rep.get('times_status')} {times.get('message', '')}"
    detail = (f"native {rep['objective_native']:.8f} official {rep['objective_times']:.8f} "
              f"rel {rep['objective_rel_diff']:.1e} qa={rep.get('qa_passed')}")
    return ("OK" if rep["objective_rel_diff"] < TOL else "DIFF"), detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("--keep", metavar="DIR", help="keep the generated models and exports in DIR")
    ap.add_argument("--gams", default=os.environ.get("PLAINTIMES_GAMS"))
    ap.add_argument("--times-src", default=os.environ.get("PLAINTIMES_TIMES_SRC"))
    args = ap.parse_args()
    work = args.keep or tempfile.mkdtemp(prefix="plaintimes-fuzz-")
    os.makedirs(work, exist_ok=True)
    counts: dict = {}
    for seed in range(args.start, args.end):
        try:
            status, detail = run(seed, work, args.gams, args.times_src)
        except Exception as exc:  # a crash is a finding too
            status, detail = "CRASH", repr(exc)
        counts[status] = counts.get(status, 0) + 1
        if status not in ("OK", "SAME"):
            print(seed, status, detail[:300], flush=True)
        if not args.keep:
            shutil.rmtree(os.path.join(work, f"fz{seed}"), ignore_errors=True)
    print(counts, flush=True)
    return 1 if any(k in counts for k in ("DIFF", "STATUSDIFF", "CRASH")) else 0


if __name__ == "__main__":
    sys.exit(main())
