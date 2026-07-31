# Persona Docs Study — Framework + Library (July 2026)

Second-round simulated-user study, run **after** a full documentation review of
both `preprocessor-framework` and `preprocessor-library`. Five personas spanning
cloud and gated/air-gapped infrastructure, with deliberately uneven Exasol
know-how, each attempting a real goal using only the published docs.

Framework `0.7.1` · Library `0.3.0`.

## Method — and its limits

This round is a **documentation-path study with offline verification**, not a
live-instance run. That distinction matters when reading the findings.

**What was genuinely executed** (not simulated):

| Verified | How |
|---|---|
| Every documented `PREPROC …` command example | `control_sugar_v1.lua` loaded in Lua via `lupa`; each doc example parsed and its emitted `EXECUTE SCRIPT` compared |
| `cast_shorthand` / `trailing_comma` behaviour claims | module Lua executed directly on every README example |
| Case-insensitive module-name matching | resolver `_select_entry` read + sugar folding output compared |
| `preproc module list / search / validate / bundle` | run against both the local checkout and the live default `latest` registry |
| Composite inventory checking | `validate confd_control` → "inventory matches the manifest (15 objects)" |
| CLI failure modes | missing env vars → exit 2; unknown module → exit 1 |
| `sync` desired-state file format | both documented spellings parsed through `parse_desired_state` |
| Release tarball reproducibility | `build_release.py` run twice, digests compared |
| Registry index freshness | `generate_index.py --check` |
| Both repos' test suites | framework 219 passed; library 22 passed / 42 DB-skipped |

**Live verification followed** on a running Exasol **2026.1.0** instance (the
`preproc-demo` container from `demo/README.md`'s recipe), at session scope only,
restored afterwards. Every substantive claim in this study — the blocking
BucketFS address, the `SYS`/sugar path, the string-literal cast limitation,
`min_framework` non-enforcement, the 15-object composite install, the default
HTTPS source, and the complete bring-your-own-module path — was confirmed against
that instance. See **[FINDINGS.md § Live verification](FINDINGS.md#live-verification-exasol-202610-preproc-demo-container)**
for the results table and the four additional findings the live run produced
(F-27 … F-30).

Still unverified: ConfD XML-RPC command execution, per-statement overhead
figures, and concurrent-`sync` behaviour.

The June 2026 study (`../2026-06-persona-ux/`) was also live. Where this round
touches the same ground, it defers to that one.

## Read in this order

1. **[SYNTHESIS.md](SYNTHESIS.md)** — themes, prioritized recommendations, and
   the feedback collection. *Start here.*
2. **[FINDINGS.md](FINDINGS.md)** — every finding with severity, evidence, and
   whether it was fixed during this pass.
3. Persona reports:
   - [persona-1-airgap-dba.md](persona-1-airgap-dba.md) — Priya, platform DBA, regulated bank, no egress (high Exasol)
   - [persona-2-cloud-newcomer.md](persona-2-cloud-newcomer.md) — Marco, cloud data engineer from Postgres (low Exasol)
   - [persona-3-analytics-cicd.md](persona-3-analytics-cicd.md) — Ade, analytics engineer, CI/CD + reproducibility (medium Exasol)
   - [persona-4-security-reviewer.md](persona-4-security-reviewer.md) — Lena, security & compliance gate, gated infra (medium-low Exasol)
   - [persona-5-module-author.md](persona-5-module-author.md) — Tom, ISV publishing a composite module (high Exasol)

## Headline

The docs describe a genuinely strong system, and the review pass fixed one
**blocking** error plus a batch of staleness. What remains is not correctness —
it is **sequencing and trust**: newcomers cannot find the ordered path, and
reviewers in gated environments cannot get the assurances they need to sign off.

- **Fixed this pass:** an air-gap install example that could not work
  (`https://…:2581` BucketFS CONNECTION address, which the resolver rejects), a
  false `cast_shorthand` capability claim, missing `sync` file format, and drift
  across every module-related doc surface.
- **Top remaining asks:** an end-to-end air-gap runbook in one place; a way to
  read a module's actual code before installing it; a pinned-by-default or
  policy-gated install source; `min_framework` enforcement; and a
  pip-installable test kit so module authors don't need the private framework
  checkout.
