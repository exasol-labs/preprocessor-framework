# Feature: preproc install CLI Verb

Provides the `preproc install` command that connects to an Exasol instance and applies the full two-schema layout plus the MASTER skeleton, modules, and admin scripts, then prints the activation statement for the DBA to run manually. This slice extends the install to additionally deploy the admin control scripts and the production `CONTROL_SUGAR_V1` convenience module, and to seed the convenience EXPAND bootstrap rule.

## Background

* The install applies all `sql/install/*.sql` files in sorted filename order, then deploys the Lua scripts and the demo UDF into `PREPROC_RT` and the admin scripts into `PREPROC`.
* The admin control scripts and the convenience module are deployed by the same machinery that already deploys MASTER, the demo modules, REFRESH, and STATUS; re-deploying is idempotent.
* The install never activates the preprocessor; it prints the `ALTER SYSTEM SET sql_preprocessor_script = PREPROC_RT.MASTER` statement for the DBA to run manually.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: Install deploys the control scripts and convenience module

* *GIVEN* a clean Exasol instance reachable via the connection environment variables
* *WHEN* the operator runs `preproc install`
* *THEN* the install MUST deploy the admin control scripts (`RULE_ADD`, `RULE_DROP`, `RULE_ENABLE`, `RULE_DISABLE`, `RULE_LIST`, `SET_MODE`, `SET_SELF_DISABLE`) into the `PREPROC` admin schema alongside `REFRESH` and `STATUS`
* *AND* the install MUST deploy the production convenience module `PREPROC_RT.CONTROL_SUGAR_V1` into the runtime schema
* *AND* re-running `preproc install` MUST remain a safe no-op for these objects
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: Install seeds the convenience EXPAND bootstrap rule

* *GIVEN* a clean Exasol instance
* *WHEN* the operator runs `preproc install`
* *THEN* `PREPROC.CONFIG` MUST contain an enabled EXPAND rule scoped to `PREPROC_ADMIN` that points at the convenience module
* *AND* that seed MUST be guarded so re-running `preproc install` does not duplicate the rule
* *AND* the install MUST NOT activate the preprocessor (the activation `ALTER SYSTEM` statement is still only printed)
<!-- /DELTA:NEW -->
