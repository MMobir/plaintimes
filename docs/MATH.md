# The formulation

This is what `kernel.py` builds. It is the core of TIMES as described in
Part II of the TIMES documentation, with the cost-accounting conventions of
the official code run with `OBJ=AUTO` and `OBLONG=YES` (the settings VEDA
and `xl2times` use). Where the official code makes a choice that is not
obvious, the choice is listed under "Conventions taken from the official
code", because matching those choices is what makes the objective values
agree.

## Time

Periods `t` have a first year `B(t)`, a last year `E(t)`, a length
`D(t) = E(t) − B(t) + 1` and a milestone year `M(t)` that labels them.
Activities, flows and capacities are annual rates that hold in every year of
a period. Costs are accounted for each calendar year `y` and discounted to
the beginning of `discount_year` with factor `q^(y − y0)`, `q = 1/(1+d)`.

Timeslices form a tree under `ANNUAL` with year fractions `fr(s)`. Each
commodity is balanced at one level of the tree and each process is dispatched
at one level. A flow of a process in slice `s` enters the balance of
commodity `c` as follows. If `s` is at or below the commodity's level, the
flow counts fully in the commodity slice that contains `s`. If `s` is
coarser, the flow is split over the commodity slices inside `s`: in
proportion to the load shape of the process's main output if that has one,
otherwise the commodity's own load shape, otherwise hours. So an
annual-level heater serving a winter-heavy demand draws its electricity
mostly in winter.

## Variables

| Variable | Meaning |
|---|---|
| `NCAP[r,p,v]` | capacity of process `p` built in period `v` |
| `ACT[r,p,(v),t,s]` | activity in period `t`, timeslice `s` (one per vintage `v` for vintaged processes) |
| `FIN`, `FOUT[r,p,(v),t,s,c]` | flow of commodity `c` in a flexible input or output group |
| `SIN`, `SOUT[r,st,t,s]` | storage charge and discharge |
| `TRD[link,dir,t,s]` | amount sent over a trade link |

All are non-negative. Fixed flows are not variables: input `c` is
`a_c / η × ACT` and output `c` is `b_c × ACT`, with `a`, `b` the
coefficients under `inputs` and `outputs` and `η` the efficiency.

## Capacity

Capacity of vintage `v` is available from `B(v)` for `L` years (its
lifetime). The capacity available in period `t` is

```
CAP[r,p,t] = Σ_v cpt(v,t) · NCAP[r,p,v] + Σ_old cpt(old,t) · stock_old + residual(t)
```

where `cpt(v,t)` is the share of period `t` during which the vintage is
alive, weighted by discount factors:

```
cpt(v,t) = Σ_{y in t, B(v) ≤ y < B(v)+L} q^(y−B(t))  /  Σ_{y in t} q^(y−B(t))
```

If the lifetime is shorter than the period, the investment is repeated
`R = ceil(D/L)` times within the period and the vintage lives `R·L` years.

## Constraints

**Capacity use.** For every process with capacity, period and timeslice `s`
at the process level,

```
ACT[t,s] ≤ AF(s) · cap2act · fr(s) · CAP[t]
```

(per vintage, with that vintage's capacity, for vintaged processes).
`availability_min` and `availability_fix` give the same row with `≥` and
`=`. `capacity_factor` gives the row summed over the year or a season.

**Flexible groups.** `Σ_c η_c · FIN[c] = ACT` and `Σ_c FOUT[c] = ACT`, with
share limits `FIN[c] ≤ share_c · Σ FIN` (and likewise for minima and
outputs). Flexible inputs are decided at the finest timeslice level of their
commodities: a process dispatched annually that can take gas or day/night
electricity has one mix per day/night timeslice, each with its own share
limits, and the activity is spread over those timeslices as described under
"Time".

**Commodity balance.** For every region, period, non-emission commodity and
commodity timeslice,

```
ie · Σ production − Σ consumption  ≥  demand · shape(s)      ( = for materials )
```

where `ie` is the commodity `efficiency`. Storage discharge and trade
arrivals are production; charging and trade departures are consumption.

**Emissions.** Net emissions of `e` in a period are the sum over processes of
factor × basis flow (an input, an output, or the activity). `cap` bounds them
per period, `budget` bounds `Σ_t D(t) · emissions(t)`, and `tax` enters the
objective.

**Peak reserve.** For a commodity with `peak_reserve = ρ`, in each peak
timeslice `s`,

```
ie · [ Σ_{p with capacity, c its main output} pk_p · cap2act_p · fr(s) · b_c · CAP[p,t]
       + Σ_{other producers} pk_p · flow_p(s) ]   ≥   (1+ρ) · ( consumption(s) + demand(s) )
```

**Bounds.** On `NCAP` (per period), on `CAP` (per period), on annual
activity `Σ_s ACT`, and on cumulative activity `Σ_t D(t) · Σ_s ACT[t]`.

**User constraints.** Any linear combination of flows, total production or
consumption of a commodity, net emissions, activities, capacities and new
capacities, per region or summed over regions, per period or summed over
periods. In a sum over periods, annual rates are weighted by `D(t)` and
capacities are not.

**Storage** (native kernel only). Within each cycle group `g` (a season for
day/night storage): `η · Σ SIN = Σ SOUT`; charge and discharge in each slice
are limited by power, `≤ AF · cap2act · fr(s) · CAP`; and if `hours` is
given, discharge per cycle is limited by the energy held,
`Σ_{s in g} SOUT ≤ cycles(g) · hours/8760 · cap2act · CAP`, with 365·fr(g)
cycles a year for day/night storage, 52·fr(g) for weekly, 1 for seasonal.

**Trade** (native kernel only). A link takes `TRD` out of the balance of the
exporting region and adds `η · TRD` to the importing region's, in the same
timeslice; both directions of a bidirectional link share its capacity.

## Objective

Minimise the sum over all years, discounted, of:

**Investment.** A unit built in year `k` at cost `C(k)` is paid as an annuity
over its economic life `EL` at the technology's hurdle rate `ds`; the
payments are discounted at the general rate `d`; and the value of the
technical life that lies beyond the last model year is credited back. In
closed form the cost of one unit commissioned in year `k` is

```
C(k) · q^(k−y0) · CRF(ds,EL)/CRF(d,EL) · (1 − S(k))
CRF(x,n) = (1 − 1/(1+x)) / (1 − (1+x)^−n)
S(k)     = ((1+d)^(L+k−EOH−1) − 1) / ((1+d)^L − 1)   if the unit outlives EOH, else 0
```

For the capacity of period `v`, `k` is the first year of the period, `B(v)`
(see convention 3 for the one exception), and `k = B(v) + j·L` for the
`j`-th repetition of a repeated investment. With `mid_year_discounting` the
annuity is multiplied by `(1+ds)^0.5`.

**Fixed O&M.** Paid in every year the unit is alive up to the end of the
horizon, at the vintage's rate.

**Variable costs.** `varom(y)` times activity in every year of the period;
`tax(y)` times net emissions.

**Slack.** With `--relax`, unserved amounts at a large penalty.

Reported costs: `objective.csv` splits the discounted total by category and
process; `costs.csv` gives undiscounted annual amounts per period.

## Conventions taken from the official code

These are the choices that had to be matched for the objectives to agree.
Each was found by comparing against official TIMES, first on hand-built
cases and then on randomly generated ones, and each is pinned by at least
one parity case.

**Periods**

1. **Which accounting variant applies depends on the period layout**, as
   with `OBJ=AUTO`. If every milestone year is the middle year of its period,
   `M = B + floor((D−1)/2)`, the standard variant is used; otherwise the
   modified one. Periods given as `{start, lengths}` are always standard.
   Periods given as `{milestones}` are standard if they happen to be
   centred, modified if not. The variants differ in `n(t)`, the number of
   yearly steps over which TIMES builds up the capacity of a period: `D(t)`
   in the standard variant; in the modified one the distance to the previous
   milestone, and for the first period `(M−B+1) + min(E−M, M−B)`.

**New capacity**

2. **Its cost is a window average.** TIMES builds a period's capacity in
   `n` equal yearly steps and books them as one lump at the centre of that
   window. The cost that counts is the discount-weighted mean of the
   investment cost curve over the window, which starts `floor(n/2)` years
   before the centre. The same averaging applies to fixed O&M. With flat
   costs this is simply the cost.
3. **The centre is `B(v)`**, with one exception: under the modified variant,
   a first period that starts before its milestone has its centre at
   `M − n + 1 + floor(n/2)`, which can be a year later than `B`. Capacity is
   still available from `B`.
4. **Salvage is taken at the centre**, not step by step.
5. **Repeated investments** (lifetime shorter than the period) use
   `n = min(n, round(L))` steps around each repetition, with
   `R = ceil(max(D, n)/L)` repetitions under the modified variant.
6. **A repetition whose window starts on the first year of a later period**
   is booked step by step, each step in its own year with its own salvage
   share, instead of as one lump. This looks like an oversight in the
   official code; it changes the cost of that repetition by a fraction of a
   percent, and more near the end of the horizon.
7. **A repetition whose window starts before the accounting begins is not
   charged at all.** The accounting starts at `MINYR` (convention 8). Under
   the modified variant with a first period that starts before its
   milestone, the first build of a short-lived technology can have its
   window start before `MINYR`; the official code then leaves that build out
   of the cost equations while still counting its capacity. The kernel
   reproduces this so that the two agree, and `check` raises warning `PT321`
   whenever a model triggers it, because it makes the technology look
   cheaper than it is. It cannot happen with `{start, lengths}` periods or
   when the first period starts at its milestone.

**Existing stock**

8. **Fixed O&M is charged from slightly before the horizon.** The
   accounting starts at `MINYR = min(B(1) − 1, min_t(M(t) − n(t)) + 1)`, so
   stock built earlier pays fixed O&M for the years from `MINYR` to
   `B(1) − 1` as well. Reported as `fixom_pre_horizon`. A constant.
9. **Stock with an investment cost pays its annuity** for as long as it is
   available inside the horizon, `stock · C(v) · CRF(ds,EL) · Σ_t cpt(v,t) ·
   PV(t)`, unless its economic life had already ended at `MINYR`, in which
   case it pays nothing. Reported as `invcost_sunk`. A constant.
10. **Residual stock** is one vintage commissioned in `B(1) − 1`. Its fixed
    O&M and annuity use the cost data of that year and are charged in
    proportion to the residual capacity of each period; for a vintaged
    process its technical data are those of that year too.

**Everything else**

11. **Capacity transfer is discount-weighted** (the formula under
    "Capacity") and coefficients below 1/512 are dropped.
12. **Flows of a coarse-level process follow the load shape of its main
    output**, and **flexible input mixes are chosen per fine timeslice**
    (see "Time" and "Flexible groups").
13. **Sums over periods in user constraints** weight flows, activities and
    commodity totals by `D(t)`; capacity terms are unweighted.
14. **Net emission totals may be negative** here. Official TIMES keeps them
    non-negative unless told otherwise, so the bridge writes a free lower
    bound when a model contains removals.

## Known differences from official TIMES

Alternative optima: where the LP has several least-cost solutions (for
example, how much free wind to curtail) the two solvers may report different
activities with the same objective.

A capacity ceiling below existing stock is an error here (`PT320`); official
TIMES relaxes it silently.

Time-dependent data are resolved before export and written for every model
year, so TIMES's own interpolation defaults never come into play. Cost
series are exported as given, because TIMES interpolates costs for every
calendar year exactly as the kernel does. Residual stock is written for
every period, including zeros, because TIMES would otherwise let a single
value decay linearly over the lifetime.

Everything listed under "Not implemented" in the README.
