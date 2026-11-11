"""Programmatic entry points (the CLI is a thin wrapper around these)."""
from __future__ import annotations

from .diagnostics import Diagnostics, ModelError
from .model import Model, build_model
from .yamlio import load_raw


def load(path: str, scenarios: list[str] | tuple[str, ...] = (), lint: bool = True) -> tuple[Model | None, Diagnostics]:
    """Load, build and statically check a model. Never raises on model errors."""
    raw, diags = load_raw(path, scenarios)
    model = None
    if raw.data:
        model = build_model(raw, diags)
    if model is not None and not diags.has_errors and lint:
        from .lint import lint_model

        lint_model(model, diags)
    if diags.has_errors:
        model = None
    return model, diags


def load_or_raise(path: str, scenarios: list[str] | tuple[str, ...] = ()) -> Model:
    model, diags = load(path, scenarios)
    if model is None:
        raise ModelError(diags)
    return model
