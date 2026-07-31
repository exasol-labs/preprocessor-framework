# Persona 1 — Priya, Platform DBA (air-gapped, regulated)

> **Who:** Platform DBA at a retail bank. Runs Exasol on-premises in a segmented
> network with **no outbound internet** from the cluster. Eight years of Exasol;
> fluent in EXAplus, BucketFS, EXAConf, role hierarchies. Change-controlled: every
> object deployed to production must be justified in a ticket.
>
> **Goal:** Deploy the framework and `cast_shorthand` onto a no-egress cluster,
> with an audit trail her change board will accept, and a rollback plan.
>
> **Exasol know-how:** High. **Infra:** gated / air-gapped.

---

## Journey

### 1. Framework deploy — smooth, and correctly cautious

`release.sql` from the GitHub release, run in EXAplus. She reads the Quick Start
and immediately approves of two things:

- The deploy **never touches the preprocessor slot**. Activation is a separate,
  explicitly-explained DBA action. "This is how it should be. A deploy that
  silently changed how every statement on my system compiles would be an instant
  reject."
- The **parameterized-SQL warning** is prominent and specific, with per-client
  instructions. She has been burned by DbVisualiser's `:name` substitution before
  and has never seen a project warn about it up front. Called out as the single
  best piece of writing in the docs.

She notes `release.sql` is clean-install only and that `preproc install` is the
idempotent path — but the CLI needs `uv`, which she cannot install on the jump
host. She stays on `release.sql`. **This works, but she wants the upgrade story
spelled out:** "clean-install only" plus "use the CLI for column upgrades" means
she has *no* toolchain-free upgrade path. → **F-11**

### 2. Activation — good, but she wants the reverse first

She activates at session scope, verifies, and then asks the question a
change-controlled shop always asks first: **how do I get out?** Deactivation is
documented, but before this pass `README.md` said `= ''` and `operations.md` said
`= NULL` with no explanation of whether these differ. She spent time deciding
whether one was wrong. → **F-06, fixed.**

### 3. Installing a module — where she nearly failed

Her cluster has no egress, so the headline
`PREPROC INSTALL MODULE cast_shorthand FOR ROLE ANALYSTS;` is not available to
her — it defaults to an HTTPS source. She correctly finds the BucketFS path.

**This is where the docs would have cost her a day.** The library README told her
to create:

```sql
CREATE CONNECTION PREPROC_BFS TO 'https://<bucketfs-host>:2581/<bucket>' USER '<user>' IDENTIFIED BY '<pw>';
```

The resolver **rejects that address outright** — it requires the native
`bucketfs:<service>/<bucket>` form and raises `unsupported BucketFS connection
address … (expected the native 'bucketfs:<service>/<bucket>' form, no host/port)`.
Worse, the framework's own `operations.md` said the *opposite* of the library
README, so the two documents she was reading side by side disagreed on the one
statement that had to be right.

> "I'd have assumed my bucket credentials were wrong and gone hunting through
> EXAConf. The error message is good — it tells you exactly what form it wants —
> but I only see that error after I've already created the wrong connection and
> failed an install in front of a change board."

→ **F-01 (S1), fixed.** The library README now uses the native form, explains
that the `:2581` URL is the *upload* endpoint and not interchangeable, and
disambiguates the two different meanings of `bucketfs:`.

> **2026-07-31 — superseded by `change-bucketfs-source-to-location`.** There is
> no longer a second meaning to disambiguate: a `bucketfs:` source is now always
> the location `bucketfs:<service>/<bucket>/<path>`. Left as written — it was
> accurate at the time.

### 4. The missing runbook

Even with the corrected statement, she had to assemble the air-gap sequence from
**four documents**: framework README (get `release.sql`), framework
`operations.md` (CONNECTION + admin boundary), library README (build the tarball,
upload, install), and `demo/03` (the only place with a working `curl` upload
example and the read-vs-write password distinction).

Her ask is specific: *one* page, in order, that goes get-artifacts → transfer →
upload → connection → catalog → install → verify, with the two credentials named
distinctly. She would hand that page to her change board as-is. → **F-02**

### 5. Audit — genuinely strong

`PREPROC_RT.INSTALLED_MODULES` is exactly what she needs, and she was pleased it
records `library_version` and `source_url` separately, so an install resolved
through a moving tag is still traceable to a fixed release. `objects` giving the
full deployed inventory is what lets her write an accurate rollback ticket.

Two gaps:

- She cannot read **the actual code** before installing. `PREPROC CATALOG MODULES`
  lists object *names*, but her review process requires reading the Lua/Python
  that will execute. On an air-gapped cluster she cannot browse GitHub, and the
  tarball is a binary blob to her until it is extracted somewhere. → **F-04**
- Before this pass, `module-authoring.md` claimed "the database itself never
  fetches or verifies anything." She read that as "the in-database install path
  does not verify sha256" and flagged it as a supply-chain hole — it was a stale
  doc, not a real hole. → **F-05, fixed.**

### 6. Rollback and break-glass — the best-covered area

- SYS-grouping as a documented break-glass path (`SET_SYS_GROUPING FALSE` →
  `SYS` sees raw SQL regardless of rules) is precisely the guarantee she needs
  and she rated it excellent.
- The retire-then-reclaim discipline (never drop while something may point at it)
  matched her instincts.
- The recovery procedure for a rule pointing at a **missing** script — where
  every admin command starts failing with
  `attempt to call a nil value (field 'rebuild')` — existed only in
  `demo/README.md`'s rough-edges list, not in `operations.md` troubleshooting,
  which is where she would look at 2am. → **F-07, fixed.**

---

## Verdict

**Would deploy** — after the corrected BucketFS statement. The safety posture,
the audit surface, and the break-glass story are all better than she expected.
What is missing is packaging *for her situation*: the air-gap path is fully
supported but is documented as scattered fragments rather than a runbook, and it
is the one path where a single wrong string produces a failure that looks like a
credentials problem.

## Top asks

1. One end-to-end air-gap runbook (**F-02**).
2. A way to read a module's artifact source before installing (**F-04**).
3. A toolchain-free upgrade path, or an explicit statement that there isn't one (**F-11**).
