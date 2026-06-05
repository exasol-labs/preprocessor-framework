# Decision Log: add-expand-command-expansion

Date: 2026-06-05

## Interview

**Q:** Which whole-statement custom command(s) should ship as the first EXPAND command module?
**A:** Minimal demo command only — "A trivial PING-style command proving the mechanism; defer real commands until syntax is settled with stakeholders."

**Q:** Should token-level conveniences be in this slice?
**A:** Defer to later slice — ship command expansion (first-match-terminal) only.

**Q (prior session):** What should `client_pattern` matching use — Lua patterns or literal substring?
**A:** Literal substring. Change `string.find(current_client, tostring(client_pattern))` to `string.find(current_client, tostring(client_pattern), 1, true)`.

**Q (prior session):** What are the terminal semantics for EXPAND — does a match stop all further phases or just further EXPAND rules?
**A:** First-match-terminal within EXPAND only. A matched EXPAND rule stops remaining EXPAND rules but REWRITE still runs afterward.

## Design Decisions

### [1] EXPAND module return contract: nil = no match; string = match + terminal

- **Decision:** An EXPAND module function signals no-match by returning `nil`. It signals match-and-terminal by returning any non-nil string (the expansion). A pcall failure is treated as no-match (fail-open): the next EXPAND rule is tried. A new `dispatch_expand_rule` helper in master.lua handles this protocol; the existing `dispatch_rule` (used for TRANSLATE and REWRITE) is unchanged.
- **Alternatives:** (a) `false` = no match; string = match — rejected because `false` is less natural Lua idiom for "this isn't mine"; (b) two-value tuple `matched, text` — rejected because it complicates the pcall result shape and requires destructuring the return; (c) keep the single dispatch_rule and add a mode parameter — rejected because EXPAND has sufficiently different semantics (nil propagation, terminal break) to deserve a separate clearly-named helper.
- **Rationale:** `nil` is idiomatic Lua for "absent" or "not applicable." A matched command always produces a different string (the expansion); returning `nil` is the clearest "pass" signal. The separate helper keeps TRANSLATE/REWRITE logic untouched and eliminates risk of regression.
- **Promotes to ADR:** yes

### [2] client_pattern matching: plain literal substring via string.find(s, p, 1, true)

- **Decision:** Change the `client_matches` function in master.lua from `string.find(current_client, tostring(client_pattern))` (Lua pattern mode) to `string.find(current_client, tostring(client_pattern), 1, true)` (literal plain find). The fourth argument `true` disables Lua pattern interpretation.
- **Alternatives:** Leave as-is (Lua pattern) — rejected because `client_pattern` values like `EXA.Client` or `ODBC (v1.2.3)` contain Lua magic chars (dots, parens) that make pattern matching silently wrong. Admins entering a client string as a literal substring match would get unexpected behavior.
- **Rationale:** The intended semantic — stated by the product owner — is "does the client string contain this text literally?" That is plain substring, not pattern matching. The fix is a one-character change (`1, true` as extra args) with zero behavior change for patterns containing no Lua special chars.
- **Promotes to ADR:** yes

### [3] Demo module name: DEMO_PING_V1

- **Decision:** The demo/reference module is named `DEMO_PING_V1` following the mission's versioning discipline for modules (`NAME_V1`, `NAME_V2`, …). The `DEMO_` prefix makes it permanently safe to leave deployed (unambiguously not a product command). Its source file is `sql/lua/demo_ping_v1.lua`.
- **Alternatives:** Unversioned `DEMO_PING` — rejected because the mission invariant is that modules are versioned objects never replaced with `CREATE OR REPLACE`; starting unversioned would require a rename the first time it needs updating.
- **Rationale:** Zero cost to follow the invariant from day one. The `DEMO_` prefix is a permanent signal to operators that this module is illustrative, not load-bearing.
- **Promotes to ADR:** no

### [4] New domain: dispatch/command-expansion (not install/)

- **Decision:** The new feature spec is filed under a new domain `dispatch/` as `command-expansion`. It is NOT filed under `install/` because the mechanism governs runtime dispatch behavior, not the install process.
- **Alternatives:** File under `install/` alongside `master-skeleton` — rejected because `install/` is becoming a catch-all; separating `dispatch/` by concern makes the spec library easier to navigate and follows the domain-per-concern principle.
- **Rationale:** The `dispatch/` domain will naturally absorb future dispatch-related features (e.g., TRANSLATE module contracts, REWRITE fail-closed design). Starting it now is low cost and correct.
- **Promotes to ADR:** no

### [5] dispatch_rule kept unchanged for TRANSLATE and REWRITE

- **Decision:** `dispatch_rule` (which returns original text on nil/failure) remains the function used for TRANSLATE and REWRITE phase rows. Only EXPAND rows use the new `dispatch_expand_rule`. The dispatch loop checks the current phase and calls the appropriate helper.
- **Alternatives:** Replace dispatch_rule entirely with dispatch_expand_rule — rejected because TRANSLATE and REWRITE phases use plain text threading (no nil signaling needed); changing them would require every existing module to be updated to never return nil, which is a breaking contract change with no benefit.
- **Rationale:** Backward compatibility. Existing test modules and future REWRITE/TRANSLATE modules have no contract to return nil. Only EXPAND modules need the nil protocol.
- **Promotes to ADR:** no

## Review Findings

<!-- Significant code-review findings that changed implementation direction. -->
<!-- Populated by speq-implement after code review. -->
