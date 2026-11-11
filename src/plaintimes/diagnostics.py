"""Structured diagnostics.

Every problem the tool finds is reported as a Diagnostic with a stable code,
a severity, the dotted path of the offending item, the file and line it came
from, and (where possible) a concrete hint for fixing it.  The same objects are
rendered as text for people and as JSON for agents.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

SEVERITIES = ("error", "warning", "info")


@dataclass
class Diagnostic:
    code: str
    severity: str
    message: str
    path: str = ""
    file: str | None = None
    line: int | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    def render(self) -> str:
        where = ""
        if self.file:
            where = f"{self.file}:{self.line}: " if self.line else f"{self.file}: "
        text = f"{where}{self.severity} {self.code}: {self.message}"
        if self.path:
            text += f"  [{self.path}]"
        if self.hint:
            text += f"\n    hint: {self.hint}"
        return text


class Diagnostics:
    """Collects diagnostics; knows how to look up file/line for a path."""

    def __init__(self, locs: dict[tuple, tuple[str, int]] | None = None):
        self.items: list[Diagnostic] = []
        self.locs = locs or {}

    # -- location lookup -------------------------------------------------
    def where(self, path: Iterable[Any]) -> tuple[str | None, int | None]:
        path = tuple(path)
        while path:
            if path in self.locs:
                return self.locs[path]
            path = path[:-1]
        return self.locs.get((), (None, None))

    def add(self, severity: str, code: str, message: str, path: Iterable[Any] = (), hint: str | None = None):
        path = tuple(path)
        file, line = self.where(path)
        self.items.append(
            Diagnostic(code, severity, message, ".".join(str(p) for p in path), file, line, hint)
        )

    def error(self, code, message, path=(), hint=None):
        self.add("error", code, message, path, hint)

    def warn(self, code, message, path=(), hint=None):
        self.add("warning", code, message, path, hint)

    def info(self, code, message, path=(), hint=None):
        self.add("info", code, message, path, hint)

    # -- summaries --------------------------------------------------------
    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "warning"]

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.items)

    def to_list(self) -> list[dict[str, Any]]:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        return [d.to_dict() for d in sorted(self.items, key=lambda d: order[d.severity])]

    def render(self) -> str:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        return "\n".join(d.render() for d in sorted(self.items, key=lambda d: order[d.severity]))


class ModelError(Exception):
    """Raised when a model cannot be built; carries the diagnostics."""

    def __init__(self, diags: Diagnostics):
        self.diags = diags
        n = len(diags.errors)
        super().__init__(f"{n} error{'s' if n != 1 else ''} in model")
