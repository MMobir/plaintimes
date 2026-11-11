"""Parameter values: scalars, year series, and optional region / secondary keys.

A parameter in a model file may be written as

    invcost: 1200                         # same everywhere, every year
    invcost: {2025: 1200, 2050: 900}      # a year series
    invcost: {NORTH: 1200, SOUTH: {2025: 1300, 2050: 950}}   # by region
    availability: {W: 0.4, S: 0.7}        # by a secondary key (here timeslice)
    availability: {NORTH: {W: {2025: 0.4}}}                   # all three

Nesting order is always region -> secondary key -> year.  Which keys are
which is decided by what they are: whole numbers are years, names of regions
are regions, names from the secondary set (timeslices or commodities) are
secondary keys.

Two interpolation rules, chosen by the kind of parameter:

* ``data``  (costs, efficiencies, demands, ...): linear between the given
  years, held flat before the first and after the last.
* ``bound`` (bounds, caps, right-hand sides): linear between the given years,
  **not applied** outside them.  A bare number applies in every period.
"""
from __future__ import annotations

from typing import Any, Iterable

from .diagnostics import Diagnostics


def is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class Series:
    __slots__ = ("points", "kind", "scalar")

    def __init__(self, raw: Any, kind: str = "data"):
        self.kind = kind
        if is_number(raw):
            self.scalar = True
            self.points = [(0, float(raw))]
        else:
            self.scalar = False
            self.points = sorted((int(y), float(v)) for y, v in raw.items())

    def at(self, year: float) -> float | None:
        pts = self.points
        if self.scalar:
            return pts[0][1]
        if year <= pts[0][0]:
            if year == pts[0][0] or self.kind == "data":
                return pts[0][1]
            return None
        if year >= pts[-1][0]:
            if year == pts[-1][0] or self.kind == "data":
                return pts[-1][1]
            return None
        for (y0, v0), (y1, v1) in zip(pts, pts[1:]):
            if y0 <= year <= y1:
                return v0 + (v1 - v0) * (year - y0) / (y1 - y0)
        return None  # pragma: no cover

    def raw_points(self) -> list[tuple[int, float]]:
        return list(self.points)

    def __repr__(self) -> str:
        return f"Series({self.points[0][1] if self.scalar else dict(self.points)})"


class Param:
    """A resolved parameter: lookup by (region, secondary key) gives a Series."""

    def __init__(self, kind: str = "data"):
        self.kind = kind
        self.table: dict[tuple[str | None, str | None], Series] = {}

    def __bool__(self) -> bool:
        return bool(self.table)

    def series(self, region: str | None = None, second: str | None = None) -> Series | None:
        for key in ((region, second), (None, second)):
            if key in self.table:
                return self.table[key]
        return None

    def at(self, region: str | None, year: float, second: str | None = None, default: float | None = None) -> float | None:
        s = self.series(region, second)
        if s is None:
            return default
        v = s.at(year)
        return default if v is None else v

    def has(self, region: str | None = None, second: str | None = None) -> bool:
        return self.series(region, second) is not None

    def seconds(self) -> list[str]:
        return sorted({k[1] for k in self.table if k[1] is not None})

    def items(self) -> Iterable[tuple[tuple[str | None, str | None], Series]]:
        return self.table.items()

    @classmethod
    def constant(cls, value: float, kind: str = "data") -> "Param":
        p = cls(kind)
        p.table[(None, None)] = Series(value, kind)
        return p


def parse_param(
    raw: Any,
    *,
    kind: str,
    regions: list[str],
    second: Iterable[str] | None,
    path: tuple,
    diags: Diagnostics,
    second_label: str = "key",
    lo: float | None = None,
    hi: float | None = None,
) -> Param:
    """Parse a raw YAML value into a Param, reporting problems to diags."""
    out = Param(kind)
    second_set = set(second) if second is not None else None
    region_set = set(regions)

    def check_value(v: float, p: tuple) -> bool:
        if v != v or v in (float("inf"), float("-inf")):
            diags.error("PT030", "value must be a finite number", p)
            return False
        if lo is not None and v < lo:
            diags.error("PT031", f"value {v:g} is below the allowed minimum {lo:g}", p)
            return False
        if hi is not None and v > hi:
            diags.error("PT031", f"value {v:g} is above the allowed maximum {hi:g}", p)
            return False
        return True

    def parse_series(node: Any, p: tuple) -> Series | None:
        if is_number(node):
            return Series(node, kind) if check_value(float(node), p) else None
        if isinstance(node, dict) and node and all(isinstance(k, int) and not isinstance(k, bool) for k in node):
            ok = True
            for y, v in node.items():
                if not is_number(v):
                    diags.error("PT032", f"value for year {y} must be a number, got {v!r}", p + (y,))
                    ok = False
                elif not check_value(float(v), p + (y,)):
                    ok = False
                if not (1900 <= y <= 2200):
                    diags.error("PT033", f"{y} does not look like a year", p + (y,))
                    ok = False
            return Series(node, kind) if ok else None
        diags.error(
            "PT034", f"expected a number or a year series like {{2030: 1.0, 2050: 0.8}}, got {type(node).__name__}", p,
        )
        return None

    def classify(node: dict) -> str:
        keys = list(node)
        if all(isinstance(k, int) and not isinstance(k, bool) for k in keys):
            return "year"
        if all(isinstance(k, str) and k in region_set for k in keys):
            return "region"
        if second_set is not None and all(str(k) in second_set for k in keys):
            return "second"
        return "mixed"

    def parse_second(node: Any, region: str | None, p: tuple):
        if isinstance(node, dict) and node and second_set is not None and classify(node) == "second":
            for k, v in node.items():
                s = parse_series(v, p + (k,))
                if s is not None:
                    out.table[(region, str(k))] = s
            return
        if isinstance(node, dict) and node and classify(node) == "mixed":
            report_mixed(node, p)
            return
        s = parse_series(node, p)
        if s is not None:
            out.table[(region, None)] = s

    def report_mixed(node: dict, p: tuple):
        bad = [k for k in node if not (isinstance(k, int) and not isinstance(k, bool))
               and k not in region_set and not (second_set is not None and str(k) in second_set)]
        what = "years, region names" + (f" or {second_label} names" if second_set is not None else "")
        if bad:
            diags.error("PT035", f"unrecognised key(s) {bad}: expected {what}", p,
                        hint=_suggest(bad[0], list(region_set) + sorted(second_set or [])))
        else:
            diags.error("PT036", f"keys mix different kinds; nest them as region -> {second_label} -> year", p)

    if raw is None:
        return out
    if isinstance(raw, dict) and raw:
        cls = classify(raw)
        if cls == "region":
            for r, v in raw.items():
                parse_second(v, r, path + (r,))
            return out
        if cls == "mixed":
            report_mixed(raw, path)
            return out
    if isinstance(raw, dict) and not raw:
        diags.error("PT034", "empty mapping; expected a number or a year series", path)
        return out
    parse_second(raw, None, path)
    return out


def _suggest(word: Any, options: list[str]) -> str | None:
    import difflib

    m = difflib.get_close_matches(str(word), [str(o) for o in options], n=1, cutoff=0.6)
    return f"did you mean '{m[0]}'?" if m else None


suggest = _suggest
