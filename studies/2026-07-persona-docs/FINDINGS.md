# Findings — Persona Docs Study (July 2026)

Framework `0.7.1` · Library `0.3.0`.

**Severity:** S1 = a documented path that cannot work · S2 = wrong or missing
information that costs real time or blocks adoption · S3 = friction ·
S4 = polish.

**Status:** `FIXED` = corrected in this pass · `DOC'D` = accurately documented,
underlying gap remains · `OPEN` = needs work (code or docs).

| ID | S | Area | Finding | Status |
|---|---|---|---|---|
| F-01 | S1 | library docs | BucketFS CONNECTION example used an `https://…:2581/<bucket>` address the resolver **rejects** | FIXED |
| F-02 | S2 | docs structure | No single end-to-end air-gap runbook; the path spans 4 documents | OPEN |
| F-03 | S2 | library docs | All 4 module READMEs led with the CLI path and omitted the SQL install path entirely; no framework prerequisite signpost | FIXED |
| F-04 | S2 | feature | No way to read a module's artifact source before installing it | OPEN |
| F-05 | S2 | framework docs | `module-authoring.md` claimed the database "never fetches or verifies anything" — false since the in-database path; contradicted `how-it-works.md` | FIXED |
| F-06 | S3 | framework docs | Deactivation shown as `= ''` in README and `= NULL` in operations.md, unexplained | FIXED |
| F-07 | S2 | framework docs | The dangling-rule lockout (`attempt to call a nil value (field 'rebuild')`) and its recovery lived only in `demo/README.md`, not in operations troubleshooting | FIXED |
| F-08 | S2 | framework docs | operations.md said **one** sugar rule is seeded (`PREPROC_ADMIN`); **two** are (`+ DBA`) — hiding both why `SYS` works and that every DBA holder gets the command surface | FIXED |
| F-09 | S2 | library docs | `cast_shorthand` README claimed a **string** literal is a supported cast operand; verified false (`'42'::DECIMAL` unchanged) | FIXED |
| F-10 | S3 | docs structure | `PREPROC_RT.MY_PIPELINE` is the "does this affect me?" view but appears in neither README | OPEN |
| F-11 | S3 | framework docs | No toolchain-free **upgrade** path: `release.sql` is clean-install only, upgrades need the CLI; not stated as a limitation | OPEN |
| F-12 | S3 | framework | The client parameterized-SQL warning is not inside `release.sql` itself, which is the artifact users open | OPEN |
| F-13 | S3 | docs structure | The "a user must **hold** the role" caveat is far from the install examples that use `FOR ROLE ANALYSTS` | OPEN |
| F-14 | S2 | framework docs | `module sync`'s desired-state file format was documented nowhere — no schema, no example | FIXED |
| F-15 | S2 | feature | No `sync --dry-run` / `plan`; the diff is computed internally but never exposed. Hard blocker for CI adoption | OPEN |
| F-16 | S2 | feature | `min_framework` is parsed and carried but **never enforced**; a module installs onto an unsupported framework and fails later at use | DOC'D |
| F-17 | S3 | framework docs | No CI usage example for `sync`, and no statement on whether concurrent `sync` runs against one database are safe | OPEN |
| F-18 | S3 | cli | `module list` / `search` never print the `description` the index carries | OPEN |
| F-19 | S2 | governance | The default install source is a **mutable tag over HTTPS** and there is no policy control to forbid or allowlist sources (deliberate — ADR-051) | OPEN |
| F-20 | S4 | framework docs | `INSTALLED_MODULES` being PUBLIC-readable is a deliberate decision (ADR-002) but is not surfaced as such in operator docs | OPEN |
| F-21 | S2 | feature | REWRITE fail-open has no per-rule fail-closed option; a skipped policy-enforcing REWRITE is a silent bypass | OPEN |
| F-22 | S2 | packaging | Authoring a module for the **public** library requires the **private** framework package; external contributors cannot validate or test locally | OPEN |
| F-23 | S3 | cli | `validate` runs `luacheck` only if on `PATH` and passes **silently** without it → green locally, red in CI | OPEN |
| F-24 | S3 | library docs | `CONTRIBUTING.md` stops at merge: nothing on when a merged module becomes installable, who cuts releases, or how `VERSION`/`v*` tags relate | OPEN |
| F-25 | S4 | framework docs | No guidance on choosing `suggested_scope` for a subsystem module that owns its own role | OPEN |
| F-26 | S4 | framework docs | Unclear whether an artifact may `CREATE SCHEMA`, and how that would appear in `[[objects]]` **[needs live]** | OPEN |

## Fixed in this pass — detail

**F-01 (S1) — the one genuinely blocking error.**
`preprocessor-library/README.md` instructed:

```sql
CREATE CONNECTION PREPROC_BFS TO 'https://<bucketfs-host>:2581/<bucket>' USER '<user>' IDENTIFIED BY '<pw>';
```

`sql/udf/module_resolver_v1.sql`'s `_mount_base` accepts only the native
`bucketfs:<service>/<bucket>` form (or an absolute `/buckets/...` path) and
raises `unsupported BucketFS connection address …` for anything else. The
framework's own `operations.md` documented the native form correctly, so the two
repos contradicted each other on the single statement the air-gap path depends
on. Corrected to the verified form from `demo/03_module_from_bucketfs.sql`, plus
a note that `https://…:2581` is the *upload* endpoint and that `bucketfs:` means
two different things (bucket address vs. CONNECTION + path).

> **2026-07-31 — superseded by `change-bucketfs-source-to-location`.** The dual
> meaning recorded above no longer exists: a `bucketfs:` source is now always
> the location `bucketfs:<service>/<bucket>/<path>`, never a CONNECTION name.
> This finding is left as written — it was true when recorded.

**F-09 (S2) — verified false capability claim.** Executed the module's Lua
directly:

```
SAME  "SELECT '42'::DECIMAL FROM dual"        -> unchanged
SAME  "SELECT '42'::DECIMAL(10,2) FROM dual"  -> unchanged
REWR  'SELECT 123::varchar(10) FROM dual'     -> 'SELECT CAST(123 AS varchar(10)) FROM dual'
```

`demo/README.md` had this right under "known rough edges"; the module's own page
claimed the opposite. The module page is what users read.

**F-08 (S2) — a fix that worked but wasn't documented.** The June study's BUG-002
was resolved by seeding the sugar rule for `DBA` in addition to `PREPROC_ADMIN`,
so `SYS` — which Exasol will not let you grant `PREPROC_ADMIN` to — gets the
command surface. `sql/install/70_control_sugar_seed.sql` does contain both
inserts and the fix has **not** regressed. But `operations.md` still described one
rule, which left users unable to explain why the sugar works for `SYS` and stops
working for a service user, and hid the security-relevant fact that every `DBA`
holder gets the command surface by default.

**F-14 (S2) — undocumented flagship format.** Both documented spellings were
verified against `parse_desired_state`:

```
d1.toml -> [DesiredModule('cast_shorthand',1,None), DesiredModule('trailing_comma',2,None)]
d2.toml -> [DesiredModule('cast_shorthand',1,('ROLE','ANALYSTS')), …]
modules = ["cast_shorthand"]  -> SyncError: module spec 'cast_shorthand' must be 'name@version'
```

## Also corrected (staleness, no persona blocked)

- `operations.md` installer object table was missing every module-related object:
  `PREPROC.MODULES`, `RULE_ENSURE`, `MODULE_REGISTER` / `RETIRE` / `RECLAIM` /
  `INSTALL` / `CATALOG`, the resolver UDF, and `PREPROC_RT.INSTALLED_MODULES`.
- The admin-scripts and `PREPROC` sugar tables omitted the module commands.
- Stale hyphenated module names (`cast-shorthand`, `trailing-comma`) in
  `operations.md` and `examples/README.md` — invalid since the snake_case rename.
- Stale versions: `preproc-lib-0.1.0.tar.gz`, `v0.2.0` pins, `/main/` registry
  ref (→ `latest`), library README's "two modules ship today" (three do).
- `INSTALLED_MODULES` column list was missing `source_url`, `library_version`,
  and `objects`.
- `how-it-works.md` described the in-database install as creating "the versioned
  script" (singular) and a module as "a Lua script in `PREPROC_RT`" — both
  pre-composite framings.
- One broken in-repo anchor (`#reclaiming-a-retired-version`).
- `demo/` was linked from no README despite being the strongest onboarding asset.

## Live verification (Exasol 2026.1.0, `preproc-demo` container)

Run after the doc pass, against a live instance at session scope only; the
instance was restored afterwards (6 original rules, system slot untouched).

| Claim | Result |
|---|---|
| **F-01** — old library README's `https://…:2581` CONNECTION address fails | **CONFIRMED.** `unsupported BucketFS connection address 'https://127.0.0.1:2581/default' (expected the native 'bucketfs:<service>/<bucket>' form, no host/port)` |
| **F-01** — the corrected native address works | **CONFIRMED.** `bucketfs:bfsdefault/default` → catalog returned all 3 modules |
| **F-08** — `SYS` reaches the sugar via the `DBA`-scoped seed | **CONFIRMED.** Both seeds present (rule 1 `PREPROC_ADMIN`, rule 2 `DBA`); `MY_PIPELINE` for `SYS` shows `CONTROL_SUGAR_V1`; `PREPROC STATUS` → `CURRENT` |
| **F-09** — string-literal cast operand not rewritten | **CONFIRMED.** `SELECT '42'::DECIMAL` → `syntax error, unexpected DOUBLE_COLON_`; `123::varchar(10)` works; `CAST('42' AS DECIMAL)` works |
| **F-16** — `min_framework` unenforced | **CONFIRMED.** A module declaring `min_framework = "99.0.0"` installed on framework 0.7.1 → `installed`. Also: read 0 times in the install path, and `PREPROC.SETTINGS` has no framework-version column, so enforcement needs that value persisted first |
| **F-26** — may an artifact create non-script objects? | **ANSWERED: yes.** `TABLE`, `VIEW`, `FUNCTION` (qualified); `SCHEMA`, `VIRTUAL SCHEMA`, `CONNECTION` (unqualified). Script *language* is any identifier, so a custom SLC alias (`PYTHON3_TE SCALAR SCRIPT`) is a valid companion |
| Composite install end to end | **CONFIRMED.** `confd_control` from BucketFS → `installed`, 15 object lines in provenance; re-install → `already-current` |
| Default HTTPS source (no `FROM`) | **CONFIRMED.** `PREPROC CATALOG MODULES` resolved `library@latest`; provenance records `library_version 0.3.0` + the effective `source_url` |
| Case-insensitive module names | **CONFIRMED.** Unquoted `cast_shorthand` folds to `CAST_SHORTHAND` and still resolves |
| Trailing-comment trap | **CONFIRMED.** `PREPROC STATUS -- check` → `syntax error, unexpected IDENTIFIER_PART_`, identical to having no preprocessor at all |
| Sugar inactive before activation | **CONFIRMED.** Same `IDENTIFIER_PART_` error — so the error is ambiguous between "not activated" and "trailing comment" |

### New findings from the live run

| ID | S | Finding | Status |
|---|---|---|---|
| F-27 | S3 | `MODULE_INSTALL` returns **5** columns (`+ library_version`); docs showed 4 | FIXED |
| F-28 | S3 | `MODULE_CATALOG` returns **7** columns incl. an undocumented `OBJECT_COUNT`; docs listed neither the count nor the column order | FIXED |
| F-29 | S2 | `MODULE_CATALOG.INSTALLED` reflects **provenance, not existence** — a hand-deployed module with live objects and an active rule reports `FALSE`. Observed on `confd_control` before it was installed through the module path | FIXED |
| F-30 | S3 | The `IDENTIFIER_PART_` syntax error is identical for "preprocessor not activated", "no sugar rule resolves to you", and "trailing `--` comment" — three different causes, one indistinguishable message | DOC'D |

**Bring-your-own-preprocessor path, verified end to end.** A customer-authored
composite module in a *private* registry (own `index.json`, own
`library_version` `9.9.9`, own tarball) was staged in BucketFS, cataloged
(`OBJECT_COUNT = 2`, `INSTALLED = False`), installed (`installed`), and its guard
fired live: `DROP TABLE …` → `blocked by my_guard`, with the companion `TABLE`
created. Nothing about the module mechanism requires the public library — this is
now documented as a first-class path rather than a single clause.

## Verification run

```
framework:  219 passed, 2 skipped, 257 deselected   (non-integration)
library:     22 passed, 42 skipped                  (DB tests skip cleanly)
registry:   registry/index.json is in sync
tarball:    build_release.py byte-reproducible across runs
anchors:    0 broken in-repo markdown anchors
```

All 17 documented `PREPROC …` command examples parse and emit the correct
`EXECUTE SCRIPT` call. The documented trailing-comment trap reproduces exactly:
`PREPROC STATUS -- check` → declined (`nil`) → raw engine syntax error.
