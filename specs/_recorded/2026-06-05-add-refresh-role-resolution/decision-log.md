# Decision Log: add-refresh-role-resolution

Date: 2026-06-05

## Interview

**Q:** Where should the refresh/role-resolution logic execute?
**A:** Entirely DB-side — a Lua script in the database is the only entry point, callable from any SQL client by an authorized admin (presumably `PREPROC_ADMIN` holders). There is no external Python administration path. (Acknowledged complication: Exasol Lua scripts run with invoker rights, so reading `EXA_DBA_ROLE_PRIVS` needs a privilege story.)

**Q:** How should role membership be expanded when resolving CONFIG rules to users?
**A:** Transitive (nested roles) — walk the role-to-role grant graph in `EXA_DBA_ROLE_PRIVS` so a rule scoped to `ROLE_A` applies to users holding `ROLE_B` where `ROLE_B` was granted `ROLE_A`. Do this in SQL using Exasol's native `CONNECT BY` hierarchical query; the Lua script orchestrates (executes the SQL, owns the transaction).

**Q:** When a user ends up with multiple matching CONFIG rules for the same phase, what wins?
**A (revised — see "Precedence correction" below):** Nothing "wins" — the user's pipeline is the additive UNION (superset) of every enabled ROLE-derived and USER-scoped rule, exactly like SQL privileges. A USER-scoped rule adds to, never replaces, ROLE-derived rules. Ordering within a phase is pure `rule_id` ascending with scope type ignored. Identical `(phase, script, function)` reaching a user by multiple paths is deduplicated to one row. No hard failures. *(An earlier answer to this question — "USER overrides ROLE for the phase" — was rejected by the product owner; the override semantics are gone.)*

**Q:** What's in scope for this slice beyond populating RESOLUTION?
**A:** All three: (1) Atomic full rebuild + last_refresh — DELETE + reinsert RESOLUTION in one transaction, stamp `SETTINGS.last_refresh`. (2) MASTER dispatch reads pipeline — upgrade `master.lua` from pass-through to querying `MY_PIPELINE` and dispatching to resolved scripts. (3) Staleness reporting — detect users/role-grants changed since last_refresh; DB-side (a Lua script and/or view callable from any SQL client), not a Python verb.

**Q:** Should administration live in the Python CLI or in the database?
**A:** In the database. The user is generally not a fan of using Python outside the environment to manage it; the entire management should be done within scripts in the DB and accessible via Lua. The Python CLI (`preproc`) is for deployment only (applying DDL, deploying Lua scripts), not runtime administration. Drop the `preproc refresh` and `preproc status` verbs entirely. Rationale: with all management DB-side, the framework can later develop its own management *syntax* intercepted and controlled by the preprocessor itself.

## Correction to the prior plan

This plan was revised after the prior planner run made a factual error and an architectural choice the owner rejected.

### Prior claim was WRONG: CONNECT BY is not a licensed add-on

The prior decision log and plan claimed that Exasol's `CONNECT BY` / "Graph Search" is a separately-licensed add-on that cannot be assumed on a customer instance, and on that basis chose iterative Lua graph-walking for transitive role expansion. **That claim is factually incorrect.** Exasol's `CONNECT BY` (`CONNECT BY PRIOR` / `START WITH`) hierarchical query is native, unlicensed Exasol SQL, confirmed by the owner (Exasol's Lead PM for the core database). The transitive role expansion is therefore done directly in SQL via `CONNECT BY` over `EXA_DBA_ROLE_PRIVS`. The Lua script no longer walks the graph itself; it orchestrates the SQL and the transaction.

### Prior approach was rejected: Python CLI administration

The prior plan added `preproc refresh` and `preproc status` Python verbs as thin `EXECUTE SCRIPT` wrappers. The owner rejected runtime administration in Python. All administration moves DB-side. The Python CLI keeps only deployment (install ships the DDL and Lua scripts). The `refresh/cli-refresh` feature delta is deleted; the `status` capability becomes a DB-side `PREPROC.STATUS` object.

## Corrections in the second revision round

After the spec deltas were drafted, the product owner (Exasol's Lead PM for the core database) reviewed them and issued two corrections and one mission-level terminology decision.

### Precedence corrected: UNION (superset), not override

The drafted role-resolution spec said a USER-scoped rule for a phase REPLACES all ROLE-derived rules for that phase. **This was wrong.** Corrected to additive UNION semantics modelled on SQL privileges (see Design Decision [5] for the full rationale and the owner's near-verbatim statement). Override/exclusion is gone entirely. Ordering within a phase is pure `rule_id` ascending with scope type ignored — confirmed via a follow-up question in which the owner explicitly rejected role-rules-first ordering. Identical `(phase, script, function)` reaching a user by multiple paths is deduplicated to one row.

### Phase taxonomy re-cut: TRANSLATE → EXPAND → REWRITE (mission-level)

The prior taxonomy was TRANSLATE → REWRITE → SUGAR (REWRITE = whole-statement custom commands; SUGAR = commutative token conveniences). The owner disliked "SUGAR" and approved a re-cut along a cleaner axis — *"was the input already valid Exasol SQL?"* (see Design Decision [9]). All plan deltas, plan.md, decision-log.md, and the mission glossary/capabilities/data-flow now use the new taxonomy. Old `SUGAR` phase examples in role-resolution scenarios became `EXPAND`.

## Design Decisions

### [1] Refresh reads the dictionary via SELECT ANY DICTIONARY granted to PREPROC_ADMIN; gated to admins, never PUBLIC

- **Decision:** `EXA_DBA_ROLE_PRIVS` and `EXA_DBA_USERS` are visible only with `SELECT ANY DICTIONARY` (verified in Exasol docs). Because invoker-rights Lua sees only what the *caller* holds, grant `SELECT ANY DICTIONARY` to the `PREPROC_ADMIN` role and restrict `PREPROC.REFRESH` (and `PREPROC.STATUS`) execution to that role. Both are administrative operations callable from any SQL client by an admin.
- **Alternatives:** A definer-rights view over the dictionary exposed to PUBLIC (rejected — leaks the database's role/user topology to every user); per-table SELECT grants to PUBLIC (same leak); making refresh callable by any user (rejected — privilege escalation surface).
- **Rationale:** Minimal, auditable privilege bridge that keeps security topology private. "Callable from any SQL client" is honoured for *admins* — any client works, but the caller must hold the admin role.
- **Promotes to ADR:** yes

### [2] Transitive role expansion is native SQL CONNECT BY, orchestrated by Lua

- **Decision:** Build the per-user transitive role closure with a SQL `CONNECT BY` hierarchical query over `EXA_DBA_ROLE_PRIVS` (`START WITH` the user/role, `CONNECT BY PRIOR` the grantee→granted edge). The Lua refresh script issues this query, joins the closure against enabled `PREPROC.CONFIG`, and writes `RESOLUTION`. The Lua does not iterate the grant graph.
- **Alternatives:** Iterative Lua graph-walking (this is what the prior plan chose, on the false premise that `CONNECT BY` was a paid add-on — rejected: more code, slower, no reason to avoid native SQL); `WITH RECURSIVE` CTE (Exasol does not support recursive CTEs); doing the join entirely in SQL with no Lua (rejected — the Lua script still needs to own the atomic rebuild transaction and return the result row count).
- **Rationale:** `CONNECT BY` is native, unlicensed Exasol SQL. Expressing transitive closure declaratively in SQL is simpler, faster, and uses the engine's own hierarchical-query support rather than re-implementing graph traversal in Lua. The owner (Exasol Lead PM, core DB) confirmed `CONNECT BY` is native.
- **Promotes to ADR:** yes

### [3] All framework administration is DB-side; the Python CLI is deployment-only

- **Decision:** Refresh and status are DB-side `PREPROC_ADMIN`-owned objects (`PREPROC.REFRESH`, `PREPROC.STATUS`), callable from any SQL client. The Python CLI (`preproc`) does deployment only — applying DDL and deploying the Lua scripts during install. No `preproc refresh` / `preproc status` runtime verbs exist.
- **Alternatives:** Thin Python `EXECUTE SCRIPT` wrapper verbs (this is what the prior plan had — rejected by the owner: Python should not manage the environment at runtime); split administration between Python and DB (rejected — inconsistent, and forecloses the future-syntax direction).
- **Rationale:** Keeping all management in the database means the framework owns its management surface end-to-end and can later intercept and control its own management *syntax* (see [4]). It also avoids requiring an external tool, language runtime, or duplicated logic for routine administration.
- **Promotes to ADR:** yes

### [4] Name and shape the DB-side scripts so a future management syntax maps onto them cleanly

- **Decision:** Treat `PREPROC.REFRESH` and `PREPROC.STATUS` as stable, named admin entry points with simple, syntax-mappable contracts (no required parameters for the common case; a result set reporting outcome). This keeps the door open — without building it now — for a future framework-controlled management syntax (e.g. a preprocessor-intercepted `PREPROC REFRESH` / `SHOW PREPROCESSOR STATUS` statement translated by MASTER into an `EXECUTE SCRIPT PREPROC.REFRESH` / `PREPROC.STATUS` call). This aligns with the mission's `EXPLAIN PREPROCESSOR` / `SHOW PREPROCESSOR PIPELINE` capability.
- **Alternatives:** Build the intercepted management syntax now (rejected — out of scope for this slice; premature); give the scripts ad-hoc names/contracts (rejected — would force a future syntax layer to adapt or rename, churning the admin surface).
- **Rationale:** A small naming/contract discipline today removes friction from the planned management-syntax layer later. No implementation cost now.
- **Promotes to ADR:** yes

### [5] Additive UNION (SQL-privilege model) of ROLE + USER rules, deduplicated, ordered by `rule_id` ascending via an explicit ordinal column

- **Decision:** A user's resolved pipeline is the additive UNION (superset) of every enabled `ROLE`-derived rule (via the transitive closure) and every enabled `USER`-scoped rule that applies — modelled on SQL privileges. There is no override, replacement, or exclusion mechanism of any kind. Identical `(phase, script, function)` reaching a user by multiple paths (multiple roles, or role + direct user) is deduplicated to exactly one row, keeping the lowest contributing `rule_id`. Within a phase, ordering is pure `rule_id` ascending with scope type ignored, recorded in the `PREPROC.RESOLUTION.ordinal` column; `MY_PIPELINE` orders by `phase, ordinal`. To make a user execute less, the admin removes them from a role, disables a rule, or defines a narrower rule — never a per-user subtraction.
- **Alternatives:** USER-scoped rule overrides/replaces ROLE-derived rules for the phase (the prior plan's choice — **rejected by the product owner**: no SQL-privilege analogue, surprising for admins); role-rules-first ordering, i.e. all ROLE rules before USER rules within a phase (**rejected on a follow-up question** — the user wanted scope-agnostic `rule_id` order); carrying `rule_id` through to the view and ordering by it (rejected — `MY_PIPELINE` does not and should not expose `rule_id`); hard-fail on conflicting rules (rejected per interview — no hard failures).
- **Rationale (product owner, near-verbatim):** "Rules that are executed are the superset of ROLE + USER, much like with SQL privileges. In SQL, I cannot define that user DAVE is assigned to a role that has CREATE ANY VIEW and CREATE ANY TABLE, but then I exclude CREATE ANY TABLE from DAVE directly. If the admin wants a user to execute less, then they should be removed from a role or a new role created or define a rule directly for that user." If ROLE_A runs functions a,b,c and DAVE has a direct rule for d, then in that phase DAVE runs a,b,c,d. Dedup prevents the same function running twice (surprising, wasteful). An explicit ordinal carries the `rule_id`-ascending order through the view without leaking internal `rule_id`s.
- **Promotes to ADR:** yes

### [6] Atomic full rebuild via DELETE + reinsert + single commit in the script

- **Decision:** The refresh script deletes all of `RESOLUTION`, inserts the freshly resolved rows (from the `CONNECT BY` join), writes `last_refresh` and the state fingerprint, then commits once. A Lua script can issue an explicit `commit()` even though it cannot read the client autocommit setting.
- **Alternatives:** MERGE/upsert without delete (rejected — would not drop orphaned rows from removed rules); per-statement autocommit (rejected — leaves a half-rebuilt resolution observable).
- **Rationale:** Full rebuild is the simplest correct semantics and drops orphans for free; one commit guarantees atomic visibility of resolution + timestamp together.
- **Promotes to ADR:** yes

### [7] Staleness via a state fingerprint stored at refresh and recomputed by a DB-side status object

- **Decision:** At refresh, store a compact fingerprint (count + hash over ordered user and role-grant identifiers) in `PREPROC.SETTINGS`. The DB-side `PREPROC.STATUS` object recomputes the fingerprint and compares; a mismatch means stale. Refresh and status share one fingerprint definition. Status is read-only.
- **Alternatives:** Timestamp-only "you might be stale" (rejected — cannot tell whether anything actually changed); full diff of dictionary against resolution inputs on every status (rejected — more expensive and unnecessary for a yes/no signal); a Python `status` verb (rejected — administration is DB-side per [3]).
- **Rationale:** O(grants) cost, definitive current/stale answer, computed where the dictionary access already lives.
- **Promotes to ADR:** no

### [8] MASTER dispatch layered on the existing guard/fail-open base; per-rule fail-open

- **Decision:** Add the dispatch loop above the already-tested guard and view-lookup fail-open. Iterate `MY_PIPELINE` rows, skip the no-rules sentinel, apply phases `TRANSLATE`→`EXPAND`→`REWRITE`, within phase in `ordinal` order, thread text rule-to-rule, and wrap each module dispatch in pcall so a failing rule is skipped rather than blocking the statement.
- **Alternatives:** Rewrite MASTER from scratch (rejected — discards the tested safety contract from ADR-004); fail the whole statement on any module error (rejected — violates the fail-open invariant).
- **Rationale:** Honours ADR-004 — dispatch is added without weakening the safety contract; per-rule fail-open preserves availability.
- **Promotes to ADR:** no

### [9] Phase taxonomy re-cut along "was the input already valid Exasol SQL?": TRANSLATE → EXPAND → REWRITE

- **Decision:** Replace the TRANSLATE → REWRITE → SUGAR taxonomy with TRANSLATE → EXPAND → REWRITE, cut along whether the input was already valid Exasol SQL.
  - **TRANSLATE** — foreign-dialect statements transpiled to Exasol SQL (unchanged; max 1/user; fail-closed).
  - **EXPAND** — input is NOT valid Exasol SQL; transforms make new syntax runnable (language extension). Absorbs BOTH the old REWRITE (whole-statement custom commands like `LS` / backup-start → `EXECUTE SCRIPT`; first match terminal) AND the old SUGAR (token-level conveniences; must stay commutative since they stack). The terminal-vs-commutative distinction is now a function-contract rule WITHIN the phase, not a phase boundary. Fail-open is inherently safe: an un-expanded statement dies in the engine with a loud syntax error.
  - **REWRITE** (redefined) — input IS valid Exasol SQL; transforms modify its behaviour (policy/filter injection, table redirection during migration, guardrails like injected LIMITs). Currently a defined-but-empty slot (no shipped examples). Fail-open-SENSITIVE: an un-applied REWRITE silently runs the original statement = silent policy bypass, so its contract must be designed fail-closed-capable (per-rule or per-phase) from day one.
  - **Pipeline order rationale:** EXPAND must precede REWRITE so behaviour-modifying rules always operate on valid, fully-expanded SQL.
- **Alternatives:** Keep TRANSLATE → REWRITE → SUGAR (rejected — owner disliked "SUGAR" and the REWRITE/SUGAR split was a fuzzy terminal-vs-commutative axis, not a property of the input); a two-phase TRANSLATE → EVERYTHING-ELSE cut (rejected — loses the fail-open-safe vs fail-open-sensitive boundary that the valid-SQL axis exposes).
- **Evidence:** All four examples in Exasol's official preprocessor docs (sqlglot T-SQL TOP-N, `IF()`→`CASE WHEN`, the `LS` command, ANY/ALL rewrite) land in TRANSLATE/EXPAND under this axis — none modify already-valid Exasol SQL. This supports the re-cut: the behaviour-modifying REWRITE phase genuinely has no shipped example yet, which is why it is a defined-but-empty slot.
- **Rationale:** The valid-SQL axis is a crisp, testable boundary that also aligns with the safety contract: TRANSLATE fail-closed, EXPAND fail-open-safe, REWRITE fail-open-sensitive. It removes the disliked "SUGAR" term and folds the commutativity rule down to where it belongs (the function contract).
- **Promotes to ADR:** yes

### [10] A reference REWRITE module is wanted as future work (not this slice)

- **Decision:** Record that the product owner wants a concrete REWRITE example in a later slice ("we should also later have an example of rewrite to use then"). The REWRITE phase ships in this slice as a defined-but-empty, fail-open-sensitive slot with no module. **Do NOT add a REWRITE module to this slice's tasks** — it is explicitly deferred.
- **Rationale:** A reference REWRITE module (e.g. policy/filter injection) would exercise the fail-open-sensitive contract and give the phase a worked example, but it is out of scope here. Capturing it now prevents the empty slot from being mistaken for an oversight and gives the future planner a clear hook.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
