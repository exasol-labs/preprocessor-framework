# Decision Log: add-ergonomics-cast-trailing-comma

Date: 2026-06-10

## Interview

**Q:** Implementation approach — hand-rolled Lua vs sqlglot UDF?
**A:** Two SEPARATE pure-Lua preprocessor modules so each fits the framework's one-concern-per-file convention. The sqlglot UDF approach was rejected: ~133–151 ms/statement VM startup is unacceptable for an everyday, broadly-applied convenience. These two features are meant to be applied to every statement that hits a user's pipeline, not scoped narrowly like the dialect-translation UDF.

**Q:** Where should the modules live — opt-in examples/ vs core install?
**A:** Opt-in examples/ (self-registering `.sql` per module, like `demo_ping.sql`). NOT added to `sql/lua/` core install path, `install.py`, or the `release.sql` bundle. The goal is to validate the strategy before any engine work; shipping through `examples/` keeps rollback trivial.

**Q:** Which dispatch phase?
**A:** TRANSLATE for both. Both modules act before the engine sees the statement — `::` and trailing commas are syntax errors in Exasol, so they must be handled at the translation phase, not REWRITE.

**Q:** Process?
**A:** Full speq spec-driven flow (this plan).

## Design Decisions

### [1] New `ergonomics/` spec domain rather than placing under `dispatch/`

- **Decision:** Create a new `ergonomics/` domain for the two new feature specs (`cast-shorthand`, `trailing-comma`), rather than adding them under the existing `dispatch/` domain.
- **Alternatives:** Place under `dispatch/` alongside `dialect-translation` and `audit-rewrite`; or create a new top-level `examples/` domain mirroring the filesystem.
- **Rationale:** The `dispatch/` domain currently covers framework dispatch mechanics (how phases work, how rules are dispatched, how audit rewrites apply). The ergonomics features are user-facing SQL conveniences delivered through the examples layer — a distinct concern. A new `ergonomics/` domain makes that boundary explicit and keeps `dispatch/` coherent. An `examples/` domain was rejected because "examples" describes the delivery vehicle, not the capability; future non-example ergonomics features (if promoted to core install) should land in the same domain.
- **Promotes to ADR:** yes

### [2] Conservative supported-operand subset for cast scanner

- **Decision:** The backwards operand scan for `::` supports a specific, documented subset of primary expression forms: unqualified and dotted identifiers, parenthesised expressions, function calls, string/numeric literals, and chained CAST results. Any `::` whose left operand is not in this subset causes the module to return the original text unchanged for the entire statement.
- **Alternatives:** Attempt fully general expression parsing (tracking all operator precedences, subquery syntax, CASE, ARRAY, etc.) to correctly identify operand boundaries in every possible SQL expression.
- **Rationale:** A wrong operand boundary silently corrupts SQL — the most dangerous failure mode in a preprocessing context. The common cases (identifier casts, literal casts, parenthesised group casts) cover the overwhelming majority of practical `::` usage. Failing closed on an unusual operand form is far preferable to silently producing `CAST(wrong_expr AS type)`. The scope limitation is explicitly documented, so module authors know when to write `CAST(expr AS type)` explicitly instead.
- **Promotes to ADR:** yes

### [3] Trailing-comma recognition: structural `comma→)` plus conservative keyword set

- **Decision:** Two pattern classes are combined: (1) structural `comma → )` (always unambiguous), and (2) keyword-terminated: `comma → FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|UNION|INTERSECT|EXCEPT|INTO|SET|ON|RETURNING` (case-insensitive, word-boundary-guarded). Commas outside these two patterns are never removed.
- **Alternatives:** Structural-only (safe but misses `SELECT a, b, FROM t`, the most common user need); full SQL grammar parser (out of scope for a single-pass Lua scanner; would require sqlglot, reintroducing the 133 ms/stmt latency); larger keyword set (higher false-positive risk on column aliases that happen to be reserved words).
- **Rationale:** The structural rule is provably safe. The keyword set is small enough to reason about exhaustively and word-boundary-guarded to avoid false positives. Together they cover the two dominant trailing-comma use cases seen in real SQL. The conservative stance (leave ambiguous commas intact) matches the additive-only governing guardrail.
- **Promotes to ADR:** yes

### [4] Lua source: inline in `examples/*.sql` AND canonical copy in `sql/lua/`

- **Decision:** Each example module maintains two copies of its Lua source: the primary inline embed in `examples/ergonomics_*.sql` (the delivery artifact, matching the existing demo convention) and a canonical copy in `sql/lua/ergonomics_*_v1.lua` (for `luacheck sql/lua/` CI invocation, matching the existing luacheck test pattern in `test_dialect_translation.py`).
- **Alternatives:** Inline only (makes luacheck awkward — the test has to extract and lint from the SQL file); `sql/lua/` only and extract at install time (breaks the self-contained `.sql` examples convention that operators run directly in EXAplus or DbVisualiser).
- **Rationale:** Both the self-contained deployment experience (operators can paste the `.sql` file directly) and the luacheck CI gate (which runs against `sql/lua/`) are valuable. Maintaining two copies adds one authoring constraint (keep in sync) but no runtime cost. The canonical file is the sync source; the inline body is updated when the canonical file changes.
- **Promotes to ADR:** no

### [5] Both modules in one plan, two separate spec files

- **Decision:** One plan (`add-ergonomics-cast-trailing-comma`) covers both features; each feature gets its own `spec.md` delta file under `ergonomics/cast-shorthand/` and `ergonomics/trailing-comma/`.
- **Alternatives:** Two separate plans, one per feature.
- **Rationale:** The features share the same initiative, the same governing constraints (additive-only, TRANSLATE phase, examples/ delivery, single-pass scanner), and the same parallelization structure. A single plan keeps the shared ADR visible in one place. The two spec files maintain the one-concern-per-file boundary.
- **Promotes to ADR:** no

## Review Findings

### [R1] Cast scanner mangled schema-qualified function-call operands

- **Finding:** In `scan_backward_operand`, the function-call branch (operand ending in `)`) scanned the leading identifier before `(` with the class `[A-Za-z0-9_]` only — excluding `.`. A schema-qualified UDF call such as `s.fn(col)::int` therefore captured only `fn(col)` as the operand and left `s.` stranded, emitting `s.CAST(fn(col) AS int)` — invalid SQL. This violated ADR [2]'s guarantee that an unhandled operand fails closed rather than emitting a corrupted rewrite, and is realistic since Exasol UDFs are schema-qualified.
- **Fix:** Widened the leading-identifier back-scan to `[A-Za-z0-9_%.]` and added a leading-dot strip, mirroring the existing dotted-identifier branch. Applied to both copies (`sql/lua/ergonomics_cast_v1.lua` and the inline body in `examples/ergonomics_cast.sql`).
- **Coverage:** Added `test_cast_schema_qualified_function_call_operand`. Verified all cast + trailing-comma scenarios (including this regression and the `from_date`/`orders.id` false-positive guards) by running the modules directly under a standalone Lua interpreter.
