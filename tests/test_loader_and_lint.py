"""Loading, scenario overlays, diagnostics and static checks."""
from plaintimes.api import load

BASE = """
    commodities:
      GAS: {kind: energy}
      ELC: {kind: demand, demand: {2025: 100}}
    processes:
      GAS_IMP: {outputs: [GAS], varom: 4}
      GAS_PP: {inputs: [GAS], outputs: [ELC], efficiency: 0.5}
    """


def codes(diags, severity=None):
    return [d.code for d in diags.items if severity is None or d.severity == severity]


def test_clean_model_has_no_diagnostics(make_model):
    model, diags = load(make_model(BASE))
    assert model is not None and not diags.items


def test_unknown_key_gets_a_suggestion_and_a_line(make_model):
    path = make_model(BASE.replace("efficiency: 0.5", "eficiency: 0.5"))
    model, diags = load(path)
    assert model is None
    d = diags.errors[0]
    assert d.code == "PT040" and "efficiency" in d.hint
    lines = open(d.file).read().splitlines()
    assert d.file.endswith("model.yaml") and "eficiency" in lines[d.line - 1]
    assert d.path == "processes.GAS_PP.eficiency"


def test_duplicate_key_is_an_error(make_model):
    text = BASE.replace("      GAS_PP:", "      GAS_IMP: {outputs: [GAS], varom: 9}\n      GAS_PP:")
    model, diags = load(make_model(text))
    assert model is None and "PT002" in codes(diags)


def test_unknown_commodity_reference(make_model):
    model, diags = load(make_model(BASE.replace("inputs: [GAS]", "inputs: [GASS]")))
    assert model is None
    d = [x for x in diags.errors if x.code == "PT110"][0]
    assert "GAS" in d.hint


def test_includes_merge_and_report_their_own_file(make_model):
    path = make_model("""
        include: ["parts/*.yaml"]
        commodities:
          GAS: {kind: energy}
          ELC: {kind: demand, demand: 100}
        """, extra={
        "parts/a.yaml": "processes:\n  GAS_IMP: {outputs: [GAS], varom: 4}\n",
        "parts/b.yaml": "processes:\n  GAS_PP: {inputs: [GAS], outputs: [ELC], eff: 0.5}\n",
    })
    model, diags = load(path)
    assert model is None
    d = diags.errors[0]
    assert d.file.endswith("parts/b.yaml") and d.line == 2


def test_same_name_in_two_files_is_an_error(make_model):
    path = make_model("include: [extra.yaml]\n" + BASE.replace("\n    ", "\n"), extra={
        "extra.yaml": "processes:\n  GAS_PP: {inputs: [GAS], outputs: [ELC], efficiency: 0.6}\n"})
    model, diags = load(path)
    assert model is None and "PT007" in codes(diags)


def test_scenario_replaces_series_whole_and_null_deletes(make_model):
    path = make_model("""
        commodities:
          GAS: {kind: energy}
          ELC: {kind: demand, demand: {2025: 100, 2030: 120}}
        processes:
          GAS_IMP: {outputs: [GAS], varom: 4}
          GAS_PP: {inputs: [GAS], outputs: [ELC], efficiency: 0.5}
          SPARE: {outputs: [ELC], varom: 50}
        """, extra={"scenarios/s1.yaml": """
        description: test overlay
        commodities:
          ELC:
            demand: {2025: 200}
        processes:
          SPARE: null
          GAS_PP: {efficiency: 0.6}
        """})
    base, _ = load(path)
    scen, diags = load(path, ["s1"])
    assert scen is not None, diags.render()
    assert base.commodities["ELC"].demand.at("R1", 2030) == 120
    assert scen.commodities["ELC"].demand.at("R1", 2030) == 200      # the old 2030 point is gone
    assert "SPARE" in base.processes and "SPARE" not in scen.processes
    assert scen.processes["GAS_PP"].efficiency.at("R1", 2025) == 0.6
    assert list(scen.processes["GAS_PP"].inputs) == ["GAS"]          # untouched keys survive the merge
    assert scen.raw.scenarios == ["s1"]


def test_missing_scenario_lists_the_available_ones(make_model):
    path = make_model(BASE, extra={"scenarios/real.yaml": "description: x\n"})
    model, diags = load(path, ["nope"])
    assert model is None
    assert "real" in diags.errors[0].hint


def test_timeslice_fractions_must_sum_to_one(make_model):
    model, diags = load(make_model("timeslices: {D: 0.5, N: 0.4}\n" + BASE.replace("\n    ", "\n")))
    assert model is None and "PT027" in codes(diags)


def test_bound_series_do_not_extrapolate(make_model):
    head = """
        plaintimes: 1
        name: t
        discount_rate: 0.05
        regions: [R1]
        periods: {milestones: [2025, 2030, 2040]}
        units: {energy: PJ, capacity: GW}
        """
    model, diags = load(make_model("""
        commodities:
          ELC: {kind: demand, demand: {2025: 100, 2040: 140}}
        processes:
          PP: {outputs: [ELC], lifetime: 30, invcost: {2025: 10, 2040: 5}, bounds: {capacity: {max: {2030: 9}}}}
        """, head=head))
    assert model is not None, diags.render()
    bound = model.processes["PP"].bounds[("capacity", "max")]
    assert [bound.at("R1", y) for y in (2025, 2030, 2040)] == [None, 9, None]
    cost = model.processes["PP"].invcost
    assert [cost.at("R1", y) for y in (2020, 2030, 2050)] == [10, 25 / 3, 5]      # data: interpolate, hold flat


# -- static checks ---------------------------------------------------------------------------
def test_demand_without_supplier(make_model):
    model, diags = load(make_model("""
        commodities:
          ELC: {kind: demand, demand: 100}
          HEAT: {kind: demand, demand: 50}
        processes:
          PP: {outputs: [ELC], varom: 1}
        """))
    assert model is None and "PT300" in codes(diags, "error")


def test_input_without_source_and_dead_end(make_model):
    model, diags = load(make_model("""
        commodities:
          GAS: {kind: energy}
          WASTE: {kind: energy}
          ELC: {kind: demand, demand: 100}
        processes:
          GAS_PP: {inputs: [GAS], outputs: {ELC: 1, WASTE: 0.1}, efficiency: 0.5}
          OTHER: {outputs: [ELC], varom: 30}
        """))
    assert model is not None
    assert {"PT301", "PT302"} <= set(codes(diags, "warning"))


def test_demand_not_covering_the_horizon_is_flagged(make_model):
    head = """
        plaintimes: 1
        name: t
        discount_rate: 0.05
        regions: [R1]
        periods: {milestones: [2025, 2030, 2040]}
        units: {energy: PJ, capacity: GW}
        """
    model, diags = load(make_model("""
        commodities:
          ELC: {kind: demand, demand: {2025: 100, 2030: 120}}
        processes:
          PP: {outputs: [ELC], varom: 1}
        """, head=head))
    assert "PT305" in codes(diags, "warning")


def test_retired_stock_and_impossible_shares(make_model):
    model, diags = load(make_model("""
        commodities:
          A: {kind: energy}
          B: {kind: energy}
          ELC: {kind: demand, demand: 100}
        processes:
          SA: {outputs: [A], varom: 1}
          SB: {outputs: [B], varom: 1}
          OLD: {inputs: [A], outputs: [ELC], lifetime: 20, existing: {1990: 5}}
          MIX:
            flexible_inputs: {A: {max_share: 0.3}, B: {max_share: 0.3}}
            outputs: [ELC]
        """))
    assert "PT306" in codes(diags, "warning")
    assert "PT310" in codes(diags, "error")


def test_process_that_can_never_have_capacity(make_model):
    model, diags = load(make_model("""
        commodities:
          ELC: {kind: demand, demand: 100}
        processes:
          PP: {outputs: [ELC], lifetime: 30, fixom: 2}
        """))
    assert model is None and "PT126" in codes(diags)


def test_capacity_ceiling_below_existing_stock(make_model):
    model, diags = load(make_model("""
        commodities:
          ELC: {kind: demand, demand: 10}
        processes:
          OLD: {outputs: [ELC], lifetime: 40, existing: {2010: 5}, bounds: {capacity: {max: 3}}, capacity_unit: PJ/yr}
          NEW: {outputs: [ELC], varom: 1}
        """))
    assert model is None and "PT320" in codes(diags, "error")


def test_period_layout_decides_the_accounting_variant():
    from plaintimes.diagnostics import Diagnostics
    from plaintimes.timebase import Periods

    def periods(raw):
        return Periods.from_raw(raw, Diagnostics())

    assert periods({"start": 2025, "lengths": [5, 10]}).mode == "std"
    p = periods({"milestones": [2025, 2030, 2040]})
    assert (p.B, p.E, p.mode) == ([2025, 2028, 2036], [2027, 2035, 2044], "mod")
    # milestones that happen to sit in the middle of their periods are the standard layout
    p = periods({"milestones": [2025, 2030, 2035], "start": 2024})
    assert (p.B, p.E, p.mode) == ([2024, 2028, 2033], [2027, 2032, 2037], "std")


def test_uncharged_first_build_is_flagged():
    import os

    case = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity", "cases", "fz291.yaml")
    model, diags = load(case)
    assert model is not None
    hits = [d for d in diags.warnings if d.code == "PT321"]
    assert hits and "WIND" in hits[0].message and "periods" in hits[0].hint
