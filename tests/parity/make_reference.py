"""Regenerate tests/parity/reference.json by solving every case with the official TIMES code.

Needs GAMS and the TIMES source:

    PLAINTIMES_GAMS=/path/to/gams PLAINTIMES_TIMES_SRC=/path/to/TIMES_model python tests/parity/make_reference.py

The cases are small enough for the free GAMS demo licence.  The recorded
objectives let the normal test suite check parity with official TIMES on any
machine, without GAMS.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))

from plaintimes.api import load  # noqa: E402
from plaintimes.oracle import compare, run_times  # noqa: E402
from plaintimes.results import solve  # noqa: E402


def main() -> int:
    ref: dict[str, dict] = {}
    cases = sorted(glob.glob(os.path.join(HERE, "cases", "*.yaml")))
    demo = os.path.join(HERE, "..", "..", "src", "plaintimes", "templates", "demo")
    jobs = [(os.path.splitext(os.path.basename(c))[0], c, []) for c in cases]
    for scen in ([], ["carbon_cap"], ["carbon_tax"], ["high_gas"], ["renewables_target"], ["carbon_cap", "high_gas"]):
        jobs.append(("demo" + ("+" + "+".join(scen) if scen else ""), demo, scen))
    bad = 0
    for name, path, scen in jobs:
        model, diags = load(path, scen)
        if model is None:
            print(f"{name}: MODEL ERRORS\n{diags.render()}")
            bad += 1
            continue
        run = solve(model)
        with tempfile.TemporaryDirectory() as tmp:
            times = run_times(model, tmp)
            rep = compare(run, times)
            qa_version = "unknown"
            qa = os.path.join(tmp, "qa_check.log")
            if os.path.exists(qa):
                m = re.search(r"VERSION\s+([0-9.]+)", open(qa, errors="replace").read())
                qa_version = m.group(1) if m else qa_version
        if times.get("status") != "optimal":
            print(f"{name}: official TIMES status {times.get('status')}: {times.get('message', '')}")
            bad += 1
            continue
        ref[name] = {"objective": times["objective"], "times_version": qa_version,
                     "qa_passed": bool(times.get("qa_passed")),
                     "scenarios": scen, "model": "demo" if path == demo else os.path.basename(path)}
        flag = "ok " if rep["agree"] else "DIFF"
        if not rep["agree"]:
            bad += 1
        print(f"{flag} {name:28s} native {rep['objective_native']:.10g}  times {rep['objective_times']:.10g}  rel {rep['objective_rel_diff']:.1e}")
    with open(os.path.join(HERE, "reference.json"), "w") as fh:
        json.dump(ref, fh, indent=1, sort_keys=True)
    print(f"\nwrote reference.json with {len(ref)} cases; {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
