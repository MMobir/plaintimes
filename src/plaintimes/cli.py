"""Command line interface.

Every command prints a short human-readable report by default and a single
JSON object with `--json` (or when PLAINTIMES_JSON=1), so that an agent can
drive the whole modelling loop without parsing prose:

    check -> solve -> explain / diff -> edit YAML -> check ...

Exit codes: 0 ok · 1 model has errors · 2 no optimal solution ·
3 an expectation or oracle comparison failed · 4 usage or environment problem ·
5 internal error (a bug in plaintimes).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any

from . import __version__
from .api import load
from .diagnostics import Diagnostics

EXIT_OK, EXIT_MODEL, EXIT_SOLVE, EXIT_EXPECT, EXIT_USAGE, EXIT_INTERNAL = 0, 1, 2, 3, 4, 5


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------
def _emit(args, payload: dict[str, Any], text: str) -> None:
    if args.json:
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text.rstrip() + "\n")


def _table(rows: list[list[Any]], header: list[str]) -> str:
    cells = [header] + [[_fmt(c) for c in row] for row in rows]
    widths = [max(len(str(r[i])) for r in cells) for i in range(len(header))]
    out = []
    for n, row in enumerate(cells):
        out.append("  " + "  ".join(str(c).ljust(widths[i]) if i == 0 else str(c).rjust(widths[i]) for i, c in enumerate(row)))
        if n == 0:
            out.append("  " + "  ".join("-" * w for w in widths))
    return "\n".join(out)


def _fmt(x: Any) -> str:
    if isinstance(x, bool) or x is None:
        return "" if x is None else str(x)
    if isinstance(x, float):
        if x == 0:
            return "0"
        a = abs(x)
        if a >= 1000:
            return f"{x:,.0f}"
        if a >= 100:
            return f"{x:.1f}"
        return f"{x:.4g}"
    return str(x)


def _pivot(block: dict[str, dict[Any, float]], periods: list[int], label: str) -> str:
    rows = [[k] + [v.get(p, v.get(str(p), 0.0)) for p in periods] for k, v in block.items()]
    return _table(rows, [label] + [str(p) for p in periods])


def _diag_text(diags: Diagnostics, root: str | None = None) -> str:
    lines = []
    for d in diags.to_list():
        where = ""
        if d.get("file"):
            f = os.path.relpath(d["file"], root) if root else d["file"]
            where = f"{f}:{d.get('line', '')}: "
        lines.append(f"{where}{d['severity']} {d['code']}: {d['message']}" + (f"  [{d['path']}]" if d.get("path") else ""))
        if d.get("hint"):
            lines.append(f"    hint: {d['hint']}")
    return "\n".join(lines)


def _diag_payload(diags: Diagnostics, root: str | None) -> list[dict[str, Any]]:
    out = diags.to_list()
    if root:
        for d in out:
            if d.get("file"):
                d["file"] = os.path.relpath(d["file"], root)
    return out


def _load(args) -> tuple[Any, Diagnostics, str | None]:
    try:
        model, diags = load(args.model, getattr(args, "scenario", []) or [])
    except FileNotFoundError as exc:
        _emit(args, {"ok": False, "error": f"not found: {exc}"}, f"error: not found: {exc}")
        sys.exit(EXIT_USAGE)
    root = model.raw.root if model is not None else (os.path.dirname(os.path.abspath(args.model))
                                                     if os.path.isfile(args.model) else os.path.abspath(args.model))
    return model, diags, root


def _require_model(args):
    model, diags, root = _load(args)
    if model is None:
        payload = {"ok": False, "errors": len(diags.errors), "diagnostics": _diag_payload(diags, root)}
        _emit(args, payload, _diag_text(diags, root) + f"\n\n{len(diags.errors)} error(s); the model was not built.")
        sys.exit(EXIT_MODEL)
    return model, diags, root


def _run_dir(model, args) -> str:
    if getattr(args, "out", None):
        return args.out
    label = "+".join(model.raw.scenarios) if model.raw.scenarios else "base"
    return os.path.join(model.raw.root, "runs", label)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_check(args) -> int:
    model, diags, root = _load(args)
    payload: dict[str, Any] = {
        "ok": model is not None and not (args.strict and diags.warnings),
        "errors": len(diags.errors), "warnings": len(diags.warnings),
        "diagnostics": _diag_payload(diags, root),
    }
    text = _diag_text(diags, root)
    if model is not None:
        from .views import overview

        payload["model"] = overview(model)
        o = payload["model"]
        text += ("\n\n" if text else "") + (
            f"{o['name']}: {len(o['regions'])} region(s), {len(o['periods'])} periods "
            f"({o['periods'][0]['start']}-{o['periods'][-1]['end']}), "
            f"{sum(o['counts']['commodities'].values())} commodities, {o['counts']['processes']} processes, "
            f"{o['counts']['constraints']} constraints.\n"
            f"{len(diags.errors)} error(s), {len(diags.warnings)} warning(s).")
    else:
        text += f"\n\n{len(diags.errors)} error(s); the model was not built."
    _emit(args, payload, text)
    if model is None:
        return EXIT_MODEL
    return EXIT_MODEL if args.strict and diags.warnings else EXIT_OK


def cmd_info(args) -> int:
    model, diags, root = _require_model(args)
    from .views import overview

    o = overview(model)
    text = (f"{o['name']}  ({', '.join(o['scenarios']) or 'base'})\n"
            f"regions: {', '.join(o['regions'])}\n"
            f"discounting: {o['discount_rate']:.3g} to {o['discount_year']}"
            f"{' (mid-year)' if o['mid_year_discounting'] else ''}, currency {o['currency']}\n\n"
            + _table([[p["period"], p["start"], p["end"], p["years"]] for p in o["periods"]], ["period", "start", "end", "years"])
            + "\n\ntimeslices: " + ("; ".join(f"{k}: {', '.join(v)}" for k, v in o["timeslices"].items()) or "annual only")
            + f"\ncounts: {json.dumps(o['counts'])}\nfiles: {', '.join(o['files'])}")
    _emit(args, o, text)
    return EXIT_OK


def cmd_show(args) -> int:
    model, diags, root = _require_model(args)
    from .views import show

    try:
        data = show(model, args.name)
    except KeyError as exc:
        _emit(args, {"ok": False, "error": str(exc.args[0])}, f"error: {exc.args[0]}")
        return EXIT_USAGE
    _emit(args, data, json.dumps(data, indent=2, default=str))
    return EXIT_OK


def cmd_res(args) -> int:
    model, diags, root = _require_model(args)
    from .views import res

    try:
        data = res(model, args.region)
    except KeyError as exc:
        _emit(args, {"ok": False, "error": str(exc.args[0])}, f"error: {exc.args[0]}")
        return EXIT_USAGE
    if args.mermaid:
        sys.stdout.write(data["mermaid"] + "\n")
        return EXIT_OK
    lines = []
    for r, coms in data["regions"].items():
        lines.append(f"[{r}]")
        for c, node in coms.items():
            lines.append(f"  {c}: {', '.join(node['producers']) or '(no source)'}  ->  {', '.join(node['consumers']) or '(end use)'}")
    _emit(args, data, "\n".join(lines))
    return EXIT_OK


def _solve_text(run, out_dir: str | None) -> str:
    s = run.summary
    lines = [f"status: {s['status']}"]
    if s["status"] != "optimal":
        lines.append(s.get("hint", ""))
        return "\n".join(lines)
    periods = [p["period"] for p in s["periods"]]
    lines.append(f"objective: {s['objective']:,.6g} {s['currency']} (discounted to {s['discount_year']})")
    lines.append(f"size: {s['size']['variables']} variables, {s['size']['constraints']} constraints; "
                 f"solved in {s['solver']['seconds']} s with {s['solver']['name']} {s['solver']['version']}")
    for key, label in (("capacity", "capacity"), ("new_capacity", "new capacity"), ("emissions", "emissions"),
                       ("annual_cost_by_category", "annual cost"), ("prices", "avg price")):
        if s.get(key):
            lines += ["", _pivot(s[key], periods, label)]
    if s.get("binding_constraints"):
        lines += ["", "binding constraints (shadow price): "
                  + ", ".join(f"{b['name']} ({_fmt(b['shadow_price'])})" for b in s["binding_constraints"][:12])]
    if s.get("unserved"):
        lines += ["", "UNSERVED (model is infeasible without --relax):",
                  _table([[u["commodity"], u["region"], u["period"], u["timeslice"], u["unserved"]] for u in s["unserved"][:20]],
                         ["commodity", "region", "period", "timeslice", "amount"])]
    if out_dir:
        lines += ["", f"results written to {out_dir}"]
    return "\n".join(lines)


def cmd_solve(args) -> int:
    model, diags, root = _require_model(args)
    from .results import solve, write_run

    run = solve(model, relax=args.relax, time_limit=args.time_limit, log=args.log, method=args.method,
                write_lp=args.write_lp)
    out_dir = None
    if not args.no_write:
        out_dir = _run_dir(model, args)
        write_run(run, out_dir)
    payload = dict(run.summary)
    payload["ok"] = run.ok and not (args.relax and run.summary.get("unserved"))
    payload["warnings"] = [d for d in _diag_payload(diags, root) if d["severity"] == "warning"]
    if out_dir:
        payload["run_dir"] = out_dir
    _emit(args, payload, _solve_text(run, out_dir))
    return EXIT_OK if run.ok else EXIT_SOLVE


def cmd_rows(args) -> int:
    """Print generated equations whose name contains the pattern."""
    model, diags, root = _require_model(args)
    from .kernel import build_lp
    from .lp import flat_name

    built = build_lp(model)
    lp = built.lp
    pat = args.pattern.lower()
    hits = [i for i, name in enumerate(lp.row_names) if pat in flat_name(name).lower()]
    shown = hits[: args.limit]
    rows = []
    for i in shown:
        sense, rhs = lp.row_sense_rhs(i)
        rows.append({"name": flat_name(lp.row_names[i]), "sense": sense, "rhs": rhs,
                     "terms": [{"var": flat_name(n), "coef": c} for n, c in lp.row_terms(i)]})
    payload = {"matches": len(hits), "shown": len(shown), "size": built.stats(), "rows": rows}
    lines = []
    for r in rows:
        terms = " ".join(f"{t['coef']:+.6g} {t['var']}" for t in r["terms"][:60])
        more = f" ... (+{len(r['terms']) - 60} terms)" if len(r["terms"]) > 60 else ""
        lines.append(f"{r['name']}:\n    {terms}{more}  {r['sense']} {r['rhs']:.6g}")
    lines.append(f"\n{len(hits)} equation(s) match '{args.pattern}'" + (f", first {len(shown)} shown" if len(hits) > len(shown) else "")
                 + f"  (model: {lp.n_vars} variables, {lp.n_rows} constraints)")
    _emit(args, payload, "\n".join(lines))
    return EXIT_OK


def cmd_explain(args) -> int:
    model, diags, root = _require_model(args)
    from . import explain as ex
    from .results import solve

    try:
        if args.what == "infeasible":
            data = ex.explain_infeasible(solve(model, relax=True))
            text = data["reading"]
            if data["unserved"]:
                text += "\n\n" + _table([[u["commodity"], u["region"], u["period"], u["timeslice"], u["unserved"]]
                                          for u in data["unserved"][:30]], ["commodity", "region", "period", "timeslice", "amount"])
            _emit(args, data, text)
            return EXIT_OK
        if not args.name:
            _emit(args, {"ok": False, "error": f"explain {args.what} needs a name"}, f"error: explain {args.what} needs a name")
            return EXIT_USAGE
        run = solve(model)
        if not run.ok:
            _emit(args, run.summary, f"status: {run.sol.status}\n{run.summary.get('hint', '')}")
            return EXIT_SOLVE
        if args.what == "process":
            data = ex.explain_process(run, args.name, args.region, args.period)
            lines = [f"{data['process']} in {data['region']}  (money per unit, {data['currency']})"]
            for op in data["operation"]:
                lines.append(f"\noperation {op['period']}: activity {_fmt(op['activity'])} {op['unit']}")
                if "breakdown" in op:
                    lines.append(_table([[b["item"], b["detail"], b["per_unit_activity"]] for b in op["breakdown"]],
                                        ["item", "detail", "per unit activity"]))
                else:
                    lines.append("  " + op["reading"])
            for inv in data["investment"]:
                lines.append(f"\ninvestment {inv['vintage']}: new capacity {_fmt(inv['new_capacity'])} {inv['unit']}")
                lines.append("  " + inv["reading"])
        else:
            data = ex.explain_price(run, args.name, args.region, args.period)
            lines = [f"price of {data['commodity']} in {data['region']}  ({data['price_unit']})"]
            for per in data["periods"]:
                for sl in per["timeslices"]:
                    lines.append(f"\n{per['period']} {sl['timeslice']}: price {_fmt(sl['price'])}")
                    if sl["suppliers"]:
                        lines.append(_table([[s["process"], s["flow"], s["role"], s["capacity_rent_per_unit_activity"]]
                                             for s in sl["suppliers"]], ["supplier", "flow", "role", "capacity rent"]))
    except KeyError as exc:
        _emit(args, {"ok": False, "error": str(exc.args[0])}, f"error: {exc.args[0]}")
        return EXIT_USAGE
    _emit(args, data, "\n".join(lines))
    return EXIT_OK


def cmd_diff(args) -> int:
    from .diff import diff_runs

    try:
        data = diff_runs(args.run_a, args.run_b, top=args.top)
    except FileNotFoundError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, f"error: {exc}")
        return EXIT_USAGE
    lines = [f"A: {args.run_a}  objective {_fmt(data['a']['objective'])}",
             f"B: {args.run_b}  objective {_fmt(data['b']['objective'])}"]
    if "objective_change" in data:
        lines.append(f"objective change B - A: {data['objective_change']:+,.6g} ({data['objective_change_pct']:+.3g}%)")
    for name, tab in data["tables"].items():
        if not tab["largest"]:
            continue
        keys = [k for k in tab["largest"][0] if k not in ("a", "b", "change")]
        lines += ["", f"{name} ({tab['field']}): {tab['changed_rows']} rows changed",
                  _table([[*(row[k] for k in keys), row["a"], row["b"], row["change"]] for row in tab["largest"]],
                         keys + ["A", "B", "change"])]
    _emit(args, data, "\n".join(lines))
    return EXIT_OK


def cmd_test(args) -> int:
    from .expect import run_expectations

    try:
        data = run_expectations(args.model)
    except FileNotFoundError as exc:
        _emit(args, {"ok": False, "error": f"not found: {exc}"}, f"error: not found: {exc}")
        return EXIT_USAGE
    if "error" in data:
        _emit(args, data, "error: " + data["error"])
        return EXIT_MODEL
    lines = []
    for r in data["results"]:
        mark = "PASS" if r["ok"] else "FAIL"
        scen = f" [{'+'.join(r['scenarios'])}]" if r.get("scenarios") else ""
        lines.append(f"{mark}  {r['name']}{scen}" + (f"  -> {r['message']}" if r.get("message") else ""))
    lines.append(f"\n{data['passed']} passed, {data['failed']} failed" if data["results"] else "no expectations defined (add an 'expect:' list)")
    _emit(args, data, "\n".join(lines))
    return EXIT_OK if data["ok"] else EXIT_EXPECT


def cmd_export_dd(args) -> int:
    model, diags, root = _require_model(args)
    from .ddexport import export_dd

    out = args.out or os.path.join(model.raw.root, "export", "+".join(model.raw.scenarios) or "base")
    try:
        info = export_dd(model, out, solver=args.solver, allow_partial=args.partial)
    except NotImplementedError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, f"error: {exc}\n(use --partial to export everything else)")
        return EXIT_USAGE
    info["ok"] = True
    text = (f"wrote {info['ts']}\n      {info['dd']}\n      {info['run']}\n"
            f"run with:  gams {info['name']}.run idir1=. idir2=<TIMES_model dir> ps=0 gdx={info['name']} filecase=4")
    if info["not_exported"]:
        text += "\nNOT exported:\n  " + "\n  ".join(info["not_exported"])
    _emit(args, info, text)
    return EXIT_OK


def cmd_oracle(args) -> int:
    model, diags, root = _require_model(args)
    from .oracle import OracleUnavailable, compare, run_times
    from .results import solve

    run = solve(model)
    work = args.out or os.path.join(model.raw.root, "export", "+".join(model.raw.scenarios) or "base")
    try:
        times = run_times(model, work, gams=args.gams, times_src=args.times_src, solver=args.solver)
    except OracleUnavailable as exc:
        _emit(args, {"ok": False, "error": str(exc)}, f"error: {exc}")
        return EXIT_USAGE
    except NotImplementedError as exc:
        _emit(args, {"ok": False, "error": str(exc)}, f"error: {exc}")
        return EXIT_USAGE
    rep = compare(run, times)
    rep["ok"] = bool(rep.get("agree"))
    rep["work_dir"] = work
    if rep.get("objective_times") is not None:
        text = (f"native kernel : {rep['objective_native']:.10g}\n"
                f"official TIMES: {rep['objective_times']:.10g}\n"
                f"relative difference: {rep['objective_rel_diff']:.2e}  ->  {'AGREE' if rep['agree'] else 'DIFFER'}\n"
                f"largest relative differences: new capacity {rep['new_capacity']['max_rel_diff']:.1e}, "
                f"capacity {rep['capacity']['max_rel_diff']:.1e}, activity {rep['activity']['max_rel_diff']:.1e}")
        if rep.get("note"):
            text += "\nnote: " + rep["note"]
    else:
        text = f"native: {rep['native_status']}; official TIMES: {rep['times_status']}\n{times.get('message', '')}"
    _emit(args, rep, text)
    return EXIT_OK if rep["ok"] else EXIT_EXPECT


def cmd_schema(args) -> int:
    from . import schema
    from .ddexport import ATTRIBUTE_MAP

    if args.times_map:
        rows = [[a, b, c] for a, b, c in ATTRIBUTE_MAP]
        if args.json:
            json.dump([{"model": a, "times": b, "note": c} for a, b, c in ATTRIBUTE_MAP], sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(_table(rows, ["model key", "TIMES attribute", "note"]) + "\n")
        return EXIT_OK
    if args.json:
        json.dump(schema.as_json(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(schema.as_markdown() + "\n")
    return EXIT_OK


def cmd_init(args) -> int:
    src = os.path.join(os.path.dirname(__file__), "templates", args.template)
    if not os.path.isdir(src):
        avail = sorted(os.listdir(os.path.join(os.path.dirname(__file__), "templates")))
        _emit(args, {"ok": False, "error": f"unknown template '{args.template}'", "available": avail},
              f"error: unknown template '{args.template}'; available: {', '.join(avail)}")
        return EXIT_USAGE
    if os.path.exists(args.dir) and os.listdir(args.dir):
        _emit(args, {"ok": False, "error": f"{args.dir} exists and is not empty"}, f"error: {args.dir} exists and is not empty")
        return EXIT_USAGE
    shutil.copytree(src, args.dir, dirs_exist_ok=True)
    files = sorted(os.path.relpath(os.path.join(dp, f), args.dir) for dp, _, fs in os.walk(args.dir) for f in fs)
    _emit(args, {"ok": True, "dir": args.dir, "files": files},
          f"created {args.dir} from template '{args.template}':\n  " + "\n  ".join(files)
          + f"\n\nnext:  plaintimes check {args.dir}  &&  plaintimes solve {args.dir}")
    return EXIT_OK


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=os.environ.get("PLAINTIMES_JSON") == "1",
                        help="print one JSON object instead of text")
    scen = argparse.ArgumentParser(add_help=False)
    scen.add_argument("model", help="model directory (containing model.yaml) or the model.yaml file")
    scen.add_argument("-s", "--scenario", action="append", default=[], metavar="NAME",
                      help="scenario overlay from <model>/scenarios/NAME.yaml (repeatable, applied in order)")

    p = argparse.ArgumentParser(prog="plaintimes", description="Plain-text TIMES-style energy system models.")
    p.add_argument("--version", action="version", version=f"plaintimes {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("init", parents=[common], help="create a new model from a template")
    a.add_argument("dir")
    a.add_argument("--template", default="demo")
    a.set_defaults(fn=cmd_init)

    a = sub.add_parser("check", parents=[common, scen], help="load the model and report every problem found")
    a.add_argument("--strict", action="store_true", help="treat warnings as failures")
    a.set_defaults(fn=cmd_check)

    a = sub.add_parser("info", parents=[common, scen], help="periods, timeslices and counts")
    a.set_defaults(fn=cmd_info)

    a = sub.add_parser("show", parents=[common, scen], help="everything about one commodity, process or constraint, resolved")
    a.add_argument("name")
    a.set_defaults(fn=cmd_show)

    a = sub.add_parser("res", parents=[common, scen], help="the reference energy system: who produces and consumes what")
    a.add_argument("--region")
    a.add_argument("--mermaid", action="store_true", help="print a Mermaid flowchart")
    a.set_defaults(fn=cmd_res)

    a = sub.add_parser("solve", parents=[common, scen], help="solve with the native kernel and write result tables")
    a.add_argument("-o", "--out", help="run directory (default <model>/runs/<scenarios or base>)")
    a.add_argument("--relax", action="store_true", help="allow unserved commodities at a high penalty, to locate infeasibilities")
    a.add_argument("--no-write", action="store_true", help="do not write result files")
    a.add_argument("--time-limit", type=float)
    a.add_argument("--log", action="store_true", help="show the solver log")
    a.add_argument("--method", default="auto", choices=["auto", "simplex", "ipm", "pdlp"],
                   help="LP algorithm (auto: simplex for small models, interior point for large ones)")
    a.add_argument("--write-lp", metavar="FILE", help="also write the LP as FILE.mps or FILE.lp for another solver")
    a.set_defaults(fn=cmd_solve)

    a = sub.add_parser("rows", parents=[common, scen], help="print generated equations whose name contains a pattern")
    a.add_argument("pattern", help="e.g. BAL.NORTH.ELC.2030, CAPACT.NORTH.WIND, UC.RENEWABLE_SHARE, PEAK")
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(fn=cmd_rows)

    a = sub.add_parser("explain", parents=[common, scen], help="why: break-even accounts from the LP duals")
    a.add_argument("what", choices=["process", "price", "infeasible"])
    a.add_argument("name", nargs="?")
    a.add_argument("--region")
    a.add_argument("--period", type=int)
    a.set_defaults(fn=cmd_explain)

    a = sub.add_parser("diff", parents=[common], help="compare two run directories")
    a.add_argument("run_a")
    a.add_argument("run_b")
    a.add_argument("--top", type=int, default=15)
    a.set_defaults(fn=cmd_diff)

    a = sub.add_parser("test", parents=[common], help="run the model's 'expect:' checks")
    a.add_argument("model")
    a.set_defaults(fn=cmd_test)

    a = sub.add_parser("export-dd", parents=[common, scen], help="write .dd and .run files for the official TIMES code")
    a.add_argument("-o", "--out")
    a.add_argument("--solver", help="LP solver to request in the run file (e.g. cplex, cbc, highs)")
    a.add_argument("--partial", action="store_true", help="export even if some features cannot be exported")
    a.set_defaults(fn=cmd_export_dd)

    a = sub.add_parser("oracle", parents=[common, scen], help="solve with official TIMES (needs GAMS) and compare")
    a.add_argument("--gams", help="path to the gams executable (or PLAINTIMES_GAMS)")
    a.add_argument("--times-src", help="directory of the TIMES source (or PLAINTIMES_TIMES_SRC)")
    a.add_argument("--solver")
    a.add_argument("-o", "--out")
    a.set_defaults(fn=cmd_oracle)

    a = sub.add_parser("schema", parents=[common], help="the model format (Markdown, or JSON with --json)")
    a.add_argument("--times-map", action="store_true", help="model key -> TIMES attribute table")
    a.add_argument("--markdown", action="store_true", help="(default)")
    a.set_defaults(fn=cmd_schema)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK
    except Exception as exc:  # a bug in plaintimes, not in the model: say so in the same shape as everything else
        import traceback

        tb = traceback.format_exc()
        _emit(args, {"ok": False, "error": f"internal error: {type(exc).__name__}: {exc}", "traceback": tb},
              f"internal error: {type(exc).__name__}: {exc}\n{tb}\nThis is a bug in plaintimes, not in your model.")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
