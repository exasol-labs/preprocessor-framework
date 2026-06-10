# Plan: add-ergonomics-cast-trailing-comma

## Summary

Delivers two SQL ergonomics conveniences — PostgreSQL-style `expr::type` cast
shorthand and optional trailing commas in SQL lists — as opt-in, pure-Lua
TRANSLATE-phase example preprocessor modules in the `examples/` directory.
These modules validate the framework's Phase-0 experimentation strategy before
any engine-level work.

## Design

### Context

The "SQL Niceness & Extensions" product initiative identifies several
conveniences that could be delivered as a preprocessing layer. The framework's
`examples/` directory is the established experimentation vehicle: modules are
opt-in, self-registering, and easy to roll back, with no coupling to the core
install. The governing guardrail is **additive only** — a statement that is
already valid Exasol SQL, or that contains no `::` / no trailing comma, MUST be
returned byte-for-byte unchanged.

Two concerns need a design decision: domain placement and operand-boundary
approach for the cast scanner.

**Goals**

- Deliver `ERGONOMICS_CAST_V1` and `ERGONOMICS_TRAILING_COMMA_V1` as opt-in
  example modules in `examples/`, not in the core install bundle.
- Honour the TRANSLATE contract: run-all text threading, string return on every
  path, fail-closed (return original on any error or non-match), no defensive
  pcall.
- Each module implements a single-pass scanner/tokenizer that skips string
  literals, quoted identifiers, line comments, and block comments — the minimum
  required for the additive-only guarantee.
- Parallel delivery: both modules are independent and can be authored and tested
  concurrently.

**Non-Goals**

- Full PostgreSQL/DuckDB dialect compatibility. These are individual features,
  not a dialect mode.
- Modifying `sql/lua/master.lua`, `release.sql`, `src/preproc/install.py`, or
  any core install artifact.
- Runtime performance optimisation beyond what a single Lua pass provides (no
  caching, no compiled patterns shared across statements).
- Handling `::` inside `CREATE TYPE`, DDL bodies, or other statement types not
  listed in the supported-operand subset.

### Decision

#### Domain: `ergonomics/` (new) rather than `dispatch/`

The two existing reference modules (`demo_translate.sql`, `demo_ping.sql`)
demonstrate framework mechanics. The new modules deliver real user conveniences
that are conceptually separate from framework dispatch mechanics. A new
`ergonomics/` spec domain makes the boundary explicit and leaves `dispatch/`
exclusively for dispatch-mechanism specs. The new domain appears in the
`specs/_plans/` delta path and, when recorded, becomes
`specs/ergonomics/cast-shorthand/spec.md` and
`specs/ergonomics/trailing-comma/spec.md`.

#### Cast scanner: conservative supported-operand subset

The left operand of `::` is scanned backwards from the `::` position. The
supported operand types are:

| Operand kind | Example | Scan rule |
|---|---|---|
| Unqualified identifier | `col` | Scan back over `[A-Za-z0-9_]` |
| Dotted (schema-qualified) | `s.t.col` | Scan back over `[A-Za-z0-9_.]` |
| Parenthesised expression | `(a + b)` | Scan back over balanced `(...)` |
| Function call | `f(...)` | Identifier + balanced `(...)` |
| String/numeric literal | `'x'`, `42` | Single-quoted token or digit run |
| Chained cast result | `CAST(...)` | A prior `CAST(` already placed by the left-to-right pass |

Operand types NOT in scope: subqueries `(SELECT ...)`, `CASE` expressions,
`ARRAY[...]`, and any other non-primary expression forms. When the backwards
scan reaches a character that does not belong to any of the above patterns, the
module returns the original text unchanged (fail-closed for that `::` token).

This conservative subset covers the vast majority of practical use cases. The
documented limitation (non-primary left operands are not rewritten) is
preferable to a silent wrong-operand-boundary corruption.

#### Trailing comma: structural + small keyword set

Two pattern classes are combined:

1. **Structural** (`comma → `)`)**: always unambiguous; always safe to remove.
2. **Keyword-terminated** (`comma → terminator-keyword`): recognised keywords
   are `FROM`, `WHERE`, `GROUP`, `ORDER`, `HAVING`, `LIMIT`, `UNION`,
   `INTERSECT`, `EXCEPT`, `INTO`, `SET`, `ON`, `RETURNING`. The match is
   case-insensitive and word-boundary-guarded. This set covers `SELECT a, b,
   FROM t` (the most common case) and avoids false positives on column names or
   identifiers that happen to be reserved words.

Commas outside these two patterns are never removed.

#### Lua source: inline in `examples/*.sql` + canonical copy in `sql/lua/`

The established examples convention is to embed the Lua body inline in the
`.sql` file. To support `luacheck` via the existing `luacheck sql/lua/` CI
invocation, a canonical-source copy is also maintained at
`sql/lua/ergonomics_cast_v1.lua` and
`sql/lua/ergonomics_trailing_comma_v1.lua`. These two canonical files are the
authoritative sources; the inline bodies in the `.sql` files are kept in sync
during authoring.

### Consequences

| Decision | Alternatives Considered | Rationale |
|---|---|---|
| New `ergonomics/` domain | Place under `dispatch/` | `dispatch/` covers framework mechanics; ergonomics features are user-facing conveniences — distinct concern |
| Conservative operand subset for cast | Fully general expression parser | A wrong boundary silently corrupts SQL; the common cases cover ~95% of real usage; scope documented explicitly |
| `comma → )` structural rule + small keyword set | Structural only; or full grammar parser | Structural alone misses `SELECT a, FROM t`; grammar parser is out of scope for a Lua one-pass scanner; the keyword set is small enough to reason about exhaustively |
| Lua source inline + `sql/lua/` canonical copy | Inline only; or `sql/lua/` only and require manual extraction | Inline only makes luacheck awkward; `sql/lua/` only breaks the established examples convention |

## Features

| Feature | Status | Spec |
|---|---|---|
| `ergonomics/cast-shorthand` | NEW | `specs/_plans/add-ergonomics-cast-trailing-comma/ergonomics/cast-shorthand/spec.md` |
| `ergonomics/trailing-comma` | NEW | `specs/_plans/add-ergonomics-cast-trailing-comma/ergonomics/trailing-comma/spec.md` |

## Implementation Tasks

- [ ] 1.1 Create `specs/ergonomics/` domain directory (record step; no code yet)
- [ ] 1.2 Author `sql/lua/ergonomics_cast_v1.lua` — single-pass scanner/tokenizer with backwards operand scan and type-name capture [expert]
- [ ] 1.3 Author `sql/lua/ergonomics_trailing_comma_v1.lua` — single-pass scanner/tokenizer with structural and keyword-terminated trailing-comma removal [expert]
- [ ] 1.4 Author `examples/ergonomics_cast.sql` — inline Lua body (sync from `sql/lua/`) + self-registration sugar
- [ ] 1.5 Author `examples/ergonomics_trailing_comma.sql` — inline Lua body (sync from `sql/lua/`) + self-registration sugar
- [ ] 1.6 Update `examples/README.md` — add two new rows to the Files table
- [ ] 2.1 Write `tests/test_ergonomics_cast.py` — pipeline fixture + all cast-shorthand scenarios
- [ ] 2.2 Write `tests/test_ergonomics_trailing_comma.py` — pipeline fixture + all trailing-comma scenarios
- [ ] 2.3 Verify luacheck passes for both new `sql/lua/` source files (`luacheck sql/lua/`)
- [ ] 2.4 Run `uv run pytest tests/test_ergonomics_cast.py tests/test_ergonomics_trailing_comma.py` and confirm green

## Parallelization

| Parallel Group | Tasks |
|---|---|
| Group A (author Lua modules, independent) | 1.2, 1.3 |
| Group B (depends on A: author .sql wrappers) | 1.4, 1.5 |
| Group C (depends on A: write tests, independent of each other) | 2.1, 2.2 |
| Group D (depends on B+C: docs + lint + run) | 1.6, 2.3, 2.4 |

Sequential dependencies:
- Group A → Group B (inline bodies sourced from canonical `.lua` files)
- Group A → Group C (tests reference the module names defined in A)
- Group B + Group C → Group D (README update, lint, and test run need all artifacts)

## Dead Code Removal

No existing code is removed by this plan. Both features are purely additive.

| Type | Location | Reason |
|---|---|---|
| (none) | — | — |

## Verification

### Scenario Coverage

| Scenario | Test Type | Test Location | Test Name |
|---|---|---|---|
| Simple identifier cast rewritten | Integration | `tests/test_ergonomics_cast.py` | `test_cast_simple_identifier` |
| Schema-qualified identifier cast | Integration | `tests/test_ergonomics_cast.py` | `test_cast_schema_qualified_identifier` |
| Parenthesised expression cast | Integration | `tests/test_ergonomics_cast.py` | `test_cast_parenthesised_expression` |
| Function-call operand cast | Integration | `tests/test_ergonomics_cast.py` | `test_cast_function_call_operand` |
| Chained cast rewritten inside-out | Integration | `tests/test_ergonomics_cast.py` | `test_cast_chained` |
| Parameterised type captured | Integration | `tests/test_ergonomics_cast.py` | `test_cast_parameterised_type` |
| No `::` → byte-for-byte unchanged | Integration | `tests/test_ergonomics_cast.py` | `test_cast_passthrough_no_colons` |
| `::` in single-quoted string not rewritten | Integration | `tests/test_ergonomics_cast.py` | `test_cast_inside_single_quoted_string` |
| `::` in double-quoted identifier not rewritten | Integration | `tests/test_ergonomics_cast.py` | `test_cast_inside_double_quoted_identifier` |
| `::` in line comment not rewritten | Integration | `tests/test_ergonomics_cast.py` | `test_cast_inside_line_comment` |
| `::` in block comment not rewritten | Integration | `tests/test_ergonomics_cast.py` | `test_cast_inside_block_comment` |
| `''`-escaped string scanned correctly | Integration | `tests/test_ergonomics_cast.py` | `test_cast_escaped_string_mixed` |
| Unrecognised left operand → original text | Integration | `tests/test_ergonomics_cast.py` | `test_cast_unrecognised_operand_fail_closed` |
| Not deployed by core install | Integration | `tests/test_ergonomics_cast.py` | `test_cast_not_in_core_install` |
| ERGONOMICS_CAST_V1 passes luacheck | Unit | `tests/test_ergonomics_cast.py` | `test_cast_module_passes_luacheck` |
| Trailing comma before `)` removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_before_paren` |
| Trailing comma in VALUES list removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_values_list` |
| Trailing comma in function args removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_function_args` |
| Trailing comma + whitespace before `)` | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_whitespace_before_paren` |
| Trailing comma + comment before `)` | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_comment_before_paren` |
| Trailing comma before FROM removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_before_from` |
| Trailing comma before GROUP BY removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_before_group_by` |
| No trailing commas → byte-for-byte unchanged | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_passthrough_no_trailing` |
| Comma in single-quoted string not removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_inside_single_quoted_string` |
| Comma in double-quoted identifier not removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_inside_double_quoted_identifier` |
| Comma in line comment not removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_inside_line_comment` |
| Comma in block comment not removed | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_inside_block_comment` |
| `''`-escaped string before real trailing comma | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_escaped_string_mixed` |
| Multiple trailing commas removed in one pass | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_multiple_in_one_statement` |
| Internal error → original text (fail-closed) | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_fail_closed` |
| Not deployed by core install | Integration | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_not_in_core_install` |
| ERGONOMICS_TRAILING_COMMA_V1 passes luacheck | Unit | `tests/test_ergonomics_trailing_comma.py` | `test_trailing_comma_module_passes_luacheck` |

### Manual Testing

| Feature | Command | Expected Output |
|---|---|---|
| Cast shorthand | Activate MASTER, run `SELECT 42::integer AS x` | Returns scalar `42` (integer); no syntax error |
| Cast shorthand passthrough | Activate MASTER, run `SELECT 1 + 1 AS x` | Returns scalar `2`; no alteration |
| Cast `::` in string passthrough | Activate MASTER, run `SELECT '::' AS x` | Returns string `::` unchanged |
| Trailing comma `(...)` | Activate MASTER, run `SELECT * FROM (SELECT 1 AS x,) t` | Returns 1 row; no syntax error |
| Trailing comma before FROM | Activate MASTER, run `SELECT 1 AS a, 2 AS b, FROM dual` | Returns 1 row with `a=1, b=2`; no syntax error |
| Trailing comma passthrough | Activate MASTER, run `SELECT 1 AS a, 2 AS b FROM dual` | Returns 1 row; no alteration |

Activate MASTER for a session:
```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
```

Register and deregister a rule for manual testing (requires `PREPROC_ADMIN`):
```sql
-- Register (from examples/ergonomics_cast.sql last line):
PREPROC ADD RULE FOR ROLE PREPROC_ADMIN PHASE TRANSLATE SCRIPT PREPROC_RT.ERGONOMICS_CAST_V1 FUNCTION cast_shorthand;
-- Register (from examples/ergonomics_trailing_comma.sql last line):
PREPROC ADD RULE FOR ROLE PREPROC_ADMIN PHASE TRANSLATE SCRIPT PREPROC_RT.ERGONOMICS_TRAILING_COMMA_V1 FUNCTION trailing_comma;
```

### Checklist

| Step | Command | Expected |
|---|---|---|
| Sync + deps | `uv sync` | Exit 0 |
| Lint (Lua) | `luacheck sql/lua/` | 0 errors/warnings |
| Lint (Python) | `uv run ruff check .` | 0 errors |
| Format check | `uv run ruff format --check .` | No changes |
| Test (cast) | `uv run pytest tests/test_ergonomics_cast.py -v` | 0 failures |
| Test (trailing comma) | `uv run pytest tests/test_ergonomics_trailing_comma.py -v` | 0 failures |
| Full test suite | `uv run pytest` | 0 failures |
