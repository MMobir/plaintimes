# plaintimes

TIMES models as plain text, solved without the legacy toolchain.

A model is a folder of small YAML files. One command checks it, one solves it
with an open-source LP solver, and every result, error and explanation comes
back as text or JSON. There is no Excel, no VEDA, no Windows machine and no
GAMS licence in the loop, so a person with a text editor or an AI agent with a
shell can do the whole job: read the model, change it, run it, find out why
the answer is what it is, and review the change as a diff.

The mathematics is still TIMES. The native kernel reproduces the objective
function of the official ETSAP TIMES code (v4.9.8) to nine significant
digits on a fixed suite of 59 validation models, and to better than seven on
1,898 randomly generated ones. The same YAML can be exported as `.dd` files
and run through the official code whenever you want a second opinion.

```
model.yaml ──► check ──► native kernel (Python) ──► HiGHS ──► CSV tables + JSON summary
     │                                                            ▲
     └──► export-dd ──► official TIMES in GAMS (optional oracle) ─┘ compare
```

## A model, in full

```yaml
plaintimes: 1
name: tiny
currency: MUSD
discount_rate: 0.05
regions: [R1]
periods: {milestones: [2025, 2030, 2040]}
units: {energy: PJ, capacity: GW}

commodities:
  GAS: {kind: energy}
  ELC: {kind: demand, demand: {2025: 100, 2040: 140}}
  CO2: {kind: emission, unit: kt}

emission_factors:
  GAS: {CO2: 56.1}

processes:
  GAS_IMP: {outputs: [GAS], varom: {2025: 7, 2040: 9}}      # the gas price
  GAS_PP:
    inputs: [GAS]
    outputs: [ELC]
    efficiency: 0.55
    lifetime: 30
    invcost: 1000          # MUSD per GW
    fixom: 22
    availability: 0.9
    emissions: combustion
  WIND:
    outputs: [ELC]
    lifetime: 25
    invcost: {2025: 1500, 2040: 1100}
    fixom: 38
    availability: 0.33
```

```
$ plaintimes check tiny/          # every problem, with file:line and a hint
$ plaintimes solve tiny/          # writes tiny/runs/base/*.csv, summary.json, manifest.json
$ plaintimes explain tiny/ process WIND --period 2030     # why it is, or is not, built
```

## What was cut, and why

The official route to a TIMES run is: Excel workbooks, read by VEDA, written
out as `.dd` files, compiled by the TIMES source code in GAMS, solved by a
commercial LP solver, and read back through VEDA. ETSAP's own guidance is
that GAMS "is commercial software and requires a valid license" and that
GAMS, the solver and VEDA "must be purchased from their respective
developers"; VEDA 2.0 "runs natively on Windows". Each layer was judged on
what it costs against what it contributes to the answer.

| Layer | What it contributes | What it costs | Verdict |
|---|---|---|---|
| Excel workbooks as the source of truth | Familiar data entry | Binary files that cannot be diffed, reviewed or merged; meaning carried by cell position and tag conventions; unreadable to an agent without a parser | Cut. Plain YAML, one fact per line, in git |
| VEDA front end | Turns workbooks into TIMES input, browses the model | Windows-only desktop application (.NET and PostgreSQL), proprietary licence, and a large vocabulary of table tags to learn | Cut. `check`, `show`, `res` and `rows` cover inspection |
| GAMS | Runs the TIMES source code | A commercial licence for every modeller and every machine that runs a model | Made optional. The native kernel generates the same LP directly |
| TIMES source code (GAMS) | The reference implementation of the equations | Needs GAMS; intricate; slow to change | Kept as the oracle, not as a dependency |
| Commercial LP solver | Fast barrier solves on very large models | Licence | Made optional. HiGHS is the default; `--write-lp` hands the LP to any solver |
| VEDA back end / results GDX | Result tables and pivots | Windows GUI; results locked in tool-specific files | Cut. Tidy CSV tables, a JSON summary, `explain` and `diff` |

What was kept is the part that carries the value: the TIMES formulation
itself (vintaged capacity, timeslices, commodity balances, peak reserve, user
constraints, the discounted objective with annualised investment and salvage)
and interoperability with the official code.

## Install

Python 3.10 or later. The only dependencies are PyYAML, NumPy and HiGHS.

```
pip install -e .                 # from this folder
plaintimes init mymodel          # copies the demo model
plaintimes check mymodel && plaintimes solve mymodel
```

`pt` is a short alias for `plaintimes`. Add `--json` to any command (or set
`PLAINTIMES_JSON=1`) to get one JSON object instead of text.

## The working loop

| Command | What it answers |
|---|---|
| `check MODEL [-s SCEN]` | Is the model well formed? Lists every error and warning with file, line and a fix hint |
| `info`, `show NAME`, `res` | What are the periods? What does this process resolve to in each period? Who produces and consumes what? |
| `solve MODEL [-s SCEN]...` | The least-cost system. Writes result tables to `MODEL/runs/<scenario>/` |
| `solve --relax` / `explain infeasible` | Where exactly can demand not be met? |
| `explain process P` / `explain price C` | Why is P built or not, and by how much does it miss? What sets the price of C in each timeslice? |
| `rows PATTERN` | The actual generated equation, e.g. `rows BAL.NORTH.ELC.2030.WD` |
| `diff RUN_A RUN_B` | What changed between two runs, largest changes first |
| `test MODEL` | Do the model's own `expect:` checks still hold? |
| `export-dd`, `oracle` | Write `.dd`/`.run` files; solve with official TIMES and compare |
| `schema` | The format reference (also in [docs/FORMAT.md](docs/FORMAT.md)) |

Exit codes are stable: 0 ok, 1 the model has errors, 2 no optimal solution,
3 an expectation or oracle comparison failed, 4 usage or environment problem.

Scenarios are small overlay files in `MODEL/scenarios/`; `-s a -s b` stacks
them. A scenario holds only what differs from the base, so a policy case is a
ten-line file and a code review of it is ten lines long.

## Built for agents

Everything an agent needs is on the command line and machine-readable:
diagnostics carry stable codes (`PT110`), the path of the offending key, the
file and line, and a suggested fix; results are flat CSV; `explain` turns LP
duals into break-even accounts; `test` gives a pass/fail regression suite that
lives with the model. [AGENTS.md](AGENTS.md) is the operating manual, and
`plaintimes init` drops a copy into every new model.

## Official TIMES as a second opinion

```
plaintimes export-dd MODEL -o out/        # out/<name>.dd, <name>_ts.dd, <name>.run
plaintimes oracle MODEL --gams /path/to/gams --times-src /path/to/TIMES_model
```

`oracle` solves the model both ways and reports the difference. The parity
suite in `tests/parity` does this for 59 models and stores the official
objectives, so `pytest` re-checks parity on any machine without GAMS.
`tests/parity/fuzz.py` goes further: it generates random models and compares
the two solvers on each, which is how most of the subtle accounting
conventions were found. Results and method are in
[docs/VALIDATION.md](docs/VALIDATION.md); the conventions that had to be
matched are in [docs/MATH.md](docs/MATH.md).

The validation in this repository was run with the free GAMS demo licence.
If you publish results that rely on it, regenerate the reference values
under your own GAMS licence with `tests/parity/make_reference.py`.

## Limits of this prototype

This is version 0.1 and it is honest about its edges.

Covered and verified against official TIMES: multi-region models without
trade, any period structure, up to three timeslice levels, vintaged and
non-vintaged processes, existing and residual stock, fixed and flexible input
and output mixes, availability and capacity factors, peak reserve, emission
caps, budgets and taxes including net removals, bounds, and user constraints.
Verified means small models (the demo licence allows 2,000 rows), one or two
regions; nothing has been compared at production scale.

Where official TIMES has an accounting quirk, the kernel reproduces it so
that the two agree, and says so. The one that changes decisions rather than
adding a constant is flagged by `check` as warning `PT321`: with a first
period that starts before its milestone year, TIMES does not charge the first
build of a technology whose lifetime is shorter than that period.

In the native kernel only, checked against hand calculations but not yet
exported to official TIMES: trade links between regions and storage. The
storage formulation is deliberately simple (energy shifted within a cycle,
limited by power and by hours of storage); it is not the TIMES storage
formulation.

Not implemented: elastic demands, endogenous technology learning, the climate
and macro modules, stochastic and myopic runs, lumpy investment, early
retirement, unit commitment, construction lead times and decommissioning
costs, commodity-level cost attributes other than emission taxes. An importer
for existing VEDA models is not included; ETSAP's open-source `xl2times` is
the natural starting point for one.

Speed: models with tens of thousands of rows solve in seconds to minutes with
HiGHS. A synthetic 12-region, 12-timeslice, 8-period model (85,000 variables,
740,000 non-zeros) took 2.5 s to generate and about 50 s to solve on two
cores. Very large models will want a commercial barrier solver, which
`--write-lp` allows.

## Repository layout

```
src/plaintimes/     the package (kernel.py is the maths, ddexport.py the bridge)
src/plaintimes/templates/demo/   the demo model that `init` copies
examples/two_regions/            trade and storage
tests/              pytest suite; tests/parity holds the cases checked against official TIMES,
                    the script that regenerates their reference values, and the random test
docs/               FORMAT.md (generated), MATH.md, VALIDATION.md
AGENTS.md           how an agent should work with a model
```

## Licence and credits

GPL-3.0-or-later, the same licence as the TIMES model generator, whose
documentation and source code (© ETSAP) define the formulation reproduced
here. plaintimes is an independent implementation and is not affiliated with
or endorsed by IEA-ETSAP, KanORS-EMR or GAMS. HiGHS is MIT-licensed.
