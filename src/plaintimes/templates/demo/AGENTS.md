# Working on a plaintimes model

This file is for an AI agent (or a new colleague) asked to read, change, run
or explain an energy system model in this format. Everything is done with the
`plaintimes` command and a text editor.

## Ground rules

1. **Always pass `--json`** (or set `PLAINTIMES_JSON=1`). Every command then
   prints exactly one JSON object. Use the exit code first: 0 ok, 1 model
   errors, 2 no optimal solution, 3 a check failed, 4 usage problem.
2. **Run `check` after every edit and before every solve.** It is instant and
   reports all problems at once, each with `code`, `message`, `path`, `file`,
   `line` and usually a `hint`. Fix errors; read warnings, they are usually
   real modelling mistakes.
3. **Never edit files under `runs/` or `export/`.** They are outputs.
4. **Put policy cases in `scenarios/`, not in the base files.** A scenario
   contains only what differs.
5. **Say where numbers come from.** Add a `sources:` entry (or a comment) for
   any value you introduce, and say so in your summary if a number is a
   placeholder.
6. **Do not guess the format.** `plaintimes schema --json` is the complete
   reference; `plaintimes show MODEL NAME --json` shows what an entity
   resolves to in each period after interpolation.

## The loop

```
plaintimes check MODEL --json                     # must be ok before going on
plaintimes solve MODEL [-s SCEN] --json           # writes MODEL/runs/<scen or base>/
plaintimes explain MODEL [-s SCEN] process P --json
plaintimes diff MODEL/runs/base MODEL/runs/SCEN --json
plaintimes test MODEL --json                      # the model's own regression checks
```

`solve --json` returns the headline numbers directly (objective, capacity,
new capacity, emissions, annual costs, average prices, binding constraints)
and the path of the run directory. Full detail is in the CSV tables there:

| table | one row per | value column |
|---|---|---|
| `capacity` | region, process, period | `capacity` |
| `new_capacity` | region, process, period | `new_capacity`, plus `reduced_cost`, `cost_gap_pct` |
| `activity` | region, process, period | `activity`, `utilization` |
| `flows` | region, process, commodity, direction, period, timeslice | `value` |
| `balance` | region, commodity, period, timeslice | `production`, `consumption`, `demand`, `price` |
| `emissions` | region, emission, process, period | `value` |
| `costs` | region, process, period, category | `annual_cost` (undiscounted, per year) |
| `objective` | region, process, category | `discounted_cost` |
| `constraints` | bound, cap, peak or user constraint | `level`, `rhs`, `binding`, `shadow_price` |

`period` is always the milestone year. Prices and shadow prices are
undiscounted, in the model currency per commodity unit.

## Recipes

**Find something.** `plaintimes res MODEL --json` lists producers and
consumers of every commodity. `show MODEL NAME --json` gives the file and
line where NAME is defined (`defined_at`) and its neighbours.

**Add a technology.** Copy a similar process in `processes/*.yaml`, rename
it, change the numbers. Minimum for a plant: `inputs`, `outputs`,
`efficiency`, `lifetime`, `invcost`. Then `check`. A process with `invcost`
can be built; one without can only use `existing` or `residual` stock; one
with no capacity keys at all is unlimited (use this only for supply and
delivery steps).

**Add a scenario.** Create `scenarios/NAME.yaml` holding only the changed
keys, for example

```yaml
description: Carbon price rising to 150 USD/t.
commodities:
  CO2:
    tax: {2025: 0.0, 2030: 0.05, 2050: 0.15}
```

Mappings merge into the base; a number, list or year series replaces the old
value entirely; `null` deletes a key. Run with `-s NAME`; stack with
`-s A -s B`.

**The model is infeasible (exit code 2).** Run
`plaintimes explain MODEL -s SCEN infeasible --json`. It re-solves with slack
on every commodity balance and reports which commodity cannot be supplied,
where and when (`unserved`). Usual causes: no capacity allowed in an early
period, an emission cap or share target that rules out every supplier, a
missing fuel supply. If it is still infeasible with slack, two bounds or
constraints contradict each other.

**Explain a result.** `explain process P --period Y` reports, per unit of
activity, revenue at shadow prices against fuel, variable, emission and
capacity costs (they balance when P runs), and for each vintage whether it is
built and, if not, the percentage by which its cost would have to fall
(`cost_gap_pct` in `new_capacity.csv` is the same number).
`explain price C --period Y` lists, per timeslice, the price and each
supplier with its role: `price-setting` (has spare capacity, so its marginal
cost is the price) or `inframarginal` (capacity is scarce and earns a rent).

**See the actual equation.** `plaintimes rows MODEL PATTERN` prints generated
constraints whose name contains PATTERN. Names: `BAL.<region>.<commodity>.<period>.<timeslice>`,
`CAPACT.<region>.<process>.<vintage>.<period>.<timeslice>.<kind>`,
`PEAK...`, `EMIS_CAP...`, `EMIS_BUDGET...`, `UC.<name>.<region>.<period>`,
`BND_CAP`, `BND_ACT`, `BND_CUM`. Variables: `NCAP.<region>.<process>.<period>`,
`ACT.<region>.<process>.[<vintage>.]<period>.<timeslice>`.

**Protect a result.** Add a check to the `expect:` list so later edits cannot
silently break it, then run `plaintimes test MODEL`.

```yaml
expect:
  - name: no new coal after 2030 under the cap
    scenarios: [carbon_cap]
    table: new_capacity
    where: {process: COAL_PP, period: [2040, 2050]}
    max: 0.001
```

**Cross-check against official TIMES** (only if GAMS and the TIMES source are
available): `plaintimes oracle MODEL --gams PATH --times-src DIR --json`.

## Things that surprise people

- **Year series interpolate; bounds do not extrapolate.** `invcost: {2025:
  1000, 2050: 700}` is linear in between and flat outside. A bound, cap or
  constraint `rhs` given as a year series applies only between its first and
  last year; write a plain number for "every period".
- **A period is not its label.** With `milestones: [2025, 2030, 2040]` the
  2030 period covers 2028-2035 (see `plaintimes info`). New capacity is
  available from the first year of its period and its cost is taken around
  that year, so `show` may report an `invcost` for 2030 that differs slightly
  from the value typed for 2030.
- **Efficiency year keys mean the operating period** unless the process says
  `vintaged: true`, in which case they mean the year the unit was built.
- **`availability` is per timeslice; `capacity_factor` is the average** over
  the year (or a season).
- **Surplus is allowed** for energy and demand commodities (`balance: ">="`),
  so wind can be curtailed for free. Materials balance exactly.
- **`invcost_sunk` and `fixom_pre_horizon`** in the cost tables are constants
  from existing stock, reported the way official TIMES accounts for them.
  They never affect decisions.
- **Warning `PT321` means a cost is missing.** With a first period that
  starts before its milestone year, official TIMES does not charge the first
  build of a technology whose lifetime is shorter than that period, and
  plaintimes reproduces this. Fix the period definition rather than living
  with it (the hint says how).
- **Free capacity is a common bug.** `invcost: 0` means capacity costs
  nothing; a process without capacity keys is unlimited. `check` reports both
  as `info`.

## Diagnostic codes

`PT0xx` YAML and file structure · `PT01x-PT02x` periods and timeslices ·
`PT03x` values · `PT04x-PT05x` keys and settings · `PT06x-PT08x` commodities ·
`PT1xx` processes, storage, trade, constraints · `PT3xx` static checks on the
whole system (no supplier, dead ends, impossible shares, retired stock).
