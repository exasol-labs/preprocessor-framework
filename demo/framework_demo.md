# Demo 1 of 2 — the framework

Companion to [`framework_demo.sql`](framework_demo.sql). Nine beats, ~9 minutes,
**four client tabs plus one non-DbVisualizer client**. Every statement verified end to end on Exasol 2026.1,
including all four sessions. No ConfD — that is a topic for its own video.

Creates no schema and no table. The only objects it creates are four small Lua
modules, which are the point.

## Before you record

1. **Clean `release.sql` install**, preprocessor **not** activated. Rule ids are
   hardcoded (`PREPROC DROP RULE 7` in beat 7) and are only right on a clean
   install. Reset by recreating the container between takes.
2. **Tab 1** connected as `SYS` or a DBA. This is where you drive.
3. **Tabs 2, 3, 4** ready to connect as `USER_A` / `USER_B` / `USER_C`, password
   `Demo1234` — they are created live in beat 4, so connect them then.
4. **One session in a different SQL client** for beat 5 - DBeaver, EXAplus,
   anything that is not DbVisualizer. It only needs `CREATE SESSION`; connect as
   your DBA user.
5. HTTPS egress for beat 8 only. Everything before it is self-contained.
6. Font up, result grid visible without scrolling.

> **Never put a trailing `--` comment on the same line as a `PREPROC …` command.**
> Most clients send it as part of the statement and the command is declined with a
> confusing syntax error. Comments in the file are all on the line above.

## The beats

| # | Time | Tab | On screen | Say |
|---|---|---|---|---|
| 1 | 0:00 | 1 | two schemas → deployed scripts → `ALTER SESSION` → `PREPROC STATUS` → `LIST RULES` | "Exasol gives you exactly one preprocessor slot — one script, system-wide. Installing this framework put a dispatcher in that slot's place and gave you two schemas: one locked, one public. Nothing is intercepting SQL yet; that stays a deliberate step. One statement, reversible, no restart — and now the database understands an admin language it did not understand a second ago. `ALTER SYSTEM` instead of `ALTER SESSION` when you mean it for the whole cluster." |
| 2 | 1:15 | 1 | `PREPROC.CONFIG` → `PREPROC.RESOLUTION` → `MY_PIPELINE` | "Three tables tell the whole story. Rules are what you edit. A refresh flattens rules against the live role-grant graph into a per-user table — and dispatch reads *only* that, never the rule table, which is why adding a rule costs nothing per statement. And every user can read their own slice through this view, and only their own. A user can always answer 'what is being done to my SQL?' without any privilege on the admin schema." |
| 3 | 2:15 | 1 | the Lua module → `ADD RULE` → `SHOW TOP 3 OBJECTS` → `SELECT 1` | "The contract is one Lua function: take SQL text, return SQL text. Return nil and you are saying 'not my command, keep scanning'. Return a string and you own the statement. That is it — no manifest, no registry, no toolchain. Twenty lines, one rule, and Exasol has a command it has never had. And a statement it declines is untouched." |
| 4 | 3:30 | 1 → 2 → 3 → 4 | roles + 3 users → the ETL module → two rules → `PREPROC.RESOLUTION` | **The core of this video.** "Ordinary roles and users — the framework introduces no parallel principal model. Now a second module with its *own* command: an ETL engineer does not care which objects are biggest, they care which changed last. Two commands, two roles. Watch what that means - each of them can run something the other simply cannot." |
| 5 | 5:30 | 1 → other client | `EXA_ALL_SESSIONS.CLIENT` → the `HELP` module → `ADD RULE … CLIENT 'DbVisualizer'` → `HELP` works here, syntax error there | "Rules narrow by role and user - and also by the client application you connect with. Some conveniences only make sense when a human is typing; you do not want them firing inside a scheduled job. A `HELP` command is exactly that. One line adds it: watch the two captures in this command - `help_menu` stays lowercase, a Lua symbol; `DbVisualizer` keeps its casing, a self-reported client name. Now watch - in DbVisualizer the command exists. In DBeaver, the same user, the same role, the same rule: syntax error." |
| 6 | 6:30 | 1 → 4 | `GRANT` → `STATUS` STALE → `REFRESH` → `STATUS` CURRENT | "One operational rule to remember. Changing a *rule* refreshes itself. Changing a *grant* cannot — the framework never sees your `GRANT`. It tells you honestly: stale. Refresh, and USER_C's session picks it up on their very next statement, no reconnect. In production you schedule the refresh; `STATUS` is the cheap probe that tells you whether you need one." |
| 7 | 7:30 | 1 | broken module → `ADD RULE` → database still answers | "The obvious objection: you have taken over the one global slot, so if your thing breaks does my database stop? No. This module raises an error on every statement I submit. Nothing happens — the broken rule is skipped, my SQL runs, the other modules still work." |
| 8 | 8:00 | 1 → 2 | `PREPROC CATALOG MODULES` → `INSTALL cast_shorthand FOR ROLE DEMO_ANALYST` | "And you do not have to write them yourself. There is a library of published preprocessors, installable from SQL in one statement — and I can hand one straight to an audience that already exists. USER_A gets it without reconnecting. How that library works, and how you publish your own into it, is the next video." |
| 9 | 8:45 | 1 | `PREPROC LIST RULES` | "Everything I did is six rows in a table. Add, disable, retarget at a role — no redeploy, no restart, nothing rewritten." |

## Beat 4 - the tab choreography

This is the payoff, so it is worth rehearsing. **Run both commands in every tab** -
the cross-check is what makes it undeniable.

**Tab 2 - `USER_A`, holds `DEMO_ANALYST`**
```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
SELECT PHASE, SCRIPT_NAME FROM PREPROC_RT.MY_PIPELINE;   -- DEMO_TOP_OBJECTS_V1

SHOW TOP 3 OBJECTS;      -- works
SHOW NEWEST 3 OBJECTS;   -- syntax error
```

**Tab 3 - `USER_B`, holds `DEMO_ETL`**
```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
SELECT PHASE, SCRIPT_NAME FROM PREPROC_RT.MY_PIPELINE;   -- DEMO_NEWEST_OBJECTS_V1

SHOW NEWEST 3 OBJECTS;   -- works
SHOW TOP 3 OBJECTS;      -- syntax error
```

**Tab 4 - `USER_C`, holds nothing**
```sql
ALTER SESSION SET sql_preprocessor_script = PREPROC_RT.MASTER;
SELECT * FROM PREPROC_RT.MY_PIPELINE;   -- one all-NULL row: no rules

SHOW TOP 3 OBJECTS;      -- syntax error
SHOW NEWEST 3 OBJECTS;   -- syntax error
```

**Each user has a command the other cannot run**, and the third has neither. Two
things to say while that is on screen:

- The failures are **syntax errors**, not "permission denied". `USER_B` cannot even
  discover that `SHOW TOP` exists. Membership in a transformation is not a
  permission check bolted on afterwards - it is whether the rule resolves to you at
  all, and an unresolved command is simply not SQL.
- The SQL surface of the database is now **per-role**. Not the data, not the
  privileges - the language. That is the thing no other Exasol feature does.

Show `MY_PIPELINE` in each tab *before* the commands. The audience should see the
explanation before the effect, or the effect looks like magic.

One more line if you have it: **the expansion runs with the caller's own
privileges.** Your DBA tab sees `PREPROC.*` objects in the results; `USER_A` sees
only what they are granted. Same rewritten SQL, different rows - a preprocessor is
not a way around privileges.

## Beat 5 - what to point at

The subtle bit, and the reason this beat is worth 60 seconds rather than 20:

```sql
SELECT PHASE, SCRIPT_NAME, CLIENT_PATTERN FROM PREPROC_RT.MY_PIPELINE;
```

Run that in **both** clients and the `HELP` rule appears in **both**, carrying
`CLIENT_PATTERN = 'DbVisualizer'`. The rule resolved to both sessions - resolution
is built from rules and role grants, neither of which knows anything about clients.
The client match happens at **dispatch** time, per statement, inside MASTER.

Three lines to say out loud:

- **This is scoping, not security.** The client name is whatever the connecting
  tool chooses to report, so a pattern keeps conveniences out of batch jobs. It
  must never be what keeps a transformation away from someone who should not have
  it - that is roles and schema grants.
- **It costs nothing when unused.** MASTER only looks up the client when at least
  one resolved rule carries a pattern.
- **One command, two folding-exempt captures.** `FUNCTION help_menu` and
  `CLIENT 'DbVisualizer'` are the only two positions in the whole grammar that
  are never identifier-folded - a Lua function name and a self-reported client
  string, both outside Exasol's identifier namespace. Worth a beat of its own.

Discover the real strings before you record - this is the query, run in each tool:

```sql
SELECT CLIENT, DRIVER FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION;
```

Measured examples: `DbVisualizer 24.1.5`, `DBeaver 23.3.0`, `PyExasol 2.2.1`. The
pattern is a literal substring, so `DbVisualizer` matches the first and not the
others. Adjust the `CLIENT` clause to whatever your tools actually report.

## What to say if asked

- **"What if a user holds both roles?"** Both rules resolve; EXPAND is
  first-match-terminal, so the lower `rule_id` wins. You control precedence by
  rule id.
- **"Does every statement pay a lookup cost?"** Only the first in a session. In
  the default `convenience` mode the session then repoints itself to a generated
  profile script with its chain baked in — ~7 ms becomes ~0.5 ms. `strict` mode
  disables that if a session must never repoint itself.
- **"What about `SYS`?"** It can be isolated from the entire role-grant closure as
  a guaranteed raw-SQL break-glass account.
- **"Where should modules live?"** `PREPROC_RT` is `EXECUTE`-to-PUBLIC by
  framework invariant, so it is for preprocessors *everyone* should be able to
  run. Anything sensitive goes in a schema you control, and a rule takes any
  schema-qualified name. Worked through in
  [`06_secure_schemas.sql`](06_secure_schemas.sql).

## Trims, if you run long

1. Beat 2 — narrate over `MY_PIPELINE` alone and skip the `CONFIG`/`RESOLUTION`
   queries (~40 s).
2. Beat 6 — drop the second `SHOW TOP 3 OBJECTS`; the `PROOF` row already lands it.
3. Tab 4 in beat 4 — fold USER_C into beat 5, where you need them anyway.

Do not trim beat 4. It is the video.

## Reference

Long-form walkthrough of the same material, for hands-on evaluation:
[`01_administration.sql`](01_administration.sql) …
[`06_secure_schemas.sql`](06_secure_schemas.sql), plus
[`README.md`](README.md). ConfD lives in
[`07_confd_finale.sql`](07_confd_finale.sql) and
[`08_guarded_module_schema.sql`](08_guarded_module_schema.sql) — deliberately out
of both videos.
