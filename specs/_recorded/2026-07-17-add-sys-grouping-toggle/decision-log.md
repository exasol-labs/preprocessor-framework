# Decision Log: add-sys-grouping-toggle

Date: 2026-07-17

## Interview

**Q:** When SYS is handled separately, which rules should SYS stop receiving?
**A:** FULL ESCAPE HATCH — SYS is excluded from the ENTIRE role closure (DBA + PUBLIC + any inherited role). SYS sees raw, untransformed SQL unless a USER-scoped rule explicitly names SYS (those must still resolve). This is the cleanest guaranteed recovery path.

**Q:** Should the toggle default to preserving today's behavior, or flip to separate?
**A:** DEFAULT = GROUPED (`group_sys_with_dba = TRUE`). No behavior change on upgrade; SYS keeps the sugar; isolation is opt-in. On upgrade of an existing install, a missing/NULL value MUST be treated as TRUE (COALESCE to TRUE).

**Q:** Whose isolation does the toggle cover?
**A:** SYS ONLY — the one Exasol account that cannot be granted PREPROC_ADMIN. Not a general excluded-users list.

**Additional locked decisions confirmed with the user (not re-litigated):**
- The toggle is a BOOLEAN column `group_sys_with_dba` on `PREPROC.SETTINGS`.
- The set path is a new admin script `PREPROC.SET_SYS_GROUPING` mirroring `PREPROC.SET_MODE` exactly: validate the boolean argument first (side-effect-free rejection), UPDATE `PREPROC.SETTINGS`, then either `rc.rebuild()` (import `PREPROC.REFRESH_CORE`) or, under a trailing `norefresh` boolean, commit-only; `RETURNS TABLE` with the new value; the `(value, norefresh)` parameter list is declared in the CREATE header emitted by `install.py` / `bundle.py`.
- Fingerprint is UNCHANGED. The toggle is NOT folded into `FINGERPRINT_SQL` in `refresh_core.lua` OR `status.lua`. Rationale: `mode` is likewise absent from the fingerprint; setting the toggle always triggers a rebuild+restamp so STATUS reports CURRENT afterward, and the two verbatim FINGERPRINT_SQL copies must stay identical.

## Design Decisions

### [1] SYS escape-hatch policy: full-closure exclusion, SYS-only, default grouped

- **Decision:** `PREPROC.SETTINGS.group_sys_with_dba` (BOOLEAN, default TRUE) governs whether `SYS` participates in the ROLE-grant closure. When FALSE, `SYS` is excluded from the ENTIRE closure — DBA-derived, PUBLIC-derived, and nested-role-derived rows — so it sees raw SQL unless a USER-scoped rule names `SYS`. The exclusion applies to `SYS` only, not a general excluded-users list. Default TRUE preserves today's behavior; a NULL (upgraded install) is treated as TRUE.
- **Alternatives:** Exclude only `DBA`-scoped rules (rejected — a PUBLIC- or nested-role rule would still reach SYS, defeating the guarantee); a general `EXCLUDED_USERS` mechanism (rejected — SYS is the only account that cannot hold PREPROC_ADMIN, so it is the only one needing role-independent recovery; a list is scope creep); default FALSE (rejected — upgrade must not change behavior).
- **Rationale:** `SYS` is the one Exasol superuser that cannot be granted `PREPROC_ADMIN`; a guaranteed raw-SQL recovery account is only meaningful if it is isolated from every rule that could reach it through the role graph. Excluding the whole closure via a single ROLE-arm gate is the cleanest guarantee. The USER arm stays live so an operator can still target SYS deliberately.
- **Promotes to ADR:** yes

### [2] Toggle applied as a single-arm pure-SQL predicate; USER arm and CTEs untouched

- **Decision:** The policy is enforced entirely inside the static `RESOLUTION_SQL` statement by appending `AND (u.user_name <> 'SYS' OR COALESCE((SELECT group_sys_with_dba FROM PREPROC.SETTINGS), TRUE) = TRUE)` to the ROLE-closure UNION arm's `EXA_DBA_USERS` join. The USER-scoped UNION arm and the downstream dedupe/ordinal CTEs are unchanged. No Lua string branching is introduced; `RESOLUTION_SQL` stays a single static statement.
- **Alternatives:** Branch the SQL in Lua (build one of two RESOLUTION_SQL strings) — rejected as it duplicates the query and breaks the single-static-statement property; add a WHERE at the outer SELECT — rejected because gating the closure arm is where DBA/PUBLIC/nested all converge, so one predicate covers all three.
- **Rationale:** DBA, PUBLIC, and nested-role derivations all flow through the one closure arm, so a single `grantee <> 'SYS'` gate excludes SYS from the full closure at once while leaving USER-`SYS` rules and every non-SYS holder untouched. COALESCE-to-TRUE at the read site is the sole default guarantee, so no backfill UPDATE is needed on upgrade.
- **Promotes to ADR:** yes

### [3] Fingerprint exclusion — toggle never enters FINGERPRINT_SQL

- **Decision:** `group_sys_with_dba` is NOT part of the resolution state fingerprint. `FINGERPRINT_SQL` in both `refresh_core.lua` and `status.lua` is left byte-for-byte identical. The setter always rebuilds+restamps on the refresh path, so STATUS reports CURRENT after a toggle change.
- **Alternatives:** Fold the toggle into `FINGERPRINT_SQL` so a manual `UPDATE` to the column would show as stale — rejected: it would require editing both verbatim copies (drift risk) and `mode` already sets the precedent of a resolution-affecting setting excluded from the fingerprint because its setter always refreshes.
- **Rationale:** The fingerprint tracks the user/role-grant inputs, not framework settings; settings changes are made through refreshing setters, so staleness detection stays correct without the toggle. Keeping the two copies identical is a maintenance invariant.
- **Promotes to ADR:** yes

### [4] Command sugar added to CONTROL_SUGAR_V1 in place (pre-release exception); V2 discipline still governs post-release

- **Decision:** The `PREPROC SET SYS_GROUPING <bool> [NOREFRESH]` verb is added directly to the live `CONTROL_SUGAR_V1` module in place (option c) and `release.sql` is regenerated. No `CONTROL_SUGAR_V2` is created. The verb is MUTATING, so it accepts the trailing `NOREFRESH` suffix exactly like `PREPROC SET MODE`; it emits `EXECUTE SCRIPT PREPROC.SET_SYS_GROUPING(<value>, <slot>)` with the value passed UNQUOTED as a boolean literal (mirroring `SET SELF_DISABLE`'s unquoted `word(3)`, not `SET MODE`'s quoted value) and the `norefresh` slot always emitted (`true` when `NOREFRESH` present, else `NULL`).
- **Alternatives:** (b) Defer the sugar entirely (the setter is usable via `EXECUTE SCRIPT`) — rejected by the user, who wants the ergonomic verb shipped now; (a) new `CONTROL_SUGAR_V2` + reseed + retire — rejected as disproportionate for one break-glass verb.
- **Rationale:** The never-`CREATE OR REPLACE`-a-live-dispatch-module discipline (mission architecture; CONTROL_SUGAR_V1 header; ADR-019) exists to prevent an in-flight statement binding a stale module body while a new body compiles. The project is not yet in use (pre-first-release), so there are no active sessions and that hazard does not exist. Editing `V1` in place and regenerating `release.sql` is therefore harmless now, and a full `V2` dance is unwarranted. This exception is strictly pre-release: any POST-RELEASE grammar change MUST still go through `CONTROL_SUGAR_V2` (new module + repointed seed + retire once unreferenced), and that discipline remains the standing rule.
- **Promotes to ADR:** yes

### [5] SET_SYS_GROUPING models SET_MODE (refreshing setter), not SET_SELF_DISABLE

- **Decision:** `set_sys_grouping.lua` follows `set_mode.lua`: validate-first (reject any value not in `{TRUE, FALSE}` before UPDATE), single-column UPDATE, then `rc.rebuild()` or commit-only under `norefresh`, `RETURNS TABLE` with the new value; parameter list `(value, norefresh)`.
- **Alternatives:** Model `set_self_disable.lua` (pure write, no refresh, single `value` param) — rejected because the toggle changes resolution (SYS's rows), so it MUST refresh, and it needs the `norefresh` opt-out that `SET_SELF_DISABLE` lacks.
- **Rationale:** `SET_MODE` is the exact structural template for a validating, refreshing, `norefresh`-capable settings setter. The boolean domain `{TRUE, FALSE}` is the analogue of `SET_MODE`'s `{convenience, strict}` whitelist; validating that the argument is a genuine boolean keeps a bad call side-effect-free.
- **Promotes to ADR:** no

### [6] Nullable column + COALESCE-to-TRUE, no backfill UPDATE

- **Decision:** `group_sys_with_dba` is nullable in both the fresh `CREATE TABLE` and the upgrade `ALTER TABLE ADD COLUMN`; the fresh seed sets TRUE, the upgraded row stays NULL, and resolution's `COALESCE(..., TRUE)` is the single default guarantee. No backfill `UPDATE` is added to the upgrade file.
- **Alternatives:** Add the column NOT NULL with a default (rejected — a NOT NULL add fails on the existing single SETTINGS row and diverges from the established `--@tolerate-exists` additive pattern); backfill the existing row to TRUE via an UPDATE (rejected as redundant — COALESCE already guarantees grouped semantics, and one guarantee at the read site is simpler than two).
- **Rationale:** Matches the existing additive-upgrade pattern (`ordinal`, `state_fingerprint`, `profile_hash`, `orphaned_at` are all nullable adds) and keeps the default policy in exactly one place (the resolution predicate).
- **Promotes to ADR:** no

## Review Findings

<!-- Populated by speq-implement after code review. -->
