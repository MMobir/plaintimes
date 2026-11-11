"""The native kernel: turns a Model into a linear programme.

The formulation follows the core of TIMES (see docs/MATH.md for the full
statement and for the deliberate differences):

  variables    NCAP  new capacity per region, tech, vintage period
               ACT   activity per region, process, [vintage], period, timeslice
               FIN / FOUT   flows of flexible input / output groups
               SIN / SOUT   storage charge / discharge
               TRD   trade flow per link, direction, period, timeslice
  constraints  capacity transfer (folded into the capacity expressions)
               capacity-activity (availability, capacity factors)
               commodity balances, peak reserve
               flexible-flow definitions and shares
               emission caps and budgets, user constraints, bounds
  objective    total discounted system cost, accounted year by year, with
               investments annualised and a salvage credit for capacity that
               outlives the horizon (TIMES 'OBLONG' conventions).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .lp import LP, INF
from .model import Model, Tech
from .timebase import ANNUAL

Expr = dict[int, float]


def add_to(expr: Expr, other: Expr, scale: float = 1.0) -> None:
    for j, c in other.items():
        expr[j] = expr.get(j, 0.0) + c * scale


@dataclass
class Vintage:
    label: int                 # milestone year for new capacity, commissioning year for old stock
    start: float               # first year of availability
    end: float                 # first year it is no longer available
    life: float
    period: int | None         # index of the investment period, None for existing stock
    var: int | None            # NCAP variable, None for existing stock
    fixed: float = 0.0         # capacity of existing stock
    cpt: list[float] = field(default_factory=list)   # availability factor per period


@dataclass
class Built:
    model: Model
    lp: LP
    relax: bool
    vintages: dict[tuple[str, str], list[Vintage]] = field(default_factory=dict)
    residual: dict[tuple[str, str, int], float] = field(default_factory=dict)
    cap: dict[tuple[str, str, int], tuple[Expr, float]] = field(default_factory=dict)
    # (region, tech, commodity, 'in'|'out', period index, commodity timeslice) -> expression
    flows: dict[tuple[str, str, str, str, int, str], Expr] = field(default_factory=dict)
    # (region, tech, emission, period index) -> expression
    emis: dict[tuple[str, str, str, int], Expr] = field(default_factory=dict)
    activity: dict[tuple[str, str, int], Expr] = field(default_factory=dict)
    demand: dict[tuple[str, int, str, str], float] = field(default_factory=dict)
    annual: dict[tuple[str, str, str, int], list[tuple[int, float]]] = field(default_factory=dict)
    annual_const: dict[tuple[str, str, str, int], float] = field(default_factory=dict)
    pv: list[float] = field(default_factory=list)
    slack_penalty: float = 0.0

    def stats(self) -> dict[str, int]:
        return {"variables": self.lp.n_vars, "constraints": self.lp.n_rows, "nonzeros": self.lp.n_nonzeros}


class Builder:
    def __init__(self, model: Model, relax: bool = False, slack_penalty: float = 1e6):
        self.m = model
        self.P = model.periods
        self.ts = model.ts
        self.dc = model.discounting
        self.lp = LP(model.name)
        self.b = Built(model, self.lp, relax, slack_penalty=slack_penalty)
        self.b.pv = [self.dc.pv_period(i) for i in range(self.P.n)]
        self.relax = relax
        self.slack_penalty = slack_penalty
        self.eoh = self.P.last_year
        # all flow expressions of a commodity in a region and period, for constraint terms
        self.flow_index: dict[tuple[str, str, str, int], list[Expr]] = {}
        self._shapes: dict[str, dict[str, float] | None] = {}
        # years in which some existing stock was commissioned (TIMES accounts costs for these years too)
        self.past_years = {y for t in model.processes.values() for ex in t.existing.values() for y in ex}

    # ------------------------------------------------------------------
    # time helpers
    # ------------------------------------------------------------------
    def cpt(self, start: float, end: float, i: int) -> float:
        """Share of period i (discount-weighted) in which capacity alive on [start, end) is available."""
        b, e1 = self.P.B[i], self.P.E[i] + 1
        lo, hi = max(start, b), min(end, e1)
        if hi <= lo:
            return 0.0
        q = self.dc.q
        if abs(1.0 - q) < 1e-12:
            return (hi - lo) / (e1 - b)
        return (q ** (lo - b) - q ** (hi - b)) / (1.0 - q ** (e1 - b))

    def year_cost(self, param, region: str, i: int) -> tuple[float, float]:
        """(present value, plain average) over the years of period i of a per-unit annual cost."""
        pv = avg = 0.0
        for y in self.P.years(i):
            v = param.at(region, y, default=0.0)
            pv += self.dc.disc(y) * v
            avg += v
        return pv, avg / self.P.D[i]

    def salvage_share(self, k: float, life: float) -> float:
        """Share of an investment made in year k whose value lies beyond the horizon."""
        over = life + k - self.eoh - 1
        if over <= 1e-9:
            return 0.0
        d = self.dc.rate
        if d <= 1e-12:
            return min(1.0, over / life)
        return min(1.0, ((1 + d) ** over - 1.0) / ((1 + d) ** life - 1.0))

    def ipd(self, i: int) -> int:
        """Number of yearly steps over which TIMES spreads the investments of period i."""
        P = self.P
        if P.mode == "std":
            return P.D[i]
        if i == 0:
            lead = P.M[0] - P.B[0] + 1
            return lead + min(P.E[0] - P.M[0], lead - 1)
        return max(P.M[i] - P.M[i - 1], P.M[i] - P.B[i] + 1)

    def cost_centre(self, i: int) -> int:
        """Year on which the investment cost of period i is centred.

        TIMES spreads the investment over ipd(i) years ending at the milestone and
        books it as one lump at the middle of that window.  That is the first year
        of the period, B(i), in every layout except a first period that starts
        before its milestone under milestone-defined periods.
        """
        n = self.ipd(i)
        return self.P.M[i] - n + 1 + n // 2

    @property
    def minyr(self) -> int:
        """First year whose payments TIMES counts (it lies before the first model year)."""
        P = self.P
        return min(P.B[0] - 1, min(P.M[i] - self.ipd(i) for i in range(P.n)) + 1)

    def window_cost(self, param, region: str, centre: float, n: int) -> float:
        """Cost for an investment centred on year `centre`.

        TIMES builds new capacity in n equal yearly steps around the start of
        the period and normalises them to be worth one lump at the centre, so
        the cost that counts is the discount-weighted mean of the cost curve
        over that window.  With a flat cost curve this is just the cost.
        """
        s = param.series(region)
        if s is None:
            return 0.0
        if s.scalar or n <= 1:
            return s.at(centre) or 0.0
        first = centre - n // 2
        num = den = 0.0
        for k in range(n):
            w = self.dc.disc(first + k)
            num += w * (s.at(first + k) or 0.0)
            den += w
        return num / den

    def repeats(self, life: float, i: int) -> int:
        """How many times an investment is repeated within its own period (life < period length)."""
        span = self.P.D[i] if self.P.mode == "std" else max(self.P.D[i], self.ipd(i))
        return max(1, int(math.ceil(span / life - 1e-9)))

    def build_steps(self, life: float, i: int) -> int:
        """Number of yearly steps in the build-up window of one investment of period i."""
        if self.repeats(life, i) == 1:
            return self.ipd(i)
        return min(self.ipd(i), max(1, int(round(life))))

    def uncharged_builds(self, life: float, i: int) -> list[int]:
        """Repetitions of a repeated investment that official TIMES leaves out of the objective.

        TIMES starts its cost accounting in `minyr`.  The build-up window of a repeated
        investment starts floor(steps/2) years before the period; if that falls before
        `minyr` (and is not a year in which some existing stock was commissioned), TIMES
        drops the whole build from the cost equations while still counting its capacity.
        It can only happen in a first period that starts before its milestone year.
        Reproduced here for parity; `check` warns about it (PT321).
        """
        rep = self.repeats(life, i)
        if rep == 1:
            return []
        steps = self.build_steps(life, i)
        out = []
        for j in range(rep):
            first = self.P.B[i] + j * life - steps // 2
            if first < self.minyr and first not in self.past_years:
                out.append(j)
        return out

    # ------------------------------------------------------------------
    # capacity
    # ------------------------------------------------------------------
    def build_capacity(self, t: Tech, r: str) -> None:
        m, P, lp, dc = self.m, self.P, self.lp, self.dc
        key = (r, t.name)
        vints: list[Vintage] = []
        if not t.has_capacity:
            self.b.vintages[key] = vints
            return
        d = dc.rate
        # existing stock, by vintage
        minyr = self.minyr
        for year, cap in sorted(t.existing.get(r, {}).items()):
            life = t.lifetime.at(r, year, default=30.0)
            v = Vintage(year, year, year + life, life, None, None, cap)
            v.cpt = [self.cpt(v.start, v.end, i) for i in range(P.n)]
            fom = t.fixom.at(r, year, default=0.0)
            if fom:
                # TIMES starts its cost accounting at `minyr`, slightly before the first model year
                pre = sum(dc.disc(y) for y in range(max(year, minyr), P.first_year) if y < v.end)
                lp.add_const(pre * fom * cap, ("fixom_pre_horizon", r, t.name))
                for i in range(P.n):
                    yrs = [y for y in P.years(i) if v.start <= y < v.end]
                    if yrs:
                        lp.add_const(sum(dc.disc(y) for y in yrs) * fom * cap, ("fixom", r, t.name))
                        self._ann_const("fixom", r, t.name, i, fom * cap * len(yrs) / P.D[i])
            cost = t.invcost.at(r, year, default=0.0) if t.invcost else 0.0
            elife = t.economic_life.at(r, year, default=life)
            if cost and year + elife > minyr:
                # sunk investment: if the stock is not yet written off when the accounting starts,
                # TIMES charges its annuity for as long as it is available inside the horizon
                # (a constant; it cannot change any decision)
                ds = t.hurdle_rate.at(r, year, default=d)
                ann = cost * dc.crf(ds, elife) * ((1 + ds) ** 0.5 if m.mid_year else 1.0)
                for i in range(P.n):
                    if v.cpt[i] > 0:
                        lp.add_const(cap * ann * v.cpt[i] * self.b.pv[i], ("invcost_sunk", r, t.name))
                        self._ann_const("invcost_sunk", r, t.name, i, cap * ann)
            if any(c > 0 for c in v.cpt):
                vints.append(v)
        # residual stock profile (TIMES treats it as one vintage commissioned the year before the horizon)
        year0 = P.first_year - 1
        res_fom = t.fixom.at(r, year0, default=0.0) if t.fixom else 0.0
        res_ann = 0.0
        if t.invcost and t.residual:
            life0 = t.lifetime.at(r, year0, default=30.0) if t.lifetime else 30.0
            elife = t.economic_life.at(r, year0, default=life0)
            ds = t.hurdle_rate.at(r, year0, default=d)
            res_ann = t.invcost.at(r, year0, default=0.0) * dc.crf(ds, elife) * ((1 + ds) ** 0.5 if m.mid_year else 1.0)
        for i in range(P.n):
            res = t.residual.at(r, P.M[i]) if t.residual else None
            if res:
                self.b.residual[(r, t.name, i)] = res
                if res_fom:
                    lp.add_const(self.b.pv[i] * res_fom * res, ("fixom", r, t.name))
                    self._ann_const("fixom", r, t.name, i, res_fom * res)
                if res_ann:
                    lp.add_const(self.b.pv[i] * res_ann * res, ("invcost_sunk", r, t.name))
                    self._ann_const("invcost_sunk", r, t.name, i, res_ann * res)
        # new capacity
        if t.can_invest:
            for i in range(P.n):
                M = P.M[i]
                life = t.lifetime.at(r, M, default=30.0)
                rep = self.repeats(life, i)
                var = lp.var(("NCAP", r, t.name, M))
                v = Vintage(M, P.B[i], P.B[i] + rep * life, life, i, var)
                v.cpt = [self.cpt(v.start, v.end, j) if j >= i else 0.0 for j in range(P.n)]
                v.cpt = [c if c >= 1.0 / 512 else 0.0 for c in v.cpt]
                vints.append(v)
                # bounds on new capacity
                for (what, kind), p in t.bounds.items():
                    if what != "new_capacity":
                        continue
                    val = p.at(r, M)
                    if val is None:
                        continue
                    if kind in ("max", "fix"):
                        lp.set_bounds(var, ub=val)
                    if kind in ("min", "fix"):
                        lp.set_bounds(var, lb=val)
                # costs
                elife = t.economic_life.at(r, M, default=life)
                ds = t.hurdle_rate.at(r, M, default=d)
                cor = dc.crf(ds, elife) / dc.crf(d, elife)
                if m.mid_year:
                    cor *= (1 + ds) ** 0.5
                crf_s = dc.crf(ds, elife) * ((1 + ds) ** 0.5 if m.mid_year else 1.0)
                steps = self.build_steps(life, i)
                uncharged = self.uncharged_builds(life, i)
                acc = {"inv": 0.0, "fom": 0.0}
                inv_ann = [0.0] * P.n
                fom_ann = [0.0] * P.n

                def book(k: float, w: float, cost: float, fom: float) -> None:
                    """Account for a share w of one unit commissioned in year k."""
                    acc["inv"] += w * cost * dc.disc(k) * cor * (1.0 - self.salvage_share(k, life))
                    a = 0
                    while a < life - 1e-9:
                        y = k + a
                        part = min(1.0, life - a)
                        if y <= self.eoh + 1e-9:
                            acc["fom"] += w * part * fom * dc.disc(y)
                            pi = P.index_of_year(int(math.floor(y + 1e-9)))
                            if pi is not None:
                                fom_ann[pi] += w * part * fom / P.D[pi]
                        a += 1
                    a = 0
                    while a < elife - 1e-9:
                        y = k + a
                        pi = P.index_of_year(int(math.floor(y + 1e-9)))
                        if pi is not None:
                            inv_ann[pi] += w * min(1.0, elife - a) * cost * crf_s / P.D[pi]
                        a += 1

                starts = set(P.B)
                for j in range(rep):
                    if j in uncharged:
                        continue                # see uncharged_builds()
                    k = self.cost_centre(i) if rep == 1 else P.B[i] + j * life
                    first = k - steps // 2
                    if steps > 1 and first in starts and k not in starts:
                        # TIMES quirk: a repeated investment whose build-up window happens to begin
                        # on the first year of a later period is not booked as one lump at its
                        # centre; each yearly step is booked in its own year.
                        for step in range(steps):
                            kk = first + step
                            book(kk, 1.0 / steps, t.invcost.at(r, kk, default=0.0),
                                 t.fixom.at(r, kk, default=0.0) if t.fixom else 0.0)
                    else:
                        book(k, 1.0, self.window_cost(t.invcost, r, k, steps),
                             self.window_cost(t.fixom, r, k, steps) if t.fixom else 0.0)
                inv_pv, fom_pv = acc["inv"], acc["fom"]
                lp.add_cost(var, inv_pv, ("invcost", r, t.name))
                lp.add_cost(var, fom_pv, ("fixom", r, t.name))
                for pi in range(P.n):
                    self._ann("invcost", r, t.name, pi, var, inv_ann[pi])
                    self._ann("fixom", r, t.name, pi, var, fom_ann[pi])
        self.b.vintages[key] = vints
        # capacity expressions per period, and capacity bounds
        for i in range(P.n):
            expr: Expr = {}
            const = self.b.residual.get((r, t.name, i), 0.0)
            for v in vints:
                if v.cpt[i] <= 0:
                    continue
                if v.var is None:
                    const += v.cpt[i] * v.fixed
                else:
                    expr[v.var] = expr.get(v.var, 0.0) + v.cpt[i]
            self.b.cap[(r, t.name, i)] = (expr, const)
            for (what, kind), p in t.bounds.items():
                if what != "capacity":
                    continue
                val = p.at(r, P.M[i])
                if val is None:
                    continue
                sense = {"max": "<=", "min": ">=", "fix": "="}[kind]
                lp.row(("BND_CAP", r, t.name, P.M[i], kind), expr, sense, val - const)

    def _ann(self, cat: str, r: str, tech: str, i: int, var: int, coef: float) -> None:
        if coef:
            self.b.annual.setdefault((cat, r, tech, i), []).append((var, coef))

    def _ann_const(self, cat: str, r: str, tech: str, i: int, value: float) -> None:
        if value:
            k = (cat, r, tech, i)
            self.b.annual_const[k] = self.b.annual_const.get(k, 0.0) + value

    # ------------------------------------------------------------------
    # availability lookup
    # ------------------------------------------------------------------
    def af(self, param, r: str, s: str, year: float) -> float | None:
        """Availability for timeslice s: the value given for s or its nearest ancestor."""
        for a in self.ts.ancestors(s):
            if a == ANNUAL:
                break
            if param.has(r, a):
                return param.at(r, year, a)
        return param.at(r, year, None) if param.has(r, None) else None

    def capacity_rows(self, t: Tech, r: str, i: int, tag: Any, year: float, act_by_slice: dict[str, Expr],
                      cap_expr: Expr, cap_const: float, slices: list[str]) -> None:
        """Capacity-activity rows for one (vintage group, period)."""
        lp, ts, M = self.lp, self.ts, self.P.M[i]

        def emit(kind: str, g: str, value: float, sense: str):
            lhs: Expr = {}
            for s in slices:
                if ts.is_within(s, g):
                    add_to(lhs, act_by_slice[s])
            factor = value * t.cap2act * ts.frac[g]
            add_to(lhs, cap_expr, -factor)
            lp.row(("CAPACT", r, t.name, tag, M, g, kind), lhs, sense, factor * cap_const)

        have_fix = bool(t.af_fix)
        for s in slices:
            if have_fix:
                val = self.af(t.af_fix, r, s, year)
                if val is not None:
                    emit("fix", s, val, "=")
                    continue
            val = self.af(t.af_max, r, s, year) if t.af_max else None
            emit("max", s, 1.0 if val is None else val, "<=")
            if t.af_min:
                lo = self.af(t.af_min, r, s, year)
                if lo:
                    emit("min", s, lo, ">=")
        for param, kind, sense in ((t.cf_max, "cfmax", "<="), (t.cf_min, "cfmin", ">=")):
            if not param:
                continue
            if param.has(r, None):
                emit(kind, ANNUAL, param.at(r, year, None), sense)
            for g in param.seconds():
                if param.has(r, g):
                    emit(kind, g, param.at(r, year, g), sense)

    # ------------------------------------------------------------------
    # flows
    # ------------------------------------------------------------------
    def shape_of(self, c: str | None) -> dict[str, float] | None:
        """The load shape of commodity c, extended to every timeslice of the tree (None if it has none)."""
        if c is None:
            return None
        if c in self._shapes:
            return self._shapes[c]
        com, ts = self.m.commodities[c], self.ts
        full: dict[str, float] | None = None
        if com.load_shape:
            full = {}
            for s in ts.names:
                lvl = ts.level_of[s]
                if lvl == com.level:
                    full[s] = com.load_shape[s]
                elif lvl > com.level:                       # finer: by hours within the shaped slice
                    anc = ts.ancestor_at(s, com.level)
                    full[s] = com.load_shape[anc] * ts.frac[s] / ts.frac[anc]
                else:                                       # coarser: sum of what lies inside
                    full[s] = sum(com.load_shape[x] for x in ts.below(s, com.level))
        self._shapes[c] = full
        return full

    def spread(self, c: str, s: str, lead: str | None = None) -> list[tuple[str, float]]:
        """Map a flow in process timeslice s onto the timeslices of commodity c.

        A process that runs at a coarser level than a commodity it touches has that
        flow split over the finer timeslices.  As in TIMES, the split follows the
        load shape of the process's main output if it has one (an annual-level
        heater draws electricity when heat is wanted), otherwise the commodity's
        own load shape, otherwise hours.
        """
        com, ts = self.m.commodities[c], self.ts
        if ts.level_of[s] >= com.level:
            return [(ts.ancestor_at(s, com.level), 1.0)]
        subs = ts.below(s, com.level)
        shape = self.shape_of(lead) or self.shape_of(c)
        if shape is not None and shape[s] > 0:
            return [(x, shape[x] / shape[s]) for x in subs]
        return [(x, ts.frac[x] / ts.frac[s]) for x in subs]

    def add_flow(self, r: str, tech: str, c: str, direction: str, i: int, s: str, expr: Expr,
                 lead: str | None = None) -> None:
        for sc, w in self.spread(c, s, lead):
            key = (r, tech, c, direction, i, sc)
            target = self.b.flows.get(key)
            if target is None:
                target = self.b.flows[key] = {}
                self.flow_index.setdefault((r, c, direction, i), []).append(target)
            add_to(target, expr, w)

    def build_process(self, t: Tech, r: str) -> None:
        m, P, lp, ts = self.m, self.P, self.lp, self.ts
        slices = ts.at_level(t.level)
        vints = self.b.vintages[(r, t.name)]
        # the main output whose load shape (if any) the process's flows follow
        lead = next((c for c in t.primary_outputs if m.commodities[c].load_shape), None)
        for i in range(P.n):
            M = P.M[i]
            groups: list[tuple[Any, float, Expr, float]] = []   # (tag, data year, capacity expr, capacity const)
            if t.has_capacity and t.vintaged:
                for v in vints:
                    if v.cpt[i] > 0:
                        if v.var is None:
                            groups.append((v.label, v.label, {}, v.cpt[i] * v.fixed))
                        else:
                            groups.append((v.label, v.label, {v.var: v.cpt[i]}, 0.0))
                res = self.b.residual.get((r, t.name, i))
                if res:
                    groups.append((0, P.first_year - 1, {}, res))
            elif t.has_capacity:
                expr, const = self.b.cap[(r, t.name, i)]
                if not expr and const <= 0:
                    continue        # no capacity possible in this period
                groups.append((None, M, expr, const))
            else:
                groups.append((None, M, {}, 0.0))

            vom_pv, vom_avg = self.year_cost(t.varom, r, i) if t.varom else (0.0, 0.0)
            act_total: Expr = self.b.activity.setdefault((r, t.name, i), {})
            for tag, year, cap_expr, cap_const in groups:
                eff = t.efficiency.at(r, year, default=1.0) if t.efficiency else 1.0
                act_by_slice: dict[str, Expr] = {}
                for s in slices:
                    act = lp.var(("ACT", r, t.name, tag, M, s))
                    act_by_slice[s] = {act: 1.0}
                    act_total[act] = 1.0
                    if vom_pv:
                        lp.add_cost(act, vom_pv, ("varom", r, t.name))
                        self._ann("varom", r, t.name, i, act, vom_avg)
                    basis: dict[str, Expr] = {"activity": {act: 1.0}}
                    for c, coef in t.inputs.items():
                        e = {act: coef.at(r, year, default=1.0) / eff}
                        basis[c] = e
                        self.add_flow(r, t.name, c, "in", i, s, e, lead)
                    for c, coef in t.outputs.items():
                        e = {act: coef.at(r, year, default=1.0)}
                        basis[c] = e
                        self.add_flow(r, t.name, c, "out", i, s, e, lead)
                    for grp, direction, tagname in ((t.flex_in, "in", "FIN"), (t.flex_out, "out", "FOUT")):
                        if not grp:
                            continue
                        # Flexible inputs are decided at the finest level of their commodities, so a
                        # coarse-level process may use a different mix in each finer timeslice (as in
                        # TIMES); flexible outputs define the activity and stay at the process level.
                        fine = t.level
                        if direction == "in":
                            fine = max([t.level] + [m.commodities[c].level for c in grp])
                        subs = ts.below(s, fine) if fine > t.level else [s]
                        shape = self.shape_of(lead)
                        for x in subs:
                            if x == s:
                                w = 1.0
                            elif shape is not None and shape[s] > 0:
                                w = shape[x] / shape[s]
                            else:
                                w = ts.frac[x] / ts.frac[s]
                            define: Expr = {act: -w}
                            total: Expr = {}
                            fvars = {}
                            for c, mem in grp.items():
                                fv = lp.var((tagname, r, t.name, tag, M, x, c))
                                fvars[c] = fv
                                total[fv] = 1.0
                                e_c = 1.0
                                if direction == "in":
                                    e_c = mem.efficiency.at(r, year, default=eff) if mem.efficiency else eff
                                define[fv] = e_c
                                basis.setdefault(c, {})[fv] = 1.0
                                self.add_flow(r, t.name, c, direction, i, x, {fv: 1.0}, lead)
                            lp.row((tagname + "_DEF", r, t.name, tag, M, x), define, "=", 0.0)
                            for c, mem in grp.items():
                                for param, kind, sense in ((mem.max_share, "max", "<="), (mem.min_share, "min", ">=")):
                                    if not param:
                                        continue
                                    share = param.at(r, year)
                                    if share is None:
                                        continue
                                    row = {j: -share * v for j, v in total.items()}
                                    row[fvars[c]] = row.get(fvars[c], 0.0) + 1.0
                                    lp.row((tagname + "_SHR", r, t.name, tag, M, x, c, kind), row, sense, 0.0)
                    for em, bases in t.emissions.items():
                        target = self.b.emis.setdefault((r, t.name, em, i), {})
                        for bname, factor in bases.items():
                            f = factor.at(r, year, default=0.0)
                            if f and bname in basis:
                                add_to(target, basis[bname], f)
                if t.has_capacity:
                    self.capacity_rows(t, r, i, tag, year, act_by_slice, cap_expr, cap_const, slices)
            self.activity_bounds(t, r, i, act_total)
        self.cumulative_bounds(t, r)

    def activity_bounds(self, t: Tech, r: str, i: int, act_total: Expr) -> None:
        for (what, kind), p in t.bounds.items():
            if what != "activity":
                continue
            val = p.at(r, self.P.M[i])
            if val is None:
                continue
            sense = {"max": "<=", "min": ">=", "fix": "="}[kind]
            self.lp.row(("BND_ACT", r, t.name, self.P.M[i], kind), act_total, sense, val)

    def cumulative_bounds(self, t: Tech, r: str) -> None:
        for kind, table in t.cumulative_activity.items():
            val = table.get(r, table.get(None))
            if val is None:
                continue
            expr: Expr = {}
            for i in range(self.P.n):
                add_to(expr, self.b.activity.get((r, t.name, i), {}), self.P.D[i])
            self.lp.row(("BND_CUM", r, t.name, kind), expr, "<=" if kind == "max" else ">=", val)

    # ------------------------------------------------------------------
    # storage and trade
    # ------------------------------------------------------------------
    def build_storage(self, t: Tech, r: str) -> None:
        P, lp, ts = self.P, self.lp, self.ts
        c = t.commodity
        slices = ts.at_level(t.level)
        cycle_groups = ts.at_level(t.cycle_level)
        child_level = ts.levels[t.cycle_level + 1]
        for i in range(P.n):
            M = P.M[i]
            cap_expr, cap_const = self.b.cap[(r, t.name, i)]
            if not cap_expr and cap_const <= 0:
                continue
            eff = t.efficiency.at(r, M, default=1.0)
            vom_pv, vom_avg = self.year_cost(t.varom, r, i) if t.varom else (0.0, 0.0)
            act_total = self.b.activity.setdefault((r, t.name, i), {})
            sin, sout = {}, {}
            for s in slices:
                sin[s] = lp.var(("SIN", r, t.name, M, s))
                sout[s] = lp.var(("SOUT", r, t.name, M, s))
                act_total[sout[s]] = 1.0
                self.add_flow(r, t.name, c, "in", i, s, {sin[s]: 1.0})
                self.add_flow(r, t.name, c, "out", i, s, {sout[s]: 1.0})
                if vom_pv:
                    lp.add_cost(sout[s], vom_pv, ("varom", r, t.name))
                    self._ann("varom", r, t.name, i, sout[s], vom_avg)
                af = self.af(t.af_max, r, s, M) if t.af_max else None
                factor = (1.0 if af is None else af) * t.cap2act * ts.frac[s]
                for name, var in (("STG_OUT", sout[s]), ("STG_IN", sin[s])):
                    row = {var: 1.0}
                    add_to(row, cap_expr, -factor)
                    lp.row((name, r, t.name, M, s), row, "<=", factor * cap_const)
            hours = t.hours.at(r, M) if t.hours else None
            for g in cycle_groups:
                inside = [s for s in slices if ts.is_within(s, g)]
                bal = {}
                for s in inside:
                    bal[sin[s]] = eff
                    bal[sout[s]] = -1.0
                lp.row(("STG_CYCLE", r, t.name, M, g), bal, "=", 0.0)
                if hours:
                    if child_level == "daynite":
                        cycles = 365.0 * ts.frac[g]
                    elif child_level == "weekly":
                        cycles = 52.0 * ts.frac[g]
                    else:
                        cycles = 1.0
                    factor = cycles * hours / 8760.0 * t.cap2act
                    row = {sout[s]: 1.0 for s in inside}
                    add_to(row, cap_expr, -factor)
                    lp.row(("STG_ENERGY", r, t.name, M, g), row, "<=", factor * cap_const)
            self.activity_bounds(t, r, i, act_total)
        self.cumulative_bounds(t, r)

    def build_trade(self, t: Tech) -> None:
        P, lp, ts = self.P, self.lp, self.ts
        c, a, b = t.commodity, t.region_from, t.region_to
        slices = ts.at_level(t.level)
        for i in range(P.n):
            M = P.M[i]
            if t.has_capacity:
                cap_expr, cap_const = self.b.cap[(a, t.name, i)]
                if not cap_expr and cap_const <= 0:
                    continue
            eff = t.efficiency.at(a, M, default=1.0)
            vom_pv, vom_avg = self.year_cost(t.varom, a, i) if t.varom else (0.0, 0.0)
            act_total = self.b.activity.setdefault((a, t.name, i), {})
            for s in slices:
                both: Expr = {}
                for direction, src, dst in (("fwd", a, b),) + ((("rev", b, a),) if t.bidirectional else ()):
                    x = lp.var(("TRD", t.name, direction, M, s))
                    both[x] = 1.0
                    act_total[x] = 1.0
                    self.add_flow(src, t.name, c, "in", i, s, {x: 1.0})
                    self.add_flow(dst, t.name, c, "out", i, s, {x: eff})
                    if vom_pv:
                        lp.add_cost(x, vom_pv, ("varom", a, t.name))
                        self._ann("varom", a, t.name, i, x, vom_avg)
                if t.has_capacity:
                    af = self.af(t.af_max, a, s, M) if t.af_max else None
                    factor = (1.0 if af is None else af) * t.cap2act * ts.frac[s]
                    add_to(both, cap_expr, -factor)
                    lp.row(("TRD_CAP", t.name, M, s), both, "<=", factor * cap_const)
            self.activity_bounds(t, a, i, act_total)
        self.cumulative_bounds(t, a)

    # ------------------------------------------------------------------
    # system rows
    # ------------------------------------------------------------------
    def build_balances(self) -> None:
        m, P, lp, ts = self.m, self.P, self.lp, self.ts
        by_node: dict[tuple[str, int, str, str], list[tuple[str, Expr]]] = {}
        for (r, tech, c, direction, i, sc), expr in self.b.flows.items():
            by_node.setdefault((r, i, c, sc), []).append((direction, expr))
        for c, com in m.commodities.items():
            if com.kind == "emission":
                continue
            for r in m.regions:
                for i in range(P.n):
                    M = P.M[i]
                    ie = com.efficiency.at(r, M, default=1.0) if com.efficiency else 1.0
                    dem_annual = com.demand.at(r, M, default=0.0) if com.demand else 0.0
                    for sc in ts.at_level(com.level):
                        share = com.load_shape[sc] if com.load_shape else ts.frac[sc]
                        dem = dem_annual * share
                        if dem:
                            self.b.demand[(r, i, c, sc)] = dem
                        row: Expr = {}
                        for direction, expr in by_node.get((r, i, c, sc), []):
                            add_to(row, expr, ie if direction == "out" else -1.0)
                        if self.relax and (row or dem):
                            sl = lp.var(("SLACK", r, c, M, sc))
                            lp.add_cost(sl, self.slack_penalty * self.b.pv[i], ("slack", r, c))
                            row[sl] = 1.0
                        lp.row(("BAL", r, c, M, sc), row, com.sense, dem)

    def build_emissions(self) -> None:
        m, P, lp, dc = self.m, self.P, self.lp, self.dc
        self._emis_cache: dict[tuple[str, str, int], Expr] = {}
        for (r, tech, e, i), expr in self.b.emis.items():
            add_to(self._emis_cache.setdefault((r, e, i), {}), expr)
        for e, com in m.commodities.items():
            if com.kind != "emission":
                continue
            for r in m.regions:
                cum: Expr = {}
                for i in range(P.n):
                    expr = self._emis_cache.get((r, e, i), {})
                    add_to(cum, expr, P.D[i])
                    cap = com.cap.at(r, P.M[i]) if com.cap else None
                    if cap is not None:
                        lp.row(("EMIS_CAP", r, e, P.M[i]), expr, "<=", cap)
                    if com.tax and expr:
                        pv = sum(dc.disc(y) * com.tax.at(r, y, default=0.0) for y in P.years(i))
                        avg = sum(com.tax.at(r, y, default=0.0) for y in P.years(i)) / P.D[i]
                        for j, coef in expr.items():
                            lp.add_cost(j, pv * coef, ("emission_tax", r, e))
                            self._ann("emission_tax", r, e, i, j, avg * coef)
                budget = com.budget.get(r, com.budget.get(None))
                if budget is not None:
                    lp.row(("EMIS_BUDGET", r, e), cum, "<=", budget)

    def build_peak(self) -> None:
        m, P, lp, ts = self.m, self.P, self.lp, self.ts
        for c, com in m.commodities.items():
            if not com.peak_reserve:
                continue
            slices = com.peak_slices or ts.at_level(com.level)
            for r in m.regions:
                for i in range(P.n):
                    M = P.M[i]
                    rsv = com.peak_reserve.at(r, M)
                    if rsv is None:
                        continue
                    ie = com.efficiency.at(r, M, default=1.0) if com.efficiency else 1.0
                    for sc in slices:
                        row: Expr = {}
                        rhs = (1.0 + rsv) * self.b.demand.get((r, i, c, sc), 0.0)
                        for t in m.techs():
                            pk = t.peak_contribution.at(r if t.kind != "trade" else t.region_from, M, default=1.0) \
                                if t.peak_contribution else 1.0
                            out = self.b.flows.get((r, t.name, c, "out", i, sc))
                            inn = self.b.flows.get((r, t.name, c, "in", i, sc))
                            if inn:
                                add_to(row, inn, -(1.0 + rsv))
                            if out is None:
                                continue
                            if t.kind == "process" and t.has_capacity and c in t.primary_outputs:
                                expr, const = self.b.cap[(r, t.name, i)]
                                coef = 1.0 if t.flex_out else t.outputs[c].at(r, M, default=1.0)
                                f = pk * t.cap2act * ts.frac[sc] * coef * ie
                                add_to(row, expr, f)
                                rhs -= f * const
                            else:
                                add_to(row, out, pk * ie)
                        lp.row(("PEAK", r, c, M, sc), row, ">=", rhs)

    def build_constraints(self) -> None:
        m, P, lp = self.m, self.P, self.lp
        for uc in m.constraints.values():
            region_sets = [[r] for r in uc.regions] if uc.region_mode == "each" else [list(uc.regions)]
            for regs in region_sets:
                rkey = regs[0] if uc.region_mode == "each" else None
                rlabel = regs[0] if uc.region_mode == "each" else "ALL"
                cum: Expr = {}
                for i in range(P.n):
                    M = P.M[i]
                    if (uc.year_from and P.E[i] < uc.year_from) or (uc.year_until and P.B[i] > uc.year_until):
                        continue
                    row: Expr = {}
                    const = 0.0
                    for term in uc.terms:
                        # In a constraint summed over periods, annual rates (flows, activity, emissions)
                        # count once per year of the period; capacities count once per period.
                        weight = 1.0
                        if uc.period_mode == "sum" and term.kind not in ("capacity", "new_capacity"):
                            weight = float(P.D[i])
                        for r in regs:
                            coef = term.coef.at(r, M, default=0.0) * weight
                            if not coef:
                                continue
                            expr, k = self.term_expr(term, r, i)
                            add_to(row, expr, coef)
                            const += coef * k
                    if uc.period_mode == "sum":
                        add_to(cum, row)
                        cum[-1] = cum.get(-1, 0.0) + const
                        continue
                    rhs = uc.rhs.at(rkey, M)
                    if rhs is None:
                        continue
                    lp.row(("UC", uc.name, rlabel, M), row, uc.sense, rhs - const)
                if uc.period_mode == "sum":
                    const = cum.pop(-1, 0.0)
                    rhs = uc.rhs.at(rkey, P.M[-1])
                    if rhs is None:
                        rhs = uc.rhs.at(rkey, P.M[0]) or 0.0
                    lp.row(("UC", uc.name, rlabel, "ALL"), cum, uc.sense, rhs - const)

    def term_expr(self, term, r: str, i: int) -> tuple[Expr, float]:
        m, ts = self.m, self.ts
        out: Expr = {}
        const = 0.0
        if term.kind == "flow":
            com = m.commodities[term.commodity]
            for p in term.processes:
                for sc in ts.at_level(com.level):
                    for direction in ("in", "out"):
                        e = self.b.flows.get((r, p, term.commodity, direction, i, sc))
                        if e:
                            add_to(out, e)
        elif term.kind in ("production", "consumption"):
            direction = "out" if term.kind == "production" else "in"
            for e in self.flow_index.get((r, term.commodity, direction, i), []):
                add_to(out, e)
        elif term.kind == "emission":
            add_to(out, self._emis_cache.get((r, term.commodity, i), {}))
        elif term.kind == "capacity":
            for p in term.processes:
                if (r, p, i) in self.b.cap:
                    e, k = self.b.cap[(r, p, i)]
                    add_to(out, e)
                    const += k
        elif term.kind == "new_capacity":
            for p in term.processes:
                name = ("NCAP", r, p, self.P.M[i])
                if self.lp.has_var(name):
                    j = self.lp.var_index[name]
                    out[j] = out.get(j, 0.0) + 1.0
        elif term.kind == "activity":
            for p in term.processes:
                add_to(out, self.b.activity.get((r, p, i), {}))
        return out, const

    # ------------------------------------------------------------------
    def build(self) -> Built:
        m = self.m
        for t in m.techs():
            for r in t.regions:
                self.build_capacity(t, r)
        for t in m.processes.values():
            for r in t.regions:
                self.build_process(t, r)
        for t in m.storage.values():
            for r in t.regions:
                self.build_storage(t, r)
        for t in m.trade.values():
            self.build_trade(t)
        self.build_balances()
        self.build_emissions()
        self.build_peak()
        self.build_constraints()
        return self.b


def build_lp(model: Model, relax: bool = False, slack_penalty: float = 1e6) -> Built:
    return Builder(model, relax=relax, slack_penalty=slack_penalty).build()
