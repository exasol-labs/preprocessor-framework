# Synthesis — Persona Docs Study (July 2026)

Five personas, framework `0.7.1` + library `0.3.0`, run after a full
documentation review of both repos.

| # | Persona | Exasol | Infra | Verdict |
|---|---|---|---|---|
| 1 | Priya — platform DBA, regulated bank | High | Air-gapped | **Would deploy** — after the corrected BucketFS statement. Wants a runbook. |
| 2 | Marco — cloud data engineer, ex-Postgres | Low | Cloud | **Won the outcome, lost an afternoon** to sequencing. |
| 3 | Ade — analytics engineer, CI/CD | Medium | Cloud | **Wants to adopt, blocked** on no dry-run. |
| 4 | Lena — security & compliance gate | Medium-low | Gated | **Conditional approval**; rejects composites she can't read. |
| 5 | Tom — ISV module author | High | Mixed | **Wants to publish**, blocked on the private toolchain. |

---

## The one-sentence finding

The system is stronger than its documentation's *ordering*: every persona
eventually succeeded, and every persona lost time to something other than
capability — a wrong string, a missing prerequisite, a format documented nowhere,
or a control that exists internally but is never exposed.

## What is genuinely good — keep and lean into

1. **Safe-by-default activation.** Every persona independently flagged this.
   Deploy never touches the slot; activation is explained as a deliberate DBA
   action; session scope is offered before system scope. Lena called it the
   reason she kept reading.
2. **Documented failure modes, including the unflattering ones.** `how-it-works.md`
   states that a skipped REWRITE is "a policy bypass risk." Both the DBA and the
   security reviewer named this as the single biggest trust signal in the docs.
   Do not soften it.
3. **The module authoring contract is a real SDK document.** The per-phase `nil`
   semantics, the "no defensive `pcall`" rule *with its reasoning*, the `gsub`
   trap, the `local`-is-invisible-to-`import` trap, and the `_V<N>`-on-every-object
   rule are exactly the mistakes a competent author makes in this environment.
4. **The parameterized-SQL warning.** Specific, per-client, and prominent. It
   saved the newcomer even though he only found it after failing.
5. **Provenance separating `source_url` from `library_version`.** This is what
   makes a mutable-tag install auditable back to a fixed release, and it is the
   detail that made the DBA trust the audit story.
6. **`demo/`.** Verified end-to-end, honestly lists its own rough edges, and has
   time-boxed tracks. It was invisible from every README until this pass — the
   highest-value thing that was already built and unlinked.
7. **The composite module contract.** Bidirectional inventory checking, a real
   15-object worked example, and honesty that the old one-statement rule was a
   mechanical artifact rather than a principle.

## Cross-cutting themes

### Theme A — Sequencing, not content (the newcomer cliff)

The information exists; the *order* does not. Marco installed a Python toolchain
to run one SQL statement because the module page led with the CLI and never
mentioned the prerequisite. Priya assembled the air-gap path from four documents.
Ade could not write a `sync` file because its format lived only in Python.

The pattern: **the docs are organised by system component, not by user journey.**
Someone who knows the system can navigate them; someone arriving cannot.

*Addressed this pass:* module READMEs now lead with the SQL install; the library
README opens with a framework prerequisite; `sync`'s format is documented.
*Still open:* F-02 (air-gap runbook), F-10, F-13.

### Theme B — Controls that exist internally but aren't exposed

Three separate personas asked for something the system already computes:

| Ask | Already exists as |
|---|---|
| Read a module's code before installing (**F-04**) | The resolver fetches and sha256-verifies exactly that text |
| `sync --dry-run` (**F-15**) | `converge` computes a full diff and pre-flights every artifact before writing |
| Detect hand-edited drift | The same diff |

None of these need new machinery — they need a read-only surface over work
already done. **This is the highest-leverage engineering cluster in the study.**

### Theme C — Trust controls for gated environments

Priya and Lena converge from different directions on the same gap. The framework
is *auditable* (provenance, sha256, object inventory) but not *governable*: there
is no way to constrain where modules come from (**F-19**), no way to read what
will run (**F-04**), and no way to guarantee a policy-enforcing REWRITE cannot be
silently skipped (**F-21**).

Lena's framing is worth quoting: these are detective controls where her process
requires preventive ones. ADR-051's "supplying an `https:` source *is* the opt-in"
is a good *default* argument and a poor *policy* argument — the two are separable,
and a `PREPROC.SETTINGS` allowlist would not undermine the ADR's reasoning about
not gating the fetch path with a toggle.

### Theme D — Advisory metadata that looks mandatory

`min_framework` is declared in every manifest, propagated into the index,
surfaced in the catalog, and **never checked** (**F-16**). Two personas assumed it
was enforced. `confd_control` needs `0.7.0`; installing it onto `0.6.x` succeeds
and fails later at use. Either enforce it or label it advisory at every point it
appears. It is now labelled in the docs; enforcement is the real fix.

### Theme E — The public library has a private toolchain

Every static gate an external contributor needs — `load_manifest`,
`verify_artifact_inventory`, `verify_artifact_sha256`, `preproc module validate`
— lives in the unpublished framework package (**F-22**). Compounded by `validate`
passing **silently** when `luacheck` is absent (**F-23**), so the contributor
experience is "green locally, red in CI, no local way to find out why."

This is tracked as a follow-on in `operations.md`. From a would-be publisher's
side it is the gating item for the library ever being genuinely community-fed.

---

## Recommendations, in priority order

### P0 — do next

1. **Write the air-gap runbook** (**F-02**). One page: get artifacts → transfer →
   upload (write password) → CONNECTION (read password, native address) → catalog
   → install → verify → rollback. Name the two credentials distinctly. Priya
   would hand it to her change board unedited. Cheapest item with the largest
   effect on the hardest-to-serve audience.
2. **`PREPROC SHOW MODULE <name> [FROM …]`** (**F-04**). Read-only, returns the
   verified artifact text. Unblocks Lena's approval of every composite module and
   gives Priya offline reviewability. The resolver already has the bytes.
3. **`preproc module sync --dry-run`** (**F-15**). Print the computed
   add/update/retire plan and exit without writing. Unblocks Ade's pipeline and
   doubles as drift detection.

### P1 — soon

4. **Enforce `min_framework`** at install on both paths, with an override flag
   (**F-16**).
5. **Publish the module test kit** (**F-22**) and make `validate` report a missing
   `luacheck` loudly (**F-23**).
6. **A source-policy setting** (**F-19**): forbid `https:` sources, or allowlist a
   prefix, enforced at the admin boundary.
7. **Promote `MY_PIPELINE`** into both READMEs as the "does this affect me?" check
   (**F-10**), and move the role-holding caveat next to the install examples
   (**F-13**).

### P2 — worth doing

8. Per-rule fail-closed option for REWRITE (**F-21**) — gates approval of
   policy-enforcing modules.
9. Descriptions in `module list` / `search` (**F-18**).
10. Document the release path from merged PR to installable module (**F-24**).
11. State the upgrade story: `release.sql` is clean-install only, so there is no
    toolchain-free upgrade (**F-11**).
12. Copy the parameterized-SQL warning into `release.sql`'s own header (**F-12**).
13. CI example for `sync` + a statement on concurrent-run safety (**F-17**).

---

## Needs live verification

This round was doc-path + offline execution. Before treating the study as
complete, confirm on a live instance:

- `PREPROC INSTALL MODULE` end to end on both HTTPS and BucketFS sources,
  including a composite (15-object) install and its `MODULE_RECLAIM`.
- The corrected BucketFS CONNECTION statement against a real bucket.
- That `SYS` reaches the sugar via the `DBA`-scoped seed on a clean 2026.1 install
  (the June study verified this; the seed is unchanged, but the claim now carries
  more documentation weight).
- Whether an artifact may `CREATE SCHEMA` (**F-26**).
- Concurrent `sync` behaviour (**F-17**).

## Relation to the June 2026 study

- **BUG-002** (`SYS` locked out of the sugar) — fix holds; both seed inserts
  present. Newly *documented* (**F-08**).
- **BUG-005** (string-literal cast) — still present in `cast_shorthand` v1, and
  the module README actively claimed the opposite. Now documented honestly
  (**F-09**); the code fix remains open.
- **BUG-007** (no dry-run / dev-time signal) — recurs from a different angle as
  **F-15**. Two independent studies asking for the same thing.
- **BUG-008** (`RULE_ADD` not idempotent) — resolved via `RULE_ENSURE`, and
  `sync` now depends on that idempotency.
- **Theme B, the observability vacuum** — recurs as **F-10**. Still the
  longest-standing unaddressed theme across both studies.
