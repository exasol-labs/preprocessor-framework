# Feature: Two-Schema Database Layout

Establishes the foundational database objects. This delta extends the layout to add a `profile_hash` column to `PREPROC.RESOLUTION`, an `orphaned_at` column to `PREPROC.PROFILES` (for the retire-then-reclaim cleanup lifecycle), and exposes `mode` and `profile_hash` from `PREPROC_RT.MY_PIPELINE` to support session self-optimization.

## Background

The install DDL is applied idempotently. Schema-level PUBLIC grants on `PREPROC_RT` cover all current and future objects (ADR-002 invariant).

## Scenarios

<!-- DELTA:CHANGED -->

### Scenario: Admin configuration tables are created

* *GIVEN* schema `PREPROC` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST create table `PREPROC.CONFIG` with columns for role/user/client_pattern, phase, module script, function, enabled, and version
* *AND* the system MUST create table `PREPROC.RESOLUTION` with columns user_name, client_pattern, phase, script, function, an `ordinal` column that records the deterministic within-phase dispatch order, and a `profile_hash` column (VARCHAR, nullable) that records the hash of the profile script generated for that user's chain
* *AND* the system MUST create table `PREPROC.SETTINGS` with columns mode, self_disable_allowed, last_refresh, and a state-fingerprint column capturing the user/role-grant inputs seen at the last refresh
* *AND* the system MUST create table `PREPROC.PROFILES` mapping a hash to a manifest, including a nullable `orphaned_at` TIMESTAMP column that records when a profile's hash stopped being referenced by any current resolved chain (NULL while the profile is live/current)

<!-- /DELTA:CHANGED -->

<!-- DELTA:CHANGED -->

### Scenario: The CURRENT_USER-filtered pipeline view is created

* *GIVEN* schemas `PREPROC` and `PREPROC_RT` exist with the admin tables present
* *WHEN* the install DDL is applied
* *THEN* the system MUST create view `PREPROC_RT.MY_PIPELINE`
* *AND* the view SHALL join `PREPROC.SETTINGS` with `PREPROC.RESOLUTION` filtered on `CURRENT_USER`, ordered by phase then `ordinal` for deterministic within-phase dispatch
* *AND* the view SHALL expose the resolution `phase`, `client_pattern`, `script_name`, `function_name`, and `ordinal` so MASTER can apply per-rule client matching
* *AND* the view SHALL expose `PREPROC.SETTINGS.self_disable_allowed` and `PREPROC.SETTINGS.mode` so MASTER can determine the swap/disable eligibility
* *AND* the view SHALL expose `PREPROC.RESOLUTION.profile_hash` (NULL for users with no rules) so MASTER can name the target profile script without re-hashing in Lua
* *AND* the view SHALL read the admin base tables with definer (owner) rights

<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->

### Scenario: PREPROC.PROFILES gains a nullable orphaned_at column via an idempotent upgrade

* *GIVEN* an existing install where `PREPROC.PROFILES` does not yet have an `orphaned_at` column
* *WHEN* the additive column-upgrade DDL is applied
* *THEN* the system MUST add a nullable `orphaned_at` TIMESTAMP column to `PREPROC.PROFILES`
* *AND* the upgrade MUST be expressed via the existing `--@tolerate-exists` additive `ALTER TABLE ... ADD COLUMN` pattern so that re-applying it on an install that already has the column is a no-op (no error, column unchanged)
* *AND* existing `PREPROC.PROFILES` rows MUST retain their data with `orphaned_at` defaulting to NULL (treated as live/current)

<!-- /DELTA:NEW -->
