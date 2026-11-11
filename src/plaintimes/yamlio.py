"""YAML loading with line tracking, includes and scenario overlays.

A model is a directory of small YAML files.  `model.yaml` holds the settings
and an `include:` list of globs; included files contribute named entries to
the mergeable sections (commodities, processes, ...).  Scenarios are partial
models that are deep-merged over the base.

Every value remembers the file and line it came from, so diagnostics can say
`processes/power.yaml:42` instead of "somewhere in the model".
"""
from __future__ import annotations

import glob
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .diagnostics import Diagnostics

# Sections whose entries are keyed by name and may be spread over many files.
MERGE_SECTIONS = ("commodities", "processes", "storage", "trade", "constraints", "emission_factors")
LIST_SECTIONS = ("expect",)
SETTINGS = (
    "plaintimes", "name", "description", "currency", "discount_rate", "discount_year",
    "mid_year_discounting", "periods", "timeslices", "timeslice_levels", "regions",
    "units", "include", "references",
)
TOP_LEVEL = SETTINGS + MERGE_SECTIONS + LIST_SECTIONS


class _Loader(yaml.SafeLoader):
    pass


def _construct(loader: _Loader, node: yaml.Node, path: tuple, file: str, locs: dict, diags: Diagnostics):
    locs[path] = (file, node.start_mark.line + 1)
    if isinstance(node, yaml.MappingNode):
        loader.flatten_mapping(node)
        out: dict[Any, Any] = {}
        for knode, vnode in node.value:
            key = loader.construct_object(knode, deep=True)
            if isinstance(key, (list, dict)):
                diags.locs.setdefault(path, (file, knode.start_mark.line + 1))
                diags.error("PT001", "mapping keys must be scalars", path)
                continue
            if key in out:
                locs[path + (key,)] = (file, knode.start_mark.line + 1)
                diags.locs.update({path + (key,): (file, knode.start_mark.line + 1)})
                diags.error(
                    "PT002", f"duplicate key '{key}'", path + (key,),
                    hint="YAML silently keeps only the last one; remove or rename one of the entries.",
                )
            out[key] = _construct(loader, vnode, path + (key,), file, locs, diags)
            locs[path + (key,)] = (file, knode.start_mark.line + 1)
        return out
    if isinstance(node, yaml.SequenceNode):
        return [_construct(loader, v, path + (i,), file, locs, diags) for i, v in enumerate(node.value)]
    return loader.construct_object(node, deep=True)


def load_yaml(path: str, diags: Diagnostics, prefix: tuple = ()) -> tuple[Any, dict]:
    """Load one YAML file, returning (data, locations)."""
    locs: dict[tuple, tuple[str, int]] = {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    loader = _Loader(text)
    try:
        node = loader.get_single_node()
    except yaml.YAMLError as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        diags.locs[("__yaml__", path)] = (path, (line or 0) + 1)
        diags.error("PT000", f"YAML syntax error: {getattr(exc, 'problem', exc)}", ("__yaml__", path))
        return None, locs
    finally:
        loader.dispose()
    if node is None:
        return {}, locs
    loader = _Loader(text)
    data = _construct(loader, node, prefix, path, locs, diags)
    return data, locs


def _is_year_series(value: Any) -> bool:
    return isinstance(value, dict) and len(value) > 0 and all(isinstance(k, int) and not isinstance(k, bool) for k in value)


def deep_merge(base: Any, over: Any, path: tuple, base_locs: dict, over_locs: dict) -> Any:
    """Merge `over` into `base`.

    Mappings merge key by key.  Scalars, lists and year series ({2030: x, ...})
    are replaced wholesale, so a scenario never ends up with a half-old,
    half-new time series.  A value of null deletes the key.
    """
    if isinstance(base, dict) and isinstance(over, dict) and not _is_year_series(over) and not _is_year_series(base):
        out = dict(base)
        for key, val in over.items():
            sub = path + (key,)
            if val is None:
                out.pop(key, None)
                for k in [k for k in base_locs if k[: len(sub)] == sub]:
                    del base_locs[k]
                continue
            if key in out:
                out[key] = deep_merge(out[key], val, sub, base_locs, over_locs)
            else:
                out[key] = val
                for k, v in over_locs.items():
                    if k[: len(sub)] == sub:
                        base_locs[k] = v
            if sub in over_locs:
                base_locs[sub] = over_locs[sub]
        return out
    # replace: drop stale locations below this path, adopt the overlay's
    for k in [k for k in base_locs if k[: len(path)] == path and k != path]:
        del base_locs[k]
    for k, v in over_locs.items():
        if k[: len(path)] == path:
            base_locs[k] = v
    return over


@dataclass
class RawModel:
    data: dict[str, Any]
    locs: dict[tuple, tuple[str, int]]
    root: str                      # directory containing model.yaml
    model_file: str
    files: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)

    def file_hashes(self) -> dict[str, str]:
        out = {}
        for f in self.files:
            with open(f, "rb") as fh:
                out[os.path.relpath(f, self.root)] = hashlib.sha256(fh.read()).hexdigest()[:16]
        return out

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for name, digest in sorted(self.file_hashes().items()):
            h.update(f"{name}:{digest};".encode())
        return h.hexdigest()[:16]


def resolve_model_file(path: str) -> str:
    if os.path.isdir(path):
        for name in ("model.yaml", "model.yml"):
            cand = os.path.join(path, name)
            if os.path.exists(cand):
                return cand
        raise FileNotFoundError(f"no model.yaml in {path}")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return path


def resolve_scenario_file(root: str, name: str) -> str:
    cands = [name, os.path.join(root, name), os.path.join(root, "scenarios", name)]
    cands += [c + ext for c in list(cands) for ext in (".yaml", ".yml")]
    for cand in cands:
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        f"scenario '{name}' not found (looked for {os.path.join(root, 'scenarios', name)}.yaml)"
    )


def load_raw(path: str, scenarios: list[str] | tuple[str, ...] = (), diags: Diagnostics | None = None) -> tuple[RawModel, Diagnostics]:
    """Load model.yaml, its includes and any scenario overlays."""
    diags = diags or Diagnostics()
    model_file = os.path.abspath(resolve_model_file(path))
    root = os.path.dirname(model_file)
    data, locs = load_yaml(model_file, diags)
    files = [model_file]
    if not isinstance(data, dict):
        if data is not None:
            diags.locs[()] = (model_file, 1)
            diags.error("PT003", "model.yaml must contain a mapping at the top level")
        return RawModel({}, locs, root, model_file, files), diags
    locs[()] = (model_file, 1)

    # -- includes ---------------------------------------------------------
    includes = data.get("include") or []
    if isinstance(includes, str):
        includes = [includes]
    for i, pattern in enumerate(includes):
        matches = sorted(glob.glob(os.path.join(root, str(pattern)), recursive=True))
        if not matches:
            diags.locs.update(locs)
            diags.warn("PT004", f"include pattern '{pattern}' matched no files", ("include", i))
        for inc in matches:
            inc = os.path.abspath(inc)
            if inc in files or os.path.isdir(inc):
                continue
            files.append(inc)
            sub, sublocs = load_yaml(inc, diags)
            if sub is None:
                continue
            if not isinstance(sub, dict):
                diags.locs[("__file__", inc)] = (inc, 1)
                diags.error("PT003", "included file must contain a mapping at the top level", ("__file__", inc))
                continue
            for section, entries in sub.items():
                if section in LIST_SECTIONS:
                    base_list = data.setdefault(section, [])
                    offset = len(base_list)
                    for j, item in enumerate(entries or []):
                        base_list.append(item)
                        for k, v in sublocs.items():
                            if k[:2] == (section, j):
                                locs[(section, offset + j) + k[2:]] = v
                    continue
                if section not in MERGE_SECTIONS:
                    diags.locs[(section,)] = sublocs.get((section,), (inc, 1))
                    diags.error(
                        "PT005", f"'{section}' is not allowed in an included file",
                        (section,),
                        hint=f"included files may contain: {', '.join(MERGE_SECTIONS + LIST_SECTIONS)}. Settings belong in model.yaml.",
                    )
                    continue
                if entries is None:
                    continue
                if not isinstance(entries, dict):
                    diags.locs[(section,)] = sublocs.get((section,), (inc, 1))
                    diags.error("PT006", f"'{section}' must be a mapping of name -> definition", (section,))
                    continue
                target = data.setdefault(section, {})
                if target is None:
                    target = data[section] = {}
                for name, entry in entries.items():
                    if name in target:
                        diags.locs[(section, name)] = sublocs.get((section, name), (inc, 1))
                        prev = locs.get((section, name), ("?", 0))
                        diags.error(
                            "PT007", f"{section[:-1] if section.endswith('s') else section} '{name}' is defined twice",
                            (section, name),
                            hint=f"first defined at {os.path.relpath(prev[0], root)}:{prev[1]}. To change it in a scenario, put the change in a scenario file instead.",
                        )
                        continue
                    target[name] = entry
                    for k, v in sublocs.items():
                        if k[:2] == (section, name):
                            locs[k] = v
                locs.setdefault((section,), sublocs.get((section,), (inc, 1)))

    raw = RawModel(data, locs, root, model_file, files)

    # -- scenario overlays ------------------------------------------------
    for name in scenarios:
        try:
            sfile = os.path.abspath(resolve_scenario_file(root, name))
        except FileNotFoundError as exc:
            avail = sorted(
                os.path.splitext(os.path.basename(f))[0]
                for f in glob.glob(os.path.join(root, "scenarios", "*.y*ml"))
            )
            diags.error("PT008", str(exc), (), hint=f"available scenarios: {', '.join(avail) or '(none)'}")
            continue
        over, olocs = load_yaml(sfile, diags)
        files.append(sfile)
        raw.scenarios.append(os.path.splitext(os.path.basename(sfile))[0])
        if over is None:
            continue
        if not isinstance(over, dict):
            diags.locs[("__file__", sfile)] = (sfile, 1)
            diags.error("PT003", "scenario file must contain a mapping at the top level", ("__file__", sfile))
            continue
        over.pop("description", None)
        over.pop("name", None)
        raw.data = deep_merge(raw.data, over, (), raw.locs, olocs)

    diags.locs = raw.locs
    return raw, diags
