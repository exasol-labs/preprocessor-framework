# Feature: Admin Control Scripts

Provides the complete SQL-only administrative surface for the preprocessor framework as `EXECUTE SCRIPT` entry points in the `PREPROC` admin schema. This delta adds `PREPROC.SET_SYS_GROUPING`, the setter for the `PREPROC.SETTINGS.group_sys_with_dba` policy. `group_sys_with_dba` joins `mode` and `self_disable_allowed` as the operator-mutable settings columns; because the value changes resolution (whether `SYS` receives ROLE-derived rows), the setter auto-refreshes like `SET_MODE` (with the same `NOREFRESH` opt-out), rather than being a pure settings write like `SET_SELF_DISABLE`.

## Background

* `PREPROC.SET_SYS_GROUPING` is modelled on `PREPROC.SET_MODE`: it is an unversioned `CREATE OR REPLACE LUA SCRIPT`, runs with INVOKER rights, binds `:param` placeholders, validates its argument BEFORE any UPDATE (side-effect-free rejection), then either runs the shared refresh logic or, under `NOREFRESH`, commits the UPDATE on its own. It is `RETURNS TABLE` and returns the new value.
* Its parameter list is `(value, norefresh)`, declared in the `CREATE` header emitted by `install.py` / `bundle.py` (parameters are bare names, no SQL types). `value` is the new boolean policy; a trailing `norefresh` boolean suppresses the auto-refresh for batch workflows.
* Validation domain: the boolean argument must be a genuine boolean (`TRUE` or `FALSE`). Any other value — `NULL`, a string, a number — is rejected with a validation error naming the offending value, before any UPDATE, so a bad call leaves `PREPROC.SETTINGS` exactly as it was. This mirrors `SET_MODE`'s validate-first whitelist, with `{TRUE, FALSE}` as the whitelist.
* `SET_SYS_GROUPING` is added to the set of mutating scripts that auto-refresh (`RULE_ADD`, `RULE_DROP`, `RULE_ENABLE`, `RULE_DISABLE`, `SET_MODE`, and now `SET_SYS_GROUPING`); `SET_SELF_DISABLE` remains the only setter that does not refresh.
* The command sugar (`PREPROC …`) exposes this setter as `PREPROC SET SYS_GROUPING <bool> [NOREFRESH]`, added to the live `CONTROL_SUGAR_V1` module in place. This in-place grammar change is a pre-release exception justified by there being no active sessions yet (the never-`CREATE OR REPLACE`-a-live-dispatch-module hazard of ADR-019 does not apply); any POST-release grammar change MUST still arrive via a new `CONTROL_SUGAR_V2`. The full sugar contract lives in the `control/command-sugar` feature; here the setter remains directly invocable as `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(...)`.

## Scenarios

<!-- DELTA:NEW -->

### Scenario: SET_SYS_GROUPING updates the grouping flag and auto-refreshes

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* an enabled rule scoped to `ROLE` `DBA`
* *WHEN* the caller executes `SET_SYS_GROUPING` with the value `FALSE` and no `NOREFRESH`
* *THEN* the script MUST update `PREPROC.SETTINGS.group_sys_with_dba` to `FALSE`
* *AND* the script MUST run the shared refresh logic so `PREPROC.RESOLUTION` reflects the new policy (`SYS` no longer carries the `DBA`-derived row) before the call returns
* *AND* the script SHOULD return the new `group_sys_with_dba` value so the caller can observe the outcome

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: SET_SYS_GROUPING rejects a non-boolean value side-effect-free

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `SET_SYS_GROUPING` with a value that is not a genuine boolean (for example a string or `NULL`)
* *THEN* the script MUST reject the call with a validation error naming the offending value
* *AND* the script MUST NOT update `PREPROC.SETTINGS`
* *AND* `PREPROC.RESOLUTION` MUST remain unchanged

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: SET_SYS_GROUPING with NOREFRESH commits without rebuilding

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *WHEN* the caller executes `SET_SYS_GROUPING` with a valid boolean value and the `NOREFRESH` flag set
* *THEN* the script MUST update `PREPROC.SETTINGS.group_sys_with_dba` and commit
* *AND* the script MUST NOT rebuild `PREPROC.RESOLUTION`
* *AND* `PREPROC.SETTINGS.last_refresh` MUST be unchanged by the call

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: SET_SYS_GROUPING changes only the grouping flag among operator settings

* *GIVEN* a caller holding `PREPROC_ADMIN`
* *AND* known current values of `PREPROC.SETTINGS.mode` and `PREPROC.SETTINGS.self_disable_allowed`
* *WHEN* the caller executes `SET_SYS_GROUPING` with a valid boolean value
* *THEN* the script MUST NOT alter `PREPROC.SETTINGS.mode` or `PREPROC.SETTINGS.self_disable_allowed`
* *AND* on the refresh path `PREPROC.SETTINGS.last_refresh` and `state_fingerprint` MAY be restamped by the rebuild, so STATUS reports the resolution as CURRENT after the call

<!-- /DELTA:NEW -->
