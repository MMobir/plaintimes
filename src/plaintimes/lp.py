"""A small named-LP builder on top of HiGHS.

Variables and rows are addressed by tuples such as ("ACT", region, process,
vintage, period, timeslice), which keeps the generated LP inspectable: any
row can be printed back as an equation, and any dual or reduced cost can be
traced to the thing it belongs to.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Hashable

import numpy as np

INF = math.inf


@dataclass
class Solution:
    status: str                    # optimal | infeasible | unbounded | error
    objective: float | None
    col_value: np.ndarray | None = None
    col_dual: np.ndarray | None = None
    row_value: np.ndarray | None = None
    row_dual: np.ndarray | None = None
    solver: str = "HiGHS"
    solver_version: str = ""
    seconds: float = 0.0
    iterations: int = 0
    message: str = ""


class LP:
    def __init__(self, name: str = "model"):
        self.name = name
        self.var_index: dict[Hashable, int] = {}
        self.var_names: list[Hashable] = []
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.cost: list[float] = []
        self.row_index: dict[Hashable, int] = {}
        self.row_names: list[Hashable] = []
        self.row_lo: list[float] = []
        self.row_hi: list[float] = []
        self.row_start: list[int] = [0]
        self.col_idx: list[int] = []
        self.values: list[float] = []
        self.obj_const = 0.0
        # cost bookkeeping: (category, key) -> list[(var, coef)] and constants
        self.cost_terms: dict[tuple, list[tuple[int, float]]] = {}
        self.cost_consts: dict[tuple, float] = {}
        # rows with no variables whose right-hand side cannot hold (model is infeasible)
        self.empty_infeasible: list[tuple] = []

    # -- variables --------------------------------------------------------
    def var(self, name: Hashable, lb: float = 0.0, ub: float = INF) -> int:
        idx = self.var_index.get(name)
        if idx is None:
            idx = len(self.var_names)
            self.var_index[name] = idx
            self.var_names.append(name)
            self.lb.append(lb)
            self.ub.append(ub)
            self.cost.append(0.0)
        return idx

    def has_var(self, name: Hashable) -> bool:
        return name in self.var_index

    def set_bounds(self, idx: int, lb: float | None = None, ub: float | None = None) -> None:
        if lb is not None:
            self.lb[idx] = max(self.lb[idx], lb)
        if ub is not None:
            self.ub[idx] = min(self.ub[idx], ub)

    def add_cost(self, idx: int, coef: float, category: tuple | None = None) -> None:
        if coef == 0.0:
            return
        self.cost[idx] += coef
        if category is not None:
            self.cost_terms.setdefault(category, []).append((idx, coef))

    def add_const(self, value: float, category: tuple | None = None) -> None:
        if value == 0.0:
            return
        self.obj_const += value
        if category is not None:
            self.cost_consts[category] = self.cost_consts.get(category, 0.0) + value

    # -- rows -------------------------------------------------------------
    def row(self, name: Hashable, coefs: dict[int, float], sense: str, rhs: float) -> int | None:
        """Add  sum(coef * var)  <sense>  rhs.  Empty rows are checked, not added."""
        items = [(j, c) for j, c in coefs.items() if c != 0.0]
        if not items:
            ok = (sense == "<=" and 0.0 <= rhs + 1e-9) or (sense == ">=" and 0.0 >= rhs - 1e-9) or \
                 (sense == "=" and abs(rhs) <= 1e-9)
            if not ok:
                self.empty_infeasible.append((name, sense, rhs))
            return None
        if name in self.row_index:
            raise KeyError(f"duplicate row {name}")
        idx = len(self.row_names)
        self.row_index[name] = idx
        self.row_names.append(name)
        if sense == "<=":
            self.row_lo.append(-INF)
            self.row_hi.append(rhs)
        elif sense == ">=":
            self.row_lo.append(rhs)
            self.row_hi.append(INF)
        else:
            self.row_lo.append(rhs)
            self.row_hi.append(rhs)
        items.sort()
        self.col_idx.extend(j for j, _ in items)
        self.values.extend(c for _, c in items)
        self.row_start.append(len(self.col_idx))
        return idx

    # -- inspection -------------------------------------------------------
    @property
    def n_vars(self) -> int:
        return len(self.var_names)

    @property
    def n_rows(self) -> int:
        return len(self.row_names)

    @property
    def n_nonzeros(self) -> int:
        return len(self.values)

    def row_terms(self, idx: int) -> list[tuple[Hashable, float]]:
        a, b = self.row_start[idx], self.row_start[idx + 1]
        return [(self.var_names[self.col_idx[k]], self.values[k]) for k in range(a, b)]

    def row_sense_rhs(self, idx: int) -> tuple[str, float]:
        lo, hi = self.row_lo[idx], self.row_hi[idx]
        if lo == hi:
            return "=", lo
        return ("<=", hi) if lo == -INF else (">=", lo)

    def format_row(self, idx: int, max_terms: int = 40) -> str:
        terms = self.row_terms(idx)
        parts = []
        for name, coef in terms[:max_terms]:
            parts.append(f"{coef:+.6g}*{fmt_name(name)}")
        if len(terms) > max_terms:
            parts.append(f"... (+{len(terms) - max_terms} more terms)")
        sense, rhs = self.row_sense_rhs(idx)
        return f"{fmt_name(self.row_names[idx])}:  {' '.join(parts)}  {sense}  {rhs:.6g}"

    # -- solve ------------------------------------------------------------
    def _highs_lp(self, with_names: bool = False):
        import highspy

        lp = highspy.HighsLp()
        lp.num_col_ = self.n_vars
        lp.num_row_ = self.n_rows
        lp.col_cost_ = np.array(self.cost, dtype=np.float64)
        lp.col_lower_ = np.array(self.lb, dtype=np.float64)
        lp.col_upper_ = np.array(self.ub, dtype=np.float64)
        lp.row_lower_ = np.array(self.row_lo, dtype=np.float64)
        lp.row_upper_ = np.array(self.row_hi, dtype=np.float64)
        lp.offset_ = self.obj_const
        lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
        lp.a_matrix_.start_ = np.array(self.row_start, dtype=np.int32)
        lp.a_matrix_.index_ = np.array(self.col_idx, dtype=np.int32)
        lp.a_matrix_.value_ = np.array(self.values, dtype=np.float64)
        if with_names:
            lp.col_names_ = [flat_name(n) for n in self.var_names]
            lp.row_names_ = [flat_name(n) for n in self.row_names]
        return lp

    def write(self, path: str) -> None:
        """Write the LP as an .mps or .lp file, with readable names, for use with any other solver."""
        import highspy

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.passModel(self._highs_lp(with_names=True))
        h.writeModel(path)

    def solve(self, time_limit: float | None = None, method: str = "auto", log: bool = False) -> Solution:
        """Solve with HiGHS. method: auto | simplex | ipm | pdlp (auto uses interior point for large models)."""
        import time

        import highspy

        t0 = time.time()
        h = highspy.Highs()
        h.setOptionValue("output_flag", bool(log))
        if time_limit:
            h.setOptionValue("time_limit", float(time_limit))
        if method in ("auto", "choose"):
            method = "ipm" if self.n_rows + self.n_vars > 40000 else "choose"
        if method != "choose":
            h.setOptionValue("solver", method)
        version = f"{h.versionMajor()}.{h.versionMinor()}.{h.versionPatch()}" if hasattr(h, "versionMajor") else ""
        if self.empty_infeasible:
            name, sense, rhs = self.empty_infeasible[0]
            return Solution("infeasible", None, solver_version=version,
                            message=f"constraint {fmt_name(name)} has no variables but requires 0 {sense} {rhs:g}")
        lp = self._highs_lp()
        h.passModel(lp)
        h.run()
        status = h.getModelStatus()
        info = h.getInfo()
        name = h.modelStatusToString(status)
        secs = time.time() - t0
        iters = int(getattr(info, "simplex_iteration_count", 0) or 0) + int(getattr(info, "ipm_iteration_count", 0) or 0)
        if status == highspy.HighsModelStatus.kOptimal:
            sol = h.getSolution()
            return Solution(
                "optimal", float(info.objective_function_value),
                np.array(sol.col_value), np.array(sol.col_dual), np.array(sol.row_value), np.array(sol.row_dual),
                solver_version=version, seconds=secs, iterations=iters, message=name,
            )
        low = name.lower()
        if "infeasible" in low and "unbounded" not in low:
            st = "infeasible"
        elif "unbounded" in low:
            st = "unbounded" if "infeasible" not in low else "infeasible_or_unbounded"
        else:
            st = "error"
        return Solution(st, None, solver_version=version, seconds=secs, iterations=iters, message=name)


def fmt_name(name: Hashable) -> str:
    if isinstance(name, tuple):
        head, rest = name[0], name[1:]
        return f"{head}[{','.join(str(x) for x in rest if x is not None)}]"
    return str(name)


def flat_name(name: Hashable) -> str:
    """A name safe for .lp / .mps files: ACT.NORTH.COAL_PP.2030.WD"""
    parts = name if isinstance(name, tuple) else (name,)
    text = ".".join(str(x) for x in parts if x is not None)
    return "".join(ch if ch.isalnum() or ch in "._" else "_" for ch in text)
