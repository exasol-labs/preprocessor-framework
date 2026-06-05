# Feature: Demo Configuration Seed and Teardown

Provides two SQL scripts — `sql/demo/seed.sql` and `sql/demo/teardown.sql` — that stand up and tear down a complete, self-documenting demonstration of the framework's config model. The seed creates demo roles and users with deliberately different resolved pipelines, inserts scoped rules into `PREPROC.CONFIG` (including at least one `client_pattern`-scoped rule), runs `PREPROC.REFRESH`, and is heavily commented so the file itself reads as documentation of how scoping, roles, phases, and resolution work. The seed showcases the framework's actual selling point: different users get different pipelines from one config-driven lookup.

## Background

* The seed is an SQL script (not a Python tool), consistent with the framework's "administered through DB-side scripts" stance and the one-file philosophy. It runs as an admin holding `PREPROC_ADMIN` (or DBA), because it writes `PREPROC.CONFIG` and executes `PREPROC.REFRESH`.
* The seed creates demo roles `DEMO_ANALYST` and `DEMO_ETL` and at least two demo users granted different role sets, so their resolved pipelines differ. All created objects carry a `DEMO_` prefix so they are unmistakable and safe to leave or remove.
* The seed inserts rules spanning all three phases using the demo modules: a TRANSLATE rule (`DEMO_TRANSLATE_V1`), an EXPAND rule (`DEMO_PING_V1`), and a REWRITE rule (`DEMO_AUDIT_V1`), scoped so that the two demo users resolve to visibly different pipelines. At least one rule MUST be `client_pattern`-scoped to demonstrate client-aware dispatch.
* The seed is heavily commented: every INSERT is preceded by prose explaining the scope (`ROLE` vs `USER`), the phase, why the rule exists, and what the user will observe — the file doubles as config-model documentation. It uses fixed, high `rule_id` values in a reserved demo range so it never collides with operator rules.
* After inserting rules, the seed runs `EXECUTE SCRIPT PREPROC.REFRESH` so `PREPROC.RESOLUTION` reflects the demo config, and includes commented `SELECT … FROM PREPROC_RT.MY_PIPELINE` examples (and the equivalent admin query for impersonating each demo user) so the reader can see each user's resolved pipeline.
* `teardown.sql` removes exactly what the seed created — the demo CONFIG rows (by their reserved `rule_id` range), the demo users, and the demo roles — and re-runs `PREPROC.REFRESH` so `RESOLUTION` returns to its pre-demo state. Teardown is idempotent: re-running after a partial seed leaves no demo objects.
* Neither script activates the preprocessor; activation remains a separate deliberate DBA action.

## Scenarios

### Scenario: Seed creates demo roles and users with distinct pipelines

* *GIVEN* a database where the framework and the demo modules are installed
* *AND* the caller holds `PREPROC_ADMIN` (or DBA)
* *WHEN* the caller runs `sql/demo/seed.sql`
* *THEN* the demo roles `DEMO_ANALYST` and `DEMO_ETL` MUST exist
* *AND* at least two demo users MUST exist holding different demo role sets
* *AND* the two demo users MUST resolve to different pipelines in `PREPROC.RESOLUTION`

### Scenario: Seed inserts rules across all three phases

* *GIVEN* the seed is being applied
* *WHEN* the rule INSERTs into `PREPROC.CONFIG` complete
* *THEN* `PREPROC.CONFIG` MUST contain a `TRANSLATE` rule referencing `PREPROC_RT.DEMO_TRANSLATE_V1`
* *AND* `PREPROC.CONFIG` MUST contain an `EXPAND` rule referencing `PREPROC_RT.DEMO_PING_V1`
* *AND* `PREPROC.CONFIG` MUST contain a `REWRITE` rule referencing `PREPROC_RT.DEMO_AUDIT_V1`

### Scenario: Seed includes a client_pattern-scoped rule

* *GIVEN* the seed is being applied
* *WHEN* the rule INSERTs complete
* *THEN* at least one inserted `PREPROC.CONFIG` rule MUST carry a non-NULL `client_pattern`
* *AND* the resolved `PREPROC.RESOLUTION` row for that rule MUST carry the same `client_pattern`

### Scenario: Seed runs refresh so resolution reflects the demo config

* *GIVEN* the seed has inserted its demo rules
* *WHEN* the seed completes
* *THEN* the seed MUST have executed `PREPROC.REFRESH`
* *AND* `PREPROC.RESOLUTION` MUST contain rows derived from the demo rules for the demo users
* *AND* each demo user MUST be able to read their own resolved rules through `PREPROC_RT.MY_PIPELINE`

### Scenario: Different demo users see different resolved pipelines

* *GIVEN* the seed has completed and `PREPROC.REFRESH` has run
* *WHEN* each demo user's resolved pipeline is read
* *THEN* the analyst demo user's pipeline MUST differ from the ETL demo user's pipeline in at least one phase or rule
* *AND* the difference MUST be attributable to the role and client scoping in the seed

### Scenario: Teardown removes everything the seed created

* *GIVEN* a database where `sql/demo/seed.sql` has been applied
* *WHEN* the caller runs `sql/demo/teardown.sql`
* *THEN* the demo CONFIG rows in the reserved demo `rule_id` range MUST be removed
* *AND* the demo users and demo roles MUST be dropped
* *AND* the teardown MUST execute `PREPROC.REFRESH` so `PREPROC.RESOLUTION` no longer contains demo-derived rows

### Scenario: Teardown is idempotent

* *GIVEN* a database where teardown has already run (or seed only partially applied)
* *WHEN* the caller runs `sql/demo/teardown.sql` again
* *THEN* the script MUST complete without error
* *AND* no demo roles, users, or CONFIG rows MUST remain
