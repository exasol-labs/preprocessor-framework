# Decision Log: add-install-two-schema-layout

Date: 2026-06-05

## Interview

**Q:** What authentication model should the CLI use to connect to Exasol?
**A:** Environment variables — `EXASOL_DSN`, `EXASOL_USER`, `EXASOL_PASSWORD`. Standard 12-factor; composable with `.env` for local dev and CI secrets.

**Q:** Should `preproc install` be safe to re-run?
**A:** Yes. Use `CREATE SCHEMA/TABLE/VIEW IF NOT EXISTS` and `CREATE OR REPLACE` for scripts and views. Re-running is a no-op if nothing changed, and an upgrade path if the DDL changes.

**Q:** Should install activate the preprocessor?
**A:** No. Activation is left to the DBA. Install creates all objects and prints the `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER` statement but does not execute it. An `--activate` flag may come in a later slice but is explicitly out of scope here.

## Design Decisions

### [1] Two-schema layout with a definer-rights view as the privilege bridge

- **Decision:** Split objects into a locked admin schema `PREPROC` (config/resolution/settings/profiles, owned by `PREPROC_ADMIN`) and a PUBLIC runtime schema `PREPROC_RT` (MASTER, views). The only path from invoker-rights Lua scripts to admin tables is the definer-rights view `PREPROC_RT.MY_PIPELINE`, filtered by `CURRENT_USER`.
- **Alternatives:** Single schema with table grants to PUBLIC (leaks admin config to all users); scripts reading admin tables directly (impossible — invoker rights, verified experimentally).
- **Rationale:** Exasol scripts run with invoker rights and views with definer rights; this asymmetry is the load-bearing mechanism that lets the framework expose per-user pipeline data without granting users access to the admin tables.
- **Promotes to ADR:** yes

### [2] Schema-level PUBLIC grants are a framework invariant

- **Decision:** Install issues `GRANT EXECUTE ON SCHEMA PREPROC_RT TO PUBLIC` and `GRANT SELECT ON SCHEMA PREPROC_RT TO PUBLIC`; these schema-level grants cover all current and future runtime objects.
- **Alternatives:** Per-object grants re-issued on every object creation (fragile; a missed grant is catastrophic).
- **Rationale:** In enforced mode a user lacking EXECUTE on the active preprocessor cannot run *any* statement (even `ALTER SESSION` is blocked) — total lockout. Schema-level grants make the invariant durable; a future `status` verb will verify it. Verified experimentally.
- **Promotes to ADR:** yes

### [3] Install creates but does not activate; prints the activation statement

- **Decision:** `preproc install` creates every object but never runs the `ALTER SYSTEM` activation; it prints the exact statement for the DBA. `--activate` is deferred.
- **Alternatives:** Auto-activate on install; add `--activate` now.
- **Rationale:** Activation is system-wide and high-blast-radius; the DBA must own the cutover. Keeps this bedrock slice safe.
- **Promotes to ADR:** yes

### [4] Idempotent re-run as the upgrade path (no migrations framework)

- **Decision:** Idempotent DDL (`IF NOT EXISTS`, `CREATE OR REPLACE`, guarded single-row seed) is the upgrade mechanism; no migrations/version table in this slice.
- **Alternatives:** Separate `upgrade` verb; a schema-version tracking table.
- **Rationale:** Simpler and sufficient now; DDL diffs are the upgrade. Revisit if destructive migrations become necessary.
- **Promotes to ADR:** no

### [5] Env-var-driven pytest fixture instead of an Exasol pytest plugin

- **Decision:** Hand-roll the pytest connection fixture in `tests/conftest.py`, resolving `EXASOL_DSN/USER/PASSWORD`, rather than depending on `pytest-exasol-backend`. Tests skip cleanly when the env is unset.
- **Alternatives:** `pytest-exasol-backend` / `pytest-itde` plugins.
- **Rationale:** Minimal dependency surface and one identical connection path whether the target is a Docker container (CI) or a live instance (local) — matches the 12-factor auth decision. Full control over skip-vs-error behavior.
- **Promotes to ADR:** no

### [6] MASTER ships as a guarded fail-open skeleton, not a bare pass-through

- **Decision:** The skeleton already implements the dangerous contract: guard `ALTER SESSION/SYSTEM` (return early, no lookup, no rewrite), fail open on any lookup error, return `sqltext` unchanged, and handle `setsqltext`/`gsub` multi-return correctly — even though no dispatch logic exists yet.
- **Alternatives:** Bare `return` skeleton with the safety rules added when dispatch lands.
- **Rationale:** The guard and fail-open behavior is the correctness-critical part once the preprocessor is active; establishing and testing it on the skeleton means dispatch is layered onto an already-safe base.
- **Promotes to ADR:** yes

## Review Findings

<!-- Populated by speq-implement after code review. -->
