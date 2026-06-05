# Plan: add-expand-command-expansion

## Summary

Ships the EXPAND phase command-expansion mechanism: first-match-terminal dispatch within EXPAND (REWRITE still runs after), the module return contract (`nil` = no match; string = match + terminal), a switch of `client_pattern` matching from Lua pattern to literal substring, and a permanent demo module (`DEMO_PING_V1`) proving the mechanism end-to-end. Real product commands and token-level conveniences are deferred.

## Design

### Context

MASTER currently runs ALL matching rules in every phase with no terminal semantics. EXPAND-phase custom commands require first-match-terminal: the first module that recognises the statement owns the expansion and remaining EXPAND rules are skipped (REWRITE still runs). A module needs to signal "not my command" (nil return) vs "I matched, here is the expansion" (string return). Additionally, `client_pattern` matching uses `string.find` in Lua pattern mode today, which is a latent bug — a pattern like `EXA.1.0` has `.` interpreted as "any char"; literal substring was the intended semantic.

- **Goals** — Add EXPAND first-match-terminal dispatch to MASTER; change `client_pattern` to literal substring; ship `DEMO_PING_V1` as the canonical authoring example and mechanism proof; document the module authoring contract in that source file.
- **Non-Goals** — Real product commands (LS, backup-start), token-level conveniences, TRANSLATE changes, profiles/self-disable, management syntax, any grant beyond what the existing schema-level PUBLIC grant already covers.

### Decision

#### Architecture

```
Statement arrives at MASTER
        │
        ▼
TRANSLATE phase (unchanged; plain text threading)
        │
        ▼
EXPAND phase (NEW: first-match-terminal)
   ┌────────────────────────────────────────────────┐
   │ for each EXPAND rule in ordinal order:         │
   │   call dispatch_expand_rule(script, fn, text)  │
   │   if result is non-nil string → use result,    │
   │     set expanded flag, break                   │
   │   if nil (or pcall failure) → continue         │
   └────────────────────────────────────────────────┘
        │
        ▼ (expanded text, or original text if no match)
REWRITE phase (unchanged; plain text threading)
        │
        ▼
setsqltext (only if text changed)
```

The EXPAND dispatcher is a NEW `dispatch_expand_rule` helper in master.lua that differs from `dispatch_rule` in one key way: it propagates `nil` from the module function (no match) instead of falling back to the original text. A `nil` return from pcall success means "not my command — keep looking." A string return means "matched — here is the expansion." A pcall failure means "module broken — skip and keep looking" (fail-open, same as other phases).

#### Patterns

| Pattern | Where | Why |
|---------|-------|-----|
| `nil`-signaled no-match | `dispatch_expand_rule` in master.lua | Unambiguous: a matched command always produces a string (even if identical) |
| Literal substring via `string.find(s, p, 1, true)` | `client_matches` in master.lua | Avoids Lua pattern misinterpretation of dots, parens, hyphens in client strings |
| Versioned module filename (`DEMO_PING_V1`) | `sql/lua/demo_ping_v1.lua` | Mission versioning discipline: never `CREATE OR REPLACE` a live module |
| Self-documenting authoring contract | Header comment of `demo_ping_v1.lua` | Demo module is the canonical reference; comments are the operator-facing docs |

### Consequences

| Decision | Alternatives Considered | Rationale |
|----------|------------------------|-----------|
| `nil` = no match; string = match + terminal | `false` = no match; or two-value tuple | `nil` is idiomatic Lua for "absent"; two-value tuple breaks the pcall result shape; `false` is less natural for "I don't handle this" |
| Separate `dispatch_expand_rule` instead of modifying `dispatch_rule` | Generalize `dispatch_rule` with a mode parameter | EXPAND semantics are different enough (nil propagation, terminal break) that a separate helper is clearer; `dispatch_rule` stays unchanged for TRANSLATE and REWRITE |
| `string.find(s, p, 1, true)` literal flag | Change nothing; use `string.find(s, p)` | `true` as 4th arg is the standard Lua plain-find; zero behavior change for patterns with no special chars, fixes latent bug for patterns with dots/parens |
| `DEMO_PING_V1` — versioned, named as demo | Unversioned `DEMO_PING`; or skip demo | Versioning discipline is a mission invariant; "DEMO_" prefix makes it permanently safe to leave deployed without confusion as a product command |

## Features

| Feature | Status | Spec |
|---------|--------|------|
| command-expansion | NEW | `specs/_plans/add-expand-command-expansion/dispatch/command-expansion/spec.md` |
| master-skeleton | CHANGED | `specs/_plans/add-expand-command-expansion/install/master-skeleton/spec.md` |
| cli-install | CHANGED | `specs/_plans/add-expand-command-expansion/install/cli-install/spec.md` |

## Implementation Tasks

- [ ] 1.1 Fix `client_matches` in `sql/lua/master.lua`: add `1, true` to `string.find` call to use literal substring matching
- [ ] 1.2 Update `test_dispatch_client_pattern_match` in `tests/test_master_dispatch.py`: the test comment says "Lua pattern match" — update the docstring and variable names to reflect literal substring semantics (behavior is unchanged since the test already uses a plain prefix substring with no special chars)
- [ ] 1.3 Update `test_dispatch_client_pattern_no_match` docstring similarly for literal substring language
- [ ] 2.1 Add `dispatch_expand_rule` function to `sql/lua/master.lua`: wraps `pcall(import + fn call)`, returns the function's return value directly (nil or string), propagates nil on pcall failure (fail-open, returns nil not text) [expert]
- [ ] 2.2 Modify the EXPAND section of the dispatch loop in `sql/lua/master.lua` to use first-match-terminal: call `dispatch_expand_rule`, break loop on non-nil result, set `sqltext` to result [expert]
- [ ] 2.3 Ensure TRANSLATE and REWRITE phases continue to use `dispatch_rule` (plain text threading, no terminal semantics) — no changes to those loop sections
- [ ] 2.4 Run luacheck against `sql/lua/` and fix any warnings introduced by the new function
- [ ] 3.1 Create `sql/lua/demo_ping_v1.lua` with the `ping(sqltext)` function: trim and uppercase the statement; if it equals `'PING'` return `"SELECT 'PONG'"`, else return `nil`; add full header comment block covering the authoring contract [expert]
- [ ] 3.2 Add deploy of `DEMO_PING_V1` to `src/preproc/install.py` (alongside MASTER deployment, using `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.DEMO_PING_V1`)
- [ ] 4.1 Write integration test `test_expand_terminal_first_match` in `tests/test_master_dispatch.py`: seed two EXPAND rules (ordinal 1 returns expansion, ordinal 2 also would match), assert only ordinal 1 ran
- [ ] 4.2 Write integration test `test_expand_nil_continues_to_next` in `tests/test_master_dispatch.py`: seed ordinal-1 rule that returns nil for the probe statement, ordinal-2 that returns expansion; assert ordinal 2 ran
- [ ] 4.3 Write integration test `test_expand_failing_module_skipped_continues` in `tests/test_master_dispatch.py`: ordinal-1 throws, ordinal-2 matches; assert expansion from ordinal-2 is used
- [ ] 4.4 Write integration test `test_expand_no_match_passes_through` in `tests/test_master_dispatch.py`: all EXPAND rules return nil; assert original statement unchanged
- [ ] 4.5 Write integration test `test_expand_rewrite_still_runs_after_match` in `tests/test_master_dispatch.py`: EXPAND rule matches, REWRITE rule tags the expanded text; assert both ran in order
- [ ] 4.6 Write integration test `test_demo_ping_expands` in `tests/test_master_dispatch.py`: deploy DEMO_PING_V1 via a rule, submit `PING`, assert result is `PONG` [expert]
- [ ] 4.7 Write integration test `test_demo_ping_nil_for_non_ping` in `tests/test_master_dispatch.py`: DEMO_PING_V1 in EXPAND pipeline; submit a normal SELECT; assert no expansion occurred
- [ ] 4.8 Write unit test `test_client_pattern_literal_substring` in `tests/test_master_dispatch.py` or a dedicated test module: verify that a `client_pattern` containing a Lua-special char (e.g. `"EXA.Client"`) matches only as a literal substring, not as a Lua pattern
- [ ] 5.1 Run full test suite (`uv run pytest`) — all 50 existing tests plus new tests MUST pass
- [ ] 5.2 Run luacheck (`luacheck sql/lua/`) — zero warnings across all files including new module

## Parallelization

| Parallel Group | Tasks |
|----------------|-------|
| Group A | 1.1, 1.2, 1.3 (client_pattern fix and test updates — independent of dispatch changes) |
| Group B | 2.1, 2.2, 2.3 (EXPAND dispatch logic — must be done together, depends on Group A being stable) |
| Group C | 3.1, 3.2 (demo module authoring and install wiring — independent of dispatch tests) |
| Group D | 4.1–4.8 (integration tests — depend on Groups B and C) |
| Group E | 2.4, 5.1, 5.2 (luacheck + full suite — depend on Groups B, C, D) |

Sequential dependencies:
- Group A → Group B (literal fix must land before dispatch tests verify it)
- Group B + Group C → Group D (dispatch mechanism and demo module must exist before tests)
- Group D → Group E (tests must be written before running the full suite)

## Dead Code Removal

| Type | Location | Reason |
|------|----------|--------|
| None | — | No existing code is removed; `dispatch_rule` is kept for TRANSLATE/REWRITE phases |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|----------|-----------|---------------|-----------|
| EXPAND stops at first matching module | Integration | `tests/test_master_dispatch.py` | `test_expand_terminal_first_match` |
| EXPAND continues when module returns nil | Integration | `tests/test_master_dispatch.py` | `test_expand_nil_continues_to_next` |
| Failing EXPAND module skipped fail-open | Integration | `tests/test_master_dispatch.py` | `test_expand_failing_module_skipped_continues` |
| Unrecognised statement passes through all EXPAND rules | Integration | `tests/test_master_dispatch.py` | `test_expand_no_match_passes_through` |
| REWRITE runs after matched EXPAND expansion | Integration | `tests/test_master_dispatch.py` | `test_expand_rewrite_still_runs_after_match` |
| DEMO_PING_V1 expands PING to SELECT 'PONG' | Integration | `tests/test_master_dispatch.py` | `test_demo_ping_expands` |
| DEMO_PING_V1 returns nil for non-PING statement | Integration | `tests/test_master_dispatch.py` | `test_demo_ping_nil_for_non_ping` |
| DEMO_PING_V1 passes luacheck | Integration | `tests/test_master_dispatch.py` + CI | `test_demo_ping_expands` (deployment proof) + luacheck run |
| Module authoring contract documented in source | Manual | `sql/lua/demo_ping_v1.lua` | Code review / header inspection |
| client_pattern literal substring matching | Integration | `tests/test_master_dispatch.py` | `test_client_pattern_literal_substring` |
| Dispatch honours client_pattern — existing match test | Integration | `tests/test_master_dispatch.py` | `test_dispatch_client_pattern_match` (updated docstring) |
| Dispatch honours client_pattern — existing no-match test | Integration | `tests/test_master_dispatch.py` | `test_dispatch_client_pattern_no_match` (updated docstring) |
| EXPAND nil return is not terminal (continues) | Integration | `tests/test_master_dispatch.py` | `test_expand_nil_continues_to_next` |
| EXPAND first-match-terminal does not apply to REWRITE | Integration | `tests/test_master_dispatch.py` | `test_expand_rewrite_still_runs_after_match` |

### Manual Testing

| Feature | Command | Expected Output |
|---------|---------|-----------------|
| DEMO_PING_V1 end-to-end | Seed an EXPAND rule pointing at `PREPROC_RT.DEMO_PING_V1 / ping`; activate MASTER; run `PING` in a SQL client | Query returns single column with value `PONG` |
| DEMO_PING_V1 non-match | With same setup; run any normal SELECT | SELECT executes normally; DEMO_PING_V1 returns nil and REWRITE runs if applicable |
| client_pattern literal fix | Seed a rule with `client_pattern = 'EXA.Client'` (dot is Lua special); verify match only when client string contains that exact substring | Rule dispatches only for exact substring; a client named `EXAXClient` (which would match the Lua pattern) does NOT match |
| Full install including DEMO_PING_V1 | `preproc install` against a clean instance | Command exits 0; `PREPROC_RT.DEMO_PING_V1` script exists and is callable |

### Checklist

| Step | Command | Expected |
|------|---------|----------|
| Sync environment | `uv sync` | Exit 0 |
| Full test suite | `uv run pytest` | 0 failures |
| Lint | `uv run ruff check .` | 0 errors/warnings |
| Format | `uv run ruff format --check .` | No changes |
| Lua lint | `luacheck sql/lua/` | 0 warnings, 0 errors |
