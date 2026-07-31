# Persona 3 — Ade, Analytics Engineer (CI/CD, reproducibility)

> **Who:** Analytics engineer owning the dbt + CI pipeline for a media company's
> Exasol warehouse on GCP. Three environments (dev / staging / prod) that must
> stay identical. Believes anything not in version control does not exist.
> Comfortable with Exasol SQL and the catalog; has not written UDFs or Lua.
>
> **Goal:** Manage preprocessor modules as code — pinned versions, reviewed in
> PRs, applied by CI, identical across all three environments.
>
> **Exasol know-how:** Medium. **Infra:** cloud, with a hardened CI runner.

---

## Journey

### 1. He finds the right verb immediately, then hits a wall

`preproc module sync` is exactly his model, and `operations.md` sold it well:
declarative convergence, one refresh, "re-running against an unchanged desired
file is a true no-op." He was sold in one paragraph.

Then he tried to write the file. **The format was documented nowhere.** The
subcommand table said "Path to the desired-state TOML file"; `--help` said the
same; the prose said "diff the desired-state TOML against `INSTALLED_MODULES`."
No schema, no example, no field list, anywhere in either repo.

> "The flagship verb for my exact use case, and I have to read the Python to
> learn what to put in the file. That's the one thing I can't do in a PR review."

→ **F-14 (S2), fixed.** `operations.md` now documents both accepted spellings
with worked examples, states that `version` is mandatory and why, covers `scope`
overrides, and — importantly for him — states that the file is **authoritative**
(a module dropped from it is retired). Both documented forms were verified
against `parse_desired_state`.

### 2. Pinning — the strongest part of the docs for him

The `latest`-vs-immutable-tag trade-off is explained clearly and in more than one
place, including the CDN-cache lag on a just-moved tag. He appreciated that
`sync` converges to an **exact integer version** rather than "latest", and that
provenance records `library_version` separately from `source_url`, so even a
`latest`-resolved install is auditable back to a fixed release.

He verified the reproducibility claim himself and it held: `build_release.py` run
twice produces byte-identical tarballs.

### 3. What blocks him from actually shipping it

**No dry-run.** `sync` has no `--dry-run` / `plan` mode (confirmed: no dry-run
anywhere in the CLI). His pipeline convention is `plan` on PR, `apply` on merge,
and he cannot adopt a tool that has only `apply`. The convergence logic already
computes a complete diff and pre-flights every artifact before writing — so the
plan exists internally and simply is not exposed. He rated this his single
biggest blocker. → **F-15**

**`min_framework` is not enforced.** He wants CI to fail when a module needs a
newer framework than the target environment runs. It is parsed, carried into the
index, and never checked — so a staging box on an older framework accepts an
install and fails later at use. Now documented as advisory (**F-16, partly
fixed** — documented, not enforced), but he wants the check.

**No CI example.** Nothing shows `sync` wired into a pipeline: no workflow
snippet, no guidance on where credentials come from, no statement about whether
concurrent `sync` runs against one database are safe. That last one worries him
most — two environments promoting at once is normal for him. → **F-17**

### 4. Drift detection

He wants to answer "has anyone changed prod by hand?" `INSTALLED_MODULES` gives
him the state and he can diff it against `desired.toml` himself, but there is no
supported command for it. He noted this is the same gap as the missing dry-run —
one `plan` verb would solve both.

### 5. A smaller irritation with real cost

`preproc module list` and `search` print only `name / version / phase /
deploy_mode` — **no description**, even though the registry index carries one for
every module:

```
cast_shorthand  v1  TRANSLATE  library-deployed
confd_control   v1  EXPAND     library-deployed
trailing_comma  v2  TRANSLATE  library-deployed
```

To find out what any of these do he must open the index JSON or the repo. For a
discovery command this is a poor default, and it is the surface a PR reviewer
would look at. → **F-18**

---

## Verdict

**Wants to adopt, cannot yet.** The design is right for him — declarative,
version-pinned, idempotent, with real provenance — and the reproducibility
guarantees are better than he expected. He is blocked on tooling ergonomics
rather than on capability: without a dry-run he cannot put it in a pipeline, and
without `min_framework` enforcement he cannot trust promotion between
environments.

The documentation fix in this pass unblocked him from *writing* the file. The
remaining asks are features, not prose.

## Top asks

1. `preproc module sync --dry-run` (or a `plan` verb) — hard blocker (**F-15**).
2. Enforce `min_framework` at install, or expose a check CI can call (**F-16**).
3. Show descriptions in `list` / `search` (**F-18**).
4. A CI usage example, and a statement on concurrent-`sync` safety (**F-17**).
