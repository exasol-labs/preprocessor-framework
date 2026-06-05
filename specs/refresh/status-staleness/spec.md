# Feature: Resolution Staleness Reporting

Reports whether `PREPROC.RESOLUTION` is current with respect to the database's users and role grants. Because resolution is materialised offline, role changes after a refresh leave the resolution stale until the next refresh; this feature makes that drift observable so an operator (or a scheduled job) knows when to re-run refresh. Staleness is detected by comparing a cheap fingerprint of the user/role-grant state captured at the last refresh against the fingerprint recomputed now. All of this is DB-side: it is exposed as a `PREPROC_ADMIN`-owned database object callable from any SQL client, never as an external (Python) command.

## Background

* Staleness reporting is administered entirely inside the database. There is no Python `status` verb. The framework keeps all administration in DB-side scripts so a future framework-controlled management syntax can intercept a management statement and translate it onto this same object.
* The entry point is a `PREPROC_ADMIN`-owned object — a Lua script `PREPROC.STATUS` (and/or a companion definer-rights view) — callable from any SQL client by a caller holding `PREPROC_ADMIN` (or `EXECUTE ANY SCRIPT` / DBA). It returns a current/stale verdict and the `last_refresh` time.
* The refresh script records, alongside `last_refresh`, a fingerprint of the inputs that determine resolution: the set of users (`EXA_DBA_USERS`) and the role-to-role and role-to-user grants (`EXA_DBA_ROLE_PRIVS`). The fingerprint is a compact value (for example a row count plus a hash over the ordered grant/user identifiers), cheap to compute and store. It is persisted in `PREPROC.SETTINGS`. Refresh and status share one fingerprint definition.
* Computing the current fingerprint requires `SELECT ANY DICTIONARY`; the comparison logic that needs the dictionary runs inside the `PREPROC_ADMIN`-owned object (invoker rights), mirroring the refresh privilege story.
* Status is read-only: it never rebuilds resolution, never writes `RESOLUTION`, and never changes `last_refresh`.

## Scenarios

### Scenario: Status reports current immediately after refresh

* *GIVEN* the framework is installed and a refresh has just completed
* *AND* no users or role grants have changed since
* *WHEN* an admin invokes the `PREPROC.STATUS` object from any SQL client
* *THEN* it MUST report the resolution as current (not stale)
* *AND* it MUST report the `last_refresh` time

### Scenario: Status reports stale after a new role grant

* *GIVEN* the framework is installed and a refresh has completed
* *WHEN* a role is granted to a user (or to another role) after that refresh
* *AND* an admin invokes the `PREPROC.STATUS` object
* *THEN* it MUST report the resolution as stale
* *AND* it SHOULD indicate that a refresh is needed

### Scenario: Status reports stale after a new user is created

* *GIVEN* the framework is installed and a refresh has completed
* *WHEN* a new database user is created after that refresh
* *AND* an admin invokes the `PREPROC.STATUS` object
* *THEN* it MUST report the resolution as stale

### Scenario: Status is read-only

* *GIVEN* a populated `PREPROC.RESOLUTION` and a recorded `last_refresh`
* *WHEN* an admin invokes the `PREPROC.STATUS` object
* *THEN* `PREPROC.RESOLUTION` MUST be unchanged after the call returns
* *AND* `PREPROC.SETTINGS.last_refresh` MUST be unchanged

### Scenario: Status before any refresh reports never-refreshed

* *GIVEN* the framework is installed but a refresh has never run (`last_refresh` is NULL)
* *WHEN* an admin invokes the `PREPROC.STATUS` object
* *THEN* it MUST report that no refresh has yet been performed
* *AND* it MUST report the resolution as stale (or never-refreshed)

### Scenario: Status requires admin privileges

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`, `EXECUTE ANY SCRIPT`, or DBA
* *WHEN* that user attempts to invoke the `PREPROC.STATUS` object
* *THEN* the database MUST reject the invocation with an insufficient-privileges error
* *AND* no `PREPROC` admin table MUST be exposed to that user
