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
