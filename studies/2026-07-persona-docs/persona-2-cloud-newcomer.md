# Persona 2 — Marco, Cloud Data Engineer (Exasol newcomer)

> **Who:** Data engineer at a mid-size SaaS company. Spun up Exasol Personal on
> AWS three weeks ago; the team is migrating a Postgres analytics workload. Strong
> SQL, strong dbt/Python, **no Exasol depth** — has never written a Lua script,
> does not know what BucketFS is, learned "preprocessor" from this repo.
>
> **Goal:** Make his ported Postgres queries stop failing on `::` casts, without
> rewriting 400 queries.
>
> **Exasol know-how:** Low. **Infra:** cloud (AWS, egress available).

---

## Journey

### 1. Arrival — he lands in the wrong repo first

He searched for "exasol postgres cast syntax" and landed directly on
**`preprocessor-library`**, on `cast_shorthand`. That page told him what he
wanted to hear, and its Install section (before this pass) said:

```
uv run preproc module add cast_shorthand --registry /path/to/preprocessor-library
```

So his first action was to install `uv`, clone the library, and run that — which
failed on missing `EXASOL_DSN`. He set the env vars. It then failed again because
**the framework was not deployed at all**. Nothing on the module page told him
there was a prerequisite, and nothing told him the far simpler SQL path existed.

> "I spent forty minutes installing a Python toolchain to run one SQL statement I
> didn't know I could just run."

Two fixes landed from this:

- Module READMEs now **lead with**
  `PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;` and present the CLI
  as the alternative for fleet management. → **F-03, fixed.**
- The library README now opens with a **"New here?"** line pointing at the
  framework Quick Start before anything else. → **F-03, fixed.**

### 2. Framework deploy — fine, once he found it

`release.sql` in DbVisualiser. He hit the parameterized-SQL trap *despite* the
warning, because he ran the file before reading the prose around it — then found
the warning while searching the error text, and recovered. He rated the warning
as "the thing that saved me", and suggested it also appear as a **comment banner
inside `release.sql` itself**, since that is the artifact people actually open.
→ **F-12**

### 3. The install — worked, first try

```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;
```

He was connected as `SYS`, which worked — the installer seeds the sugar rule for
`DBA` as well as `PREPROC_ADMIN` specifically so `SYS` (which cannot be granted
`PREPROC_ADMIN`) gets the command surface. This was a real fix from the June
study, and it held. **However, nothing in the docs told him why it worked**, so
when he later created a service user and the sugar stopped working for it, he had
no way to reason about it. → **F-08, fixed** (the `DBA` seed and its consequence
are now documented, plus a symptom-first triage list for
`syntax error, unexpected IDENTIFIER_PART_`).

### 4. Then it didn't do what he expected

His test case was, naturally, the simplest thing he could type:

```sql
SELECT '42'::DECIMAL;
```

Unchanged → syntax error. He concluded the module was broken and had actually
started drafting a bug report.

The module README (before this pass) listed "a string or numeric literal" as a
supported left operand. **That was false.** Executing the module's Lua directly
confirms a string-literal operand is passed through untouched (a numeric one
works):

```
SAME  "SELECT '42'::DECIMAL FROM dual"   -> unchanged
REWR  'SELECT 123::varchar(10) FROM dual' -> 'SELECT CAST(123 AS varchar(10)) …'
```

This was known — `demo/README.md` listed it under rough edges — but the module's
own page claimed the opposite, and the module page is what a user reads.
→ **F-09 (S2), fixed:** the capability claim is corrected, the limitation is
called out with the recommended workaround, and both behaviours are in the
examples table.

### 5. "Is it even on?"

His recurring question, and the one the docs answer least well. He wanted a
single "am I affected right now?" check. `PREPROC_RT.MY_PIPELINE` is exactly
that view, and it is PUBLIC-readable — but it appears only in `how-it-works.md`'s
schema table and in troubleshooting, never in either README as *the* thing an
end user runs. This is a repeat of the June study's observability theme.
→ **F-10**

### 6. Scope confusion

He installed `FOR ROLE ANALYSTS` because he copied the example, then wondered why
nothing happened for his own user — he did not hold `ANALYSTS`. The docs *do* say
a user must hold the role, but it is stated once, in prose, in a different
section from the examples. For a newcomer the copy-paste example is the
documentation. → **F-13**

---

## Verdict

**Won the outcome, lost an afternoon.** The system did what he needed and the
one-statement install genuinely delighted him once he found it. Every hour he
lost was to *sequencing*: no prerequisite signpost, the hard path presented
before the easy one, and a capability claim that did not match behaviour.

He is the persona the fixes in this pass help most — and the one most exposed to
whatever remains undocumented.

## Top asks

1. A "does this apply to me right now?" one-liner promoted into both READMEs (**F-10**).
2. Put the scope/role-holding caveat *next to* the install examples (**F-13**).
3. Copy the client-configuration warning into `release.sql` itself (**F-12**).
