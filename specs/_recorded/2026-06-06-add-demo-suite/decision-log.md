# Decision Log: add-demo-suite

Date: 2026-06-05

## Interview

**Q:** Which foreign-dialect translation should DEMO_TRANSLATE_V1 demonstrate? (NOW() is out — valid Exasol syntax.)
**A:** The bigger question is to find a syntax that sqlglot will actually translate without any other code. Find that and use it as an example.

**Q:** How should deployment be packaged, given your one-SQL-file preference?
**A:** Generated bundle — a generation step that emits one `deploy_all.sql` from the install SQL + Lua sources. One file to run, no source duplication. CLI install stays for dev/CI.

**Q:** How should the demo config (users, roles, rules) be set up and torn down?
**A:** SQL seed + teardown scripts (`sql/demo/seed.sql` + `teardown.sql`) — fits the one-SQL-file philosophy, readable as documentation of the config model itself.

## Design Decisions

### [1] Demo translation example: T-SQL TOP n (+ GETDATE())

- **Decision:** Use T-SQL `SELECT TOP n …` → `SELECT … LIMIT n` as the headline demo translation, with `GETDATE()` → `CURRENT_TIMESTAMP()` as a secondary example. Chosen after a live feasibility experiment against the docker-db (sqlglot 27.17.0, `read='tsql', write='exasol'`).
- **Feasibility evidence:** `SELECT TOP 5 col FROM t` → `SELECT col FROM t LIMIT 5`; `GETDATE()` → `CURRENT_TIMESTAMP()`; `ISNULL`→`COALESCE`; MySQL backticks → double quotes; Postgres `::int` → `CAST(... AS INT)` — all transpiled with zero extra code. Critically, raw `SELECT TOP 5 …` is a hard Exasol syntax error (verified: "syntax error, unexpected UNSIGNED_INTEGER_"), and the transpiled `CURRENT_TIMESTAMP()` executes — so the demo shows a statement that would fail being silently made to work.
- **Alternatives:** `NOW()` (rejected by interviewer — valid Exasol). Postgres `::` casts / MySQL backticks (work, but less visually compelling than `TOP`→`LIMIT`).
- **Rationale:** Maximum demo impact (visible failure → success), zero extra transpiler code, empirically verified on the live target version.
- **Promotes to ADR:** no

### [2] TRANSLATE trigger heuristic: explicit dialect-marker comment

- **Decision:** Trigger translation only when a statement begins with `--!dialect:<name>` (e.g. `--!dialect:tsql`). The module strips the marker, calls the UDF with that dialect, and on success replaces the statement; otherwise returns the text unchanged.
- **Alternatives:** Always-transpile for holders of a demo role (rejected: ~133–151 ms/statement UDF cost with no warm-VM reuse, and cross-dialect quote-escaping can corrupt already-valid Exasol SQL).
- **Rationale:** Explicit, fail-closed-friendly, self-documenting, cheap (only marked statements pay the UDF cost), and demo-readable.
- **Promotes to ADR:** yes

### [3] Fail-closed TRANSLATE via ERR: sentinel from the UDF

- **Decision:** The Python UDF returns `ERR:<message>` (never raises) on any sqlglot parse/transpile failure. The Lua module treats any `ERR:`-prefixed result as a failure and returns the original statement text unchanged, never threading the sentinel or partial text forward.
- **Alternatives:** Let the UDF raise (caught by MASTER's per-rule pcall fail-open) — rejected because it loses the ability to distinguish failure from a legitimately short transpilation and risks threading corrupted text.
- **Rationale:** The mission mandates TRANSLATE be fail-closed; the sentinel gives the module a reliable, in-band failure signal so it never emits wrong-dialect or corrupted SQL.
- **Promotes to ADR:** yes

### [4] Bundle is generated and slash-delimited

- **Decision:** `preproc bundle` generates `deploy_all.sql` from the `sql/` sources, terminating each statement with a `/` on its own line (EXAplus-style). The file is never hand-edited; CLI install remains the dev/CI path.
- **Alternatives:** `;`-split single file (rejected: script/UDF bodies contain semicolons and would break); committing a static bundle (rejected: drifts from sources).
- **Rationale:** Script bodies contain semicolons, so only the `/` terminator round-trips them in interactive clients; generation keeps the `sql/` tree the single source of truth.
- **Promotes to ADR:** yes

### [5] Demo config delivered as commented SQL seed/teardown, not a Python tool

- **Decision:** `sql/demo/seed.sql` and `sql/demo/teardown.sql` create/destroy demo roles, users, and CONFIG rules (reserved `rule_id` range), run `PREPROC.REFRESH`, and carry heavy comments so the seed reads as config-model documentation.
- **Alternatives:** A `preproc demo` Python verb (rejected: contradicts the one-file/DB-side-administration stance and is less readable as documentation).
- **Rationale:** Matches the interview answer and the framework's "administered through DB-side scripts" principle; the seed doubles as living documentation of scoping/roles/phases/resolution.
- **Promotes to ADR:** no

### [6] Audit REWRITE appends an SQL comment, no table writes

- **Decision:** `DEMO_AUDIT_V1` appends `/* PREPROC_AUDIT user=<CURRENT_USER> */` to the statement; it writes to no table and does not change query results.
- **Alternatives:** Writing an audit-log table row at compile time (rejected: explicitly out of scope in the mission — transaction-conflict hazard).
- **Rationale:** Safest visible REWRITE demonstration; honours the out-of-scope boundary while still proving the phase fires per-user.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
