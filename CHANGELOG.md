# Changelog

## 0.1.0 (unreleased)

First version.

- Plain-text model format (YAML): regions, periods, timeslices, commodities,
  processes, storage, trade links, user constraints, scenario overlays,
  expectations.
- Native kernel: generates the TIMES linear programme in Python and solves it
  with HiGHS. Cost accounting follows the official code run with `OBJ=AUTO`
  and `OBLONG=YES`.
- Command line with JSON output: `check`, `info`, `show`, `res`, `rows`,
  `solve`, `explain`, `diff`, `test`, `export-dd`, `oracle`, `schema`, `init`.
- GAMS bridge: export to `.dd` / `.run` files for the official ETSAP TIMES
  code, and `oracle` to solve both ways and compare.
- Validation: a parity suite of hand-built and randomly generated cases whose
  objectives were recorded from official TIMES v4.9.8, plus a randomized
  differential test (`tests/parity/fuzz.py`). See docs/VALIDATION.md.

Known limits are listed in the README.
