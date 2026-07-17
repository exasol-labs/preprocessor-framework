# Feature: CAST Shorthand (`expr::type` → `CAST(expr AS type)`)

The CAST shorthand ergonomics module is REMOVED from the framework repo and
migrated to the central module library as module #1 (proving the
contract → package → registry → `preproc module add` path). It was never part of
the engine `release.sql`/`deploy_all.sql` bundle — it shipped only via
`examples/ergonomics_cast.sql` with a canonical Lua copy at
`sql/lua/ergonomics_cast_v1.lua` and tests at `tests/test_ergonomics_cast.py` —
so removing it does NOT change any engine bundle output or statement count. The
capability continues to exist, now installable via
`preproc module add cast-shorthand` from the library.

## Background

* The framework no longer ships the cast-shorthand module. `examples/ergonomics_cast.sql`,
  `sql/lua/ergonomics_cast_v1.lua`, and `tests/test_ergonomics_cast.py` are
  removed; the module is authored in the library repo
  `exasol-labs/preprocessor-library` as `modules/cast-shorthand/`, its Lua body
  carried over unchanged.
* This removal supersedes ADR-032's placement of the `ergonomics/` domain inside
  the framework: user-facing convenience transforms now live in the library, not
  the framework's spec tree.
* `examples/README.md` retains a pointer noting the ergonomics modules have moved
  to the central library and are installable via `preproc module add`.

## Scenarios

<!-- DELTA:REMOVED -->
### Scenario: The cast-shorthand feature is removed from the framework repo

* *GIVEN* the framework previously shipped the cast-shorthand module only via the examples layer (never in the engine `release.sql`/`deploy_all.sql` bundle)
* *WHEN* this plan is applied and its every scenario (identifier, schema-qualified, parenthesised, function-call, chained, parameterised, no-token passthrough, string/identifier/line/block-comment skips, escaped-string, fail-closed operand, example-only deployment, luacheck) migrates to the library module
* *THEN* the framework repo MUST NOT contain `examples/ergonomics_cast.sql`, `sql/lua/ergonomics_cast_v1.lua`, or `tests/test_ergonomics_cast.py`
* *AND* the `ergonomics/cast-shorthand` feature MUST be removed from the framework specs on record
* *AND* the capability MUST remain available via `preproc module add cast-shorthand` from the central library
<!-- /DELTA:REMOVED -->
