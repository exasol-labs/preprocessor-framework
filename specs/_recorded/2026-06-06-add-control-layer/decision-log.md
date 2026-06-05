# Decision Log: add-control-layer

Date: 2026-06-06

## Interview

**Q:** Which operations should the control script surface cover in this iteration?
**A:** Full admin surface — Rule lifecycle (RULE_ADD, RULE_DROP, RULE_ENABLE, RULE_DISABLE, RULE_LIST) plus SETTINGS management (mode, self-disable flag) and any future profile operations — everything an admin touches goes through scripts. REFRESH/STATUS also reachable through the same convenience layer.

**Q:** Should control scripts auto-run REFRESH after each CONFIG mutation?
**A:** Auto with opt-out — Default auto-refresh after each mutating script, plus a NOREFRESH flag/parameter for batch workflows.

**Q:** Given invoker-rights makes hard lockdown impossible, how strictly should direct CONFIG DML be discouraged?
**A:** Validation + convention — Scripts validate inputs (phase names, script existence, rule_id allocation) so they are the obviously-better path; docs declare direct DML unsupported. No technical barrier.

**Q:** What syntax style should the dogfooded convenience commands use?
**A:** SQL-like keywords, e.g. `PREPROC ADD RULE FOR ROLE ANALYSTS PHASE EXPAND SCRIPT PREPROC_RT.DEMO_PING_V1 FUNCTION ping;`, `PREPROC DROP RULE 42;`, `PREPROC DISABLE RULE 42;`, `PREPROC LIST RULES;`, `PREPROC REFRESH;`. Parsed by an EXPAND module, expands to EXECUTE SCRIPT.

## Design Decisions

### [1] Whole admin surface is scripted; direct CONFIG DML is unsupported by convention, not blocked

- **Decision:** Every CONFIG/SETTINGS mutation is surfaced as a validating `EXECUTE SCRIPT` in the `PREPROC` schema. Direct DML stays technically possible but is declared unsupported in docs.
- **Alternatives:** Definer-rights wrapper to physically block direct DML (rejected: invoker rights make a hard lock impossible — a caller already needs the DML privilege). Leaving DML as the primary path (rejected: no validation, collision-prone rule_ids, one bad write impacts every resolved user).
- **Rationale:** Validation + convention makes the scripted path the obviously-correct one without pretending an enforcement boundary the engine cannot provide.
- **Promotes to ADR:** yes

### [2] Auto-refresh runs inside the mutating script, with a NOREFRESH opt-out

- **Decision:** Each mutating control script rebuilds RESOLUTION in-script after a successful mutation unless `NOREFRESH` is set.
- **Alternatives:** Emit a second `EXECUTE SCRIPT PREPROC.REFRESH` statement from the sugar (rejected: an EXPAND expansion must be exactly one statement, so chaining is impossible). Require the operator to remember to refresh (rejected: leaves resolution silently stale).
- **Rationale:** The single-statement EXPAND constraint forces refresh into the script; NOREFRESH preserves efficient batch workflows.
- **Promotes to ADR:** yes

### [3] Shared refresh logic factored out of refresh.lua

- **Decision:** The resolution rebuild + fingerprint + stamp is extracted from `refresh.lua` into a reusable, import-able form the mutating scripts call directly.
- **Alternatives:** Duplicate the rebuild SQL in each mutating script (rejected: drift). Have each script issue a nested `EXECUTE SCRIPT PREPROC.REFRESH` (rejected: nested EXECUTE SCRIPT from inside a script is fragile and obscures the transaction boundary).
- **Rationale:** Single source of truth for the atomicity/fingerprint-first contract established by ADR-010.
- **Promotes to ADR:** no

### [4] Dogfooded EXPAND convenience module — one command maps to one EXECUTE SCRIPT

- **Decision:** A production EXPAND module `PREPROC_RT.CONTROL_SUGAR_V1` parses `PREPROC …` keyword commands and expands each to exactly one `EXECUTE SCRIPT PREPROC.<SCRIPT>(...)` call, reusing the existing first-match-terminal EXPAND contract (ADR-012).
- **Alternatives:** A separate CLI-only command parser (rejected: the framework should eat its own dog food and the sugar should work for any SQL client). A multi-statement expansion (rejected: EXPAND output must be a single statement).
- **Rationale:** Demonstrates the framework on itself; auto-refresh inside the target script keeps the one-statement contract intact.
- **Promotes to ADR:** yes

### [5] Production module is the first non-DEMO_ dispatched module; admin scripts stay unversioned

- **Decision:** `CONTROL_SUGAR_V1` carries no `DEMO_` prefix and is versioned (`_V1`) per the live-replace discipline; the admin control scripts are unversioned `CREATE OR REPLACE` like REFRESH/STATUS.
- **Alternatives:** Version everything, or version nothing (both rejected as inconsistent with ADR-008's rule: dispatched modules are versioned, admin scripts are not).
- **Rationale:** Keeps the existing versioning discipline coherent; encodes the production/demo distinction in the deployed name.
- **Promotes to ADR:** no

### [6] Installer seeds the convenience EXPAND rule via direct DML (sanctioned bootstrap)

- **Decision:** Install adds a guarded `WHERE NOT EXISTS` CONFIG row registering the sugar rule scoped to `PREPROC_ADMIN`.
- **Alternatives:** Ship a separate "enable-sugar" command (rejected: chicken-and-egg — the registration command does not exist until the sugar is registered).
- **Rationale:** Installer/seed direct DML is the already-sanctioned bootstrap path (mirrors the demo seed); operator direct DML is what is unsupported, not installer DML.
- **Promotes to ADR:** yes

### [7] New `control/` domain rather than extending dispatch/command-expansion

- **Decision:** Two new features under a new `control/` domain — `admin-scripts` and `command-sugar`.
- **Alternatives:** Fold the sugar into `dispatch/command-expansion` (rejected: that feature documents the generic EXPAND mechanism and its DEMO_PING_V1 reference, not a specific product command surface).
- **Rationale:** Keeps the generic mechanism spec clean and gives the control surface its own lifecycle.
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
