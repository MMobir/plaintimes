"""The command line (JSON mode) and the GAMS bridge files."""
import json
import os

import pytest

from plaintimes.api import load
from plaintimes.cli import main
from plaintimes.ddexport import export_dd

from conftest import DEMO, EXAMPLES


def cli(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, out


def cli_json(capsys, *argv):
    code, out = cli(capsys, *argv, "--json")
    return code, json.loads(out)


def test_check_reports_ok(capsys):
    code, data = cli_json(capsys, "check", DEMO)
    assert code == 0 and data["ok"] and data["errors"] == 0
    assert data["model"]["counts"]["processes"] == 14


def test_check_fails_with_exit_code_1(capsys, demo_copy):
    f = os.path.join(demo_copy, "processes", "power.yaml")
    open(f, "a").write("  BROKEN: {inputs: [NOPE], outputs: [ELC]}\n")
    code, data = cli_json(capsys, "check", demo_copy)
    assert code == 1 and not data["ok"]
    d = data["diagnostics"][0]
    assert d["code"] == "PT110" and d["file"] == os.path.join("processes", "power.yaml") and d["line"] > 60


def test_solve_writes_a_run_directory(capsys, demo_copy):
    code, data = cli_json(capsys, "solve", demo_copy, "-s", "carbon_cap")
    assert code == 0 and data["status"] == "optimal"
    run_dir = data["run_dir"]
    assert run_dir.endswith(os.path.join("runs", "carbon_cap"))
    for name in ("capacity.csv", "flows.csv", "balance.csv", "summary.json", "manifest.json"):
        assert os.path.exists(os.path.join(run_dir, name))
    manifest = json.load(open(os.path.join(run_dir, "manifest.json")))
    assert manifest["scenarios"] == ["carbon_cap"] and len(manifest["input_fingerprint"]) == 16
    assert data["emissions"]["CO2"]["2050"] <= 6000 + 1e-6


def test_infeasible_model_exits_2_and_relax_explains(capsys, demo_copy):
    open(os.path.join(demo_copy, "scenarios", "impossible.yaml"), "w").write(
        "commodities:\n  CO2:\n    cap: {2025: 100, 2050: 100}\n")
    code, data = cli_json(capsys, "solve", demo_copy, "-s", "impossible", "--no-write")
    assert code == 2 and data["status"] == "infeasible" and "relax" in data["hint"]
    code, data = cli_json(capsys, "explain", demo_copy, "-s", "impossible", "infeasible")
    assert code == 0 and not data["feasible_without_relaxation"]
    assert data["unserved_by_commodity"]


def test_diff_between_two_runs(capsys, demo_copy):
    cli(capsys, "solve", demo_copy)
    cli(capsys, "solve", demo_copy, "-s", "carbon_tax")
    code, data = cli_json(capsys, "diff", os.path.join(demo_copy, "runs", "base"), os.path.join(demo_copy, "runs", "carbon_tax"))
    assert code == 0 and data["objective_change"] > 0
    assert data["tables"]["emissions"]["changed_rows"] > 0


def test_explain_process_breaks_even(capsys):
    code, data = cli_json(capsys, "explain", DEMO, "process", "WIND", "--period", "2040")
    assert code == 0
    op = data["operation"][0]
    assert op["activity"] > 0 and abs(op["net_per_unit_activity"]) < 1e-6
    assert any(b["item"] == "capacity_rent" for b in op["breakdown"])
    inv = data["investment"][0]
    assert inv["new_capacity"] > 0 and abs(inv["reduced_cost"]) < 1e-6
    # new coal is not built in 2040, and the report says by how much it misses
    code, data = cli_json(capsys, "explain", DEMO, "process", "COAL_PP", "--period", "2040")
    inv = data["investment"][0]
    assert "not built" in inv["reading"] and inv["reduced_cost"] > 0
    assert sum(inv["discounted_value_per_unit"].values()) + inv["reduced_cost"] == pytest.approx(inv["discounted_cost_per_unit"])


def test_explain_price(capsys):
    code, data = cli_json(capsys, "explain", DEMO, "price", "ELC", "--period", "2030")
    assert code == 0
    slices = data["periods"][0]["timeslices"]
    assert [s["timeslice"] for s in slices] == ["WD", "WN", "SD", "SN"]
    assert all(s["price"] > 0 and s["suppliers"] for s in slices)


def test_show_and_res(capsys):
    code, data = cli_json(capsys, "show", DEMO, "GAS_CC")
    assert code == 0 and data["vintaged"] and data["defined_at"].startswith(os.path.join("processes", "power.yaml"))
    assert data["regions"]["NORTH"]["upstream"] == {"GAS": ["GAS_IMP"]}
    code, data = cli_json(capsys, "res", DEMO)
    assert "GAS_CC" in data["regions"]["NORTH"]["ELC"]["producers"]
    assert data["mermaid"].startswith("flowchart LR")
    code, data = cli_json(capsys, "show", DEMO, "GAS_CCC")
    assert code == 4 and "GAS_CC" in data["error"]


def test_expectations(capsys, demo_copy):
    code, data = cli_json(capsys, "test", demo_copy)
    assert code == 0 and data["failed"] == 0 and data["passed"] == 4
    f = os.path.join(demo_copy, "expect.yaml")
    open(f, "a").write("  - name: impossible\n    table: capacity\n    where: {process: HYDRO, period: 2030}\n    max: 0.5\n")
    code, data = cli_json(capsys, "test", demo_copy)
    assert code == 3 and data["failed"] == 1


def test_examples_solve_and_pass_their_checks(capsys):
    code, data = cli_json(capsys, "test", os.path.join(EXAMPLES, "two_regions"))
    assert code == 0, data


def test_schema_and_times_map(capsys):
    code, out = cli(capsys, "schema", "--json")
    data = json.loads(out)
    assert "processes.<NAME>" in data["sections"]
    code, out = cli(capsys, "schema", "--times-map")
    assert "NCAP_COST" in out


def test_schema_covers_every_key_the_loader_accepts():
    from plaintimes import model as M
    from plaintimes.schema import SCHEMA

    def keys(section):
        out = set()
        for k in SCHEMA[section]:
            out |= {x.strip() for x in k.split("/")}
        return out

    assert M.COMMODITY_KEYS <= keys("commodities.<NAME>")
    assert M.PROCESS_KEYS <= keys("processes.<NAME>")
    assert M.STORAGE_KEYS <= keys("storage.<NAME>")
    assert M.TRADE_KEYS <= keys("trade.<NAME>")
    assert M.CONSTRAINT_KEYS <= keys("constraints.<NAME>")


def test_init_creates_a_working_model(capsys, tmp_path):
    target = str(tmp_path / "mymodel")
    code, data = cli_json(capsys, "init", target)
    assert code == 0 and "model.yaml" in data["files"]
    code, data = cli_json(capsys, "check", target)
    assert code == 0


def test_export_dd_files(tmp_path):
    model, _ = load(DEMO, ["renewables_target"])
    info = export_dd(model, str(tmp_path))
    dd = open(info["dd"]).read()
    run = open(info["run"]).read()
    assert os.path.basename(info["dd"]) == "demo.dd"
    for token in ("SET MODLYEAR", "NCAP_COST ' '/", "NORTH.2012.GAS_CC 1.5", "UC_RHSRT", "COM_PKRSV", "PRC_VINT"):
        assert token in dd
    assert dd.index("SET UNITS\n") < dd.index("SET UNITS_MONY")          # sets before their subsets
    assert "$SET OBLONG   YES" in run and "SET MILESTONYR / 2025, 2030, 2040, 2050 /;" in run
    assert not info["not_exported"]


def test_export_refuses_what_it_cannot_express(tmp_path):
    model, _ = load(os.path.join(EXAMPLES, "two_regions"))
    with pytest.raises(NotImplementedError) as exc:
        export_dd(model, str(tmp_path))
    assert "storage.BATTERY" in str(exc.value) and "trade.COAST_INLAND" in str(exc.value)
    info = export_dd(model, str(tmp_path), allow_partial=True)
    assert len(info["not_exported"]) == 2
