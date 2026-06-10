# Verification Report: add-ergonomics-cast-trailing-comma

Date: 2026-06-10

## Scope verified

Two opt-in TRANSLATE-phase ergonomics modules delivered via `examples/`:
- `ERGONOMICS_CAST_V1` — `expr::type` → `CAST(expr AS type)`
- `ERGONOMICS_TRAILING_COMMA_V1` — trailing-comma removal (structural `)` + keyword set)

Changed/added files:
- `sql/lua/ergonomics_cast_v1.lua`, `sql/lua/ergonomics_trailing_comma_v1.lua`
- `examples/ergonomics_cast.sql`, `examples/ergonomics_trailing_comma.sql` (self-registering wrappers; inline Lua synced from `sql/lua/`)
- `examples/README.md` (two Files-table rows)
- `tests/test_ergonomics_cast.py`, `tests/test_ergonomics_trailing_comma.py`

## Gate results

| Gate | Result |
|------|--------|
| luacheck (both new sources) | OK — 0 warnings / 0 errors |
| New pytest files | 4 passed, 28 skipped (skips = `@integration`, no live Exasol) |
| Full pytest suite | 31 passed, 183 skipped |
| ruff check | All checks passed |
| Cast inline body == canonical source | True (byte-identical) |
| Standalone Lua harness (cast, 11 scenarios incl. regression) | 0 failures |
| Standalone Lua harness (trailing-comma, 14 scenarios incl. false-positive guards) | 0 failures |

The integration tests skip locally (no live Exasol). Logic was instead exercised
directly under a standalone Lua interpreter (`/home/nc/.luaenv/bin/lua`) against
every spec scenario plus added edge cases — all pass.

## Code review

One finding, fixed (see decision-log [R1]): the cast scanner mangled
schema-qualified function-call operands (`s.fn(col)::int` → `s.CAST(fn(col) AS int)`),
breaking ADR [2]'s fail-closed guarantee. Widened the leading-identifier back-scan
to include dots in both copies; added `test_cast_schema_qualified_function_call_operand`.

False-positive guards on the trailing-comma keyword rule were spot-checked:
`from_date` (FROM prefix) and `orders.id` (ORDER prefix) are correctly left intact.

## Constraint checks

- Additive-only / not in core install: `test_*_not_in_core_install` scans `install.py`
  and `sql/install/*.sql` for both module names — neither present.
- Not in `release.sql` bundle (per ADR): confirmed by the same source-scan tests and
  the examples/-only delivery design.

## Conclusion

All Phase 3 gates green. Implementation matches the plan and the two `ergonomics/`
spec deltas. Ready to record (`speq:record`) and/or commit.
