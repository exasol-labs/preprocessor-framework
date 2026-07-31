# Persona 4 — Lena, Security & Compliance Reviewer

> **Who:** Application security reviewer at a health-data processor. Sits on the
> change-approval board; her sign-off is required before anything reaches
> production. Reads SQL and Python competently, is not a DBA, and has **no Exasol
> background** — she evaluates privilege models, supply chains, and blast radius.
> Her default answer is no.
>
> **Goal:** Decide whether to approve `preprocessor-framework` plus one library
> module for production use in a regulated environment.
>
> **Exasol know-how:** Medium-low. **Infra:** gated, egress via proxy allowlist only.

---

## Journey

### 1. She starts with blast radius, and the docs meet her there

Her first question — "what is the worst thing this can do?" — is answered
directly and early. She rated the following as unusually candid:

- **Exactly one preprocessor slot, system-wide.** Stated in the first paragraph.
  She understood immediately that activation means *every statement on the
  instance* is intercepted.
- **Activation is a separate deliberate action** the deploy never performs.
- **Fail-open is documented per phase, including where it is a risk.**
  `how-it-works.md` says plainly that a skipped REWRITE "silently runs the
  original statement, which is a policy bypass risk." She singled this out:

> "They wrote down the failure mode that makes their own feature look bad. That
> buys more trust than a page of assurances."

- **`SYS` break-glass isolation** as a documented, configurable guarantee.

### 2. The privilege model — clear, and she could follow it without Exasol depth

The two-schema split and its *reason* (Exasol has no definer-rights Lua scripts,
so `MASTER` runs with invoker rights and can only reach admin data through a
definer-rights view scoped to `CURRENT_USER`) was legible to her as a
non-Exasol-specialist. She verified the claim that the resolver UDF — the only
component with BucketFS and network reach — lives in the locked admin schema and
is never granted to PUBLIC.

### 3. Where she raised objections

**Installing a module is arbitrary DDL, and she cannot read it first.**
The docs are honest that an artifact may create objects in *any schema the
installing admin can write*, and that install is "arbitrary DDL executed under
the `PREPROC_ADMIN` boundary." Her review standard is: read the code that will
run. `PREPROC CATALOG MODULES` gives her object *names* and a verified `sha256` —
which tells her the artifact is unmodified, but **nothing about what it does**.
For `confd_control`, that means approving 15 objects including 9 Python UDFs
sight-unseen, or going out-of-band to GitHub (which her proxy allowlist may not
permit).

Her ask: a read-only `PREPROC SHOW MODULE <name> [FROM …]` that returns the
artifact text. The resolver already fetches and verifies exactly that text; it is
simply never surfaced. → **F-04 (her #1 blocker)**

**The default install source is mutable, and there is no way to forbid that.**
Omitting `FROM` resolves
`…/preprocessor-library/latest/registry/index.json` — a **moving tag**, over
HTTPS, from a third-party host. She accepts this is a reasonable default for
convenience; she cannot accept that it is **unopt-outable**. The docs state the
design decision explicitly ("supplying an `https:` source *is* the opt-in — there
is no toggle", ADR-051), which she credits for transparency, but the consequence
is that any `PREPROC_ADMIN` holder can pull code from the internet and there is
no institutional control to prevent it.

What she wants is not a redesign but a **policy setting**: forbid `https:`
sources, or pin an allowed source prefix, enforced at the admin boundary. She
noted the mitigating factor herself — `library_version` and `source_url` are
recorded, so it is *auditable after the fact* — but "detective control only" is a
finding in her framework, not a pass. → **F-19 (her #2 blocker)**

**Provenance is PUBLIC-readable.** `INSTALLED_MODULES` is readable by every user
with no `CURRENT_USER` filter. Documented and deliberate (ADR-002), and she
accepted the rationale, but she wants it stated as a *decision with a reason* in
the operator docs rather than only in a design note, since some environments treat
"what is installed" as sensitive. → **F-20 (minor)**

**REWRITE fail-open has no fail-closed option.** For a module enforcing a policy
(row filtering, redaction), a silently skipped REWRITE is exactly the failure she
must prevent. The docs tell her to "design REWRITE modules with this in mind,"
which she reads as placing the burden on module authors for a property only the
dispatcher can guarantee. She would want a per-rule `fail_closed` flag before
approving any *policy-enforcing* module — though not before approving syntax
modules like `cast_shorthand`. → **F-21**

### 4. A doc issue that nearly caused a false finding

`module-authoring.md` stated "the database itself never fetches or verifies
anything; the CLI machine is the trust boundary." Read alongside
`how-it-works.md`, which describes the resolver UDF fetching over HTTPS and
verifying `sha256`, she drafted a finding that the in-database path performed
**no integrity verification**. That would have been wrong — the resolver does
verify, and aborts before any write on a mismatch. The stale sentence, not the
system, created the finding. → **F-05, fixed.**

> "When two documents in the same repo contradict each other on integrity
> checking, I have to assume the weaker one is true."

---

## Verdict

**Conditional approval.** She would approve the framework itself, and
`cast_shorthand` / `trailing_comma` as syntax-only modules — the privilege model
is sound, the blast radius is honestly described, and the audit surface is real.

She would **not** approve `confd_control` (or any composite module) without being
able to read its artifact first, and she would attach a finding about the
unopt-outable mutable default source.

Notably, none of her objections are about the design being wrong. They are about
**a reviewer's controls not being exposed**: the system verifies integrity but
won't show her the code; it records provenance but won't let her constrain
sources.

## Top asks

1. `PREPROC SHOW MODULE <name>` — return the verified artifact text, read-only (**F-04**).
2. A policy control over install sources: forbid `https:`, or allowlist a prefix (**F-19**).
3. A per-rule fail-closed option for policy-enforcing REWRITE modules (**F-21**).
