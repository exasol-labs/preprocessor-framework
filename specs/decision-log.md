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
