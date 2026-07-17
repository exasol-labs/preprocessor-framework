# Feature: Two-Schema Database Layout

Establishes the foundational database objects. This delta extends `PREPROC.SETTINGS` with a `group_sys_with_dba` BOOLEAN column that governs whether the `SYS` account is grouped with `DBA` during resolution. Fresh installs seed it `TRUE` (grouped — preserving today's behavior); existing installs gain it as a nullable additive upgrade whose `NULL` is treated as `TRUE` by the resolution `COALESCE`, so an upgrade changes no behavior.

## Background

The install DDL is applied idempotently. Column additions on already-installed databases ship as guarded `--@tolerate-exists` `ALTER TABLE ... ADD COLUMN` statements. The `group_sys_with_dba` column is nullable in both the fresh-install `CREATE TABLE` and the upgrade `ALTER TABLE`, matching the established additive-upgrade pattern (a NOT NULL add would fail on the existing single SETTINGS row); the seed sets the fresh-install value explicitly to `TRUE` and the resolution SQL `COALESCE`s a `NULL` to `TRUE`.

## Scenarios

<!-- DELTA:CHANGED -->

### Scenario: Admin configuration tables are created

* *GIVEN* schema `PREPROC` exists
* *WHEN* the install DDL is applied
* *THEN* the system MUST create table `PREPROC.CONFIG` with columns for role/user/client_pattern, phase, module script, function, enabled, and version
* *AND* the system MUST create table `PREPROC.RESOLUTION` with columns user_name, client_pattern, phase, script, function, an `ordinal` column that records the deterministic within-phase dispatch order, and a `profile_hash` column (VARCHAR, nullable) that records the hash of the profile script generated for that user's chain
* *AND* the system MUST create table `PREPROC.SETTINGS` with columns mode, self_disable_allowed, last_refresh, a state-fingerprint column capturing the user/role-grant inputs seen at the last refresh, and a nullable `group_sys_with_dba` BOOLEAN column that governs whether the `SYS` account participates in the ROLE-grant closure
* *AND* the system MUST create table `PREPROC.PROFILES` mapping a hash to a manifest, including a nullable `orphaned_at` TIMESTAMP column that records when a profile's hash stopped being referenced by any current resolved chain (NULL while the profile is live/current)

<!-- /DELTA:CHANGED -->

<!-- DELTA:CHANGED -->

### Scenario: SETTINGS is seeded with a single default row

* *GIVEN* table `PREPROC.SETTINGS` has just been created and is empty
* *WHEN* the install DDL is applied
* *THEN* the system MUST insert exactly one row into `PREPROC.SETTINGS`
* *AND* the row SHALL default `mode` to convenience
* *AND* the row SHALL default `group_sys_with_dba` to `TRUE` (grouped — preserving the pre-toggle behavior for fresh installs)
* *AND* re-applying the DDL MUST NOT insert a second row

<!-- /DELTA:CHANGED -->

<!-- DELTA:NEW -->

### Scenario: PREPROC.SETTINGS gains a group_sys_with_dba column via an idempotent upgrade

* *GIVEN* an existing install where `PREPROC.SETTINGS` does not yet have a `group_sys_with_dba` column
* *WHEN* the additive column-upgrade DDL is applied
* *THEN* the system MUST add a nullable `group_sys_with_dba` BOOLEAN column to `PREPROC.SETTINGS`
* *AND* the upgrade MUST be expressed via the existing `--@tolerate-exists` additive `ALTER TABLE ... ADD COLUMN` pattern so that re-applying it on an install that already has the column is a no-op (no error, column unchanged)
* *AND* the existing single `PREPROC.SETTINGS` row MUST retain its data with `group_sys_with_dba` defaulting to `NULL`, which resolution treats as `TRUE` (grouped) via `COALESCE`, so the upgrade changes no resolution behavior

<!-- /DELTA:NEW -->
