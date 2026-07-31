# Demo 2 of 2 — the module library

Companion to [`library_demo.sql`](library_demo.sql). Seven beats, ~8 minutes, one
tab. Assumes the viewer has seen demo 1 but not that they ran it. No ConfD — the
composite module in the catalog gets its own video.

Two halves: **consume** (beats 1–5) and **contribute** (beat 6).

## Before you record

1. Clean `release.sql` install, preprocessor activated, connected as `SYS`/DBA.
   Rule ids are hardcoded (`PREPROC DROP RULE 4` in beat 5) — clean install only.
2. HTTPS egress for beats 1 and 2.
3. A library release tarball staged in BucketFS plus the read password for the
   `CREATE CONNECTION` in beat 3 — recipe in [`README.md`](README.md) § Setup.
4. For beat 6: a checkout of
   [`preprocessor-library`](https://github.com/exasol-labs/preprocessor-library),
   `uv`, and `PYTHONPATH` or an editable install of the framework package.

> Trailing `--` comments on the same line as a `PREPROC …` command break it.
> All comments in the file are on the line above.

## The beats

| # | Time | On screen | Say |
|---|---|---|---|
| 1 | 0:00 | `PREPROC CATALOG MODULES` | "This is a catalog of published preprocessors, and I am browsing it from SQL — no CLI, no package manager, no checkout. Read the columns, because this is your review surface: what phase it runs in, whether the library ships the code or just points at it, whether you already have it, and every object an install would create. You see the blast radius before you run anything." |
| 2 | 1:00 | `INSTALL cast_shorthand` → the cast works → provenance | "One statement does the whole thing: resolve the index, verify the artifact's hash, create the script, register the rule, write provenance, refresh. Any failure aborts before the first write, so a bad download never leaves you half-installed. And Postgres cast syntax now works on Exasol. Note what got recorded — where it came from, which release, which hash. An audit is a SELECT, not a support ticket." |
| 3 | 2:15 | `CREATE CONNECTION` → catalog and install `FROM 'bucketfs:…'` → trailing comma works → provenance shows both transports | "Most Exasol clusters have no internet. So: one connection object, granted once as a read credential, and then the same two commands with one different source string — naming the bucket directly, not the connection — that points at a release tarball instead. This is not a network call with extra steps; a BucketFS read is cluster-local, straight off the mount. Air-gapped is a first-class path, not a degraded one. And both modules just ran on that one statement — TRANSLATE is run-all, so they chain." |
| 4 | 3:30 | `PREPROC LIST RULES` | "Installing put the code on the cluster and bound one audience to it. Widening or narrowing that audience later is a rule change — the code does not move, nothing redeploys. That is the whole of the previous video in one sentence." |
| 5 | 4:00 | `MODULE_RETIRE` → `MODULE_RECLAIM` blocked → drop rule → reclaim | "Taking one back out is deliberately two steps. Retire marks the version dead. Reclaim drops the objects — and watch, it refuses, because a rule still references it. You cannot accidentally pull a module out from under a live session. Drop the rule, then reclaim, and it is gone." |
| 6 | 4:45 | terminal — see below | **The second half.** "Everything so far was consuming. Here is how you get something *in*. Remember the `SHOW TOP [n] OBJECTS` preprocessor we hand-wrote in the first video? Let's publish it." |
| 7 | 7:30 | `CATALOG` → `LIST RULES` | "A catalog you can read, install from, contribute to, and audit — and every one of those is either one SQL statement or one pull request." |

## Beat 6 — publishing a module

The finished article is in [`library_demo/top_objects/`](library_demo/top_objects/).
It is the `SHOW TOP [n] OBJECTS` module from demo 1, packaged to the library contract,
and it has been validated through every gate the library's CI applies. Copy it into
your checkout and walk it, or build it live — it is four files.

**The narration arc: a preprocessor becomes a module by acquiring a manifest.**
The Lua does not change.

```bash
cd preprocessor-library
cp -r modules/_template modules/top_objects
cd modules/top_objects
mv my_module_v1.sql top_objects_v1.sql
```

**1. The artifact** — `top_objects_v1.sql`. The same Lua as demo 1, wrapped in the
EXAplus block markers (`--/` … `/`) because the body contains semicolons. Point at
three things while it is on screen:

- `_V1` on the deployed name. You never `CREATE OR REPLACE` a live module; a
  behaviour change ships as `_V2` alongside it.
- `return nil` for anything it does not recognise — the EXPAND contract.
- no defensive `pcall`. The dispatcher's `pcall` is the only error boundary; one
  inside a module turns a visible fault into a silent wrong answer.

**2. The manifest** — `module.toml`. This is the whole delta from "a script I wrote"
to "a module anyone can install":

```toml
name = "top_objects"
description = "Adds SHOW TOP OBJECTS [n], ranking the objects the caller can see by raw size."
phase = "EXPAND"
script_name = "PREPROC_RT.TOP_OBJECTS_V1"
function = "top_objects"
version = 1
min_framework = "0.7.0"
sha256 = "a5daae6a..."
deploy_mode = "library-deployed"

[suggested_scope]
type = "ROLE"
value = "PREPROC_ADMIN"
```

```bash
sha256sum top_objects_v1.sql        # the value that goes in sha256
```

Say why the hash is there: the operator's CLI verifies it before deploying, so the
library cannot ship you different bytes than it advertised. `version` must equal the
`_V<N>` suffix. A single-statement artifact needs no `[[objects]]` array — that
becomes required the moment your artifact creates more than one object, and then CI
rejects any disagreement between what you declared and what the artifact actually
creates.

**3. Tests** — `tests/test_top_objects.py`. Three static gates (manifest parses,
sha256 matches, declared inventory matches the artifact) plus live tests that deploy
the artifact and assert the entry function claims its own command and declines
everything else. The static gates run with no database; the live ones skip cleanly
without one.

**4. Validate, before anyone reviews it:**

```bash
uv run preproc module validate top_objects --registry /path/to/preprocessor-library
```

```
  ok: manifest conforms to the contract
  ok: artifact sha256 matches the manifest
  ok: artifact inventory matches the manifest (1 object)
  ok: luacheck clean
  ok: live smoke: 1 script statement compiled in a throwaway schema;
      entry function honoured the phase contract (returned nil)
validate: top_objects v1 OK
```

That is the beat to let land. It compiled your artifact in a disposable schema and
checked the function honours its phase contract — before a human looks at it.

**5. Regenerate the index and open a PR:**

```bash
python scripts/generate_index.py      # never hand-edit registry/index.json
pytest modules/top_objects/tests/ -q  # 17 passed
```

CI deploys your module against a docker Exasol, runs your tests, and runs
`generate_index.py --check` to reject an out-of-sync index.

**6. Install it from your own checkout** — what an author does before opening the PR,
and the payoff shot:

```bash
uv run preproc module add top_objects --scope ROLE:DBA --registry /path/to/preprocessor-library
# Added top_objects v1 at ROLE DBA (rule 7)
```

```sql
SHOW TOP 3 OBJECTS;

SELECT NAME, VERSION, SOURCE_URL, DEPLOY_MODE, STATUS
FROM   PREPROC_RT.INSTALLED_MODULES WHERE NAME = 'top_objects';
```

The command you hand-wrote in video 1 is now an installable, versioned,
hash-verified, provenance-tracked module — and the source it came from is recorded
as your local checkout, which is exactly what you want while iterating.

### Verified

Everything above was run end to end on Exasol 2026.1: 17 tests pass (static +
live), `generate_index.py --check` reports in sync, `preproc module validate`
passes all five gates, and `preproc module add` from a local checkout installs it
and the command works through the preprocessor.

### The other path, worth one sentence

A module whose code must stay in its own repo is registered rather than hosted:
one `registry/external/<name>.toml` pointing at it, `deploy_mode = "self-deployed"`,
and the library carries a curated pointer instead of the code. Same rule, same
provenance row, no artifact.

## Trims

1. Beat 4 — it is a callback to demo 1; cut to a single sentence over `LIST RULES`.
2. Beat 5 — narrate the retire/reclaim guard without running it (~45 s).
3. Beat 6 steps 3 and 5 — show `validate` and the install, mention tests and CI.

Do not trim beat 6 step 4 or step 6. `validate` passing and the module installing
from your own checkout are the two shots that make contributing look easy.

## Reference

- [`library_demo/top_objects/`](library_demo/top_objects/) — the worked example
- `preprocessor-library/CONTRIBUTING.md` — both contribution paths in full
- `preprocessor-library/README.md` § Contributing a module
- [`../docs/module-authoring.md`](../docs/module-authoring.md) — the contract:
  function signature per phase, manifest schema, `[[objects]]`, versioning
- [`03_module_from_bucketfs.sql`](03_module_from_bucketfs.sql) — long-form version
  of beat 3
