"""The model format, as data.

One place that says what every key means, what values it takes and which
TIMES attribute it corresponds to.  `plaintimes schema` prints it (JSON for
agents, Markdown for people) and docs/FORMAT.md is generated from it, so the
reference cannot drift from the code.
"""
from __future__ import annotations

from typing import Any

SERIES = "number | {year: number}"
BY_REGION = SERIES + " | {region: ...}"
BY_SLICE = "number | {timeslice: number} | {year: number} | {region: ...}"

SCHEMA: dict[str, dict[str, Any]] = {
    "model.yaml (settings)": {
        "_doc": "Top-level keys of model.yaml. Everything else may live in included files.",
        "plaintimes": ("1", "Format version. Required.", ""),
        "name": ("string", "Model name; used for run and export file names.", ""),
        "description": ("string", "Free text.", ""),
        "currency": ("string", "Label of the money unit, e.g. MUSD2025.", "CUR"),
        "discount_rate": ("number 0..1", "Social discount rate used for the whole objective.", "G_DRATE"),
        "discount_year": ("year", "Year all costs are discounted to. Default: first model year.", "G_DYEAR"),
        "mid_year_discounting": ("true | false", "Treat annual payments as falling mid-year. Default false.", "$SET MID_YEAR YES"),
        "periods": ("{start, lengths} | {milestones[, start, end]}",
                    "Model periods. {start: 2025, lengths: [5, 5, 10]} gives explicit lengths, the milestone year "
                    "(period label) is the middle year. {milestones: [2025, 2030, 2040]} puts period boundaries "
                    "halfway between milestones.", "MILESTONYR, B, E"),
        "timeslices": ("nested mapping name -> year fraction",
                       "Up to three nested levels (season, weekly, daynite). Leaves are fractions of the year and must "
                       "sum to 1. Child names are parent+child: W: {D: .3, N: .2} defines WD and WN.", "ALL_TS, TS_GROUP, TS_MAP, G_YRFR"),
        "timeslice_levels": ("list", "Optional TIMES level names for the nesting depths, e.g. [season, daynite].", "TS_GROUP"),
        "regions": ("list of names", "Model regions. Required.", "REG"),
        "units": ("{energy, capacity}", "Default units: commodities default to units.energy, capacities to units.capacity.", "UNITS*"),
        "include": ("list of globs", "Files (relative to model.yaml) whose sections are merged in.", ""),
        "references": ("mapping", "Free-form library of sources that 'sources:' entries can cite.", ""),
    },
    "commodities.<NAME>": {
        "_doc": "Energy carriers, materials, energy-service demands and emissions.",
        "kind": ("energy | material | demand | emission", "Required.", "COM_TMAP (NRG, MAT, DEM, ENV)"),
        "unit": ("string", "Default: units.energy.", "COM_UNIT"),
        "timeslice": ("annual | season | weekly | daynite", "Level at which the balance is kept. Default annual.", "COM_TSL"),
        "description": ("string", "", "COM_DESC"),
        "demand": (BY_REGION, "Annual demand to be met (kind: demand only).", "COM_PROJ"),
        "load_shape": ("{timeslice: share}", "Share of annual demand in each timeslice of the commodity's level; sums to 1. "
                       "Default: by hours.", "COM_FR"),
        "balance": ("'>=' | '='", "'>=': production may exceed consumption (default for energy, demand). "
                    "'=': exact (default for material).", "COM_LIM"),
        "efficiency": (BY_REGION, "Share of production that reaches consumers (e.g. 0.93 for grid losses). Default 1.", "COM_IE"),
        "peak_reserve": (BY_REGION, "Reserve margin: firm capacity must exceed consumption in each peak timeslice by this fraction.",
                         "COM_PEAK, COM_PKRSV"),
        "peak_timeslices": ("list", "Timeslices in which the peak constraint applies. Default: all of the commodity's level.", "COM_PKTS"),
        "cap": (BY_REGION + "  (bound rule)", "Emissions only: upper limit on annual net emissions.", "COM_BNDNET UP"),
        "budget": ("number | {region: number}", "Emissions only: upper limit on cumulative net emissions over the horizon.", "COM_CUMNET UP"),
        "tax": (BY_REGION, "Emissions only: money per unit emitted.", "COM_TAXNET"),
        "sources": ("mapping key -> citation", "Provenance notes; not used in the maths.", ""),
    },
    "emission_factors.<FUEL>": {
        "_doc": "Emissions per unit of a commodity burnt. Applied to processes that say 'emissions: combustion'.",
        "<EMISSION>": (BY_REGION, "Emission per unit of the fuel consumed.", "FLO_EMIS"),
    },
    "processes.<NAME>": {
        "_doc": "Anything that turns commodities into other commodities: supply, conversion, end-use devices.",
        "description": ("string", "", "PRC_DESC"),
        "tags": ("list of strings", "Free labels; constraints can select processes by 'tag:<label>'.", ""),
        "regions": ("list", "Regions where the process exists. Default: all.", ""),
        "inputs": ("name | [names] | {name: amount}", "Fixed inputs: amount consumed per unit of activity, divided by "
                   "'efficiency' if that is given. A list means 1 each.", "TOP IN, ACT_FLO"),
        "outputs": ("name | [names] | {name: amount}", "Fixed outputs: amount produced per unit of activity. The first "
                    "output defines the activity (and the capacity unit).", "TOP OUT, PRC_ACTUNT, PRC_ACTFLO, ACT_FLO"),
        "efficiency": (BY_REGION, "Activity per unit of input. Year keys mean the operating period, or the vintage if "
                       "'vintaged: true'.", "ACT_FLO / ACT_EFF"),
        "flexible_inputs": ("{name: {efficiency, max_share, min_share}}", "Inputs the model may mix freely: "
                            "sum(efficiency_c * input_c) = activity. Per-commodity efficiency defaults to the process "
                            "efficiency. Shares are of the group total, per timeslice.", "COM_GMAP, ACT_EFF, FLO_SHAR"),
        "flexible_outputs": ("{name: {max_share, min_share}}", "Outputs the model may mix freely: they sum to the activity.",
                             "COM_GMAP, PRC_ACTUNT, FLO_SHAR"),
        "timeslice": ("annual | season | weekly | daynite", "Level at which the process is dispatched. Default: the finest "
                      "level among its commodities.", "PRC_TSL"),
        "activity_unit": ("string", "Default: unit of the first output.", "PRC_ACTUNT"),
        "capacity_unit": ("string", "Default: units.capacity. GW/MW with PJ/TWh/..., or '<unit>/yr', convert automatically.", "PRC_CAPACT"),
        "cap2act": ("number", "Activity per year from one unit of capacity at full use. Needed only for units the tool "
                    "cannot convert.", "PRC_CAPACT"),
        "lifetime": (BY_REGION, "Technical life in years. Required if the process has capacity.", "NCAP_TLIFE"),
        "economic_life": (BY_REGION, "Years over which the investment is annualised. Default: lifetime.", "NCAP_ELIFE"),
        "hurdle_rate": (BY_REGION, "Technology-specific discount rate for annualising the investment. Default: discount_rate.", "NCAP_DRATE"),
        "invcost": (BY_REGION, "Investment cost per unit of capacity. Its presence is what allows new investment; "
                    "leave it out for stock that cannot be rebuilt, use 0 for free capacity.", "NCAP_COST (absent: NCAP_BND UP 0)"),
        "fixom": (BY_REGION, "Fixed O&M per unit of capacity per year.", "NCAP_FOM"),
        "varom": (BY_REGION, "Variable cost per unit of activity (for supply processes: the fuel price).", "ACT_COST"),
        "availability": (BY_SLICE, "Maximum output in a timeslice as a share of capacity. A value for a season applies to "
                         "the timeslices inside it. Default 1.", "NCAP_AF UP"),
        "availability_min": (BY_SLICE, "Minimum output per timeslice (must-run).", "NCAP_AF LO"),
        "availability_fix": (BY_SLICE, "Fixed output profile (no curtailment).", "NCAP_AF FX"),
        "capacity_factor": (BY_SLICE, "Maximum average use over the year (a number) or over a season ({W: 0.5}).", "NCAP_AFA / NCAP_AFS UP"),
        "capacity_factor_min": (BY_SLICE, "Minimum average use over the year or a season.", "NCAP_AFA / NCAP_AFS LO"),
        "existing": ("{year: capacity} | {region: {year: capacity}}", "Stock built before the first model year, by "
                     "commissioning year. Retires after 'lifetime'.", "NCAP_PASTI"),
        "residual": (BY_REGION + "  (bound rule)", "Alternative to 'existing': the surviving old stock in each period. "
                     "Zero outside the years given.", "PRC_RESID"),
        "vintaged": ("true | false", "Track each vintage separately so efficiency and availability are those of the year built.", "PRC_VINT"),
        "emissions": ("combustion | {EMISSION: factor | {<commodity> | activity: factor}}",
                      "'combustion' applies emission_factors to the inputs. Explicit factors are per unit of the named "
                      "input/output flow, or per unit of activity. Negative factors model removals.", "FLO_EMIS"),
        "peak_contribution": (BY_REGION, "Share of capacity that counts as firm in the peak constraint. Default 1.", "NCAP_PKCNT"),
        "bounds": ("{capacity | new_capacity | activity: {max | min | fix: series}, cumulative_activity: {max | min: number}}",
                   "Limits on installed capacity, on new capacity in a period, on annual activity, and on activity summed "
                   "over the horizon (resource size). Bound rule applies.", "CAP_BND, NCAP_BND, ACT_BND, ACT_CUM"),
        "sources": ("mapping key -> citation", "Provenance notes; not used in the maths.", ""),
    },
    "storage.<NAME>": {
        "_doc": "Shifts a commodity between timeslices (native kernel only; not exported to TIMES yet).",
        "commodity": ("name", "The stored commodity; it must be tracked by timeslice.", ""),
        "cycle": ("level name", "Level within which charge and discharge must balance. Default: the level above the "
                  "commodity's (e.g. within each season for a day/night commodity). 'annual' gives seasonal storage.", ""),
        "efficiency": (BY_REGION, "Round-trip efficiency, applied on charging. Default 1.", ""),
        "hours": (BY_REGION, "Hours of discharge at full power that the store holds; limits energy shifted per cycle.", ""),
        "capacity_unit / cap2act / lifetime / economic_life / hurdle_rate / invcost / fixom / varom / existing / residual / availability / peak_contribution / bounds / regions / tags / description / sources":
            ("as for processes", "Capacity is discharge power; varom is charged on discharge.", ""),
    },
    "trade.<NAME>": {
        "_doc": "A link carrying one commodity between two regions (native kernel only; not exported to TIMES yet).",
        "commodity": ("name", "The traded commodity.", ""),
        "from / to": ("region", "Direction of flow.", ""),
        "bidirectional": ("true | false", "Allow flow both ways over the same capacity. Default false.", ""),
        "efficiency": (BY_REGION, "Share of the amount sent that arrives. Default 1.", ""),
        "varom": (BY_REGION, "Cost per unit sent.", ""),
        "capacity_unit / cap2act / lifetime / economic_life / hurdle_rate / invcost / fixom / existing / residual / availability / bounds / tags / description / sources":
            ("as for processes", "Leave all of these out for an uncapacitated link. Costs are booked in the 'from' region.", ""),
    },
    "constraints.<NAME>": {
        "_doc": "User constraints: any linear relation between flows, activities and capacities.",
        "description": ("string", "", "UC_N"),
        "terms": ("list of terms", "Each term is exactly one of: {flow: C, of: [procs]}, {production: C}, {consumption: C}, "
                  "{emission: E}, {capacity: [procs]}, {new_capacity: [procs]}, {activity: [procs]}, plus 'coef' "
                  "(number or year series, default 1). Process selectors are names, globs ('WIND*') or 'tag:<label>'.",
                  "UC_FLO, UC_COMPRD, UC_COMCON, UC_COMNET, UC_CAP, UC_NCAP, UC_ACT"),
        "sense": ("'<=' | '>=' | '='", "Required.", "UC_RHS* limit type"),
        "rhs": (BY_REGION + "  (bound rule)", "Right-hand side. Default 0. A year series activates the constraint only "
                "between its first and last year.", "UC_RHSRT, UC_RHST, UC_RHSR, UC_RHS"),
        "regions": ("each | sum | [regions]", "'each' (default): one constraint per region. 'sum': one constraint over all regions.",
                    "UC_R_EACH / UC_R_SUM"),
        "periods": ("each | sum", "'each' (default): one constraint per period. 'sum': one constraint over the horizon, "
                    "in which flows, activity and emissions count once per year and capacities once per period.",
                    "UC_T_EACH / UC_T_SUM"),
        "from / until": ("year", "Restrict the constraint to periods overlapping these years.", ""),
    },
    "expect (list)": {
        "_doc": "Assertions about results, run by `plaintimes test`.",
        "name": ("string", "What the check means.", ""),
        "scenarios": ("list", "Scenario overlays to apply. Default: none (the base model).", ""),
        "status": ("optimal | infeasible | unbounded", "Expected solve status. Default optimal.", ""),
        "objective": ("{min, max, equals}", "Check the total discounted cost.", ""),
        "table / where / field": ("table name / {column: value} / column", "Sum 'field' over the rows of a result table "
                                  "matching 'where' (values may be lists).", ""),
        "min / max / equals / tolerance": ("numbers", "Bounds for the summed value.", ""),
    },
}

RULES = [
    ("Year series", "Any number may instead be a mapping of years to numbers. Costs, efficiencies, demands and other "
                    "data are interpolated linearly between the given years and held constant outside them."),
    ("Bound rule", "Bounds, caps, residual stock and constraint right-hand sides are interpolated linearly between the "
                   "given years and do not apply outside them. A plain number applies in every period."),
    ("By region", "Any series may be wrapped in a mapping of region names to give region-specific values."),
    ("What a year means", "Costs are evaluated for the calendar years in which they are paid. Technical data "
                          "(efficiency, availability, coefficients) are evaluated at the milestone year of the "
                          "operating period, or at the vintage year for 'vintaged: true' processes. Lifetimes and "
                          "investment data belong to the vintage."),
    ("Investment timing", "New capacity of a period is available from the first year of the period. Its investment "
                          "cost is the discount-weighted mean of the cost curve over the years around the period "
                          "start (TIMES convention); with a constant cost this is simply the cost."),
    ("Scenarios", "A scenario file is a partial model. Mappings merge; numbers, lists and year series are replaced "
                  "whole; null deletes a key."),
]


def as_json() -> dict[str, Any]:
    out: dict[str, Any] = {"sections": {}, "rules": [{"name": n, "text": t} for n, t in RULES]}
    for section, keys in SCHEMA.items():
        sec: dict[str, Any] = {"doc": keys["_doc"], "keys": {}}
        for key, val in keys.items():
            if key == "_doc":
                continue
            typ, doc, times = val
            entry = {"type": typ, "doc": doc}
            if times:
                entry["times"] = times
            sec["keys"][key] = entry
        out["sections"][section] = sec
    return out


def as_markdown() -> str:
    lines = ["# Model format reference", "",
             "_Generated by `plaintimes schema --markdown`; do not edit by hand._", ""]
    lines += ["## General rules", ""]
    for name, text in RULES:
        lines += [f"**{name}.** {text}", ""]
    for section, keys in SCHEMA.items():
        lines += [f"## `{section}`", "", keys["_doc"], "", "| key | value | meaning | TIMES |", "|---|---|---|---|"]
        for key, val in keys.items():
            if key == "_doc":
                continue
            typ, doc, times = val
            lines.append(f"| `{key}` | {typ.replace('|', chr(92) + '|')} | {doc.replace('|', chr(92) + '|')} | {times.replace('|', chr(92) + '|')} |")
        lines.append("")
    return "\n".join(lines)
