# Feature: PREPROC Command Sugar

Provides a dogfooded convenience layer: the framework's own EXPAND phase recognises SQL-like `PREPROC …` commands and expands each into exactly one `EXECUTE SCRIPT` call against the admin control scripts. This delta adds the `PREPROC SET SYS_GROUPING <bool>` verb to the live `CONTROL_SUGAR_V1` module (in place — the project is pre-first-release, so there is no active session that could bind a stale module body; the never-`CREATE OR REPLACE`-a-live-module discipline applies only to post-release grammar changes, which must use `CONTROL_SUGAR_V2`).

## Background

* The new verb maps to the new `PREPROC.SET_SYS_GROUPING` admin script (see Admin Control Scripts). It is MUTATING (it changes resolution), so it accepts a trailing `NOREFRESH` suffix exactly like `PREPROC SET MODE` — unlike `PREPROC SET SELF_DISABLE`, which is non-refreshing and rejects `NOREFRESH`.
* The parsed value is passed through UNQUOTED as a boolean literal (mirroring how `SET SELF_DISABLE` passes `word(3)` unquoted), NOT single-quoted like `SET MODE`'s string value. The trailing `norefresh` slot is always emitted (`true` when `NOREFRESH` is present, else `NULL`) so the emitted `EXECUTE SCRIPT` meets Exasol's exact-argument-count requirement, mirroring the always-pass-the-slot pattern of `SET MODE` / `ADD RULE`.
* The `CONTROL` map gains `set_sys_grouping = "PREPROC.SET_SYS_GROUPING"`; parsing is `verb == "SET" and upper_word(2) == "SYS_GROUPING"`, with the subcommand-count check consistent with `SET MODE` (three tokens after `PREPROC` before any `NOREFRESH` strip) and `value = word(3)`.
* All existing sugar contracts are unchanged: EXPAND return contract (nil = no match; string = match + terminal), single-statement output, keyword case-insensitivity with value case preserved, and the `gsub` single-capture rule.

## Scenarios

<!-- DELTA:NEW -->

### Scenario: SET SYS_GROUPING command expands to a SET_SYS_GROUPING execute-script call

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC SET SYS_GROUPING FALSE`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, NULL)` statement
* *AND* the parsed value MUST be carried UNQUOTED as a boolean literal (not single-quoted)
* *AND* when the caller submits `PREPROC SET SYS_GROUPING TRUE` the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(TRUE, NULL)` statement

<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->

### Scenario: A NOREFRESH suffix on SET SYS_GROUPING threads true

* *GIVEN* the convenience module is deployed and active for the caller
* *WHEN* the caller submits `PREPROC SET SYS_GROUPING FALSE NOREFRESH`
* *THEN* the module MUST return exactly one `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(FALSE, true)` statement
* *AND* the `NOREFRESH` keyword MUST be matched case-insensitively and MUST NOT appear in the emitted statement

<!-- /DELTA:NEW -->
