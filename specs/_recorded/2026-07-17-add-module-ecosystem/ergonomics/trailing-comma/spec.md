# Feature: Trailing-Comma Removal

The trailing-comma ergonomics module is REMOVED from the framework repo and
migrated to the central module library as module #2. Like cast-shorthand it was
never part of the engine `release.sql`/`deploy_all.sql` bundle — it shipped only
via `examples/ergonomics_trailing_comma.sql` with a canonical Lua copy at
`sql/lua/ergonomics_trailing_comma_v1.lua` and tests at
`tests/test_ergonomics_trailing_comma.py` — so removing it changes no engine
bundle output or statement count. The capability continues to exist, now
installable via `preproc module add trailing-comma` from the library.

## Background

* The framework no longer ships the trailing-comma module.
  `examples/ergonomics_trailing_comma.sql`,
  `sql/lua/ergonomics_trailing_comma_v1.lua`, and
  `tests/test_ergonomics_trailing_comma.py` are removed; the module is authored
  in the library repo `exasol-labs/preprocessor-library` as
  `modules/trailing-comma/`, its Lua body carried over unchanged.
* This removal supersedes ADR-032's placement of the `ergonomics/` domain inside
  the framework: user-facing convenience transforms now live in the library.
* `examples/README.md` retains a pointer noting the ergonomics modules have moved
  to the central library and are installable via `preproc module add`.

## Scenarios

<!-- DELTA:REMOVED -->
### Scenario: The trailing-comma feature is removed from the framework repo

* *GIVEN* the framework previously shipped the trailing-comma module only via the examples layer (never in the engine `release.sql`/`deploy_all.sql` bundle)
* *WHEN* this plan is applied and its every scenario (structural comma-before-`)` in parenthesised/VALUES/function-arg lists, whitespace and comment variants, comma-before-`FROM`/`GROUP BY`, no-trailing passthrough, string/identifier/line/block-comment skips, escaped-string, multiple-in-one-statement, fail-closed, example-only deployment, luacheck) migrates to the library module
* *THEN* the framework repo MUST NOT contain `examples/ergonomics_trailing_comma.sql`, `sql/lua/ergonomics_trailing_comma_v1.lua`, or `tests/test_ergonomics_trailing_comma.py`
* *AND* the `ergonomics/trailing-comma` feature MUST be removed from the framework specs on record
* *AND* the capability MUST remain available via `preproc module add trailing-comma` from the central library
<!-- /DELTA:REMOVED -->
