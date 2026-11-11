# Validation against official TIMES

The native kernel is checked against the official ETSAP TIMES code in two
ways: a fixed suite of 59 cases whose official results are stored in the
repository, and a randomized test that generates models, solves them both
ways and looks for any difference.

## Method

Each case is exported with `plaintimes export-dd` and solved with the
official TIMES source code, version 4.9.8, in GAMS 54.6 with CPLEX, using
the run-file settings `OBJ=AUTO` and `OBLONG=YES` (plus `MID_YEAR=YES` where
the model uses mid-year discounting). The same YAML is solved with the native
kernel and HiGHS 1.15. The comparison is on the total discounted system cost;
"Rel. diff" is `|native − official| / |official|`. New capacity, installed
capacity and activity by process and period are compared as well.

All of this was run with the free GAMS demo licence, which limits models to
2,000 rows or columns and is marked "for demonstration and instructional
purposes only". Anyone publishing these results should regenerate them under
their own GAMS licence; it is one command (below) and takes a few minutes.

Agreement on small cases shows that the equations and the cost conventions
match. It says nothing about numerical behaviour on very large models, which
has not been compared.

## The fixed suite

Official objectives are stored in `tests/parity/reference.json`, and the
test suite checks the native kernel against them on every run, so parity is
re-verified on any machine without GAMS. To regenerate the reference:

```
PLAINTIMES_GAMS=/path/to/gams PLAINTIMES_TIMES_SRC=/path/to/TIMES_model \
    python tests/parity/make_reference.py
```

Hand-built cases, each exercising one feature or a deliberate combination:

| Case | What it exercises | Official TIMES | Native kernel | Rel. diff |
|---|---|---|---|---|
| `ma` | one gas plant, three periods: discounting, annualised investment, salvage, fixed O&M | 16111.998713 | 16111.998713 | 1.7e-15 |
| `mb` | time-varying investment and fixed costs; reinvestment after a 15-year life | 24477.126564 | 24477.126564 | 4.2e-12 |
| `mb2` | constant costs; reinvestment after a 15-year life | 24692.182255 | 24692.182255 | 4.5e-12 |
| `mc` | existing stock by vintage alongside a new technology | 21605.031133 | 21605.031133 | 5.9e-13 |
| `md` | economic life shorter than technical life; technology-specific hurdle rate | 24892.913523 | 24892.913524 | 2.9e-11 |
| `me` | mid-year discounting with a hurdle rate | 25092.419117 | 25092.419118 | 2.9e-11 |
| `mf` | periods defined by milestone years | 26859.671876 | 26859.671877 | 4.4e-11 |
| `mg` | existing stock of a technology that can also be built new (sunk annuities) | 22814.785093 | 22814.785093 | 6.1e-12 |
| `mh` | first period of one year; time-varying fixed costs; existing stock | 22737.513263 | 22737.513263 | 6.2e-12 |
| `mi` | lifetime (7 y) shorter than the periods: repeated investment | 28753.589726 | 28753.589728 | 5.4e-11 |
| `mi2` | the same with milestone-defined periods | 33121.811222 | 33121.811226 | 1.1e-10 |
| `mj` | residual stock given as a profile | 21409.712286 | 21409.712286 | 6.3e-13 |
| `ts1` | four timeslices, load shape, availability by timeslice, emission factors | 22777.402974 | 22777.402975 | 8.1e-12 |
| `ts2` | as ts1 plus annual capacity factor, peak reserve with capacity credits, grid losses | 24784.792521 | 24784.792521 | 6.6e-12 |
| `em1` | annual emission cap | 23855.168029 | 23855.168028 | 7.1e-12 |
| `em2` | cumulative emission budget | 23572.607243 | 23572.607243 | 4.7e-13 |
| `em3` | emission tax rising over time | 26815.814487 | 26815.814487 | 1.5e-11 |
| `flex1` | flexible fuel mix with shares and fuel-specific efficiencies, CHP, refinery with flexible outputs, activity bound, emission cap | 45897.864075 | 45897.864075 | 1.4e-12 |
| `vint1` | vintaged process: each vintage keeps its own efficiency | 22990.796281 | 22990.796282 | 8.9e-12 |
| `uc1` | bounds on capacity, new capacity and cumulative activity; five user constraints | 23999.369464 | 23999.369464 | 8.2e-12 |
| `uc2` | binding share, build-rate, minimum-activity and cumulative mixed constraints, with grid losses | 26604.475912 | 26604.475912 | 1.5e-11 |
| `mr1` | two regions with region-specific data and a regional emission cap | 31056.753790 | 31056.753791 | 6.0e-12 |
| `mr2` | two regions: constraints summed over regions and over periods, regional capacity limits | 35616.003313 | 35616.003313 | 3.6e-12 |
| `vr1` | residual stock of a vintaged, investable process; milestone periods; time-varying costs | 25404.976196 | 25404.976196 | 1.2e-11 |
| `af1` | minimum and fixed availability; seasonal capacity factors; annual minimum use | 22975.639979 | 22975.639979 | 9.1e-12 |
| `dy1` | discount year after the first year, 7% rate, mid-year discounting, economic life and hurdle rate | 30694.430172 | 30694.430172 | 1.2e-11 |
| `bd1` | minimum and fixed new capacity, minimum capacity, fixed and maximum activity, cumulative minimum | 23790.017615 | 23790.017616 | 2.2e-11 |
| `pk1` | peak reserve in one timeslice only, time-varying reserve, exact electricity balance | 22786.027917 | 22786.027918 | 8.1e-12 |
| `neg1` | removals: net-negative emissions against a negative cap | 25182.668093 | 25182.668093 | 1.6e-11 |
| `t3` | three timeslice levels (season, weekday/weekend, day/night) | 23325.748058 | 23325.748058 | 8.8e-12 |
| `cs1` | annual-level heater serving a seasonal demand from day/night electricity; time-varying availability with and without vintaging | 36483.209613 | 36483.209613 | 1.7e-11 |
| `lv1` | day/night-level process producing an annual demand | 57734.652799 | 57734.652801 | 2.7e-11 |
| `lv2` | season-level device serving a day/night demand that has a load shape | 47947.160040 | 47947.160042 | 4.1e-11 |
| `lv3` | annual-level baseload plant producing day/night electricity | 42468.046132 | 42468.046132 | 9.2e-12 |
| `lv4` | annual-level hybrid heater with a flexible mix of gas and day/night electricity | 42436.977530 | 42436.977530 | 9.0e-12 |
| `demo` | the demo model (14 processes, vintages, residual stock, peak reserve, three demands) | 344362.535119 | 344362.535171 | 1.5e-10 |
| `demo+carbon_cap` | demo with a declining emission cap | 348135.558088 | 348135.558141 | 1.5e-10 |
| `demo+carbon_tax` | demo with a carbon tax | 370385.082958 | 370385.083015 | 1.5e-10 |
| `demo+high_gas` | demo with higher gas prices | 349292.999011 | 349292.999064 | 1.5e-10 |
| `demo+renewables_target` | demo with a renewable share constraint | 344717.734731 | 344717.734784 | 1.5e-10 |
| `demo+carbon_cap+high_gas` | demo with two scenario overlays stacked | 355748.860359 | 355748.860413 | 1.5e-10 |

Eighteen further cases (`fz0` … `fz291` in `tests/parity/cases`) are
randomly generated models that exposed a difference during development and
are kept as regression tests. They agree to 4.6e-10 or better.

Quantities (capacity, new capacity, activity) agree to better than 1e-8 in
every case except for curtailable renewables in a few, where the LP has
alternative optima: surplus generation can be produced and discarded, or not
produced, at the same cost.

The TIMES quality-assurance log reports "all quality checks passed" for 56
of the 59 cases. The other three (`fz22`, `fz83`, `fz183`) carry one notice
each, "both NCAP_AF and NCAP_AFA specified for same process", which is what
`availability` together with `capacity_factor` exports to; their objectives
agree like the rest.

## The randomized test

`tests/parity/fuzz.py` composes small models at random from the features of
the format: period layouts of both kinds with and without an early start or
late end, zero to three timeslice levels, one or two regions, vintaged and
non-vintaged plants with short and long lives, existing and residual stock,
economic lives and hurdle rates, every availability and capacity-factor
variant, fixed and flexible input mixes, a CHP plant, a refinery with a
flexible output mix, devices dispatched at coarser levels than the
commodities they use, grid losses, peak reserve, emission caps, budgets and
taxes, bounds, and user constraints. Caps and constraint limits are sized
from a first solve so that they bind.

```
PLAINTIMES_GAMS=/path/to/gams PLAINTIMES_TIMES_SRC=/path/to/TIMES_model \
    python tests/parity/fuzz.py 0 1500
```

With the code as released, 1,900 random models were compared (seeds 0–1499
of the generator in the repository and 400 from an earlier, narrower
version). 1,898 solved to optimality in both and agree to better than 1e-7
relative; one is infeasible in both; one is rejected by `plaintimes check`
before solving (a capacity ceiling below existing stock, `PT320`).

## What the comparison found

The first hand-built cases agreed early. Further hand-built cases on
timeslice levels, and then the randomized test, did not: 17 of the first 200
random models differed. Tracing each difference back led to nine conventions
of the official code that a fresh implementation would not guess (two from
the timeslice cases, seven from the random models). All are now matched,
described in [MATH.md](MATH.md), and pinned by a parity case:

- the accounting variant is chosen from the period layout, not from how the
  periods were specified;
- the cost of new capacity is centred a year later than the period start in
  one layout (a first period that starts before its milestone);
- existing stock whose economic life is already over pays no annuity;
- a repeated investment whose build-up window begins on a later period's
  first year is booked step by step;
- a repeated investment whose window begins before the accounting starts is
  not charged at all (reproduced, and flagged by `check` as `PT321`);
- a device dispatched at a coarser level than its inputs draws them
  following the load shape of what it produces;
- a flexible input mix may differ between fine timeslices even when the
  process is dispatched at a coarser level;
- residual stock given at one period only would be extended by TIMES over a
  full lifetime, so the bridge writes it for every period;
- the technical data of residual stock in a vintaged process are those of
  the year before the horizon.

Earlier, the hand-built cases had surfaced three more: the window-averaged
cost of new capacity, fixed O&M charged on existing stock for a year or two
before the horizon, and annuities charged on existing and residual stock
that carries an investment cost. These last two are reported as separate
cost categories (`fixom_pre_horizon`, `invcost_sunk`) so that they are
visible rather than buried in a total.

## What is not covered

Trade links and storage are not exported, so they have no parity cases; they
are tested against hand calculations in `tests/test_analytic.py`. Features
listed as not implemented in the README are outside the comparison
altogether. No comparison has been made on a production-scale model, with
more than two regions, or with TIMES objective variants other than the one
named above. The random generator covers combinations of the features it
knows; a model built from something it never composes could still differ.
