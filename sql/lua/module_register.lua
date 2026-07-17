-- =============================================================================
-- MODULE_REGISTER module provenance upsert  (PREPROC.MODULE_REGISTER)
-- =============================================================================
-- Role
--   The supported way to write a module provenance row into PREPROC.MODULES.
--   Direct INSERT is unsupported: this script owns the (name, version) natural
--   key and is idempotent — re-registering an identical version MUST NOT create a
--   second row. It is a pure provenance surface: it writes ONLY PREPROC.MODULES
--   and never touches PREPROC.CONFIG / RESOLUTION, so it issues NO REFRESH_CORE
--   rebuild (resolution is driven by the rule surface, RULE_ENSURE / RULE_ADD).
--
-- Idempotent upsert (MERGE on the natural key)
--   MERGE on (name, version):
--     * NOT MATCHED -> INSERT the row with status = 'deployed', installed_at =
--       CURRENT_TIMESTAMP, retired_at = NULL.
--     * MATCHED     -> UPDATE the mutable provenance metadata (source, ref,
--       sha256, script_name, function_name, phase, deploy_mode) and force the row
--       back to status = 'deployed' with retired_at = NULL. installed_at is
--       deliberately NOT touched, so it remains the first-install timestamp and a
--       re-register is idempotent in row count and semantics. Forcing 'deployed'
--       makes a re-register also the re-adoption path for a previously retired
--       version (the deploy step that re-installs an artifact), mirroring how the
--       profile lifecycle clears orphaned_at when a hash is re-adopted.
--
--   MERGE calls MODULE_REGISTER only when a version is being (re)deployed, so
--   'deployed' is always the correct post-condition; retirement is a separate
--   surface (PREPROC.MODULE_RETIRE).
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py; bare
-- names, no SQL types)
--   name           VARCHAR(128)  module name (e.g. 'cast-shorthand')
--   version        DECIMAL(9,0)  integer module version
--   source         VARCHAR(256)  registry/repo source, or SQL NULL
--   ref            VARCHAR(128)  pinned tag/ref, or SQL NULL
--   sha256         VARCHAR(64)   artifact sha256 (library-deployed), or SQL NULL
--   script_name    VARCHAR(128)  fully-qualified SCHEMA.NAME of the module script
--   function_name  VARCHAR(128)  entry function within that script
--   phase          VARCHAR(32)   'TRANSLATE', 'EXPAND', or 'REWRITE'
--   deploy_mode    VARCHAR(32)   deployment mode (stored verbatim)
--
-- Validation order (rejected call is side-effect free)
--   phase is validated first and raises via error() before any MERGE, mirroring
--   the validate-before-write discipline of the rule/mode admin scripts. deploy_mode
--   is stored verbatim (its vocabulary is owned by the Python contract layer, so
--   the DB does not second-guess it). No COMMIT precedes the validation, so
--   PREPROC.MODULES is unchanged on rejection.
--
-- NULL binding
--   Nullable parameters (source, ref, sha256) are normalised to the Exasol `null`
--   sentinel before binding: a Lua nil would drop the key from the bind table and
--   fail the bind, whereas `null` binds SQL NULL. Every USING column is CAST to
--   its target type so a NULL bind still carries an explicit type into the MERGE.
--
-- Result contract
--   Created RETURNS TABLE: (name, version, status, created). created is TRUE when
--   the row was freshly inserted and FALSE when an existing row was updated, so
--   the caller can report the outcome without a re-query.
-- =============================================================================

-- The CREATE header declares the typed parameters; luacheck cannot see it, so
-- declare them (and the Exasol `null` sentinel is already a project read_global).
-- luacheck: read globals name version source ref sha256 script_name function_name phase deploy_mode

local VALID_PHASES = {TRANSLATE = true, EXPAND = true, REWRITE = true}

-- --- Step 1: validate phase ---------------------------------------------------
if not VALID_PHASES[phase] then
    error(
        "PREPROC.MODULE_REGISTER: invalid phase: " .. tostring(phase)
            .. " (expected TRANSLATE, EXPAND, or REWRITE)"
    )
end

-- Normalise a nullable string param to the `null` sentinel so it binds SQL NULL
-- (a Lua nil would be absent from the bind table and fail the bind).
local function nz(v)
    if v == nil then
        return null
    end
    return v
end

-- --- Step 2: note whether the row already exists (for the created flag) --------
local cnt_ok, cnt_res = pquery(
    "SELECT COUNT(*) AS c FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not cnt_ok then
    error("PREPROC.MODULE_REGISTER: existence check failed: " .. tostring(cnt_res.error_message))
end
local existed = (tonumber(cnt_res[1][1]) or 0) > 0

-- --- Step 3: idempotent upsert on (name, version) -----------------------------
-- Every USING column is CAST to its target type so a NULL-bound nullable column
-- still carries an explicit type into the MERGE.
local mg_ok, mg_res = pquery(
    [[MERGE INTO PREPROC.MODULES m
      USING (
          SELECT
              CAST(:name AS VARCHAR(128))          AS name,
              CAST(:version AS DECIMAL(9,0))       AS version,
              CAST(:source AS VARCHAR(256))        AS source_repo,
              CAST(:ref AS VARCHAR(128))           AS source_ref,
              CAST(:sha256 AS VARCHAR(64))         AS sha256,
              CAST(:script_name AS VARCHAR(128))   AS script_name,
              CAST(:function_name AS VARCHAR(128)) AS function_name,
              CAST(:phase AS VARCHAR(32))          AS phase,
              CAST(:deploy_mode AS VARCHAR(32))    AS deploy_mode
      ) s
      ON m.name = s.name AND m.version = s.version
      WHEN MATCHED THEN UPDATE SET
          source_repo   = s.source_repo,
          source_ref    = s.source_ref,
          sha256        = s.sha256,
          script_name   = s.script_name,
          function_name = s.function_name,
          phase         = s.phase,
          deploy_mode   = s.deploy_mode,
          status        = 'deployed',
          retired_at    = NULL
      WHEN NOT MATCHED THEN INSERT
          (name, version, source_repo, source_ref, sha256, script_name, function_name,
           phase, deploy_mode, status, installed_at, retired_at)
          VALUES
          (s.name, s.version, s.source_repo, s.source_ref, s.sha256, s.script_name, s.function_name,
           s.phase, s.deploy_mode, 'deployed', CURRENT_TIMESTAMP, NULL)]],
    {
        name = name, version = version, source = nz(source), ref = nz(ref),
        sha256 = nz(sha256), script_name = script_name, function_name = function_name,
        phase = phase, deploy_mode = deploy_mode,
    }
)
if not mg_ok then
    error("PREPROC.MODULE_REGISTER: merge failed: " .. tostring(mg_res.error_message))
end

-- --- Step 4: commit -----------------------------------------------------------
local commit_ok, commit_res = pquery("COMMIT")
if not commit_ok then
    error("PREPROC.MODULE_REGISTER: commit failed: " .. tostring(commit_res.error_message))
end

-- --- Step 5: report the outcome -----------------------------------------------
exit(
    {{name, version, "deployed", not existed}},
    "name VARCHAR(128), version DECIMAL(9,0), status VARCHAR(32), created BOOLEAN"
)
