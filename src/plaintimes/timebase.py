"""Periods, years, discounting and the timeslice tree."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diagnostics import Diagnostics

ANNUAL = "ANNUAL"
# TIMES level names by tree depth (depth of the user's nested mapping)
LEVELS_BY_DEPTH = {
    0: ["annual"],
    1: ["annual", "season"],
    2: ["annual", "season", "daynite"],
    3: ["annual", "season", "weekly", "daynite"],
}
TIMES_LEVEL = {"annual": "ANNUAL", "season": "SEASON", "weekly": "WEEKLY", "daynite": "DAYNITE"}


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
@dataclass
class Periods:
    """Model periods.

    Each period t has a milestone year M (its label), a first year B, a last
    year E and a length D = E - B + 1.  Two ways to define them:

    * ``{start: 2025, lengths: [5, 5, 10]}``: explicit; M is the middle year,
      M = B + (D-1)//2 (the TIMES standard convention).
    * ``{milestones: [2025, 2030, 2040]}``: boundaries fall halfway between
      milestones, E(t) = floor((M(t)+M(t+1))/2) (the TIMES 'MOD' convention).
      ``start`` and ``end`` may be given; they default to M(1) and a last
      period symmetric around its milestone.
    """

    M: list[int]
    B: list[int]
    E: list[int]
    mode: str  # 'std' | 'mod': which TIMES cost-accounting variant applies (see docs/MATH.md)

    @property
    def D(self) -> list[int]:
        return [e - b + 1 for b, e in zip(self.B, self.E)]

    @property
    def n(self) -> int:
        return len(self.M)

    @property
    def first_year(self) -> int:
        return self.B[0]

    @property
    def last_year(self) -> int:
        return self.E[-1]

    def years(self, i: int) -> range:
        return range(self.B[i], self.E[i] + 1)

    def index_of_year(self, year: int) -> int | None:
        for i in range(self.n):
            if self.B[i] <= year <= self.E[i]:
                return i
        return None

    def table(self) -> list[dict[str, int]]:
        return [
            {"period": self.M[i], "start": self.B[i], "end": self.E[i], "years": self.D[i]}
            for i in range(self.n)
        ]

    @classmethod
    def from_raw(cls, raw: Any, diags: Diagnostics) -> "Periods | None":
        path = ("periods",)
        if not isinstance(raw, dict):
            diags.error(
                "PT010", "periods must be a mapping", path,
                hint="e.g. periods: {start: 2025, lengths: [5, 5, 10, 10]}  or  periods: {milestones: [2025, 2030, 2040, 2050]}",
            )
            return None
        unknown = set(raw) - {"start", "lengths", "milestones", "end"}
        if unknown:
            diags.error("PT011", f"unknown key(s) in periods: {sorted(unknown)}", path,
                        hint="allowed: start, lengths, milestones, end")
            return None
        if "lengths" in raw and "milestones" in raw:
            diags.error("PT012", "give either 'lengths' or 'milestones' in periods, not both", path)
            return None
        if "lengths" in raw:
            lengths, start = raw["lengths"], raw.get("start")
            if not isinstance(start, int):
                diags.error("PT013", "periods.start (first model year) is required with 'lengths'", path)
                return None
            if not (isinstance(lengths, list) and lengths and all(isinstance(x, int) and x > 0 for x in lengths)):
                diags.error("PT014", "periods.lengths must be a list of positive whole numbers of years", path + ("lengths",))
                return None
            B, E, M = [], [], []
            y = start
            for d in lengths:
                B.append(y)
                E.append(y + d - 1)
                M.append(y + (d - 1) // 2)
                y += d
            return cls(M, B, E, "std")
        if "milestones" in raw:
            ms = raw["milestones"]
            if not (isinstance(ms, list) and ms and all(isinstance(x, int) for x in ms)) or sorted(set(ms)) != ms:
                diags.error("PT015", "periods.milestones must be a strictly increasing list of years", path + ("milestones",))
                return None
            n = len(ms)
            E = [(ms[i] + ms[i + 1]) // 2 for i in range(n - 1)] + [0]
            B = [0] + [e + 1 for e in E[:-1]]
            B[0] = raw.get("start", ms[0])
            if not isinstance(B[0], int) or B[0] > ms[0] or ms[0] - B[0] > 10:
                diags.error("PT016", "periods.start must be a year at most 10 years before the first milestone", path)
                return None
            default_end = 2 * ms[-1] - B[-1]
            E[-1] = raw.get("end", default_end)
            if not isinstance(E[-1], int) or E[-1] < ms[-1] or E[-1] - ms[-1] > 30:
                diags.error("PT017", "periods.end must be a year from the last milestone up to 30 years after it", path)
                return None
            # TIMES (OBJ=AUTO) uses its standard cost accounting when every milestone is the
            # middle year of its period, and the modified one otherwise.
            D = [e - b + 1 for b, e in zip(B, E)]
            standard = all(ms[i] == B[i] + (D[i] - 1) // 2 for i in range(n))
            return cls(list(ms), B, E, "std" if standard else "mod")
        diags.error("PT010", "periods needs 'lengths' (with 'start') or 'milestones'", path)
        return None


class Discounting:
    """Year-by-year discount factors (beginning-of-year convention)."""

    def __init__(self, rate: float, base_year: int, periods: Periods):
        self.rate = float(rate)
        self.base_year = base_year
        self.periods = periods
        self.q = 1.0 / (1.0 + self.rate)

    def disc(self, year: float) -> float:
        return self.q ** (year - self.base_year)

    def pv_period(self, i: int) -> float:
        """Present value of one unit paid in each year of period i."""
        return sum(self.disc(y) for y in self.periods.years(i))

    def crf(self, rate: float, life: float) -> float:
        """Capital recovery factor for payments at the beginning of each year."""
        if rate <= 1e-12:
            return 1.0 / max(life, 1e-9)
        q = 1.0 / (1.0 + rate)
        return (1.0 - q) / (1.0 - q ** life)


# ---------------------------------------------------------------------------
# Timeslices
# ---------------------------------------------------------------------------
@dataclass
class Timeslices:
    """A tree of timeslices with ANNUAL at the root."""

    levels: list[str] = field(default_factory=lambda: ["annual"])
    names: list[str] = field(default_factory=lambda: [ANNUAL])
    level_of: dict[str, int] = field(default_factory=lambda: {ANNUAL: 0})
    parent: dict[str, str | None] = field(default_factory=lambda: {ANNUAL: None})
    children: dict[str, list[str]] = field(default_factory=lambda: {ANNUAL: []})
    frac: dict[str, float] = field(default_factory=lambda: {ANNUAL: 1.0})

    @property
    def depth(self) -> int:
        return len(self.levels) - 1

    def at_level(self, level: int) -> list[str]:
        return [s for s in self.names if self.level_of[s] == level]

    def level_index(self, name: str) -> int | None:
        name = str(name).lower()
        return self.levels.index(name) if name in self.levels else None

    def ancestors(self, s: str) -> list[str]:
        """s itself, then its parent, ... up to ANNUAL."""
        out = []
        cur: str | None = s
        while cur is not None:
            out.append(cur)
            cur = self.parent[cur]
        return out

    def ancestor_at(self, s: str, level: int) -> str | None:
        for a in self.ancestors(s):
            if self.level_of[a] == level:
                return a
        return None

    def below(self, g: str, level: int) -> list[str]:
        """All slices at `level` that lie inside slice g (g itself if same level)."""
        lg = self.level_of[g]
        if level < lg:
            return []
        out = [g]
        for _ in range(level - lg):
            out = [c for s in out for c in self.children[s]]
        return out

    def is_within(self, s: str, g: str) -> bool:
        return g in self.ancestors(s)

    @classmethod
    def from_raw(cls, raw: Any, level_names: Any, diags: Diagnostics) -> "Timeslices":
        ts = cls()
        if raw in (None, {}):
            return ts
        path = ("timeslices",)
        if not isinstance(raw, dict):
            diags.error("PT020", "timeslices must be a (nested) mapping of name -> year fraction", path)
            return ts

        def depth_of(node: Any) -> int:
            return 1 + max((depth_of(v) for v in node.values()), default=0) if isinstance(node, dict) else 0

        depth = depth_of(raw)
        if depth > 3:
            diags.error("PT021", "timeslices can be nested at most three levels deep (season, weekly, daynite)", path)
            return ts
        levels = LEVELS_BY_DEPTH[depth]
        if level_names is not None:
            ok = isinstance(level_names, list) and len(level_names) == depth and all(
                str(x).lower() in ("season", "weekly", "daynite") for x in level_names)
            if not ok:
                diags.error("PT022", f"timeslice_levels must list {depth} of: season, weekly, daynite", ("timeslice_levels",))
            else:
                levels = ["annual"] + [str(x).lower() for x in level_names]
        ts.levels = list(levels)

        def walk(node: dict, parent: str, prefix: str, level: int, p: tuple):
            for key, val in node.items():
                name = f"{prefix}{key}"
                if name in ts.level_of or name.upper() == ANNUAL:
                    diags.error("PT023", f"timeslice name '{name}' is not unique", p + (key,),
                                hint="child slices are named parent+child (e.g. W + D -> WD); pick names that stay unique.")
                    continue
                if not str(name).replace("_", "").isalnum():
                    diags.error("PT024", f"timeslice name '{name}' must be alphanumeric", p + (key,))
                ts.names.append(name)
                ts.level_of[name] = level
                ts.parent[name] = parent
                ts.children[name] = []
                ts.children[parent].append(name)
                if isinstance(val, dict):
                    if level == depth:
                        diags.error("PT025", "timeslice tree has uneven depth", p + (key,))
                        continue
                    walk(val, name, name, level + 1, p + (key,))
                    ts.frac[name] = sum(ts.frac.get(c, 0.0) for c in ts.children[name])
                else:
                    if level != depth:
                        diags.error("PT025", "timeslice tree has uneven depth: every branch must reach the same level",
                                    p + (key,))
                    if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
                        diags.error("PT026", f"year fraction of timeslice '{name}' must be a positive number", p + (key,))
                        ts.frac[name] = 0.0
                    else:
                        ts.frac[name] = float(val)

        walk(raw, ANNUAL, "", 1, path)
        total = sum(ts.frac[s] for s in ts.at_level(len(ts.levels) - 1))
        if abs(total - 1.0) > 1e-6:
            diags.error(
                "PT027", f"timeslice year fractions sum to {total:.6g}, not 1", path,
                hint="fractions are shares of the 8760 hours of a year and must add up to exactly 1.",
            )
        elif total != 1.0:  # absorb rounding
            for s in ts.names:
                ts.frac[s] /= total
        ts.frac[ANNUAL] = 1.0
        return ts


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
_ENERGY_J = {
    "J": 1.0, "KJ": 1e3, "MJ": 1e6, "GJ": 1e9, "TJ": 1e12, "PJ": 1e15, "EJ": 1e18,
    "WH": 3600.0, "KWH": 3.6e6, "MWH": 3.6e9, "GWH": 3.6e12, "TWH": 3.6e15,
    "TOE": 41.868e9, "KTOE": 41.868e12, "MTOE": 41.868e15,
}
_POWER_W = {"W": 1.0, "KW": 1e3, "MW": 1e6, "GW": 1e9, "TW": 1e12}
HOURS_PER_YEAR = 8760.0


def cap2act(cap_unit: str | None, act_unit: str | None) -> float | None:
    """Activity produced per year by one unit of capacity running flat out.

    GW -> PJ gives 31.536, MW -> GWh gives 8.76, 'PJ/yr' or 'PJa' -> PJ gives 1.
    Returns None when the pair is not understood (the user must then give
    cap2act explicitly).
    """
    if not cap_unit or not act_unit:
        return None
    cu, au = cap_unit.strip().upper(), act_unit.strip().upper()
    for suffix in ("/YR", "/Y", "/A", "A"):
        if cu.endswith(suffix) and cu[: -len(suffix)] and cu[: -len(suffix)] not in _POWER_W:
            base = cu[: -len(suffix)]
            if base == au:
                return 1.0
            if base in _ENERGY_J and au in _ENERGY_J:
                return _ENERGY_J[base] / _ENERGY_J[au]
            return None
    if cu in _POWER_W and au in _ENERGY_J:
        return _POWER_W[cu] * HOURS_PER_YEAR * 3600.0 / _ENERGY_J[au]
    if cu == au:
        return 1.0
    return None
