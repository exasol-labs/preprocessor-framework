# Persona 5 — Tom, ISV Module Author

> **Who:** Lead engineer at a small ISV that sells an Exasol data-governance
> add-on. Deep Exasol: writes Lua and Python UDFs, builds SLCs, knows BucketFS.
> Wants his product's SQL command surface to be installable as a preprocessor
> module rather than a bespoke installer his customers must run.
>
> **Goal:** Publish a composite module (Lua entry script + several Python UDFs +
> a state table) to the public library, and be confident it will install cleanly
> at customer sites.
>
> **Exasol know-how:** High. **Infra:** builds in cloud CI; customers are mixed,
> several air-gapped.

---

## Journey

### 1. The contract docs are the best thing he read

`module-authoring.md` is, in his words, "an actual SDK document." Specifics he
called out:

- The `fn(text) -> nil|string` contract with a **per-phase table** of what `nil`
  means and whether a string is mandatory.
- **"No defensive `pcall`"**, with the reasoning — that a module-level `pcall`
  converts a visible fault into a silent wrong answer, so it is a contract
  violation rather than a defensive improvement. He said he would have added one.
- The **`gsub` two-return-value trap** and the "functions must be plain top-level
  globals, `local` is invisible to `import`" trap. Both are exactly the mistakes
  a competent Lua author makes in *this* environment.
- The **`_V<N>` discipline extended to every companion object**, including the
  rule that a byte-identical companion still redeploys under the new version. He
  considered this over-strict for about a minute, then agreed: it is what makes
  two generations coexist without collision.

He also found the honest note explaining that the old one-statement-per-artifact
rule was "a mechanical consequence of the installer" rather than a principled
design, and appreciated being told the real reason.

### 2. Composite support is exactly what he needed — and it works

The composite path is the difference between his product being a real library
module and being a `self-deployed` pointer. He validated the reference case:

```
$ preproc module validate confd_control --registry <lib>
  ok: manifest conforms to the contract
  ok: artifact sha256 matches the manifest
  ok: artifact inventory matches the manifest (15 objects)
  ok: luacheck clean
validate: confd_control v1 OK
```

The bidirectional inventory check (rejected if the artifact creates an
undeclared object *or* declares one it never creates) is the guardrail he wanted,
and `confd_control` in the library is a real 15-object worked example rather than
a toy. `_template`'s `[[objects]]` comment block is a good copy-in starting point.

### 3. The blocker

**To develop a module for the *public* library he needs a checkout of the
*private*, unpublished framework package.** `CONTRIBUTING.md` states this
plainly:

> the framework package, which is private and unpublished: `pip install -e
> ../preprocessor-framework` from a local checkout, or export
> `PYTHONPATH=<checkout>/src`

Every static gate he needs — `load_manifest`, `verify_artifact_inventory`,
`verify_artifact_sha256`, and the `preproc module validate` command itself — lives
in that package. So an external contributor cannot validate a module, cannot run
the library's own test suite, and cannot regenerate the index, without access
they may not have.

> "The library is public and invites contributions. The tools to make a valid
> contribution are not. I can get the checkout because I know you — a drive-by
> contributor opens a PR that CI rejects and has no way to find out why locally."

This is known and tracked as a follow-on ("Standalone pip-installable test kit")
in `operations.md`, but it is filed as a nice-to-have. From his side it is the
gating item for external module authorship. → **F-22 (S2)**

### 4. `validate` passes locally, CI can still reject

`validate_static` runs `luacheck` **only when it is on `PATH`**, and reports
success without it. So a contributor without `luacheck` installed gets a clean
`validate: OK` and a CI failure on the same commit. The skip is silent — the
"ok: luacheck clean" line is simply absent, which nobody notices in a passing
run.

This is the same class of problem `CONTRIBUTING.md` warns about for
`skipif`-guarded tests ("a check guarded that way is only ever as reliable as its
declaration") — the doc identifies the pattern and then the validator does it.
→ **F-23**

### 5. Publishing — the process stops short

`CONTRIBUTING.md` covers authoring and PR mechanics well, including the composite
rules and the `--check` index gate. What it does not cover is **what happens
after merge**:

- When does his module become installable via `PREPROC INSTALL MODULE`? (Answer:
  only after a release moves the `latest` tag — so merge alone is not enough.)
- Who cuts releases, on what cadence, and how does `VERSION` relate to the `v*`
  tag his entry's `source.ref` is stamped with?
- His `min_framework` value: how does he choose it, and what happens to a
  customer who installs onto an older framework? (Answer, from this study:
  nothing — it is unenforced.)

For an ISV planning a release alongside his own product's, "when will customers
be able to install this" is a scheduling question he cannot answer from the docs.
→ **F-24**

### 6. Two smaller notes

- The **`suggested_scope` semantics** are good, but nothing discusses how an ISV
  should choose one. His subsystem needs its own role; the examples all use
  `PREPROC_ADMIN` or `DBA`. A paragraph on scoping a subsystem module to its own
  role would help. → **F-25 (minor)**
- He asked whether a module may create its own **schema** (his product wants one).
  The docs say objects may live "in any schema the installing admin can write",
  which implies the schema must pre-exist — but do not say whether a
  `CREATE SCHEMA` statement in the artifact is permitted, or how the object
  inventory should declare one. **[needs live]** → **F-26**

---

## Verdict

**Wants to publish, and technically can — but not as an arm's-length external
contributor.** The contract documentation is genuinely excellent and the
composite support removes the architectural blocker that previously forced
subsystem modules into `self-deployed`. The remaining obstacle is packaging of
the *authoring toolchain*, not of the modules.

His summary: "The hard part — the contract — is done well. The easy part — letting
me run your validator without your private repo — is what's stopping me."

## Top asks

1. Publish the module test kit / validator so external authors can work without the private framework checkout (**F-22**).
2. Make `validate` fail loudly, or at least report, when `luacheck` is absent (**F-23**).
3. Document the release/publication path from merged PR to installable module (**F-24**).
