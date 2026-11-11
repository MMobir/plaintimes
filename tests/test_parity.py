"""Parity with the official ETSAP TIMES code.

reference.json holds objective values obtained by exporting each case with
the GAMS bridge and solving it with the official TIMES source (see
make_reference.py).  These tests check that the native kernel reproduces
them, on any machine, without GAMS.
"""
import json
import os

import pytest

from plaintimes.api import load
from plaintimes.results import solve

from conftest import DEMO

HERE = os.path.dirname(os.path.abspath(__file__))
REF = json.load(open(os.path.join(HERE, "parity", "reference.json")))


@pytest.mark.parametrize("name", sorted(REF))
def test_matches_official_times(name):
    ref = REF[name]
    path = DEMO if ref["model"] == "demo" else os.path.join(HERE, "parity", "cases", ref["model"])
    model, diags = load(path, ref["scenarios"])
    assert model is not None, diags.render()
    run = solve(model)
    assert run.sol.status == "optimal"
    rel = abs(run.sol.objective - ref["objective"]) / abs(ref["objective"])
    assert rel < 1e-8, f"native {run.sol.objective!r} vs official TIMES {ref['objective']!r} (rel {rel:.2e})"


@pytest.mark.skipif(not (os.environ.get("PLAINTIMES_GAMS") and os.environ.get("PLAINTIMES_TIMES_SRC")),
                    reason="live comparison needs PLAINTIMES_GAMS and PLAINTIMES_TIMES_SRC")
def test_live_oracle(tmp_path):
    from plaintimes.oracle import compare, run_times

    model, _ = load(DEMO, ["carbon_cap"])
    run = solve(model)
    rep = compare(run, run_times(model, str(tmp_path)))
    assert rep["agree"], rep
