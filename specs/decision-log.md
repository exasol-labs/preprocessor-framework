# Architecture Decision Records

<!-- ADRs are numbered sequentially starting from ADR-001. Never renumber. -->
<!-- recorder-agent appends new ADRs from plan decision logs. -->

---

## ADR-001: Two-schema layout with a definer-rights view as the privilege bridge

**Date:** 2026-06-05
**Plan:** `add-install-two-schema-layout`
**Status:** Accepted

### Context

Exasol Lua scripts run with invoker rights, meaning a script cannot read admin tables that the calling user lacks grants on. Views, however, run with definer rights, so the view owner's privileges apply. This asymmetry makes a naive single-schema design unworkable: admin config tables cannot be grant-exposed to all users (leaks data), and invoker-rights scripts cannot read them directly (experimentally verified impossibility). The framework needs a mechanism to let a dispatcher script reach per-user pipeline config without granting ordinary users access to admin tables.

### Decision

Split objects into a locked admin schema `PREPROC` (config, resolution, settings, profiles — owned by the `PREPROC_ADMIN` role) and a PUBLIC-accessible runtime schema `PREPROC_RT` (MASTER script, views). The only path from invoker-rights Lua code to admin tables is the definer-rights view `PREPROC_RT.MY_PIPELINE`, filtered by `CURRENT_USER`.

### Options Considered

| Option | Verdict |
|--------|---------|
| Two-schema layout with definer-rights view | Chosen — exploits the invoker/definer asymmetry to expose per-user data without leaking admin config |
| Single schema with table grants to PUBLIC | Rejected — leaks admin configuration to every database user |
| Scripts reading admin tables directly | Rejected — impossible under invoker rights (verified experimentally) |

### Consequences

The definer-rights view is the single, auditable privilege bridge; adding new admin tables requires updating the view or creating parallel views. The invoker/definer asymmetry becomes a documented, tested contract rather than an implementation detail.

---

## ADR-002: Schema-level PUBLIC grants are a framework invariant

**Date:** 2026-06-05
**Plan:** `add-install-two-schema-layout`
**Status:** Accepted

### Context

In Exasol enforced preprocessor mode, a user lacking EXECUTE on the active preprocessor script cannot run any statement — including `ALTER SESSION`. A missed grant on any runtime object causes total user lockout. Per-object grants are fragile: every new object added to `PREPROC_RT` requires a manual re-grant, and a single omission is catastrophic. This risk was verified experimentally.

### Decision

Install issues `GRANT EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC` and `GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC`. Schema-level grants cover all current and future objects in the runtime schema automatically.

### Options Considered

| Option | Verdict |
|--------|---------|
| Schema-level PUBLIC grants on `PREPROC_RT` | Chosen — durable invariant; covers future objects automatically |
| Per-object grants re-issued on every object creation | Rejected — fragile; a missed grant is catastrophic in enforced mode |

### Consequences

Any object created in `PREPROC_RT` is immediately accessible to PUBLIC without additional grant steps. A future `status` verb should verify the schema-level grants are still present. No runtime object in `PREPROC_RT` can be made private without reworking this invariant.

---

## ADR-003: Install creates but does not activate; prints the activation statement

**Date:** 2026-06-05
**Plan:** `add-install-two-schema-layout`
**Status:** Accepted

### Context

`ALTER SYSTEM SET sql_preprocessor_script` is a system-wide, high-blast-radius operation. Activating an untested or misconfigured preprocessor blocks every user on the instance. The DBA — not an automated install tool — must own the cutover decision and timing.

### Decision

`preproc install` creates every object (schemas, tables, view, MASTER script, grants) but never executes the activation statement. It prints the exact `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER` statement to stdout for the DBA to run when ready. An `--activate` flag is explicitly deferred to a later slice.

### Options Considered

| Option | Verdict |
|--------|---------|
| Install creates objects, prints activation statement | Chosen — DBA owns the system-wide cutover; keeps install safe |
| Auto-activate on install | Rejected — too high blast radius; wrong ownership |
| Add `--activate` flag now | Rejected — deferred; out of scope for this bedrock slice |

### Consequences

Activation is a documented manual step. The printed statement is the exact command the DBA needs. Future work can add `--activate` as an explicit opt-in. The install verb is safe to use in automated pipelines without risk of accidentally enabling a system-wide preprocessor.

---

## ADR-004: MASTER ships as a guarded fail-open skeleton, not a bare pass-through

**Date:** 2026-06-05
**Plan:** `add-install-two-schema-layout`
**Status:** Accepted

### Context

Once a preprocessor is active in enforced mode it intercepts every SQL statement on the system. A skeleton that ships without its safety contract — guard `ALTER SESSION/SYSTEM`, fail open on lookup errors, correct `setsqltext`/`gsub` multi-return handling — forces dispatch logic to be layered onto an unsafe base. If the safety rules are added later and any of them are wrong, the system preprocessor can block all statements or corrupt query text.

### Decision

The MASTER skeleton already implements the full safety contract before any dispatch logic exists: it guards `ALTER SESSION` and `ALTER SYSTEM` (returns early, no lookup, no rewrite), fails open on any pipeline view query error (returns original `sqltext` unchanged), handles `gsub` multi-return correctly, and passes luacheck with zero warnings.

### Options Considered

| Option | Verdict |
|--------|---------|
| Ship skeleton with full guard and fail-open contract | Chosen — establishes and tests the safety rules before dispatch is layered on top |
| Bare `return getsqltext()` skeleton; add safety rules with dispatch | Rejected — defers the correctness-critical contract to a later, higher-complexity slice |

### Consequences

The guard and fail-open behavior is tested on the skeleton; dispatch logic is layered onto an already-safe base. The safety contract is encoded and verifiable from day one of the preprocessor being activatable. Future dispatch work has no excuse to skip the guard rules — they are already present.

---

## ADR-005: Refresh reads the dictionary via SELECT ANY DICTIONARY granted to PREPROC_ADMIN; gated to admins, never PUBLIC

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

`EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS` — the dictionary tables the refresh script needs to expand role grants — are visible only to holders of `SELECT ANY DICTIONARY`. Exasol Lua scripts run with invoker rights, so the refresh script can read those tables only if the *caller* holds the privilege. The privilege must not be granted to PUBLIC, as that would expose the full user and role-grant topology to every database user.

### Decision

Grant `SELECT ANY DICTIONARY` to the `PREPROC_ADMIN` role only. The `PREPROC.REFRESH` and `PREPROC.STATUS` scripts are executable only by holders of `PREPROC_ADMIN` (or `EXECUTE ANY SCRIPT` / DBA). Both scripts are administrative operations callable from any SQL client — by an admin.

### Options Considered

| Option | Verdict |
|--------|---------|
| Grant `SELECT ANY DICTIONARY` to `PREPROC_ADMIN`; gate scripts to admins | Chosen — minimal, auditable privilege bridge that keeps security topology private |
| Definer-rights view over dictionary exposed to PUBLIC | Rejected — leaks the database's role/user topology to every user |
| Per-table SELECT grants to PUBLIC | Rejected — same security leak as above |
| Make refresh callable by any user | Rejected — privilege escalation surface |

### Consequences

Administration is admin-only: "callable from any SQL client" is honoured for callers who hold the admin role. The `SELECT ANY DICTIONARY` grant is a documented, auditable part of the install DDL. Future dictionary-reading scripts follow the same pattern.

---

## ADR-006: Transitive role expansion is native SQL CONNECT BY, orchestrated by Lua

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

Resolving CONFIG rules scoped to a role requires expanding that role to all users who hold it directly or through nested role grants. This requires traversing `EXA_DBA_ROLE_PRIVS` transitively. A prior plan version incorrectly claimed Exasol's `CONNECT BY` hierarchical query was a separately-licensed add-on and chose iterative Lua graph-walking instead. That premise was factually wrong — confirmed by the product owner (Exasol Lead PM for the core database).

### Decision

Build the transitive role-to-user closure with a SQL `CONNECT BY PRIOR` / `START WITH` hierarchical query over `EXA_DBA_ROLE_PRIVS`. The Lua refresh script issues this query, joins the closure against enabled `PREPROC.CONFIG`, and writes `RESOLUTION`. The Lua does not iterate the grant graph itself.

### Options Considered

| Option | Verdict |
|--------|---------|
| Native SQL `CONNECT BY` over `EXA_DBA_ROLE_PRIVS` | Chosen — native, unlicensed Exasol SQL; declarative closure beats hand-rolled traversal |
| Iterative Lua graph-walking | Rejected — more code, slower, based on false premise that `CONNECT BY` was a paid add-on |
| `WITH RECURSIVE` CTE | Rejected — Exasol does not support recursive CTEs |

### Consequences

The transitive closure is expressed in a single SQL statement. The Lua script orchestrates (owns the transaction, executes the query) but does not re-implement graph traversal. Any future expansion of resolution logic works within the same SQL-first approach.

---

## ADR-007: All framework administration is DB-side; the Python CLI is deployment-only

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

A prior plan version added `preproc refresh` and `preproc status` Python verbs as thin `EXECUTE SCRIPT` wrappers. The product owner rejected runtime administration in Python. The strategic direction is for the framework to own its management surface end-to-end, eventually intercepting and controlling its own management syntax from within the preprocessor.

### Decision

Refresh and status are `PREPROC_ADMIN`-owned DB-side objects (`PREPROC.REFRESH`, `PREPROC.STATUS`), callable from any SQL client by an admin. The Python CLI keeps only deployment — applying DDL and deploying the Lua scripts during `preproc install`. No `preproc refresh` / `preproc status` runtime verbs exist.

### Options Considered

| Option | Verdict |
|--------|---------|
| DB-side scripts; Python CLI is deployment-only | Chosen — keeps management surface in the database; enables future framework-controlled management syntax |
| Thin Python `EXECUTE SCRIPT` wrapper verbs | Rejected — owner directive: Python should not manage the environment at runtime |
| Split Python/DB administration | Rejected — inconsistent; forecloses the future-syntax direction |

### Consequences

Routine administration requires a SQL client, not a Python environment. The management surface is entirely within the database, enabling the planned future feature of the preprocessor intercepting its own management statements. The Python CLI remains a deployment-only tool with no runtime state.

---

## ADR-008: DB-side admin scripts named and shaped for future management-syntax mapping

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

The mission roadmap includes a future framework-controlled management syntax (e.g. a preprocessor-intercepted `PREPROC REFRESH` statement). If the DB-side admin entry points are given ad-hoc names or contracts now, a future syntax layer must adapt around them or rename them — churning the admin surface and creating a migration burden.

### Decision

Treat `PREPROC.REFRESH` and `PREPROC.STATUS` as stable, named admin entry points with simple, syntax-mappable contracts: no required parameters for the common case; a result set reporting the outcome. This keeps the door open for a future intercepted management statement to translate cleanly onto these same DB objects.

### Options Considered

| Option | Verdict |
|--------|---------|
| Stable names with syntax-mappable contracts | Chosen — no implementation cost now; removes friction from the future syntax layer |
| Build the intercepted management syntax now | Rejected — out of scope for this slice; premature |
| Ad-hoc names / contracts | Rejected — would force a future syntax layer to rename or adapt, churning the admin surface |

### Consequences

`PREPROC.REFRESH` and `PREPROC.STATUS` are the stable, versioned entry points for the administration surface. Future slices implementing `PREPROC REFRESH` or `SHOW PREPROCESSOR STATUS` intercepted syntax can translate onto these without renaming. The contract discipline (no required params for common case, result set reporting outcome) is the binding interface.

---

## ADR-009: Additive UNION (SQL-privilege model) of ROLE + USER rules, deduplicated, ordered by rule_id ascending via explicit ordinal column

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

When a user holds multiple roles and/or has direct USER-scoped rules, there are multiple ways to combine them. A prior plan version said a USER-scoped rule for a phase replaces all ROLE-derived rules for that phase. The product owner rejected override/exclusion semantics as having no SQL-privilege analogue and being surprising to admins. A further question about scope ordering (role rules before user rules) was also rejected.

### Decision

A user's resolved pipeline is the additive UNION (superset) of every enabled ROLE-derived rule (via the transitive closure) and every enabled USER-scoped rule — modelled on SQL privileges. There is no override, replacement, or exclusion. Identical `(phase, script, function)` reaching a user by multiple paths is deduplicated to one row (lowest contributing `rule_id`). Within a phase, ordering is pure `rule_id` ascending with scope type ignored, recorded in a new `PREPROC.RESOLUTION.ordinal` column; `MY_PIPELINE` orders by `phase, ordinal`.

### Options Considered

| Option | Verdict |
|--------|---------|
| Additive UNION; deduplicated; `rule_id`-ascending ordinal | Chosen — SQL-privilege semantics; no surprising exclusions; admin controls order via `rule_id` assignment |
| USER-scoped rule overrides ROLE-derived rules for the phase | Rejected by product owner — no SQL-privilege analogue; surprising for admins |
| Role-rules-first ordering within a phase | Rejected on follow-up — owner wanted scope-agnostic `rule_id` order |
| Carry `rule_id` through to the view and order by it | Rejected — `MY_PIPELINE` should not expose internal `rule_id`s |
| Hard-fail on conflicting rules | Rejected — violates the fail-open invariant |

### Consequences

Admins control execution sequence through the `rule_id` values they assign; scope type is irrelevant to order. The `ordinal` column is the view-friendly carrier of that order without leaking `rule_id`. To make a user execute less, the admin removes roles, disables rules, or defines narrower rules — never a per-user subtraction. The deduplication rule (one row per `(phase, script, function)` per user) prevents the same function running twice.

---

## ADR-010: Atomic full rebuild via DELETE + reinsert + single commit in the refresh script

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

Resolution is materialised offline. During a rebuild, intermediate states (empty table, partial insert) must not be observable by concurrent readers. A Lua script can issue an explicit `commit()` regardless of the client's autocommit setting; this provides the required atomicity guarantee.

### Decision

The refresh script deletes all rows from `RESOLUTION`, inserts the freshly resolved rows, writes `last_refresh` and the state fingerprint to `SETTINGS`, then commits once. Either the new resolution and its timestamp are both visible, or neither is.

### Options Considered

| Option | Verdict |
|--------|---------|
| Full DELETE + reinsert + single `commit()` | Chosen — simplest correct semantics; drops orphans for free; atomic visibility |
| MERGE / upsert without delete | Rejected — would not drop orphaned rows from removed or disabled rules |
| Per-statement autocommit | Rejected — leaves a half-rebuilt resolution observable to concurrent readers |

### Consequences

Full rebuild is idempotent and correct regardless of prior state. Running refresh twice produces the same result. Orphaned rows from removed rules are automatically eliminated. A failed refresh (script error before commit) leaves the previous resolution intact.

---

## ADR-011: Phase taxonomy TRANSLATE → EXPAND → REWRITE, cut on whether input is already valid Exasol SQL

**Date:** 2026-06-05
**Plan:** `add-refresh-role-resolution`
**Status:** Accepted

### Context

The prior phase taxonomy — TRANSLATE → REWRITE → SUGAR — had the owner disliking "SUGAR" and the REWRITE/SUGAR boundary was a fuzzy terminal-vs-commutative axis rather than an observable property of the input. A cleaner axis was needed that also aligns with the safety contract (different fail-open sensitivity depending on phase).

### Decision

Replace TRANSLATE → REWRITE → SUGAR with TRANSLATE → EXPAND → REWRITE, cut on whether the input was already valid Exasol SQL:
- **TRANSLATE** — foreign-dialect statements transpiled to Exasol SQL (unchanged; max 1/user; fail-closed).
- **EXPAND** — input is NOT valid Exasol SQL; transforms make new syntax runnable. Absorbs both the old REWRITE (whole-statement custom commands) and the old SUGAR (token-level conveniences). Terminal-vs-commutative is now a function-contract rule within the phase. Fail-open is safe: an un-expanded statement dies in the engine with a loud syntax error.
- **REWRITE** (redefined) — input IS valid Exasol SQL; transforms modify behaviour (policy injection, table redirection, guardrails). Currently a defined-but-empty slot. Fail-open-sensitive: an un-applied rewrite silently runs the original statement = silent policy bypass.
- EXPAND precedes REWRITE so behaviour-modifying rules always operate on valid, fully-expanded SQL.

### Options Considered

| Option | Verdict |
|--------|---------|
| TRANSLATE → EXPAND → REWRITE, cut on valid-SQL axis | Chosen — crisp testable boundary; aligns with fail-open safety contract; removes disliked "SUGAR" |
| Keep TRANSLATE → REWRITE → SUGAR | Rejected — owner disliked "SUGAR"; boundary was fuzzy terminal-vs-commutative axis |
| Two-phase TRANSLATE → EVERYTHING-ELSE | Rejected — loses the fail-open-safe vs fail-open-sensitive boundary exposed by the valid-SQL axis |

### Consequences

The valid-SQL axis is a testable, observable property of the input. EXPAND fail-open is safe (syntax error); REWRITE fail-open is sensitive (silent bypass). All four examples in Exasol's official preprocessor docs land in TRANSLATE/EXPAND — confirming REWRITE as genuinely empty today. The phase taxonomy is now a mission-level definition stable across all future slices.

---

## ADR-012: EXPAND module return contract — nil = no match; string = match + terminal

**Date:** 2026-06-05
**Plan:** `add-expand-command-expansion`
**Status:** Accepted

### Context

EXPAND modules need a way to signal "not my command, keep looking" versus "I matched this command, here is the expansion." TRANSLATE and REWRITE modules use plain text threading (always return a string — the same or modified text), so their contract cannot be reused for EXPAND without ambiguity. The dispatch loop needs an unambiguous signal to continue iterating versus stopping. Additionally, a `pcall` failure in a module should not block the statement — EXPAND is fail-open.

### Decision

An EXPAND module function signals no-match by returning `nil`. It signals match-and-terminal by returning any non-nil string (the expansion text). A `pcall` failure is treated as no-match (fail-open): the failing rule is skipped and the next EXPAND rule is tried. A new `dispatch_expand_rule` helper in `master.lua` implements this protocol; the existing `dispatch_rule` (used for TRANSLATE and REWRITE) is left unchanged.

### Options Considered

| Option | Verdict |
|--------|---------|
| `nil` = no match; non-nil string = match + terminal; separate `dispatch_expand_rule` helper | Chosen — `nil` is idiomatic Lua for "absent"; separate helper keeps TRANSLATE/REWRITE logic untouched; clear naming |
| `false` = no match; string = match | Rejected — `false` is less natural Lua idiom for "this isn't mine" |
| Two-value tuple `matched, text` | Rejected — complicates the `pcall` result shape and requires destructuring the return |
| Generalize `dispatch_rule` with a mode parameter | Rejected — EXPAND semantics are different enough (nil propagation, terminal break) that a separate clearly-named helper is cleaner; mode parameter would add complexity with no benefit |

### Consequences

Every EXPAND module MUST return `nil` for non-match and a string for match; a module that always returns a string (even an empty one) would claim every statement as its own. The separate `dispatch_expand_rule` helper name is self-documenting and eliminates regression risk for the TRANSLATE/REWRITE path. The fail-open contract on pcall failure means a broken EXPAND module degrades gracefully rather than blocking the statement.

---

## ADR-013: client_pattern matching uses literal substring search, not Lua pattern matching

**Date:** 2026-06-05
**Plan:** `add-expand-command-expansion`
**Status:** Accepted

### Context

The `client_matches` function in `master.lua` filters dispatch rules by matching the connecting client identity against a rule's `client_pattern` column. The prior implementation used `string.find(current_client, tostring(client_pattern))` without the plain-flag argument, which activates Lua pattern matching. Client pattern values like `EXA.Client` or `ODBC (v1.2.3)` contain Lua magic characters (`.` matches any character; `(` and `)` are grouping metacharacters). An admin entering a literal substring would get silently wrong match behavior: `EXA.Client` as a Lua pattern matches `EXAXClient`, `EXAZCLIENT`, etc.

### Decision

Change `client_matches` to use `string.find(current_client, tostring(client_pattern), 1, true)` — the fourth argument `true` disables Lua pattern interpretation and forces plain (literal) substring search. This is a one-character-plus-argument change. Behavior is unchanged for `client_pattern` values that contain no Lua special characters.

### Options Considered

| Option | Verdict |
|--------|---------|
| `string.find(s, p, 1, true)` — literal substring | Chosen — matches the stated semantic ("does the client string contain this text literally?"); fixes latent bug for patterns with dots/parens/hyphens; zero-cost for patterns with no special chars |
| Leave as-is (Lua pattern mode) | Rejected — silently wrong for any `client_pattern` containing Lua magic characters; admins entering literal strings would get unexpected match behavior |

### Consequences

Admins who have already entered `client_pattern` values that inadvertently relied on Lua pattern behavior will see a behavior change, but this is a bug fix not a regression. All existing tests use `client_pattern` values with no Lua special characters, so test behavior is unchanged. Future documentation should describe `client_pattern` as a literal substring, not a regular expression.

---

## ADR-014: TRANSLATE trigger is an explicit dialect-marker comment, not always-transpile

**Date:** 2026-06-05
**Plan:** `add-demo-suite`
**Status:** Accepted

### Context

The TRANSLATE phase demo module must decide when to invoke the `sqlglot` transpile UDF. Always-transpiling every statement for holders of a TRANSLATE rule carries a ~133–151 ms per-statement cost (UDF VM startup, no warm-VM reuse across statements) and poses a correctness hazard: cross-dialect quote handling can silently corrupt already-valid Exasol SQL. A trigger heuristic is needed that is cheap, fail-closed-friendly, and legible to the demo reader.

### Decision

Trigger translation only when a statement begins with the explicit marker comment `--!dialect:<name>` (e.g. `--!dialect:tsql`). The module strips the marker, calls the UDF with that dialect, and on success returns the transpiled text; on failure it returns the original text unchanged (fail closed). Statements without the marker are returned byte-for-byte unchanged without calling the UDF.

### Options Considered

| Option | Verdict |
|--------|---------|
| Explicit `--!dialect:<name>` marker comment | Chosen — cheap (only marked statements pay the UDF cost), fail-closed-friendly, self-documenting, demo-readable |
| Always-transpile for holders of a TRANSLATE rule | Rejected — ~133–151 ms/statement cost with no warm-VM reuse; cross-dialect quote-escaping can corrupt already-valid Exasol SQL |

### Consequences

Only statements the author explicitly marks as foreign-dialect incur the UDF cost. The marker doubles as documentation in the demo walkthrough. Production rules should be scoped narrowly via `client_pattern` to further limit who pays the cost. The fail-closed behavior (return original on any doubt) is the module's responsibility; MASTER's fail-open per-rule pcall is a separate, lower-level safety net.

---

## ADR-015: Fail-closed TRANSLATE via ERR: sentinel return from the UDF, never raise

**Date:** 2026-06-05
**Plan:** `add-demo-suite`
**Status:** Accepted

### Context

The TRANSLATE phase is fail-closed by mission contract: an un-applied translation must never let corrupted or wrong-dialect text reach the engine masquerading as successful Exasol SQL. The Python UDF needs to signal transpile failure to the calling Lua module in a way the module can act on. Raising an exception from the UDF is caught by MASTER's per-rule `pcall` fail-open, which would skip the rule and pass the original (possibly still-invalid) statement on — but the Lua module would have no opportunity to inspect the failure type and distinguish a real translation from a partial/corrupt one.

### Decision

The Python UDF returns the string prefix `ERR:<message>` (never raises) on any `sqlglot` parse or transpile failure. The Lua module inspects the return value: if it begins with `ERR:`, it returns the original statement text unchanged (fail closed). The `ERR:` sentinel is never threaded forward as statement text.

### Options Considered

| Option | Verdict |
|--------|---------|
| UDF returns `ERR:<message>` sentinel; module checks and fails closed | Chosen — gives the module a reliable in-band failure signal; module can distinguish failure from a legitimately short transpilation; never emits wrong-dialect or corrupted SQL |
| UDF raises on failure; MASTER pcall catches and fails open | Rejected — loses the ability to distinguish failure from a real (possibly short) translation; risks threading corrupted text forward; fails open instead of closed, violating the TRANSLATE phase contract |

### Consequences

The TRANSLATE module is the boundary where the fail-closed contract is enforced. Any UDF failure — parse error, transpile error, unexpected exception — results in the original statement being returned unchanged. Downstream phases see either a clean translation or the original SQL, never a partial or corrupt form. The sentinel prefix `ERR:` is a stable interface between the UDF and the Lua module.

---

## ADR-016: Bundle is generated and slash-delimited; never hand-edited; CLI install remains the dev/CI path

**Date:** 2026-06-05
**Plan:** `add-demo-suite`
**Status:** Accepted

### Context

Operators want a single file they can run against a clean Exasol instance without a Python environment. Exasol's `CREATE LUA SCRIPT` and `CREATE PYTHON3 SCALAR SCRIPT` statement bodies contain semicolons, so a naively semicolon-split SQL file would break those statements. A committed static bundle would drift from the `sql/` source tree over time. The CLI `install` verb already handles the dev/CI path.

### Decision

`preproc bundle` generates `deploy_all.sql` deterministically from the `sql/install/*.sql`, `sql/lua/*.lua`, and `sql/udf/*.sql` sources, terminating each statement with a `/` on its own line (EXAplus/DbVisualizer interactive-client format). The file is never hand-edited — the `sql/` tree is the single source of truth. The CLI `preproc install` remains the path for development and CI. The bundle omits the `ALTER SYSTEM` activation statement and all demo seed data.

### Options Considered

| Option | Verdict |
|--------|---------|
| Generated, slash-delimited `deploy_all.sql`; CLI install for dev/CI | Chosen — script bodies survive the `/` terminator; generation keeps `sql/` as single source of truth; reproducible and auditable |
| Semicolon-split single file | Rejected — script/UDF bodies contain semicolons; would break `CREATE LUA SCRIPT` and `CREATE PYTHON3 SCALAR SCRIPT` statements |
| Committed static bundle | Rejected — would drift from the `sql/` source tree as modules are added or changed |
| CLI-only deploy; no bundle | Rejected — operators want a single SQL file runnable without a Python environment |

### Consequences

The bundle is always derived from sources and is byte-for-byte reproducible. An operator can audit the exact SQL being applied. Regeneration is required after any change to the `sql/` tree. The `/` delimiter format ties the bundle to interactive Exasol clients (EXAplus, DbVisualizer); it is not directly runnable by pyexasol or JDBC drivers in script mode.

---

## ADR-017: Whole admin surface is scripted; direct CONFIG DML is unsupported by convention, not blocked

**Date:** 2026-06-06
**Plan:** `add-control-layer`
**Status:** Accepted

### Context

Every CONFIG/SETTINGS mutation (rule lifecycle, settings) needs input validation, rule_id collision avoidance, and consistent state. Exasol scripts run with invoker rights, so a definer-rights hard lock on admin tables cannot be enforced — a caller who already holds the DML privilege can write directly regardless. The question is whether to build a technical enforcement boundary or rely on convention backed by validation tooling.

### Decision

Every CONFIG/SETTINGS mutation is surfaced as a validating `EXECUTE SCRIPT` in the `PREPROC` schema. Direct DML stays technically possible but is declared unsupported in documentation. The scripted path is the obviously-correct one because it validates inputs, allocates rule_ids safely, and maintains referential consistency.

### Options Considered

| Option | Verdict |
|--------|---------|
| Scripted admin surface; direct DML unsupported by convention | Chosen — validation + convention makes the scripted path the obviously-correct one without pretending an enforcement boundary the engine cannot provide |
| Definer-rights wrapper to physically block direct DML | Rejected — invoker rights make a hard lock impossible; a caller who already needs the DML privilege can still bypass it |
| Leave DML as the primary path | Rejected — no validation, collision-prone rule_ids, one bad write impacts every resolved user |

### Consequences

Operators who bypass the control scripts lose validation, collision safety, and auto-refresh. The scripted path remains the documented and tested contract. The enforcement gap is explicit rather than hidden.

---

## ADR-018: Auto-refresh runs inside the mutating script, with a NOREFRESH opt-out

**Date:** 2026-06-06
**Plan:** `add-control-layer`
**Status:** Accepted

### Context

After any CONFIG mutation the RESOLUTION table is stale until REFRESH runs. The EXPAND contract (established by ADR-012) requires that a module expansion produce exactly one statement — so a command-sugar expansion cannot emit a second `EXECUTE SCRIPT PREPROC.REFRESH` statement after the mutating script. The operator must not be left responsible for remembering to refresh after every mutation.

### Decision

Each mutating control script rebuilds RESOLUTION in-script after a successful mutation unless the `NOREFRESH` parameter is set. Refresh logic is factored into a shared import so the atomicity/fingerprint-first contract from ADR-010 has a single source of truth.

### Options Considered

| Option | Verdict |
|--------|---------|
| Auto-refresh inside the mutating script with NOREFRESH opt-out | Chosen — the single-statement EXPAND constraint forces refresh into the script; NOREFRESH preserves efficient batch workflows |
| Emit a second `EXECUTE SCRIPT PREPROC.REFRESH` from the sugar expansion | Rejected — an EXPAND expansion must be exactly one statement; chaining is impossible under ADR-012 |
| Require the operator to remember to refresh | Rejected — leaves resolution silently stale after every mutation |

### Consequences

Mutation and refresh are atomic from the caller's perspective. Batch workflows that mutate many rules can use NOREFRESH and issue one explicit REFRESH at the end for efficiency. The shared refresh import becomes a load-bearing internal dependency that must be kept in sync with the standalone REFRESH script.

---

## ADR-019: Dogfooded EXPAND convenience module — one command maps to one EXECUTE SCRIPT

**Date:** 2026-06-06
**Plan:** `add-control-layer`
**Status:** Accepted

### Context

Operators need a human-readable way to issue admin commands from any SQL client without knowing the underlying script signatures. The framework already has a first-match-terminal EXPAND dispatch mechanism (ADR-012). The question is whether to build a separate CLI parser or produce a production EXPAND module that the framework dispatches like any other module.

### Decision

A production EXPAND module `PREPROC_RT.CONTROL_SUGAR_V1` parses `PREPROC …` keyword commands (e.g., `PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.DEMO_PING_V1 FUNCTION ping;`) and expands each to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call, reusing the existing first-match-terminal EXPAND contract (ADR-012).

### Options Considered

| Option | Verdict |
|--------|---------|
| Dogfooded EXPAND module expanding to one EXECUTE SCRIPT | Chosen — demonstrates the framework on itself; sugar works from any SQL client; one-statement contract intact because refresh runs inside the target script |
| Separate CLI-only command parser | Rejected — the framework should eat its own dog food; sugar would be unavailable to non-CLI SQL clients |
| Multi-statement expansion | Rejected — EXPAND output must be a single statement per ADR-012 |

### Consequences

`CONTROL_SUGAR_V1` is the first non-demo dispatched module in production. Its presence in the dispatch table demonstrates the framework's live-replace versioning discipline. Any new admin command requires updating the sugar module and its spec scenarios.

---

## ADR-020: Installer seeds the convenience EXPAND rule via direct DML (sanctioned bootstrap)

**Date:** 2026-06-06
**Plan:** `add-control-layer`
**Status:** Accepted

### Context

ADR-017 declares operator direct CONFIG DML unsupported. However, the sugar module that would let an operator register rules via `PREPROC ADD RULE …` cannot register itself — it does not exist in the dispatch table until the installer runs. This is a bootstrapping dependency: the registration command is unavailable before the rule exists.

### Decision

Install adds a guarded `WHERE NOT EXISTS` CONFIG row registering the `CONTROL_SUGAR_V1` EXPAND rule scoped to `PREPROC_ADMIN`. This is installer/seed direct DML, which is the already-sanctioned bootstrap path (mirroring the demo seed pattern); it is distinct from operator direct DML, which remains unsupported.

### Options Considered

| Option | Verdict |
|--------|---------|
| Installer seeds the rule via direct DML with WHERE NOT EXISTS guard | Chosen — mirrors the demo seed pattern; the only viable bootstrap path |
| Ship a separate "enable-sugar" command | Rejected — chicken-and-egg: the registration command does not exist until the sugar rule is registered |

### Consequences

The distinction between installer DML (sanctioned bootstrap) and operator DML (unsupported) is explicit in ADR-017 and here. The WHERE NOT EXISTS guard makes the seed idempotent. Any future re-install is safe.

---

## ADR-021: Profile generation is eager (at refresh time) under admin context — not lazy/on-demand by MASTER

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

MASTER runs with invoker rights (ADR-001). An ordinary database user lacks `CREATE SCRIPT` in `PREPROC_RT`. Therefore MASTER physically cannot create scripts for that user on demand. On-demand creation by MASTER is impossible without architectural changes that would violate ADR-001. Python CLI is deployment-only per ADR-007. Profile scripts (`PREPROC_RT.P_<hash>`) must exist before sessions can swap to them.

### Decision

Profile scripts are generated by `PREPROC.REFRESH_CORE.rebuild()` running under the `PREPROC_ADMIN` invoker context. MASTER never creates a profile script on demand.

### Options Considered

| Option | Verdict |
|--------|---------|
| Eager generation at refresh time under admin context | Chosen — the only viable path given invoker-rights constraint |
| On-demand creation by MASTER | Rejected — invoker rights block CREATE SCRIPT for ordinary users (ADR-001) |
| Python CLI generates profiles | Rejected — Python CLI is deployment-only per ADR-007 |

### Consequences

Profile scripts are always current as of the last refresh. MASTER can only swap to hashes that were current at the last refresh. A session's first statement always pays the full lookup cost; subsequent statements pay zero (profile) or reduced (~0.5 ms) overhead after the swap.

---

## ADR-022: Generational retire-then-reclaim cleanup; same-refresh drop of orphaned profiles is provably unsafe

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

A session can only be pinned to `P_<hash>` if it swapped to that hash while the hash was current. Once a refresh commits a new RESOLUTION, MY_PIPELINE returns only current hashes, so no new session can acquire the old hash. The set of sessions potentially pinned to an orphan is fixed at orphan-time and only shrinks. Dropping on the same refresh that orphans a profile cannot be proven safe — a just-pinned session is still active and dropping its script causes every subsequent statement to fail with a catastrophic lockout.

### Decision

Orphaned profiles are cleaned up in two generations inside `REFRESH_CORE.rebuild()`. (1) **Retire:** on the orphaning refresh, replace the profile body with a retirement stub and stamp `orphaned_at`. (2) **Reclaim:** on a later refresh, physically `DROP SCRIPT` and delete the PROFILES row only once provably unpinned (ADR-023 criterion).

### Options Considered

| Option | Verdict |
|--------|---------|
| Generational retire-then-reclaim | Chosen — inherently safe; deferred reclaim allows pinned sessions to drain safely |
| Same-refresh drop | Rejected — catastrophic lockout for pinned sessions; equivalent hazard to ADR-002 enforced-mode grant omission |
| Never reclaim / tombstone-forever | Rejected — unbounded accumulation of profile scripts and PROFILES rows |
| Separate out-of-band cleanup job | Rejected — adds operational surface and a second place to get the race wrong |

### Consequences

Profile scripts accumulate until the next refresh after all sessions that might be pinned have ended. The retire step ensures no session is ever left pointing at a missing script. Cleanup is fully automated within the existing atomic rebuild; no operator action is required.

---

## ADR-023: MASTER dispatches statement 1 fully, then issues the ALTER SESSION swap before setsqltext

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

In a convenience-mode session, MASTER needs to dispatch the first statement through the pipeline AND issue the ALTER SESSION that repoints the session to the optimized target. The ordering of dispatch, swap, and setsqltext must be defined precisely. The ALTER SESSION pquery from inside a preprocessor is not itself preprocessed (no recursion risk), confirmed by the MASTER header comment.

### Decision

On statement 1, MASTER: (a) reads MY_PIPELINE, (b) dispatches the statement through the full pipeline, (c) issues the ALTER SESSION pquery to swap/self-disable, (d) writes the transformed text via setsqltext. The swap is attempted after dispatch and before setsqltext.

### Options Considered

| Option | Verdict |
|--------|---------|
| Dispatch → swap → setsqltext | Chosen — statement 1 gets full pipeline treatment; swap takes effect for statement 2 onward |
| Issue ALTER SESSION before dispatching statement 1 | Rejected — would skip statement 1's transformation |
| Issue ALTER SESSION after setsqltext | Rejected — no functional difference, but ordering before setsqltext is cleaner |

### Consequences

Every session using a convenience-mode preprocessor pays the full MASTER lookup and dispatch cost exactly once (statement 1), then pays zero or ~0.5 ms for all subsequent statements. The transformation of statement 1 is never skipped.

---

## ADR-024: profile_hash computed in SQL at refresh time and stored in RESOLUTION; MY_PIPELINE surfaces it

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

MASTER must name the target profile script `PREPROC_RT.P_<hash>` during the swap. Computing the hash in Lua at runtime introduces a Lua/SQL hash-mismatch hazard (different algorithms, encoding, or sort order). A separate lookup to an admin table would violate the single-lookup design.

### Decision

`REFRESH_CORE.rebuild()` computes `profile_hash` per user as an MD5 over the ordered `phase|ordinal|script_name|function_name|client_pattern` chain using Exasol's `HASH_MD5` SQL function. The hash is stored in `PREPROC.RESOLUTION` (new nullable column). `PREPROC_RT.MY_PIPELINE` exposes it. MASTER reads the hash from the single MY_PIPELINE result and uses it directly.

### Options Considered

| Option | Verdict |
|--------|---------|
| SQL-computed hash stored in RESOLUTION; surfaced via MY_PIPELINE | Chosen — eliminates Lua/SQL hash-mismatch hazard; compatible with single-lookup design; no new admin-table reads |
| MASTER re-hashes rule rows in Lua | Rejected — Lua/SQL hash-mismatch hazard |
| Separate per-user mapping table | Rejected — new object; no benefit over RESOLUTION column |

### Consequences

The hash is a single source of truth computed in one place (SQL, at refresh time). MASTER's invoker-rights constraint is respected — no new admin table access is needed. The all-NULL sentinel for no-rule users (NULL `profile_hash`) is the correct signal for the self-disable path.

---

## ADR-025: Profile script Lua body bakes a static rule table; reproduces full MASTER semantics; issues no MY_PIPELINE call

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

The entire point of profile scripts is to eliminate the per-statement DB read overhead. If the profile script re-reads RESOLUTION or MY_PIPELINE, the overhead saving is lost. The script must reproduce full MASTER dispatch semantics to ensure correctness parity.

### Decision

Each `PREPROC_RT.P_<hash>` script is generated Lua that hardcodes the user's resolved `(phase, ordinal, script_name, function_name, client_pattern)` chain as a static Lua table. The script reproduces full MASTER dispatch logic (control-statement guard, EXA_ALL_SESSIONS client lookup, `client_matches` literal-substring helper, per-rule pcall fail-open, EXPAND first-match-terminal, TRANSLATE → EXPAND → REWRITE phase order, setsqltext-on-change) and issues NO pquery to MY_PIPELINE or any admin table.

### Options Considered

| Option | Verdict |
|--------|---------|
| Baked static rule table; no MY_PIPELINE lookup | Chosen — achieves ~0.5 ms/stmt goal; hash naming ensures idempotent CREATE OR REPLACE |
| Profile script calls MASTER with a user-id hint | Rejected — does not eliminate the DB read |
| Profile script re-reads RESOLUTION for one user | Rejected — still a DB read per statement; not ~0.5 ms |

### Consequences

Profile scripts are intentionally divergent from MASTER in one way: they bake a static rule table and issue no lookup. This is their purpose. Semantic divergence from MASTER's dispatch helpers is a hazard that must be managed by generating the profile Lua to mirror MASTER's helpers exactly.

---

## ADR-026: Hash manifest includes client_pattern and ordinal, not just module@version.function

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

Two users with identical function lists but different `client_pattern` values on their rules dispatch differently — one rule fires for client A but not client B. Hashing only the function list would collide them onto the same profile, producing wrong dispatch. Similarly, two users with the same functions in different ordinal order execute them in different sequences.

### Decision

The profile hash manifest is over the ordered tuple `(phase, ordinal, script_name, function_name, client_pattern)` for each row in the user's resolved chain, concatenated in ordinal order. Both `client_pattern` and `ordinal` are included because they affect dispatch behavior.

### Options Considered

| Option | Verdict |
|--------|---------|
| Hash ordered `(phase, ordinal, script_name, function_name, client_pattern)` | Chosen — avoids incorrect collisions on client_pattern and ordinal differences |
| Hash only `(script_name, function_name)` tuples | Rejected — collapses users with different client_pattern or ordinal onto the same profile; produces wrong dispatch |

### Consequences

Two users with the same functions in different ordinal order or with different client_pattern values on any rule will always receive distinct profile scripts. Profile count grows proportionally to distinct chain manifests, not just distinct function sets.

---

## ADR-027: Profile codegen atomicity — CREATE OR REPLACE before COMMIT in REFRESH_CORE

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

A committed RESOLUTION row referencing a `profile_hash` whose script does not yet exist would leave MASTER in a state where it cannot swap a session (the target script is missing). This is analogous to the enforced-mode lockout hazard from ADR-002. DDL in Exasol Lua (pquery CREATE OR REPLACE) is visible to subsequent pquery calls in the same session without requiring a separate commit.

### Decision

`REFRESH_CORE.rebuild()` runs profile script creation (CREATE OR REPLACE, pquery) and PROFILES table upsert inside the same transaction as the RESOLUTION DELETE/INSERT. All of this commits in the single COMMIT at the end of rebuild(). Profile scripts must exist before the COMMIT that makes the `RESOLUTION.profile_hash` column values visible to readers.

### Options Considered

| Option | Verdict |
|--------|---------|
| CREATE OR REPLACE inside the rebuild transaction, before COMMIT | Chosen — eliminates the committed-RESOLUTION-without-script window; ADR-010 atomicity preserved |
| Generate profiles in a separate post-commit step | Rejected — creates a window where RESOLUTION references a non-existent script |

### Consequences

The atomicity invariant from ADR-010 now covers profile script existence. A reader who sees a RESOLUTION row with a non-NULL `profile_hash` can rely on that profile script existing in `EXA_ALL_SCRIPTS`.

---

## ADR-028: Generational retire-then-reclaim step order: generate → retire → reclaim inside the ADR-010 atomic envelope

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

The generate, retire, and reclaim phases must run in a specific order to satisfy safety invariants. Generate must run before retire so a re-adopted hash is restored to its real body before the retire phase could re-retire it. All three phases must run before the single COMMIT to preserve ADR-010 atomicity.

### Decision

Fixed step order in `REFRESH_CORE.rebuild()`: fingerprint → DELETE/INSERT RESOLUTION → generate (CREATE OR REPLACE real bodies for current chains; clear `orphaned_at` on re-adoption) → retire (stub + stamp `orphaned_at` for newly-orphaned hashes) → reclaim (DROP SCRIPT + delete PROFILES row for provably-unpinned retired hashes) → stamp SETTINGS → single COMMIT. All phases run before COMMIT.

### Options Considered

| Option | Verdict |
|--------|---------|
| generate → retire → reclaim before single COMMIT | Chosen — re-adoption is handled correctly; all DDL is atomic; no committed RESOLUTION row can reference a dropped script |
| Retire before generate | Rejected — a re-adopted hash would be retired and then generated, leaving it as a stub when it should be the real body |
| Post-commit reclaim | Rejected — violates ADR-010; creates a window where RESOLUTION references a dropped script |

### Consequences

The fixed ordering is a load-bearing invariant. Any future phase added to rebuild() must be slotted into this sequence with care. Re-adopted hashes are always restored to their real bodies, never left as stubs.

---

## ADR-029: Reclaim safety criterion — drop only when no active session logged in before orphaned_at

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

Any session that could be pinned to `P_<hash>` must have logged in before the refresh that orphaned it (hence before `orphaned_at`). If every active session logged in at or after `orphaned_at`, none can be pinned, so the drop is provably safe. The criterion is conservative: a session that logged in before `orphaned_at` but was never pinned to `<hash>` merely delays the reclaim — it never causes an unsafe drop.

### Decision

A retired profile is reclaimable iff no active session in `EXA_ALL_SESSIONS` has `LOGIN_TIME < orphaned_at`. If there are zero active sessions, all retired profiles are reclaimable. `LOGIN_TIME` is read from `EXA_ALL_SESSIONS`; the `SELECT ANY DICTIONARY` privilege already granted to `PREPROC_ADMIN` (ADR-005) covers this.

### Options Considered

| Option | Verdict |
|--------|---------|
| `no active LOGIN_TIME < orphaned_at` | Chosen — provably safe; needs no new bookkeeping; within existing privilege model |
| Track exact pinned-hash per session | Rejected — Exasol does not expose the session's current preprocessor script reliably; would still race |
| Reference-count pins | Rejected — no atomic hook to decrement on session end |
| Time-based TTL | Rejected — arbitrary; can drop while a long-lived pinned session is still active |

### Consequences

Cleanup latency is bounded by the session lifetime of the longest-lived session that logged in before `orphaned_at`. This is acceptable — delaying cleanup is safe; dropping unsafely is not. The criterion needs no new tables or triggers.

---

## ADR-030: Retirement stub re-runs full MASTER-equivalent resolve→dispatch→swap for the current statement; MASTER is the single canonical inline dispatcher

**Date:** 2026-06-08
**Plan:** `add-zero-overhead-session-paths`
**Status:** Accepted

### Context

A session pinned to a retired profile needs to re-run correctly for its next statement. An earlier design used a pass-through + bounce approach that ran the triggering statement untransformed (silently skipping pipeline rules including fail-open-sensitive REWRITE rules). The product owner rejected this. A MASTER_CORE shared-module approach was also explored but abandoned when it was discovered that Exasol dynamic `import()` works only at top-level preprocessor scope — a thin import-wrapper silently fail-opens all dispatch (the imports happen outside any function, so the imported module's functions are never called in dispatch context).

### Decision

The retirement stub is functionally identical to MASTER. The refresh path sources the stub body from `PREPROC_RT.MASTER`'s own `SCRIPT_TEXT` in `EXA_ALL_SCRIPTS`, strips the leading `CREATE ... AS` header, and deploys the remainder verbatim as the stub. MASTER itself remains the single canonical inline dispatcher with all resolve→dispatch→swap logic implemented inline in `sql/lua/master.lua` — no `PREPROC_RT.MASTER_CORE` module exists.

### Options Considered

| Option | Verdict |
|--------|---------|
| Stub sourced from MASTER's catalog body (stub == MASTER by construction) | Chosen — single source of truth; no divergence hazard; automatically picks up dispatch+swap changes |
| Pass-through + bounce (run statement untransformed, then re-point) | Rejected by product owner — runs triggering statement untransformed, silently skipping pipeline rules |
| Extract MASTER_CORE shared import module | Rejected — Exasol `import()` works only at top-level preprocessor scope; import-wrapper silently fail-opens all dispatch |
| Bake a third copy of dispatch logic into the stub template | Rejected — re-introduces the divergence hazard from ADR-025 |

### Consequences

Every retirement stub is byte-for-byte MASTER's body. A change to the dispatch+swap contract in `master.lua` automatically takes effect for all stubs created or refreshed afterward — no separate stub-codegen change needed. MASTER's inline design is a documented constraint, not an implementation detail.

## ADR-031: A rule scoped to the PUBLIC role resolves to every user via a synthetic closure edge

**Date:** 2026-06-09
**Plan:** `(direct bug fix — PUBLIC role resolution)`
**Status:** Accepted

### Context

A `ROLE`-scoped CONFIG rule whose `scope_value` is `PUBLIC` resolved to nobody. The resolution closure in `PREPROC.REFRESH_CORE` walks `EXA_DBA_ROLE_PRIVS`, but every user's membership in the built-in `PUBLIC` role is implicit and automatic — it is never materialised as a row in `EXA_DBA_ROLE_PRIVS` (verified live: zero rows mention `PUBLIC`, though `PUBLIC` is a real role in `EXA_ALL_ROLES`). So the `CONNECT BY` closure never reached any user from a `PUBLIC`-scoped rule, and the rule silently applied to no one — contrary to the meaning of PUBLIC (applies to all users, unconditionally).

### Decision

The closure source UNIONs in a synthetic `'PUBLIC' -> user` edge for every user in `EXA_DBA_USERS` before the `CONNECT BY`. PUBLIC then behaves exactly like a role every user holds: a rule scoped to `PUBLIC` (origin_role = `'PUBLIC'`) resolves to all users, and any real role granted *to* `PUBLIC` fans out to all users transitively because its chain reaches the synthetic PUBLIC node.

### Options Considered

| Option | Verdict |
|--------|---------|
| Synthetic `PUBLIC -> user` edges UNIONed into the closure source | Chosen — one mechanism handles both direct PUBLIC-scoped rules and roles-granted-to-PUBLIC; no special-casing in the JOIN or in MASTER |
| Special-case `cfg.scope_value = 'PUBLIC'` with a separate cross-join to all users | Rejected — a second resolution path to maintain; misses roles transitively granted to PUBLIC |
| Expand PUBLIC in Lua / the MASTER dispatcher | Rejected — resolution is a refresh-time, SQL-side concern; MASTER must stay a thin reader of the materialised RESOLUTION |

### Consequences

`PUBLIC` is now a supported, documented scope that applies a rule to all users unconditionally. The fingerprint inputs are unchanged, so STATUS staleness detection is unaffected. The synthetic edges add one row per user to the closure input — negligible at any realistic user count.

---

## ADR-032: New `ergonomics/` spec domain rather than placing under `dispatch/`

**Date:** 2026-06-10
**Plan:** `add-ergonomics-cast-trailing-comma`
**Status:** Accepted

### Context

The `dispatch/` domain covers framework dispatch mechanics — how phases work, how rules are dispatched, how audit rewrites apply. The two new ergonomics features (`cast-shorthand`, `trailing-comma`) are user-facing SQL conveniences delivered through the examples layer. Placing them under `dispatch/` would blur the boundary between framework internals and end-user SQL sugar. An `examples/` domain was also considered, but "examples" describes the delivery vehicle, not the capability; future non-example ergonomics features (if promoted to core install) should land in the same domain.

### Decision

Create a new `ergonomics/` top-level domain for the two new feature specs (`cast-shorthand`, `trailing-comma`), rather than adding them under the existing `dispatch/` domain.

### Options Considered

| Option | Verdict |
|--------|---------|
| New `ergonomics/` domain | Chosen — makes the user-facing-convenience vs dispatch-mechanics boundary explicit; keeps `dispatch/` coherent; future ergonomics features land in the same domain regardless of delivery vehicle |
| Place under `dispatch/` | Rejected — `dispatch/` covers framework mechanics; ergonomics is a distinct concern |
| New `examples/` domain | Rejected — names the delivery vehicle, not the capability; would exclude future core-install promotions |

### Consequences

The `ergonomics/` domain is the canonical home for user-facing SQL convenience transforms. The `dispatch/` domain stays focused on framework dispatch mechanics. Any future ergonomics feature — whether delivered via `examples/` or promoted to core install — belongs in `ergonomics/`.

---

## ADR-033: Conservative supported-operand subset for the cast shorthand scanner

**Date:** 2026-06-10
**Plan:** `add-ergonomics-cast-trailing-comma`
**Status:** Accepted

### Context

The `ERGONOMICS_CAST_V1` module must scan backwards from each `::` token to find the left operand boundary. A wrong boundary silently corrupts SQL — the most dangerous failure mode in a preprocessing context. A fully general expression parser (tracking all operator precedences, subquery syntax, CASE, ARRAY, etc.) would be complex and still prone to edge cases. The module needs a documented, testable subset that covers the overwhelming majority of practical usage and fails closed on anything outside that subset.

### Decision

The backwards operand scan supports a specific, documented subset of primary expression forms: unqualified and dotted identifiers, parenthesised expressions, function calls (including schema-qualified UDF calls), string and numeric literals, and chained CAST results. Any `::` whose left operand is not in this subset causes the module to return the original text unchanged for the entire statement.

### Options Considered

| Option | Verdict |
|--------|---------|
| Conservative documented subset; fail closed on unrecognised operand | Chosen — wrong operand boundary silently corrupts SQL; the supported subset covers the overwhelming majority of practical `::` usage; failing closed on unusual forms is far preferable to emitting `CAST(wrong_expr AS type)` |
| Fully general expression parsing | Rejected — complex, still prone to edge cases, out of scope for a single-pass Lua scanner |

### Consequences

The supported-operand subset is explicitly documented, so module authors know when to write `CAST(expr AS type)` explicitly instead. A `::` with an unrecognised left operand causes the module to return the original statement unchanged — it never emits a partial or corrupted rewrite. The scope limitation is a documented, testable contract.

---

## ADR-034: Trailing-comma recognition: structural `comma→)` plus conservative keyword set

**Date:** 2026-06-10
**Plan:** `add-ergonomics-cast-trailing-comma`
**Status:** Accepted

### Context

`ERGONOMICS_TRAILING_COMMA_V1` must decide which commas are trailing commas and safe to remove. A purely structural rule (`comma → )`) is provably safe but misses the most common real-world case (`SELECT a, b, FROM t`). A full SQL grammar parser reintroduces the ~133–151 ms/statement UDF latency that motivated pure-Lua modules in the first place. A larger keyword set increases the risk of false positives on column aliases that happen to be reserved words.

### Decision

Two pattern classes are combined: (1) structural `comma → )` (always unambiguous), and (2) keyword-terminated: `comma → FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|UNION|INTERSECT|EXCEPT|INTO|SET|ON|RETURNING` (case-insensitive, word-boundary-guarded). Commas outside these two patterns are never removed.

### Options Considered

| Option | Verdict |
|--------|---------|
| Structural `comma → )` plus conservative keyword set | Chosen — structural rule is provably safe; keyword set covers the dominant `SELECT a, b, FROM t` use case; word-boundary guard prevents false positives on identifier prefixes; conservative stance matches the additive-only governing guardrail |
| Structural-only (`comma → )`) | Rejected — safe but misses `SELECT a, b, FROM t`, the most common user need |
| Full SQL grammar parser | Rejected — requires sqlglot UDF; reintroduces ~133–151 ms/statement latency |
| Larger keyword set | Rejected — higher false-positive risk on column aliases that happen to be reserved words |

### Consequences

The two recognition patterns are the documented, testable contract for the module. Commas outside the two patterns are left intact — the module errs on the side of leaving ambiguous commas rather than removing a syntactically required one. The word-boundary guard prevents false positives on identifiers such as `from_date` (FROM prefix) and `orders.id` (ORDER prefix), as verified by spot-check tests.

---

## ADR-035: SYS escape-hatch policy — full-closure exclusion, SYS-only, default grouped

**Date:** 2026-07-17
**Plan:** `add-sys-grouping-toggle`
**Status:** Accepted

### Context

BUG-002's onboarding fix seeded the control-sugar bootstrap for both `PREPROC_ADMIN` and `DBA`, so `SYS` — which cannot hold `PREPROC_ADMIN` but does hold `DBA` — can use the `PREPROC …` sugar. That fix left a policy tail: because resolution walks the role-grant closure and `SYS` holds `DBA`, every `DBA`-scoped (and `PUBLIC`-scoped, and nested-role) rule now reaches `SYS`. Some operators want `SYS` to remain a guaranteed raw-SQL recovery account, seeing untransformed SQL regardless of what rules the framework carries.

### Decision

`PREPROC.SETTINGS.group_sys_with_dba` (BOOLEAN, default TRUE) governs whether `SYS` participates in the ROLE-grant closure. When FALSE, `SYS` is excluded from the ENTIRE closure — DBA-derived, PUBLIC-derived, and nested-role-derived rows — so it sees raw SQL unless a USER-scoped rule names `SYS`. The exclusion applies to `SYS` only, not a general excluded-users list. Default TRUE preserves today's behavior; a NULL (upgraded install) is treated as TRUE.

### Options Considered

| Option | Verdict |
|--------|---------|
| Full-closure exclusion, SYS-only, default TRUE | Chosen — the product owner chose the cleanest guaranteed recovery path: `SYS` sees raw SQL unless a USER-scoped rule names it |
| Exclude only `DBA`-scoped rules | Rejected — a `PUBLIC`- or nested-role rule would still reach `SYS`, defeating the break-glass guarantee |
| A general `PREPROC.EXCLUDED_USERS` table/column | Rejected — `SYS` is the single Exasol account that cannot be granted `PREPROC_ADMIN`, so it is the only account needing role-independent recovery; a general list is scope creep with no requested use case |
| Default FALSE (isolated) | Rejected — no behavior change on upgrade is a hard requirement |

### Consequences

`SYS` is the one Exasol superuser that cannot be granted `PREPROC_ADMIN`; a guaranteed raw-SQL recovery account is only meaningful if it is isolated from every rule that could reach it through the role graph. The USER arm stays live so an operator can still target SYS deliberately. A NULL toggle (upgraded install) is treated as TRUE with no backfill UPDATE required.

---

## ADR-036: SYS-grouping toggle applied as a single-arm pure-SQL predicate; USER arm and CTEs untouched

**Date:** 2026-07-17
**Plan:** `add-sys-grouping-toggle`
**Status:** Accepted

### Context

The SYS escape-hatch policy (ADR-035) needs an enforcement mechanism inside `RESOLUTION_SQL`. `RESOLUTION_SQL` is a single static statement (ADR-006 orchestration model); the policy must be enforced without introducing Lua string branching or duplicating the query.

### Decision

The policy is enforced entirely inside the static `RESOLUTION_SQL` statement by appending `AND (u.user_name <> 'SYS' OR COALESCE((SELECT group_sys_with_dba FROM PREPROC.SETTINGS), TRUE) = TRUE)` to the ROLE-closure UNION arm's `EXA_DBA_USERS` join. The USER-scoped UNION arm and the downstream dedupe/ordinal CTEs are unchanged. No Lua string branching is introduced; `RESOLUTION_SQL` stays a single static statement.

### Options Considered

| Option | Verdict |
|--------|---------|
| Single-arm gate on the ROLE-closure UNION arm | Chosen — DBA, PUBLIC, and nested-role derivations all flow through the one closure arm, so a single `grantee <> 'SYS'` gate excludes SYS from the full closure at once |
| Branch the SQL in Lua (build one of two RESOLUTION_SQL strings) | Rejected — duplicates the query and breaks the single-static-statement property |
| Add a WHERE at the outer SELECT | Rejected — gating the closure arm is where DBA/PUBLIC/nested all converge, so one predicate covers all three; an outer WHERE would not distinguish arms |

### Consequences

The USER-scoped arm and every non-SYS holder are untouched. `COALESCE`-to-`TRUE` at the read site is the sole default guarantee, so no backfill UPDATE is needed on upgrade.

---

## ADR-037: SYS-grouping toggle excluded from the resolution state fingerprint

**Date:** 2026-07-17
**Plan:** `add-sys-grouping-toggle`
**Status:** Accepted

### Context

`PREPROC.SETTINGS.group_sys_with_dba` changes resolution output, raising the question of whether it should be tracked by `FINGERPRINT_SQL` for STATUS staleness detection. `mode` is an existing precedent for a resolution-affecting setting excluded from the fingerprint because its setter always refreshes.

### Decision

`group_sys_with_dba` is NOT part of the resolution state fingerprint. `FINGERPRINT_SQL` in both `refresh_core.lua` and `status.lua` is left byte-for-byte identical. The setter (`SET_SYS_GROUPING`) always rebuilds and restamps on the refresh path, so STATUS reports CURRENT after a toggle change.

### Options Considered

| Option | Verdict |
|--------|---------|
| Exclude from `FINGERPRINT_SQL`; rely on the refreshing setter | Chosen — `mode` already sets the precedent; the fingerprint tracks user/role-grant inputs, not framework settings |
| Fold the toggle into `FINGERPRINT_SQL` | Rejected — requires editing both verbatim copies (drift risk) for no benefit, since the setter always refreshes |

### Consequences

Staleness detection stays correct because settings changes are made through refreshing setters. Keeping the two `FINGERPRINT_SQL` copies identical remains a maintenance invariant.

---

## ADR-038: Command sugar added to CONTROL_SUGAR_V1 in place (pre-release exception); V2 discipline still governs post-release

**Date:** 2026-07-17
**Plan:** `add-sys-grouping-toggle`
**Status:** Accepted

### Context

The never-`CREATE OR REPLACE`-a-live-dispatch-module discipline (ADR-019) exists to prevent an in-flight statement binding a stale module body while a new body compiles. The project wanted the `PREPROC SET SYS_GROUPING` verb shipped now, without deferring it to a future `CONTROL_SUGAR_V2`.

### Decision

The `PREPROC SET SYS_GROUPING <bool> [NOREFRESH]` verb is added directly to the live `CONTROL_SUGAR_V1` module in place, and `release.sql` is regenerated. No `CONTROL_SUGAR_V2` is created. The verb is MUTATING, so it accepts the trailing `NOREFRESH` suffix exactly like `PREPROC SET MODE`; it emits `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(<value>, <slot>)` with the value passed UNQUOTED as a boolean literal (mirroring `SET SELF_DISABLE`'s unquoted `word(3)`, not `SET MODE`'s quoted value) and the `norefresh` slot always emitted (`true` when `NOREFRESH` present, else `NULL`). This exception is strictly pre-release: any POST-RELEASE grammar change MUST still go through `CONTROL_SUGAR_V2` (new module + repointed seed + retire once unreferenced), and that discipline remains the standing rule.

### Options Considered

| Option | Verdict |
|--------|---------|
| Edit live `CONTROL_SUGAR_V1` in place; regenerate `release.sql` | Chosen — the project is pre-first-release, so there are no active sessions and the stale-module-binding hazard (ADR-019) does not apply; a full `V2` dance is disproportionate for one break-glass verb |
| Defer the sugar entirely (setter usable via `EXECUTE SCRIPT` only) | Rejected by the user, who wants the ergonomic verb shipped now |
| New `CONTROL_SUGAR_V2` + reseed + retire | Rejected as disproportionate for one break-glass verb |

### Consequences

Editing `V1` in place and regenerating `release.sql` is harmless now because the project has no active sessions. This exception is strictly pre-release: any post-release grammar change must still go through `CONTROL_SUGAR_V2`, and that discipline remains the standing rule going forward.

---

## ADR-039: Framework as the single-slot multiplexer

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

Exasol exposes exactly ONE `sql_preprocessor_script` slot (one system-wide, one per session). Every preprocessor project today simply grabs it — this framework sets `PREPROC_RT.MASTER`; others (e.g. exasol-semantic-views) clear and set it to their own script — so two independent preprocessor projects cannot coexist: last writer wins.

### Decision

Position the framework as the sole owner of Exasol's one preprocessor slot; the module ecosystem is how every other syntax provider multiplexes through MASTER instead of fighting for the slot.

### Options Considered

| Option | Verdict |
|--------|---------|
| Framework as sole slot-owner and multiplexer | Chosen — only one dispatcher can occupy the slot; making it a shared, extensible multiplexer is the framework's core value proposition |
| Let each project keep grabbing the slot (status quo) | Rejected — projects cannot coexist |
| A per-session convention | Rejected — still last-writer-wins |

### Consequences

Every other syntax provider must hook in through MASTER dispatch rather than owning the slot itself. This is the foundational premise the entire module ecosystem operationalizes.

---

## ADR-040: One integration contract, not two

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

An earlier proposal split module integration into a "bundled module" kind and a heavyweight "subsystem" kind, requiring two separate contracts for authors to learn.

### Decision

A module is always `fn(text) -> nil|string` in a Lua script + a registration rule + a `module.toml`. No "bundled vs subsystem" split.

### Options Considered

| Option | Verdict |
|--------|---------|
| One contract for every module | Chosen — the dispatcher already treats every rule uniformly; what is behind the script is the author's concern |
| Two contracts (bundled kind + subsystem kind) | Rejected by the owner — explicitly rejected as unnecessary complexity |

### Consequences

The SDK surface stays minimal and stable to document. A tiny transform and a whole subsystem-backed provider share the exact same registration and dispatch path.

---

## ADR-041: Deploy mode is orthogonal metadata

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

Modules can arrive either as a single self-contained artifact the CLI deploys, or as a project with its own installer that only needs its rule registered. This distinction risked becoming a second contract.

### Decision

`deploy_mode` (`library-deployed` | `self-deployed`) is a `module.toml` field describing how the artifact gets into the DB, not a second contract. Self-deployed modules are register-only for the CLI.

### Options Considered

| Option | Verdict |
|--------|---------|
| `deploy_mode` as metadata on the one contract | Chosen — keeps the contract single; lets a subsystem and a one-file module share the same registration and dispatch path |
| Encode deployment differences as distinct module types | Rejected — reintroduces the two-kinds split ADR-040 rejects |

### Consequences

A future semantic-views hook-in becomes a straightforward register-only case rather than requiring a new contract type.

---

## ADR-042: Canonical artifact is one self-contained .sql; standard layout

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted — refined by ADR-054 (the one file may carry 1..N statements; the one-statement restriction is lifted, the one-file rule and the standard layout stand)

### Context

Modules need a canonical, auditable, hand-carryable artifact shape that fits the air-gap deployment model and the `_V<N>` versioning discipline.

### Decision

The module is one `CREATE OR REPLACE LUA SCRIPT PREPROC_RT.<NAME>_V<N>` file under `modules/<name>/{<name>_v<N>.sql, module.toml, README.md, tests/}`.

### Options Considered

| Option | Verdict |
|--------|---------|
| One self-contained `.sql` artifact, standard layout | Chosen — mirrors the engine's `release.sql` philosophy; trivially hand-carryable for air-gap; easy to hash and verify |
| Multi-file artifacts | Rejected — complicates hashing/verification and hand-carry |
| A packaged archive format | Rejected — adds tooling weight for no MVP benefit |

### Consequences

Every module is a single auditable file whose integrity a sha256 digest can verify end to end, matching the engine's own bundling discipline.

---

## ADR-043: Federated registry with curated PR-an-entry

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

The registry must cover both central-library modules and external repos while keeping a human trust gate, since the CLI verifies and deploys code sight-unseen by the framework team.

### Decision

A generated federated index over central-library modules (auto-indexed) and external repos (one curated `registry/external/<name>.toml` block per module, human-reviewed). Pin by tag; verify sha256. No crawling.

### Options Considered

| Option | Verdict |
|--------|---------|
| Federated index, auto-indexed library + curated external entries | Chosen — human review is the trust gate; auto-indexing keeps the library effortless; pinning + sha make registry state reproducible and tamper-evident |
| Automated discovery/crawling of repos | Rejected — no human trust gate |
| A fully manual list | Rejected — loses the auto-indexing benefit for the library's own modules |

### Consequences

External modules only enter the registry through a reviewed PR, while library modules stay always-in-sync automatically via CI-generated indexing.

---

## ADR-044: Registry hosted in the central library repo

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

The registry index needs a home. Candidates were the framework repo, a dedicated registry repo, or the new central library repo that already holds every library module's `module.toml`.

### Decision

The registry index lives in the central library repo (`registry/index.json`, CI-generated), not in the framework repo and not in a separate dedicated registry repo (yet).

### Options Considered

| Option | Verdict |
|--------|---------|
| Registry hosted in the central library repo | Chosen — library CI already has all manifests locally (trivial, always-in-sync auto-index); index format is repo-agnostic so a future split is lift-and-shift |
| A file in the framework repo | Rejected — would couple engine releases to registry churn and invert ownership |
| A dedicated `preprocessor-registry` repo | Rejected — adds a third repo and cross-repo sync for no MVP benefit |

### Consequences

Registry churn is decoupled from framework releases. If a dedicated registry repo is ever warranted, the index format's repo-agnostic design makes that a lift-and-shift, not a rewrite.

---

## ADR-045: Air-gap discipline — the DB never fetches; the CLI is the bridge

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

Many customer databases have no internet egress. The module ecosystem must let operators fetch, verify, and deploy modules without ever requiring the database itself to reach the network.

### Decision

All fetch/verify/deploy happens on the operator's CLI machine; the database only receives SQL. `preproc module bundle` emits a self-contained `.sql` for no-egress databases; `--registry <path>` reads a local checkout.

### Options Considered

| Option | Verdict |
|--------|---------|
| CLI-side fetch/verify/deploy; DB is SQL-only | Chosen — matches ADR-006/007's DB-as-management-only-surface; satisfies the hard air-gap constraint |
| DB-side fetch (e.g. a UDF pulling from GitHub) | Rejected — violates the no-egress hard constraint many customer DBs impose |

### Consequences

Air-gapped customers get a fully supported offline path (`bundle` + local `--registry`) with no reduced functionality versus online operation.

---

## ADR-046: Module V<N> lifecycle mirrors profile generate/retire/reclaim

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

A convenience-mode session can be pinned to a profile that baked an old module version. Naively replacing or dropping a live module version out from under such a session would break dispatch.

### Decision

Update is deploy `_V<N+1>` → repoint the CONFIG rule → refresh → mark-retire `_V<N>` → guarded reclaim (drop only when no enabled rule and no non-orphaned PROFILES manifest reference it). Provenance is recorded in `PREPROC.MODULES` with a PUBLIC-readable `PREPROC_RT.INSTALLED_MODULES` view.

### Options Considered

| Option | Verdict |
|--------|---------|
| Generational deploy/repoint/retire/reclaim, mirroring profile lifecycle | Chosen — the profile machinery already solved exactly this generational problem |
| `CREATE OR REPLACE` a live version | Rejected — violates ADR-019 |
| Immediate drop on retire | Rejected — catastrophic for pinned sessions, mirrors the never-same-refresh-drop hazard of ADR-022 |

### Consequences

Module updates get the same safety guarantees as profile updates, at the cost of carrying retired versions until reclaim is safe. Provenance is now queryable via `PREPROC_RT.INSTALLED_MODULES`.

---

## ADR-047: Idempotent RULE_ENSURE / declarative sync (closes BUG-008)

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

`RULE_ADD` stacks duplicate rules on repeated calls (BUG-008), which makes a TRANSLATE/REWRITE module run N times and defeats declarative re-apply — a problem `preproc module sync` needs solved to be safely idempotent.

### Decision

Add `PREPROC.RULE_ENSURE` (idempotent on the natural rule key, returns the existing `rule_id` when present) and make `preproc module sync` declarative and idempotent. `RULE_ADD` keeps its allocate-new behaviour.

### Options Considered

| Option | Verdict |
|--------|---------|
| New `RULE_ENSURE` surface alongside unchanged `RULE_ADD` | Chosen — a purpose-built ensure surface is the clean fix and the foundation `sync` needs |
| Make `RULE_ADD` itself idempotent | Rejected — breaks its documented allocate-new behaviour and existing tests |
| Leave dedupe to the CLI | Rejected — fragile, races under concurrent operations |

### Consequences

`preproc module sync` can be re-run safely against an unchanged desired state with no duplicate rules, closing BUG-008.

---

## ADR-048: Ergonomics migrated out of the framework as a clean move (module #1)

**Date:** 2026-07-17
**Plan:** `add-module-ecosystem`
**Status:** Accepted

### Context

The module ecosystem needed a real proving case: a capability migrated end-to-end through the contract → package → registry → `preproc module add` path. `cast-shorthand` and `trailing-comma` were self-contained, examples-only ergonomics transforms already fully in-repo, never part of `release.sql`/`deploy_all.sql`.

### Decision

Remove `cast-shorthand` and `trailing-comma` from the framework (examples, `sql/lua` copies, tests, `ergonomics/` specs) and author them as two independent modules directly in the existing `exasol-labs/preprocessor-library` repo (`modules/cast-shorthand/`, `modules/trailing-comma/`). Leave a moved-to-library pointer in `examples/README.md`. Supersedes ADR-032's in-framework `ergonomics/` domain.

### Options Considered

| Option | Verdict |
|--------|---------|
| Clean move to the library repo | Chosen — ergonomics was never in the engine bundle, so removal changes no bundle output or statement count; it is the ideal proving case because it is self-contained and already fully in-repo |
| A deprecation shim / keep a copy in the framework | Rejected — reintroduces the exact drift the ecosystem eliminates |

### Consequences

`specs/ergonomics/` is removed from the framework spec tree (superseding ADR-032's placement); the capability remains available via `preproc module add cast-shorthand` / `preproc module add trailing-comma` from the central library. This is the reference migration future modules follow.

---

## ADR-049: DB-side module install as a first-class ALTERNATIVE deployment path

**Date:** 2026-07-21
**Plan:** `add-in-db-module-install`
**Status:** Accepted

### Context

`preproc module add` deploys a module by fetching + verifying the artifact on the operator's CLI machine and then issuing SQL. Many DBAs manage Exasol purely through a SQL client and would rather stage a release once in BucketFS (or point at an HTTPS source) and install from SQL, never touching a Python/uv toolchain. ADR-007's strategic direction is the framework owning its management surface end-to-end, "eventually intercepting and controlling its own management syntax from within the preprocessor" — but ADR-007 deliberately kept *deployment* in the CLI.

### Decision

Ship `PREPROC.MODULE_INSTALL(source, module_name, scope)` — driven by a Python resolver UDF and a Lua orchestrator that re-homes the CLI's five-step `deploy_module` sequence into the DB — as an ALTERNATIVE to `preproc module add`. The end-state is byte-identical to a CLI install (script + rule + provenance row); `sha256` verification aborts before any write; a self-deployed module is register-only.

### Options Considered

| Option | Verdict |
|--------|---------|
| Python resolver UDF + Lua orchestrator re-homing `deploy_module`, byte-identical end-state | Chosen — extends ADR-007's vision to deployment without superseding it; the CLI stays fully supported and both paths converge on the same end-state |
| Keep deployment CLI-only (ADR-007's original split) | Rejected — leaves no SQL-only deployment path for DBAs without a Python/uv toolchain |
| Vendor the whole `preproc` package into a UDF | Rejected — far heavier than re-implementing the ~3 resolver functions for Phase 1 |

### Consequences

A DBA can now install a module entirely from SQL with no external toolchain, while `MASTER`/`MY_PIPELINE` dispatch is unchanged regardless of which path installed the module. The CLI + `bundle` + local `--registry` air-gap path remains the default, first-class path.

---

## ADR-050: BucketFS-staged in-DB read is air-gap-safe; refines ADR-045

**Date:** 2026-07-21
**Plan:** `add-in-db-module-install`
**Status:** Accepted

### Context

ADR-045 established that all fetch/verify/deploy happens on the operator's CLI machine because the database must never be required to reach the network on a no-egress cluster. ADR-045 rejected "DB-side fetch (a UDF pulling from GitHub)" as network egress, but never considered a UDF reading a locally-staged, cluster-replicated bundle.

### Decision

A UDF reading the cluster-replicated release tarball natively via the BucketFS mount (`/buckets/…`, located by an operator-created CONNECTION named in the source string) involves NO network egress — nothing leaves the DB perimeter. This is established as a NEW air-gap-safe option, not the option ADR-045 rejected.

### Options Considered

| Option | Verdict |
|--------|---------|
| BucketFS-native UDF read via an operator-created CONNECTION | Chosen — a cluster-local file read is not egress; satisfies the hard air-gap constraint |
| Treat any DB-side read as an air-gap violation and forbid it | Rejected — conflates a local BucketFS read with genuine network egress |

### Consequences

Air-gapped clusters gain a fully SQL-side install path via BucketFS staging. ADR-045 is refined, not reversed — the CLI's no-egress guarantee for external fetches still holds.

---

## ADR-051: HTTPS DB-fetch is activated implicitly by an `https:` source — no toggle; further departs from ADR-045

**Date:** 2026-07-21
**Plan:** `add-in-db-module-install`
**Status:** Accepted

### Context

Egress-permitted clusters could benefit from a lower-friction HTTPS install path direct from SQL. An earlier design considered a default-off admin toggle gating this path, mirroring the caution of ADR-045.

### Decision

Ship the HTTPS reader (`urllib` GET of `registry/index.json` + the artifact, `sha256`-verified). It runs whenever the `source` uses the `https:` scheme — there is NO settings toggle and NO stored flag. Activation is implicit: the operator opts in by supplying an `https://` source AND the cluster being able to reach the host. On a cluster with no outbound egress the GET fails with a clear network error and NO partial deploy occurs (abort-before-write holds). BucketFS native read (ADR-050) remains the recommended default for no-egress customers.

### Options Considered

| Option | Verdict |
|--------|---------|
| No toggle — implicit activation via the `https:` scheme | Chosen — the operator has already opted in twice over (source scheme + cluster egress); a separate toggle is redundant friction |
| A default-off admin toggle (`SET_DB_FETCH` / `allow_https_fetch`) | Rejected on user feedback — dropped as unnecessary friction for the lowest-friction-UX goal |
| Never ship HTTPS at all | Rejected — leaves egress-permitted clusters without the lowest-friction path |

### Consequences

Egress-permitted clusters get a one-step SQL install from an HTTPS source with no admin toggle to manage. ADR-045's no-egress guarantee is preserved by physics, not policy, for clusters that have no outbound path.

---

## ADR-052: In-DB install runs invoker-rights under the PREPROC_ADMIN boundary; resolver UDFs are admin-only

**Date:** 2026-07-21
**Plan:** `add-in-db-module-install`
**Status:** Accepted

### Context

The feature was first framed around "definer-rights scripts owned by PREPROC_ADMIN so a delegated admin installs without raw CREATE SCRIPT rights." That framing is inconsistent with the verified repo reality: Exasol has NO definer-rights Lua scripts (ADR-001) — this is precisely why the two-schema definer-rights-VIEW bridge exists.

### Decision

`MODULE_INSTALL`, `MODULE_CATALOG`, and the resolver UDF(s) run with INVOKER rights and live in the locked `PREPROC` schema owned by `PREPROC_ADMIN`; the resolver UDFs are NEVER granted to PUBLIC. The caller MUST hold `PREPROC_ADMIN`, which confers ownership of `PREPROC`/`PREPROC_RT`, the `CREATE SCRIPT` system privilege, and — once the operator grants it — ACCESS on the BucketFS CONNECTION. The "delegated admin" is a `PREPROC_ADMIN` grantee, not a DBA.

### Options Considered

| Option | Verdict |
|--------|---------|
| Invoker rights + `PREPROC_ADMIN` role boundary, admin-only resolver UDFs | Chosen — matches the boundary `REFRESH_CORE` already uses to `CREATE` `P_<hash>` scripts at refresh time; closes the hole where a non-admin could invoke the DB's BucketFS/network reader |
| Definer-rights scripts owned by `PREPROC_ADMIN` | Rejected — not possible; Exasol has no definer-rights Lua scripts |

### Consequences

Installing a module without a DBA account or system-wide `CREATE ANY SCRIPT` is possible for any `PREPROC_ADMIN` grantee, while the resolver's BucketFS/network access is never reachable by an ordinary user.

---

## ADR-053: Intercepted `PREPROC INSTALL MODULE` maps onto MODULE_INSTALL per ADR-008

**Date:** 2026-07-21
**Plan:** `add-in-db-module-install`
**Status:** Accepted

### Context

ADR-008 keeps admin entry points syntax-mappable so a future intercepted statement can map cleanly onto them. The in-DB module installer is that future slice for install and catalog discovery.

### Decision

Add `PREPROC INSTALL MODULE <name> FROM '<source>' FOR (ROLE|USER) <x>` (plus `PREPROC CATALOG MODULES FROM …`) to the live `CONTROL_SUGAR_V1` module in place, each rewriting into exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)`. The `FOR (ROLE|USER) x` clause becomes the `scope` `TYPE:value` argument (activation target). The scripted entry points remain the stable, syntax-mappable contract; the sugar is additive.

### Options Considered

| Option | Verdict |
|--------|---------|
| Extend `CONTROL_SUGAR_V1` in place (ADR-038 pre-release exception) | Chosen — reuses the established pre-release in-place-edit exception; a second EXPAND module would just compete in the same phase for no benefit |
| A separate `INSTALL_SUGAR_V1` EXPAND module | Rejected — no benefit over extending the existing sugar module pre-release |
| No sugar — require `EXECUTE SCRIPT` directly | Rejected — loses the readable-keyword-command UX the sugar layer exists for |

### Consequences

DBAs get a readable `PREPROC INSTALL MODULE …` / `PREPROC CATALOG MODULES …` surface that funnels through the same validating admin scripts as the scripted entry points, with no new privilege introduced by the sugar layer.

---

## ADR-054: Module artifact is a composite .sql carrying 1..N statements

**Date:** 2026-07-27
**Plan:** `change-module-artifact-to-composite`
**Status:** Accepted — refines ADR-042 (the artifact stays ONE self-contained file; only the one-statement restriction is lifted)

### Context

`module/contract` promised "exactly ONE contract regardless of whether the module is a tiny transform or the front door to a whole subsystem", while simultaneously requiring the artifact to be exactly one `CREATE OR REPLACE LUA SCRIPT`. Both could not hold. Any module needing a Python UDF or a table was silently pushed to `self-deployed`, where the library carries only a pointer and never delivers the code — making `preprocessor-library` a phone book rather than a distribution channel for precisely the modules most worth distributing.

The documented justification for the one-statement rule (`docs/module-authoring.md`: "trivially hand-carryable for air-gapped deployment and trivially hashed for integrity verification") does not survive scrutiny — a multi-statement file is equally hand-carryable and hashes identically. The real cause was mechanical: `module_install.lua` executed a single `pquery(artifact_text)`, and `pquery` takes one statement.

### Decision

The canonical artifact is ONE self-contained `.sql` file carrying ONE OR MORE statements. Exactly one statement MUST be the `CREATE OR REPLACE LUA SCRIPT <script_name>` entry script; the remaining statements MAY create any other objects the module needs. N=1 is the degenerate case and stays valid unchanged. `deploy_mode` gains no third value: `library-deployed` is generalised in place and `self-deployed` is untouched.

### Options Considered

| Option | Verdict |
|--------|---------|
| One file, 1..N statements; `library-deployed` generalised in place | Chosen — resolves the contradiction, keeps one contract, and makes the library a real distribution channel for subsystem modules |
| A third `deploy_mode` for composite modules | Rejected — reintroduces the two-kinds split ADR-040 rejects |
| A packaged archive format | Rejected — adds tooling weight and stops the artifact being hand-runnable, killing the air-gap story |
| Multi-file artifacts | Rejected — complicates hashing and hand-carry (unchanged from ADR-042) |

### Consequences

A module that is the front door to a whole subsystem can now be `library-deployed` and installed from the registry. `sha256` is unaffected — it remains a digest over the whole file. The mechanical consequence is that every executor must split the artifact into statements, which ADR-058 and ADR-059 govern.

---

## ADR-055: Object inventory is declared in module.toml AND parse-verified

**Date:** 2026-07-27
**Plan:** `change-module-artifact-to-composite`
**Status:** Accepted

### Context

Once an artifact can create N objects, something must know which objects a module owns — for pre-install review, for compensating cleanup on a failed install, and for reclaim. The database never retains the artifact, so reclaim cannot re-derive the list at drop time.

### Decision

`module.toml` gains an `[[objects]]` array declaring every object the artifact creates, each with a `type` and a fully-qualified `name`. Tooling ALSO parses the artifact to derive the same list independently and rejects the module when the two disagree in either direction — the same accept/reject discipline already applied to `sha256`. `[[objects]]` MAY be omitted when the artifact carries exactly one statement, in which case the inventory is derived as the single object named by `script_name`; it is REQUIRED once the artifact carries more than one statement.

### Options Considered

| Option | Verdict |
|--------|---------|
| Declare in the manifest AND verify by parsing | Chosen — drift becomes impossible, the manifest stays human-reviewable before install, and reclaim reads the declaration without re-parsing |
| Derive by parsing only | Rejected — nothing human-reviewable before install, and reclaim would have to re-parse an artifact the database does not retain |
| Declare only, no parse check | Rejected — permits silent drift between manifest and artifact, exactly the failure mode the `sha256` discipline exists to prevent |
| Require `[[objects]]` even at N=1 | Rejected — would force edits to `cast_shorthand` and `trailing_comma` for zero information gain, breaking the "N=1 keeps working untouched" invariant |

### Consequences

The inventory becomes the module's ownership boundary: it is the undo list for compensating cleanup, the drop set for reclaim, and the pre-install review surface `MODULE_CATALOG` reports. It travels in the registry index, which is why the index `schema_version` moves to 2. It is persisted in `PREPROC.MODULES.objects` at install time.

---

## ADR-056: Every object in a composite artifact carries the module version suffix

**Date:** 2026-07-27
**Plan:** `change-module-artifact-to-composite`
**Status:** Accepted

### Context

ADR-019 forbids `CREATE OR REPLACE` on a live module so a pinned session is never mutated out from under. With a single-script artifact that discipline was fully expressed by the entry script's `_V<N>` suffix. A composite artifact creates companions too, and an unversioned companion would be overwritten by the next generation's install.

### Decision

Every object an artifact creates MUST carry the module's `_V<N>` version suffix, not only the entry script. A companion whose body is identical between v1 and v2 is still redeployed under the new versioned name. The entry script MUST reference its companions by their versioned names.

### Options Considered

| Option | Verdict |
|--------|---------|
| Version every object | Chosen — v1 and v2 generations share no object name, so they coexist with zero collisions and reclaim is unambiguous |
| Version only the entry script | Rejected — a v2 install would `CREATE OR REPLACE` a live v1 companion, which is precisely what ADR-019 forbids |
| Version only companions that changed | Rejected — makes the inventory generation-dependent and reclaim ambiguous about shared ownership |

### Consequences

Redeploying an unchanged companion under a new name costs a little duplication, bought against unambiguous generational isolation. Reclaiming v1 can never drop an object v2 is using. A migrating subsystem must suffix every object it ships.

---

## ADR-057: Composite artifacts may target any writable schema; reclaim is inventory-bounded

**Date:** 2026-07-27
**Plan:** `change-module-artifact-to-composite`
**Status:** Accepted

### Context

`module_reclaim.lua` guarded its `DROP SCRIPT` with a "PREPROC_RT-owned artifacts only" check, reasoning that the framework only ever deploys module artifacts into `PREPROC_RT`. A subsystem module, however, legitimately owns its own schema; restricting the namespace would push exactly those modules back to `self-deployed`, defeating ADR-054.

### Decision

A composite artifact MAY create objects in any schema the installing admin can write. The `PREPROC_RT`-only drop guard is replaced by the recorded object inventory as the ownership boundary: reclaim drops every object in the version's recorded inventory, in any schema, and MUST NOT drop an object absent from that inventory. Because installing a library-deployed module is therefore arbitrary DDL under the `PREPROC_ADMIN` boundary, `PREPROC.MODULE_CATALOG` reports each advertised module's declared object inventory so an operator sees exactly what an install would create BEFORE running it.

### Options Considered

| Option | Verdict |
|--------|---------|
| Unrestricted namespace, inventory-bounded reclaim, catalog as review gate | Chosen — safe because the inventory is declared and parse-verified (ADR-055), so reclaim knows precisely what it owns |
| Restrict objects to `PREPROC_RT` / `PREPROC` | Rejected — a subsystem module owns its own schema; the restriction would force it back to `self-deployed` |
| Allow any schema but keep reclaim `PREPROC_RT`-only | Rejected — would strand out-of-schema objects permanently on reclaim |
| Require an explicit operator allow-list of writable schemas | Rejected — duplicates the privilege check the database already performs at DDL time |

### Consequences

The security posture is explicit rather than implied: installing a module is arbitrary DDL bounded by what `PREPROC_ADMIN` can write, and the pre-install review path is the catalog's object listing. Reclaim's correctness now depends entirely on the inventory being accurate, which ADR-055 guarantees by parse verification.

---

## ADR-058: Statement separation uses the EXAplus `--/` … `/` block convention

**Date:** 2026-07-27
**Plan:** `change-module-artifact-to-composite`
**Status:** Accepted

### Context

A composite artifact needs an unambiguous statement separator. Lua and Python bodies contain semicolons freely, so a bare `;` split is incorrect. Whatever is chosen must not break the air-gap invariant that the artifact is one hand-carryable file an operator can simply run.

### Decision

Statements whose bodies contain semicolons are wrapped between a line consisting solely of `--/` and a line consisting solely of `/`. All other statements are terminated by `;`. This is the convention the framework's own `release.sql` already emits, that `confd-via-sql`'s `build.py` already emits, and that `module_install.lua` already half-knows — it strips a lone `/` terminator today.

### Options Considered

| Option | Verdict |
|--------|---------|
| EXAplus `--/` … `/` block markers | Chosen — the artifact stays directly runnable by hand in EXAplus/DbVisualiser, so hand-carry needs no tooling; already emitted by `release.sql` and `build.py` |
| Bare `;` splitting | Rejected — cannot survive semicolons inside Lua and Python bodies |
| A JSON or TOML array of statements | Rejected — the file stops being runnable by hand, killing the air-gap story |
| A custom sentinel comment (e.g. `-- @@STATEMENT@@`) | Rejected — invents a dialect the framework's own tooling and Exasol clients do not already speak |

### Consequences

The air-gap story is strengthened rather than preserved: an operator can hand-carry the file and just run it, with no framework tooling in the loop. The cost is that every executor needs a splitter implementing this grammar, in Python and in Lua, whose agreement is pinned by a shared conformance corpus.

---

## ADR-059: Composite-install atomicity is constructed, not inherited

**Date:** 2026-07-27
**Plan:** `change-module-artifact-to-composite`
**Status:** Accepted

### Context

A single-statement install was atomic by accident: one `pquery`, one outcome. A composite artifact executes N statements, so a failure at statement k raises the question of what happens to statements 1..k-1.

The framework's transactional reality is asymmetric and easy to get wrong. DML inside a Lua script IS transactional and rollback-able — `refresh_core.lua` performs DELETE + INSERT + stamp under a single explicit `COMMIT`, issued as SQL through `pquery` because there is no `commit()` global (`refresh_core.lua:775`), and ADR-era notes record that this provides the required atomicity guarantee regardless of the client's autocommit setting. DDL is NOT: `refresh_core.lua:111,117,742` and `module_reclaim.lua:68` all document that `CREATE OR REPLACE` and `DROP SCRIPT` implicitly commit preceding DML and cannot themselves be rolled back, and `refresh_core.lua` visibly works around this. A composite artifact is mostly DDL.

### Decision

All-or-nothing is BUILT from four mechanisms rather than delegated to a transaction:

1. **Pre-flight validation** of all N statements and the object inventory BEFORE the first DDL, mirroring the existing "ANY resolver row with status = ERROR aborts HERE, BEFORE any DB write" pattern. This is the primary mechanism — the overwhelming majority of failures never reach a write.
2. **Compensating cleanup** on failure at statement k: drop the objects created by statements 1..k-1, walking the declared inventory in reverse. Best-effort, and MUST NOT mask the original error. This is the second reason the inventory is required.
3. **Idempotent re-run**: every artifact statement uses `CREATE OR REPLACE`, so re-running a part-way failed install converges even if compensating cleanup was itself incomplete.
4. **Rule registration last**: the rule is DML, is rollback-able, and `RULE_ENSURE` already validates the script exists before inserting. A module whose statements deployed but whose rule was never registered is INERT — the grammar never activates — rather than half-active.

### Options Considered

| Option | Verdict |
|--------|---------|
| Pre-flight + compensating cleanup + idempotent re-run + rule-last | Chosen — the only construction available given that DDL cannot roll back; makes the framework's existing ordering a stated guarantee rather than an accident |
| Wrap the install in a transaction | Rejected — a false guarantee: DDL implicitly commits preceding DML and cannot be rolled back, as the framework's own code documents and works around |
| Leave partial state and require manual operator cleanup | Rejected — violates the existing "every mutating subcommand MUST leave the database consistent on failure" invariant in `module/cli` |
| Stage objects under temporary names and rename on success | Rejected — Exasol has no atomic multi-object rename, so it moves the same problem without solving it |

### Consequences

The failure mode of a composite install is a well-defined one: nothing written at all (the common case, caught by pre-flight), or objects created and then dropped, with no rule and no provenance row — inert either way. Correctness depends on the inventory being accurate (ADR-055) and on the CLI and in-DB splitters agreeing, since a split divergence would break the byte-identical end-state parity `module/db-install` guarantees.

---

## ADR-060: RULE_ADD takes client_pattern, rather than the sugar switching to RULE_ENSURE

**Date:** 2026-07-30
**Plan:** `add-client-scoped-rules`
**Status:** Accepted

### Context

`PREPROC.CONFIG.client_pattern` was honoured end-to-end by dispatch, resolution, and profile codegen, but no operator-facing surface could SET it: `PREPROC.RULE_ADD`'s parameter list had no such slot, so the `PREPROC ADD RULE` sugar had no clause for it, and the only way to create a client-scoped rule was `PREPROC.RULE_ENSURE`, an upsert built for the module installer's idempotent needs. Exasol Lua scripts have no optional parameters — `EXECUTE SCRIPT` enforces an exact argument count — so any fix that touches `RULE_ADD`'s signature is unavoidably a breaking change for every existing caller.

### Decision

Add `client_pattern` to `PREPROC.RULE_ADD` between `function_name` and `norefresh`, making its parameter list identical to `RULE_ENSURE`'s, and accept the breaking signature change.

### Options Considered

| Option | Verdict |
|--------|---------|
| Add `client_pattern` to `RULE_ADD`, accepting a breaking signature change | Chosen — a stale six-argument call raises "got 6 expected 7" immediately and visibly, at the call, with no partial write; loud beats silent |
| Leave `RULE_ADD` alone and have the sugar emit `RULE_ENSURE` when a `CLIENT` clause is present | Rejected — makes one verb mean two things: `RULE_ADD` allocates and returns a NEW `rule_id` while `RULE_ENSURE` upserts and returns an EXISTING one on a match, so whether the operator got a new rule or silently adopted an existing one would depend on an unrelated clause |

### Consequences

`RULE_ADD` and `RULE_ENSURE` now differ in exactly one dimension — insert vs upsert — which is the distinction they exist to draw. Every in-repo `RULE_ADD` call site needed the extra argument; the migration was enumerated and completed as part of this plan.

---

## ADR-061: The CLIENT operand is a quoted literal, exempt from identifier folding

**Date:** 2026-07-30
**Plan:** `add-client-scoped-rules`
**Status:** Accepted

### Context

The `PREPROC ADD RULE` sugar needed a clause to carry a `client_pattern` value. Every other captured value in the grammar (`PHASE`, `SCRIPT`, role/user) follows Exasol identifier folding — unquoted folds to UPPER CASE. But `client_pattern` is matched as a literal substring against `EXA_ALL_SESSIONS.CLIENT`, a self-reported application name measured on Exasol 2026.1 as `DbVisualizer 24.1.5`, `DBeaver 23.3.0`, `PyExasol 2.2.1`, `EXAplus` — mixed-case strings that can contain spaces.

### Decision

`CLIENT` takes a single-quoted literal carried through verbatim: no folding, no trimming, with the quoted span kept as one token so embedded whitespace survives. The keyword itself is matched case-insensitively like every other keyword. This is the second capture position after `FUNCTION` exempt from folding.

### Options Considered

| Option | Verdict |
|--------|---------|
| Single-quoted literal, exempt from folding | Chosen — reuses the tokenizer behaviour already proven by `INSTALL MODULE`'s `FROM '<source>'`, and visually marks the value as an opaque external string |
| Fold to UPPER like every other captured value | Rejected — `DBVISUALIZER` is not a substring of `DbVisualizer 24.1.5`, so a folded pattern matches nothing, SILENTLY: the rule resolves, dispatch evaluates it, and `RULE_LIST` shows a correct-looking rule that never fires |
| `WHEN CLIENT '<p>'` | Rejected — an extra token for no parse benefit, and breaks the bare-keyword-plus-operand shape of `PHASE`/`SCRIPT`/`FUNCTION` |
| `FOR CLIENT '<p>'` | Rejected — `FOR` already introduces the scope, so two `FOR` clauses invite reading the second as another scope rather than a narrowing of the first |
| An unquoted operand | Rejected — could not carry the spaces real client strings contain |

### Consequences

Silent non-matching is worse than a parse error, because the operator sees a correctly-listed rule that does nothing. `CLIENT` joins `FUNCTION` as the grammar's two folding-exempt positions, recorded in the module's quoting-semantics header.

---

## ADR-062: An empty client_pattern is rejected, and an over-length one is rejected rather than truncated

**Date:** 2026-07-30
**Plan:** `add-client-scoped-rules`
**Status:** Accepted

### Context

`client_pattern` is matched with `string.find(client, pattern, 1, true)`, a literal substring match. An empty pattern returns a match position of 1 for every client — identical behaviour to NULL ("all clients") — while an over-length pattern exceeds the `PREPROC.CONFIG.client_pattern` column width of `VARCHAR(256)`.

### Decision

Reject an empty or whitespace-only `client_pattern` with a validation error before any mutation, naming NULL as the way to express "all clients". Reject a pattern longer than the column width; never truncate.

### Options Considered

| Option | Verdict |
|--------|---------|
| Reject empty/whitespace-only; reject over-length without truncating | Chosen — this framework's failure-mode instinct consistently prefers a loud error over a silent wrong answer |
| Silently coerce an empty pattern to NULL, or store it and let it mean "always" | Rejected — produces a rule whose displayed scope contradicts its behaviour |
| Truncate an over-length pattern to the column width | Rejected — clipping a SUBSTRING pattern WIDENS the matched set, so `'DbVisualizer-Prod'` becomes `'DbVisualizer'` and fires for every DbVisualizer session |

### Consequences

Through SQL, Exasol collapses an empty string literal `''` to NULL in transit (`'' IS NULL` returns YES, verified live), so an empty `client_pattern` can never actually reach the validator from a SQL caller — it arrives at `RULE_ADD` already as the NULL sentinel and is accepted as "all clients". Only the whitespace-only half of this guard is reachable and testable through SQL; the empty-string check remains in the validator as defence-in-depth for an in-DB Lua caller that constructs the argument programmatically. This is why the `PREPROC ADD RULE` sugar separately declines an empty `CLIENT ''` operand rather than carrying it through — otherwise the sugar would silently create an all-clients rule from a command that reads as a restriction.

---

## ADR-063: One shared client_pattern validator, hosted in PREPROC.REFRESH_CORE

**Date:** 2026-07-30
**Plan:** `add-client-scoped-rules`
**Status:** Accepted

### Context

Once `RULE_ADD` validates `client_pattern`, `RULE_ENSURE` — which already accepted the parameter with no validation — needed the same checks, or the module-installer path could persist a pattern the operator surface would refuse. Exasol Lua's only cross-script sharing mechanism is `import(name, alias)` against a deployed script; there is no build-time textual include.

### Decision

`validate_client_pattern()` is implemented exactly once, exported from `PREPROC.REFRESH_CORE` alongside `rebuild()`, and called from BOTH `rule_add.lua` and `rule_ensure.lua`. Each script's `import("PREPROC.REFRESH_CORE", "rc")` moves to the top of the file so validation precedes any DML.

### Options Considered

| Option | Verdict |
|--------|---------|
| Shared validator hosted in `PREPROC.REFRESH_CORE` | Chosen — `REFRESH_CORE` is the sole existing shared module, both writers already import it, and its header already justifies itself as the place logic goes rather than being duplicated |
| A new deployed `PREPROC.CONTROL_VALIDATE` script | Rejected — needs entries in `resources.py`, `install.py`, `bundle.py`, and `release.sql`, and creates a new object in the locked `PREPROC` schema for a twelve-line function |
| Duplicate the validator in both files | Rejected — the divergence hazard this framework consistently refuses |
| Validate only in `RULE_ADD`, treat `RULE_ENSURE` as a follow-on | Rejected — leaves the module-installer path able to persist `client_pattern = ''` |

### Consequences

`REFRESH_CORE`'s stated role widens from "the resolution rebuild" to "shared logic for `PREPROC.REFRESH` and the mutating admin scripts". Moving the import earlier costs a module load on the `NOREFRESH` path and changes no behaviour, since `import()` binds functions and performs no work of its own.
