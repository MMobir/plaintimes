"""Models small enough to solve by hand."""
import pytest

from plaintimes.api import load
from plaintimes.results import solve


def run(path, scenarios=()):
    model, diags = load(path, list(scenarios))
    assert model is not None, diags.render()
    r = solve(model)
    assert r.sol.status == "optimal", r.summary
    return r


def rows(r, table, **where):
    return [x for x in r.tables[table] if all(x[k] == v for k, v in where.items())]


def test_fuel_chain(make_model):
    r = run(make_model("""
        commodities:
          GAS: {kind: energy}
          ELC: {kind: demand, demand: 100}
        processes:
          GAS_IMP: {outputs: [GAS], varom: 4}
          GAS_PP: {inputs: [GAS], outputs: [ELC], efficiency: 0.5}
        """))
    assert r.sol.objective == pytest.approx(800.0)          # 100 / 0.5 * 4
    price = {x["commodity"]: x["price"] for x in r.tables["balance"]}
    assert price == pytest.approx({"GAS": 4.0, "ELC": 8.0})


def test_discounting_sums_every_year(make_model):
    head = """
        plaintimes: 1
        name: t
        discount_rate: 0.05
        regions: [R1]
        periods: {start: 2025, lengths: [2, 3]}
        units: {energy: PJ, capacity: GW}
        """
    r = run(make_model("""
        commodities:
          X: {kind: demand, demand: 10}
        processes:
          SUP: {outputs: [X], varom: 1}
        """, head=head))
    assert r.sol.objective == pytest.approx(10 * sum(1.05 ** -y for y in range(5)))
    assert [p["period"] for p in r.summary["periods"]] == [2025, 2028]


def test_capacity_and_investment_cost(make_model):
    # 1-year horizon, lifetime 1: the whole investment falls inside the horizon, no salvage
    r = run(make_model("""
        commodities:
          ELC: {kind: demand, demand: 31.536}
        processes:
          PP: {outputs: [ELC], lifetime: 1, invcost: 100, fixom: 5, availability: 0.5}
        """))
    cap = rows(r, "capacity", process="PP")[0]["capacity"]
    assert cap == pytest.approx(2.0)                         # 31.536 PJ / (0.5 * 31.536 PJ per GW)
    assert r.sol.objective == pytest.approx(2.0 * 105)


def test_salvage_credits_unused_life(make_model):
    # lifetime 2 in a 1-year horizon, zero discounting: half the investment is salvaged
    head = """
        plaintimes: 1
        name: t
        discount_rate: 0.0
        regions: [R1]
        periods: {start: 2025, lengths: [1]}
        units: {energy: PJ, capacity: GW}
        """
    r = run(make_model("""
        commodities:
          ELC: {kind: demand, demand: 31.536}
        processes:
          PP: {outputs: [ELC], lifetime: 2, invcost: 100}
        """, head=head))
    assert r.sol.objective == pytest.approx(50.0)


TWO_SLICES = """
    plaintimes: 1
    name: t
    discount_rate: 0.05
    regions: [R1]
    periods: {start: 2025, lengths: [1]}
    timeslices: {D: 0.5, N: 0.5}
    units: {energy: PJ, capacity: PJ/yr}
    """


def test_storage_shifts_day_to_night(make_model):
    r = run(make_model("""
        commodities:
          ELC: {kind: energy, timeslice: season}
          LOAD: {kind: demand, timeslice: season, demand: 100, load_shape: {D: 0.5, N: 0.5}}
        processes:
          SUN: {outputs: [ELC], varom: 1, lifetime: 1, invcost: 0, availability: {D: 1, N: 0}}
          PEAKER: {outputs: [ELC], varom: 10}
          GRID: {inputs: [ELC], outputs: [LOAD]}
        storage:
          BATT: {commodity: ELC, efficiency: 0.8, lifetime: 1, invcost: 0}
        """, head=TWO_SLICES))
    flows = {(x["process"], x["direction"], x["timeslice"]): x["value"] for x in r.tables["flows"] if x["commodity"] == "ELC"}
    assert flows[("BATT", "in", "D")] == pytest.approx(62.5)      # 50 / 0.8
    assert flows[("BATT", "out", "N")] == pytest.approx(50.0)
    assert ("PEAKER", "out", "N") not in flows
    assert r.sol.objective == pytest.approx(112.5)                 # all energy from SUN at 1 per PJ
    price = {x["timeslice"]: x["price"] for x in rows(r, "balance", commodity="ELC")}
    assert price == pytest.approx({"D": 1.0, "N": 1.25})            # night power carries the storage loss


def test_storage_energy_limit(make_model):
    # 2 hours of storage at 1 GW cannot move more than 2 GWh per day
    head = TWO_SLICES.replace("timeslices: {D: 0.5, N: 0.5}", "timeslices: {A: {D: 0.5, N: 0.5}}").replace("PJ/yr", "GW")
    r = run(make_model("""
        commodities:
          ELC: {kind: energy, timeslice: daynite}
          LOAD: {kind: demand, timeslice: daynite, demand: 100, load_shape: {AD: 0.5, AN: 0.5}}
        processes:
          SUN: {outputs: [ELC], varom: 1, lifetime: 1, invcost: 0, availability: {AD: 1, AN: 0}}
          PEAKER: {outputs: [ELC], varom: 10}
          GRID: {inputs: [ELC], outputs: [LOAD]}
        storage:
          BATT: {commodity: ELC, hours: 2, lifetime: 1, invcost: 0, bounds: {capacity: {max: 1}}}
        """, head=head))
    out = sum(x["value"] for x in r.tables["flows"] if x["process"] == "BATT" and x["direction"] == "out")
    assert out == pytest.approx(365 * 2 * 3.6e-3)                  # 730 GWh = 2.628 PJ a year


def test_trade_arbitrage(make_model):
    head = """
        plaintimes: 1
        name: t
        discount_rate: 0.05
        regions: [A, B]
        periods: {start: 2025, lengths: [1]}
        units: {energy: PJ, capacity: GW}
        """
    r = run(make_model("""
        commodities:
          ELC: {kind: demand, demand: {A: 0, B: 100}}
          FUEL: {kind: energy}
        processes:
          SUP: {outputs: [FUEL], varom: {A: 2, B: 10}}
          USE: {inputs: [FUEL], outputs: [ELC]}
        trade:
          LINK: {commodity: FUEL, from: A, to: B, efficiency: 0.9, varom: 1}
        """, head=head))
    assert r.sol.objective == pytest.approx(100 / 0.9 * 3)
    price_b = rows(r, "balance", region="B", commodity="FUEL")[0]["price"]
    assert price_b == pytest.approx(3 / 0.9)
    assert rows(r, "activity", process="LINK")[0]["activity"] == pytest.approx(100 / 0.9)


def test_emission_tax_switches_fuel(make_model):
    text = """
        commodities:
          COAL: {kind: energy}
          GAS: {kind: energy}
          ELC: {kind: demand, demand: 100}
          CO2: {kind: emission, unit: kt%s}
        emission_factors:
          COAL: {CO2: 100}
          GAS: {CO2: 50}
        processes:
          COAL_IMP: {outputs: [COAL], varom: 2}
          GAS_IMP: {outputs: [GAS], varom: 5}
          COAL_PP: {inputs: [COAL], outputs: [ELC], efficiency: 0.4, emissions: combustion}
          GAS_PP: {inputs: [GAS], outputs: [ELC], efficiency: 0.5, emissions: combustion}
        """
    base = run(make_model(text % ""))
    assert base.sol.objective == pytest.approx(500.0)              # coal: 100 / 0.4 * 2
    assert base.summary["emissions"]["CO2"][2025] == pytest.approx(25000)
    # coal costs 5 + 250 t per PJ of power, gas 10 + 100 t: they tie at a tax of 1/30 per kt
    taxed = run(make_model(text % ", tax: 0.04"))
    assert taxed.summary["emissions"]["CO2"][2025] == pytest.approx(10000)
    assert taxed.sol.objective == pytest.approx(1000 + 0.04 * 10000)


def test_relaxed_solve_locates_shortfall(make_model):
    path = make_model("""
        commodities:
          ELC: {kind: demand, demand: 100}
        processes:
          PP: {outputs: [ELC], lifetime: 10, invcost: 10, bounds: {capacity: {max: 1}}, capacity_unit: PJ/yr}
        """)
    model, _ = load(path)
    assert solve(model).sol.status == "infeasible"
    relaxed = solve(model, relax=True)
    assert relaxed.sol.status == "optimal"
    assert relaxed.tables["unserved"][0]["commodity"] == "ELC"
    assert relaxed.tables["unserved"][0]["unserved"] == pytest.approx(99.0)


def test_coarse_process_follows_the_load_shape_of_what_it_serves(make_model):
    # An annual-level heater serves a demand that is 80% in winter. Its electricity use must be
    # 80% in winter too (not spread by hours), which is what official TIMES does.
    head = TWO_SLICES.replace("timeslices: {D: 0.5, N: 0.5}", "timeslices: {W: 0.5, S: 0.5}")
    r = run(make_model("""
        commodities:
          ELC: {kind: energy, timeslice: season}
          HEAT: {kind: demand, timeslice: season, demand: 100, load_shape: {W: 0.8, S: 0.2}}
        processes:
          GEN: {outputs: [ELC], varom: 1}
          HEATER: {inputs: [ELC], outputs: [HEAT], timeslice: annual}
        """, head=head))
    use = {x["timeslice"]: x["value"] for x in r.tables["flows"] if x["process"] == "HEATER" and x["commodity"] == "ELC"}
    assert use == pytest.approx({"W": 80.0, "S": 20.0})


def test_flexible_mix_can_differ_by_timeslice(make_model):
    # Night power is cheap, day power dear: an annual-level hybrid heater should take power at
    # night and gas by day, up to its 60% power share in each timeslice.
    r = run(make_model("""
        commodities:
          GAS: {kind: energy}
          ELC: {kind: energy, timeslice: season}
          HEAT: {kind: demand, demand: 100}
        processes:
          GAS_IMP: {outputs: [GAS], varom: 5}
          DAY_GEN: {outputs: [ELC], varom: 9, lifetime: 1, invcost: 0, availability: {D: 1, N: 0}}
          NIGHT_GEN: {outputs: [ELC], varom: 1, lifetime: 1, invcost: 0, availability: {D: 0, N: 1}}
          HYBRID:
            flexible_inputs: {GAS: {}, ELC: {max_share: 0.6}}
            outputs: [HEAT]
            timeslice: annual
        """, head=TWO_SLICES))
    use = {(x["commodity"], x["timeslice"]): x["value"] for x in r.tables["flows"] if x["process"] == "HYBRID" and x["direction"] == "in"}
    assert use[("ELC", "N")] == pytest.approx(30.0)        # 60% of the 50 units of heat made at night
    assert ("ELC", "D") not in use                          # day power costs more than gas
    assert use[("GAS", "ANNUAL")] == pytest.approx(70.0)
    assert r.sol.objective == pytest.approx(30 * 1 + 70 * 5)
