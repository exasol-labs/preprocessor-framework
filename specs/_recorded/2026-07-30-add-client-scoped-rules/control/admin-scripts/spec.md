# Feature: Admin Control Scripts

Provides the complete SQL-only administrative surface for the preprocessor framework as `EXECUTE SCRIPT` entry points in the `PREPROC` admin schema, so every config and settings mutation an operator performs goes through a validating script rather than direct DML. The scripts own rule lifecycle (add, drop, enable, disable, list) and settings management (mode, self-disable flag), validate their inputs so the scripted path is the obviously-correct one, and keep `PREPROC.RESOLUTION` current by auto-refreshing after each mutation (with an opt-out for batch workflows). Direct `INSERT`/`UPDATE`/`DELETE` against `PREPROC.CONFIG` remains technically possible (invoker rights preclude a hard lock) but is declared unsupported. This adds `PREPROC.SET_SYS_GROUPING`, the setter for the `PREPROC.SETTINGS.group_sys_with_dba` policy. `group_sys_with_dba` joins `mode` and `self_disable_allowed` as the operator-mutable settings columns; because the value changes resolution (whether `SYS` receives ROLE-derived rows), the setter auto-refreshes like `SET_MODE` (with the same `NOREFRESH` opt-out), rather than being a pure settings write like `SET_SELF_DISABLE`. This delta closes the last gap in the `client_pattern` surface: `PREPROC.RULE_ADD` gains a `client_pattern` parameter so a client-scoped rule can be CREATED through the sanctioned control surface, `PREPROC.RULE_LIST` returns `CLIENT_PATTERN` so it can be OBSERVED there, and a single shared validator rejects the patterns that would silently misbehave. The column has been honoured end-to-end by dispatch, resolution and profile codegen since the beginning; only the operator-facing half was missing.

## Background

* The control scripts live in the `PREPROC` admin schema alongside `REFRESH` and `STATUS`, are deployed by `preproc install` and emitted by `preproc bundle`, and are unversioned `CREATE OR REPLACE LUA SCRIPT` objects (they are not dispatched modules, so the `_V1` versioning discipline does not apply — same precedent as `REFRESH`/`STATUS`, ADR-008).
* The scripts run with INVOKER rights (Exasol Lua constraint): a caller must already hold the underlying DML privileges on `PREPROC.CONFIG`/`PREPROC.SETTINGS`, which are granted to `PREPROC_ADMIN`. A non-admin caller fails at the first table access with an insufficient-privileges error before any mutation. Hard lockdown of direct DML is therefore impossible; the scripts win on validation and convenience, not enforcement (validation + convention).
* All `pquery` calls bind parameters with named `:param` placeholders sourced from a keyed table; positional `?` placeholders silently fail under Exasol Lua and MUST NOT be used.
* Mutating scripts (`RULE_ADD`, `RULE_DROP`, `RULE_ENABLE`, `RULE_DISABLE`, `SET_MODE`, `SET_SELF_DISABLE`, `SET_SYS_GROUPING`) auto-run the refresh logic inside the same script invocation after a successful mutation, because an EXPAND expansion is a single statement and a separate `REFRESH` call cannot be chained. A `NOREFRESH` parameter suppresses the auto-refresh for batch workflows where the caller will refresh once at the end.
* Read-only scripts (`RULE_LIST`) are `RETURNS TABLE` and never mutate `CONFIG`, `SETTINGS`, or `RESOLUTION`.
* `PREPROC.CONFIG` has no ordinal column; ordering within a phase is derived by refresh from `rule_id` ascending. The operator rule_id space is below the reserved demo range (9000–9999); `RULE_ADD` auto-allocates rule_ids from the operator space and never collides with the demo range.
* `PREPROC.SETTINGS` is a single-row table with columns `mode`, `self_disable_allowed`, `last_refresh`, `state_fingerprint`, `group_sys_with_dba`. `mode`, `self_disable_allowed`, and `group_sys_with_dba` are operator-mutable through the control surface; `last_refresh` and `state_fingerprint` are owned by refresh.
* Script existence validation uses the dictionary view `EXA_ALL_SCRIPTS` (or `EXA_ALL_SCRIPTS`-equivalent), readable by the admin caller.
* `PREPROC.SET_SYS_GROUPING` is modelled on `PREPROC.SET_MODE`: it is an unversioned `CREATE OR REPLACE LUA SCRIPT`, runs with INVOKER rights, binds `:param` placeholders, validates its argument BEFORE any UPDATE (side-effect-free rejection), then either runs the shared refresh logic or, under `NOREFRESH`, commits the UPDATE on its own. It is `RETURNS TABLE` and returns the new value.
* Its parameter list is `(value, norefresh)`, declared in the `CREATE` header emitted by `install.py` / `bundle.py` (parameters are bare names, no SQL types). `value` is the new boolean policy; a trailing `norefresh` boolean suppresses the auto-refresh for batch workflows.
* Validation domain: the boolean argument must be a genuine boolean (`TRUE` or `FALSE`). Any other value — `NULL`, a string, a number — is rejected with a validation error naming the offending value, before any UPDATE, so a bad call leaves `PREPROC.SETTINGS` exactly as it was. This mirrors `SET_MODE`'s validate-first whitelist, with `{TRUE, FALSE}` as the whitelist.
* The command sugar (`PREPROC …`) exposes this setter as `PREPROC SET SYS_GROUPING <bool> [NOREFRESH]`, added to the live `CONTROL_SUGAR_V1` module in place. This in-place grammar change is a pre-release exception justified by there being no active sessions yet (the never-`CREATE OR REPLACE`-a-live-dispatch-module hazard of ADR-019 does not apply); any POST-release grammar change MUST still arrive via a new `CONTROL_SUGAR_V2`. The full sugar contract lives in the `control/command-sugar` feature; here the setter remains directly invocable as `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(...)`.
* `PREPROC.CONFIG.client_pattern` is `VARCHAR(256)`, nullable, and NULL means "apply to all clients". Dispatch matches it as a LITERAL SUBSTRING of the connecting client's self-reported application name (`string.find(client, pattern, 1, true)`) — not a Lua pattern, not a SQL `LIKE` wildcard. Nothing about that matching behaviour changes here; only the write and read surfaces do.
* `PREPROC.RULE_ADD`'s parameter list becomes `(scope_type, scope_value, phase, script_name, function_name, client_pattern, norefresh)` — `client_pattern` inserted between `function_name` and `norefresh`, making the list identical to `PREPROC.RULE_ENSURE`'s. The two scripts then differ only in insert-vs-upsert semantics, which is the distinction they exist to draw.
* Exasol Lua scripts have NO optional parameters — `EXECUTE SCRIPT` enforces an exact argument count. Adding the parameter is therefore a BREAKING signature change for every existing `RULE_ADD` caller. The failure is an immediate argument-count error at the call with no partial write, which is the correct failure mode for this class of change: loud beats silent.
* `client_pattern` validation is implemented ONCE and shared by every write path, so no path can persist a pattern the other would reject. The shared helper is exported from the already-imported `PREPROC.REFRESH_CORE` module (the framework's only cross-script Lua sharing mechanism, `import(name, alias)`), which both `RULE_ADD` and `RULE_ENSURE` already load. The import moves to the top of each script so validation precedes any DML; `import()` only binds the module's functions and performs no work of its own.
* Validation domain for `client_pattern`: NULL is ACCEPTED and means "all clients". An empty or whitespace-only pattern is REJECTED — `string.find(client, "", 1, true)` returns 1, so an empty pattern matches EVERY client while reading in `RULE_LIST` output as a restriction; the error names NULL as the way to express "all clients". A pattern longer than the `VARCHAR(256)` column width is REJECTED and NEVER truncated, because silently clipping a SUBSTRING pattern WIDENS the set of clients it matches (`'DbVisualizer-Prod'` clipped to `'DbVisualizer'` fires for every DbVisualizer session).
* `client_pattern` validation joins the existing validate-before-mutate chain (phase, then scope_type, then script existence), all before any DML, so a rejected call leaves `PREPROC.CONFIG` and `PREPROC.RESOLUTION` exactly as they were.
* The stored pattern is NEVER case-folded, trimmed, or otherwise normalised. It is matched against a self-reported application string, not an Exasol identifier: measured values on Exasol 2026.1 include `DbVisualizer 24.1.5`, `DBeaver 23.3.0`, `PyExasol 2.2.1`, and `EXAplus`. Upper-casing `DbVisualizer` to `DBVISUALIZER` would match nothing, and would do so SILENTLY — the rule resolves, dispatch evaluates it, and it simply never fires.
* `client_pattern` is SCOPING, not security: the client application name is self-reported by the connecting tool and trivially spoofable. It exists to keep conveniences out of batch jobs, never to withhold a transformation from a principal — that is what ROLE/USER scoping and schema `EXECUTE` grants are for.
* `RULE_LIST` remains `RETURNS TABLE` and read-only; gaining a column changes its result shape but not its contract. `CLIENT_PATTERN` is positioned after `scope_value`, mirroring the `PREPROC.CONFIG` column order.

## Scenarios

<!-- DELTA:CHANGED -->

### Scenario: RULE_ADD inserts a validated rule and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* a deployed module script the rule will reference
* *WHEN* the caller executes `RULE_ADD` with a valid scope type, scope value, phase, script name, function name, and `client_pattern`
* *THEN* the script MUST insert exactly one enabled row into `PREPROC.CONFIG` carrying those values, allocating a `rule_id` in the operator space below the reserved demo range (9000–9999)
* *AND* the script MUST run the refresh logic so `PREPROC.RESOLUTION` reflects the new rule before the call returns
* *AND* the script SHOULD return the allocated `rule_id` so the caller can observe the outcome

<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->

### Scenario: RULE_ADD persists a client_pattern verbatim

* *GIVEN* a caller holding `PREPROC_ADMIN` and a deployed module script the rule will reference
* *WHEN* the caller executes `RULE_ADD` with a mixed-case, non-empty `client_pattern` such as `DbVisualizer`
* *THEN* the inserted `PREPROC.CONFIG` row's `client_pattern` column MUST hold the supplied value byte-for-byte
* *AND* the value MUST NOT be case-folded, trimmed, or otherwise normalised, because it is matched against a self-reported application string rather than an Exasol identifier
* *AND* `PREPROC.RESOLUTION` MUST carry the same pattern before the call returns

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: RULE_ADD treats a NULL client_pattern as all clients

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` with `client_pattern` as SQL NULL
* *THEN* the script MUST insert the row with `client_pattern` NULL
* *AND* the resolved rule MUST apply regardless of the connecting client, preserving the behaviour of every rule created before this delta

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: RULE_ADD rejects an empty or whitespace-only client_pattern

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` with a `client_pattern` that is the empty string, or consists only of whitespace
* *THEN* the script MUST reject the call with a validation error explaining that an empty pattern would match every client, and naming NULL as the way to express "all clients"
* *AND* the script MUST NOT insert any row into `PREPROC.CONFIG`
* *AND* `PREPROC.RESOLUTION` MUST remain unchanged

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: RULE_ADD rejects an over-length client_pattern without truncating

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` with a `client_pattern` longer than the `PREPROC.CONFIG.client_pattern` column width of 256 characters
* *THEN* the script MUST reject the call with a validation error naming the offending length
* *AND* the script MUST NOT insert a truncated value, because a silently truncated substring pattern matches a WIDER set of clients than the operator asked for
* *AND* `PREPROC.RESOLUTION` MUST remain unchanged

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: RULE_ADD validates the client_pattern before any side effect

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `RULE_ADD` with BOTH an invalid phase and an invalid `client_pattern`
* *THEN* the script MUST reject the call
* *AND* `PREPROC.CONFIG` and `PREPROC.RESOLUTION` MUST be exactly as they were before the call, with no partial write and no refresh having run
* *AND* `PREPROC.SETTINGS.last_refresh` MUST be unchanged by the rejected call

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: RULE_ADD keeps its parameter list aligned with RULE_ENSURE

* *GIVEN* the deployed `PREPROC.RULE_ADD` and `PREPROC.RULE_ENSURE` scripts
* *WHEN* their declared parameter lists are compared
* *THEN* `RULE_ADD`'s MUST be `(scope_type, scope_value, phase, script_name, function_name, client_pattern, norefresh)`
* *AND* it MUST match `RULE_ENSURE`'s parameter names and order exactly, so the two scripts differ only in insert-vs-upsert behaviour
* *AND* a caller passing the pre-delta six-argument form MUST fail with an argument-count error rather than binding a value to the wrong parameter

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: A single shared validator governs client_pattern on every write path

* *GIVEN* the deployed control scripts that write `PREPROC.CONFIG.client_pattern`, namely `RULE_ADD` and `RULE_ENSURE`
* *WHEN* the same invalid `client_pattern` is supplied to each of them in turn
* *THEN* both MUST reject it, so no supported path can persist a pattern another path would refuse
* *AND* the validation MUST be implemented exactly once and shared, rather than duplicated per script, so the two surfaces cannot drift apart
* *AND* neither script MUST have written to `PREPROC.CONFIG` when it rejects

<!-- /DELTA:NEW -->

<!-- DELTA:CHANGED -->

### Scenario: RULE_LIST returns the current rules and is read-only

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* `PREPROC.CONFIG` containing at least one rule carrying a `client_pattern` and at least one without
* *WHEN* the caller executes `RULE_LIST`
* *THEN* the script MUST return one row per rule carrying `rule_id`, `scope_type`, `scope_value`, `client_pattern`, `phase`, `script_name`, `function_name`, and `enabled`, so an operator never has to query `PREPROC.CONFIG` directly to discover that a rule is client-scoped
* *AND* the `client_pattern` column MUST carry the stored pattern for the client-scoped rule and NULL for the unscoped one
* *AND* the script MUST NOT modify `PREPROC.CONFIG`, `PREPROC.SETTINGS`, or `PREPROC.RESOLUTION`, and the rows SHOULD be ordered by `rule_id` ascending

<!-- /DELTA:CHANGED -->
