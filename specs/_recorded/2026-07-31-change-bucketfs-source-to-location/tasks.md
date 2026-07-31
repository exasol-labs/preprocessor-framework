# Tasks: change-bucketfs-source-to-location

Make a BucketFS module source the plain location `bucketfs:<service>/<bucket>/<path>`,
remove `exa.get_connection` from the resolver's BucketFS path, and sweep the
change through the specs, tests, docs, and demos of this repo.

The sibling `preprocessor-library` README is OUT OF SCOPE: the user updated it
by hand on 2026-07-31 (verified — it already carries
`bucketfs:bfsdefault/<bucket>/preproc-lib-<ver>.tar.gz` at `README.md:171-172`
and the dual-meaning paragraph is gone). Do not edit that repo.

Tests are folded into their subject task so each is built test-first (TDD).

Tasks tagged `[expert]` need deep reasoning: an unverified platform premise the
whole design rests on, a parser rewrite whose failure modes must stay loud, and
a test-harness seam replacement that must not degrade into bypassing the code
under test.

**Group 0 is a gate.** If the premise is falsified, STOP and report — do not
start Group A.

## Group 0 - Verify the premise (gate)

- [x] 0.0 Provision the read-protected bucket via ConfD. Use `MEMORY.md` →
      "Exasol Personal test env recipe" to re-derive the environment (dynamic
      IP; the container shell needs a PTY wrap). Install the ConfD add-on per
      `examples/confd_admin.md` — a `CONFD_CONNECTION` pointing at the ConfD
      XML-RPC endpoint plus `examples/confd_admin.sql` run whole — then create
      a PRIVATE bucket carrying a read password:

      ```sql
      CONTROL BUCKETFS ADD BUCKET 'preproc-protected' TO SERVICE 'bfsdefault' PRIVATE
          WITH read_password = '<read-pw>', write_password = '<write-pw>';
      CONTROL BUCKETFS SHOW SERVICE 'bfsdefault';   -- confirm buckets.preproc-protected.public = false
      ```

      `PUBLIC|PRIVATE` is required by ConfD on `ADD BUCKET`; `PRIVATE` emits
      `"public":false` (`examples/confd_admin.sql:790-792`), which is what makes
      the bucket read-protected and therefore able to falsify the premise.
      Equivalent raw form if the sugar misbehaves:
      `EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB('bucket_add', '{"bucket_name":"preproc-protected","service_name":"bfsdefault","public":false,"read_password":"…","write_password":"…"}')`.
      Note in the decision log which form was used. `bucket_add` is currently
      marked ◻️ Documented-not-verified in `examples/confd_admin.md:125` — if
      the sugar works end-to-end, flip that row (and `bucket_delete`, if used
      for teardown) to ✅ with the run date, since this task is exactly the live
      exercise that marker is waiting for. Record the service name, bucket name,
      and both passwords for Tasks 0.1 and 3.4
- [x] 0.1 Verify against a LIVE Exasol instance that the read-protected bucket
      from 0.0 is mounted at `/buckets/<service>/<bucket>` for a caller granted a
      `CONNECTION TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'`,
      with the UDF never naming that CONNECTION. Do NOT substitute the `default`
      bucket — it is publicly readable (`tests/conftest.py:83-85`) and would
      confirm the premise vacuously. Upload a release into the protected bucket
      (BucketFS write service + the write password from 0.0), deploy the CURRENT
      resolver, and probe the mount from a Python UDF that reads
      `/buckets/<service>/<bucket>/<file>` directly with no `exa.get_connection`
      call. Record BOTH observations: granted → readable, revoked → not
      readable. Paste the evidence, the date, and the instance identity into
      `decision-log.md` Design Decision [1]. If either observation contradicts
      the premise, STOP: the location-only design is not viable as specified and
      the plan must be re-opened [expert]

## Group A - Resolver UDF (needs 0)

- [x] 1.1 `sql/udf/module_resolver_v1.sql`: rewrite `_bucketfs_reader` (line
      ~609) for the location grammar. Strip the `bucketfs:` scheme, split the
      remainder on `/`, and require at least THREE segments, each non-empty and
      neither `.` nor `..`; anything else is `_BadSource` naming the expected
      `bucketfs:<service>/<bucket>/<path>` shape. Take segment 1 as the service,
      segment 2 as the bucket, and join the rest as the release path. Build the
      mount base as `MOUNT_ROOT + '/' + service + '/' + bucket` from a new
      module-level `MOUNT_ROOT = '/buckets'` constant declared beside
      `_ARCHIVE_EXTENSIONS`. Keep the existing tarball-vs-auto-extracted probe
      pair verbatim, derived from the new base. Delete the
      `exa.get_connection` call and its `connection %r is not accessible` error.
      A malformed source MUST be rejected BEFORE any filesystem call [expert]
- [x] 1.2 `sql/udf/module_resolver_v1.sql`: extend the `_SourceUnreadable`
      message raised when neither probe finds a release so it names the tarball
      path, the auto-extracted directory path, AND the credential prerequisite —
      "A read-protected bucket is only mounted for a caller granted a CONNECTION
      TO 'bucketfs:<service>/<bucket>' IDENTIFIED BY '<read-password>'." —
      with the service and bucket interpolated from the source. Keep it ONE
      `source_unreadable` kind; add NO second error kind and NO bucket-existence
      probe
- [x] 1.3 `sql/udf/module_resolver_v1.sql`: delete `_mount_base` (lines
      ~673-697) entirely, and rewrite the header `Sources` block (lines ~18-39)
      for the location grammar — remove the `exa.get_connection(name)` sentence
      at line 24 and the CONNECTION-address rules, keep the auto-extract note
      and the no-egress statement. Confirm `grep -n "exa\." ` over the file
      returns nothing

## Group B - Offline resolver suite (needs A)

- [x] 2.1 `tests/_dbfixtures.py`: replace the `exa` seam with a mount-root seam.
      Drop the `exa` parameter of `run_resolver_body` and the
      `namespace["exa"] = …` line; add `mount_root: str | None = None` which,
      when given, sets `namespace["MOUNT_ROOT"]` after the `exec` so the
      resolver's own `<service>/<bucket>/<path>` derivation runs against a
      temporary tree rather than being bypassed. Delete `make_fake_exa` and its
      `SimpleNamespace` import if now unused. Do NOT monkeypatch `os.path` —
      the point of the seam is to exercise the derivation, not to stub it
      [expert]
- [x] 2.2 `tests/test_module_resolver_udf.py`: rewrite `_stage_local` (lines
      48-66) to build the release under `tmp_path/<service>/<bucket>/` and
      return `(source, mount_root, built)` where `source` is
      `bucketfs:<service>/<bucket>/<tarball>`; keep both the `extracted` and
      `tarball` staging modes. Update every `run_resolver_body(...)` call in the
      file (~35 sites) from `exa=exa` to `mount_root=mount_root`, and drop the
      `make_fake_exa` import
- [x] 2.3 `tests/test_module_resolver_udf.py`: the offline grammar tests named
      in `plan.md` § Scenario Coverage —
      `test_bucketfs_path_makes_no_connection_lookup` (assert the exec'd
      namespace exposes no `exa` reference on the BucketFS path, e.g. by running
      with no `exa` bound at all and expecting success),
      `test_two_segment_source_is_bad_source`,
      `test_traversal_segment_is_bad_source` (empty, `.`, and `..` segments),
      and `test_unreadable_location_message_names_paths_and_grant` (assert all
      three message elements). Retarget the two former missing-connection tests
      at lines 313-316 and 348-350 to an unmounted location

## Group C - Live fixtures and DB-backed suites (needs A)

- [x] 3.1 `tests/_dbfixtures.py`: add `BucketFsConfig.bucketfs_source(path)`
      returning `f"bucketfs:{self.service}/{self.bucket}/{path}"` as the single
      choke point for the new form, documented alongside the existing
      `bucketfs_address` (which STAYS — the CONNECTION address is unchanged).
      Add the optional read-protected-bucket configuration Task 3.4 needs
      (`EXASOL_BFS_PROTECTED_BUCKET`, `EXASOL_BFS_PROTECTED_READ_PASSWORD`,
      `EXASOL_BFS_PROTECTED_WRITE_PASSWORD`), returning `None` so the test skips
      when unset. These point at the ConfD-provisioned PRIVATE bucket from Task
      0.0 — the suite CONSUMES that bucket, it does not create one: keeping
      ConfD (and a `CONFD_CONNECTION`) out of the framework's own test
      dependencies. Document the provisioning command in the config docstring so
      the bucket is reproducible without re-reading the plan
- [x] 3.2 `tests/conftest.py:108`: build `source` from
      `config.bucketfs_source(_RELEASE_BUCKET_PATH)`. KEEP the
      `CREATE OR REPLACE CONNECTION` and `GRANT ACCESS` at lines 104-106 and the
      teardown `DROP CONNECTION` — they are the credential prerequisite, and
      keeping them while the source no longer names them is what demonstrates
      the separation. Update the fixture docstring accordingly
- [x] 3.3 Update the source literals in the DB-backed suites:
      `tests/test_db_resolver.py` (fixtures at 127, 205, 253 and
      `test_resolver_admin_only` at 438), `tests/test_db_module_install.py`
      (the `_stage` helper at 407 and `test_install_non_admin_rejected` at
      1123), `tests/test_db_module_catalog.py` (fixtures at 174, 225 and
      `test_catalog_non_admin_rejected` at 441), and
      `tests/test_command_sugar.py` (the four pass-through tests at 801-928,
      whose expected expansions change only in the literal). Route every one
      through `bucketfs_source(...)` where a `BucketFsConfig` is in scope
- [x] 3.4 New integration tests named in `plan.md` § Scenario Coverage:
      `test_resolver_rejects_legacy_connection_form` and
      `test_resolver_unreadable_location_reports_credential_hint` in
      `tests/test_db_resolver.py`;
      `test_install_malformed_bucketfs_source_aborts_before_write` and
      `test_install_unreadable_location_reports_credential_hint` in
      `tests/test_db_module_install.py`;
      `test_catalog_malformed_bucketfs_source_reports_without_write` in
      `tests/test_db_module_catalog.py`;
      `test_module_source_token_carried_verbatim_at_any_depth` in
      `tests/test_command_sugar.py`; and
      `test_read_protected_bucket_resolves_only_while_granted` in
      `tests/test_db_resolver.py`, which stages into the read-protected bucket
      from 3.1, asserts OK while granted and `source_unreadable` after
      `REVOKE ACCESS`, and skips cleanly when the protected bucket is not
      configured. Extend `tests/test_schema_layout.py`'s
      `test_install_creates_no_bucketfs_connection` to assert no framework
      artefact names a CONNECTION on the source path

## Group D - Code comments (needs A)

- [x] 4.1 Update the source-grammar comments that outlive the code change:
      `sql/lua/module_install.lua` line 25 (the `source` parameter), line 17
      (reframe "ACCESS on the BucketFS CONNECTION" as the credential grant that
      makes Exasol mount the bucket) and line 858 (`source_url` provenance);
      `sql/lua/module_catalog.lua:16` (`bucketfs:CONN/path` →
      `bucketfs:<service>/<bucket>/<path>`); `sql/lua/module_register.lua:41`;
      `sql/lua/control_sugar_v1.lua:603` (comment only — the code is unchanged);
      `sql/install/22_modules_table.sql:26`. Verify `src/preproc/bundle.py:239`
      needs no change (it refers to the literal `'bucketfs:'` colon, not the
      grammar) and that nothing under `src/preproc/module/` parses a
      `bucketfs:` source

## Group E - Primary-repo docs and demos (needs A)

- [x] 5.1 `docs/air-gap-runbook.md`: DELETE the "Two meanings of `bucketfs:`"
      heading and table (lines 229-235) and replace it with the single-meaning
      statement — `bucketfs:` always names a location; the CONNECTION is the
      credential that makes a read-protected one readable and is never named in
      SQL the framework consumes. Update the source literals at 248, 267, 270,
      304, 305, 403, 405, 428, 430 (304-305 are verified `source_url`
      provenance output — regenerate them from the Task 7.2 run rather than
      hand-editing). Reword 47, 201, 237, 402 and 419 so the CONNECTION reads as
      a credential grant, and strengthen 64/68 so the service name is presented
      as needed for BOTH the CONNECTION address and the source string. Rewrite
      the troubleshooting table at 470-473: delete the two CONNECTION-address
      rows, rewrite the `malformed bucketfs source` row for the new grammar, and
      add a row for a two-segment `bad_source`
- [x] 5.2 `docs/operations.md`: DELETE lines 811-813 (the
      `bucketfs:<CONNECTION_NAME>/<path>` contract statement) and replace with
      the credential-grant framing; update the source literals at 828, 840, 843,
      887, 890; keep the CONNECTION prerequisite block at 789-809 and its
      native-address callout intact
- [x] 5.3 `docs/how-it-works.md:333-335`: replace "CONNECTION-mounted file" with
      the mount the grant makes visible, read by location. `README.md:234`:
      `bucketfs:MY_BFS/own-lib-2.0.0.tar.gz` → the location form
- [x] 5.4 Demos: `demo/03_module_from_bucketfs.sql` (literals at 62, 88; reframe
      the "never hardcodes its name" note at 34-36 and the resolver note at
      49-52; KEEP the `CREATE CONNECTION` + `GRANT` at 43-47),
      `demo/07_confd_finale.sql` (59, 91), `demo/library_demo.sql` (63, 67;
      keep 57-60), `demo/library_demo.md:29` (beat-3 narration), and
      `demo/framework_demo.sql:274-275` (already the new form — add the missing
      trailing semicolons)
- [x] 5.5 `studies/2026-07-persona-docs/FINDINGS.md` and
      `persona-1-airgap-dba.md`: add a short dated "superseded by
      change-bucketfs-source-to-location" note where the dual meaning of
      `bucketfs:` is recorded as a finding (FINDINGS.md 56-57,
      persona-1 line 72). Do NOT rewrite the historical record

## Group G - Full validation (needs everything)

- [x] 7.1 Run the checklist in `plan.md` § Verification: `uv sync`,
      `uv run pytest`, `uv run pytest tests/test_module_resolver_udf.py`
      (offline, must pass with no cluster and no skips),
      `uv run ruff check .`, `uv run ruff format --check .`,
      `luacheck sql/lua/`. Then the two sweeps:
      `grep -rn "bucketfs:[A-Z_]*/" README.md docs/ demo/ specs/ sql/ src/ tests/`
      must have no hits outside `specs/_recorded/**` and `studies/**`
- [x] 7.2 Run the manual-testing table in `plan.md` against the live instance,
      including the two hard-break probes and the message-contract probe, then
      run `demo/03_module_from_bucketfs.sql` and `demo/library_demo.sql` clean
      from a fresh `release.sql` install. Capture the real `source_url`
      provenance output and feed it back into `docs/air-gap-runbook.md:304-305`
      (Task 5.1). Walk every contract check in `plan.md` § Verification

## Group F - Code-review fixes (needs A-E)

Findings from the Phase 4 review. No blockers; all should-fix.

- [x] 6.1 `specs/_plans/change-bucketfs-source-to-location/module/db-resolver/spec.md:66`:
      the hard break is cited as `ADR-064`, which is decision [1]'s number.
      `decision-log.md:6-8` reserves ADR-064..067 for decisions [1]..[4] in
      order, so the hard break (decision [2]) is `ADR-065`. Fix the citation —
      `/speq:record` would otherwise make the wrong number durable
- [x] 6.2 `tests/test_db_resolver.py` `test_read_protected_bucket_resolves_only_while_granted`
      never stages the release it asserts `OK` on — it silently depends on the
      artefact Task 0.1 left on one instance. On any other correctly-configured
      instance it FAILS with `source_unreadable` on the granted path, which
      looks exactly like the premise being falsified. Stage the release in the
      test (`build_library_tarball` + `bucketfs_put`, as
      `tests/test_db_module_install.py:405` `_stage` does) using the write
      password the fixture ALREADY requires, and `bucketfs_delete` it in a
      `finally`. That also makes the write-password skip-gate honest [expert]
- [x] 6.3 `tests/test_schema_layout.py:646-653`: the new static scan of
      `sql/**/*.{sql,lua}` for a `bucketfs:<CONNECTION>/` literal is the
      anti-regression guard for this whole change, but it sits inside
      `test_install_ddl_creates_no_connection(installed)`, which skips without a
      cluster — so it never runs in a developer's normal loop. Lift the file
      scan into its own DB-free test; leave the `EXA_DBA_CONNECTIONS`
      before/after check in the `installed` test
- [x] 6.4 `demo/framework_demo.sql:274`: the HTTPS source points at
      `github.com/.../blob/...`, which serves HTML, so `_https_reader`'s
      `json.loads` fails with `registry index is not valid JSON`. Use
      `raw.githubusercontent.com`, as `DEFAULT_MODULE_SOURCE`
      (`sql/lua/control_sugar_v1.lua:208-209`) and every other site do. Also
      reconcile the `0.3.1` pin at line 275 with the `0.3.0` used everywhere else
- [x] 6.5 `demo/03_module_from_bucketfs.sql:44-48` and `demo/library_demo.sql:57-60`
      create and grant a CONNECTION on the `default` bucket, which this repo's
      own tests call "always-public". The grant was mechanically load-bearing
      before this change and is now a no-op there, so a presenter who deletes it
      sees the demo still work and concludes it is vestigial — the R13 failure
      mode arriving via the demos. Add one line distinguishing public from
      read-protected buckets, and soften `demo/03:37-38`'s "nobody else can",
      which is false for a public bucket
- [x] 6.6 `sql/udf/module_resolver_v1.sql`: ~60 new comment lines against ~25 new
      code lines, with three arguments each told two or three times. Cut
      `_bucketfs_reader`'s docstring (~668-675 — it restates
      `_parse_bucketfs_location`'s docstring, the header, AND the inline comment
      at 688-694); reduce the header `Sources` no-fallback paragraph (~35-39) to
      the grammar statement, leaving the argument beside the code that enforces
      it; merge `MOUNT_ROOT`'s (~619-628) two duplicate seam sentences; drop one
      of the two `(verified live)` tags at 688-694. Also correct the `MOUNT_ROOT`
      comment: `tests/test_module_resolver_udf.py:343-354` `_resolver_namespace`
      rebinds the seam too, not only `run_resolver_body`
- [x] 6.7 Nits worth taking: `docs/air-gap-runbook.md:64-68` says the service is
      "the first segment" of the source (it is the first of TWO);
      the troubleshooting rows at 476-478 omit the `[error_kind]` prefix that
      `MODULE_INSTALL` actually emits (`sql/lua/module_install.lua:766-768`), so
      `bad_source` / `source_unreadable` appear nowhere an operator would Ctrl-F;
      `examples/confd_admin.md:126-132` links a `specs/_plans/...` path that
      `/speq:record` will move to `specs/_recorded/...`
