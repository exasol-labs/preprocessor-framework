-- =============================================================================
-- MODULE_INSTALL in-database module installer  (PREPROC.MODULE_INSTALL)
-- =============================================================================
-- Role
--   The DB-side sibling of the CLI's `preproc module add`. Re-homes the CLI's
--   deploy.deploy_module sequence (src/preproc/module/deploy.py) into the
--   database so a SQL-only DBA can install a preprocessor module with no external
--   Python/uv toolchain. The capability split is deliberate (ADR-001): the
--   Python resolver UDF (PREPROC.MODULE_RESOLVER_V1) READS + integrity-verifies
--   the artifact off BucketFS / the network, and this Lua orchestrator WRITES the
--   DDL (only Lua EXECUTE SCRIPTs run DDL; only UDFs read sources).
--
-- Placement / privilege  (invoker rights under the PREPROC_ADMIN boundary)
--   An unversioned admin entry point in PREPROC, created INVOKER rights (Exasol
--   has no definer-rights scripts). The caller MUST hold PREPROC_ADMIN, which
--   confers ownership of PREPROC / PREPROC_RT, CREATE SCRIPT, and — once the
--   operator grants it — ACCESS on the BucketFS CONNECTION. This is exactly the
--   boundary REFRESH_CORE already uses to CREATE the PREPROC_RT.P_<hash> profile
--   scripts at refresh time. The resolver UDF is admin-only (never PUBLIC), so a
--   non-admin cannot reach BucketFS / the network through it, and a non-admin
--   caller fails at the first privileged access.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py; bare
-- names, no SQL types)
--   source       VARCHAR  a `bucketfs:` or `https:` source (see the resolver)
--   module_name  VARCHAR  the module to install
--   scope        VARCHAR  REQUIRED `TYPE:value`, TYPE in {ROLE, USER}; targets
--                         RULE ACTIVATION (who receives the transform), NOT
--                         install permission
--
-- Sequence (mirrors deploy.deploy_module exactly, in a fixed order that keeps the
-- CLI's "leave the DB consistent on failure" invariant)
--   1. Drive the resolver (SELECT PREPROC.MODULE_RESOLVER_V1(source, module_name),
--      read once, NO FROM). ANY emitted row with status = 'ERROR' aborts HERE via
--      error() — BEFORE any DB write. A sha256 mismatch, module-not-found,
--      unreadable source, or unreachable-HTTPS-host / network error therefore
--      never leaves a half-installed database (parity with
--      deploy.fetch_and_verify_artifact).
--   2. For a library-deployed module (chunk_count >= 1): OPEN SCHEMA PREPROC_RT,
--      reassemble artifact_chunk in chunk_index order, strip any interactive lone
--      `/` terminator line (parity with deploy.strip_script_terminator), and
--      CREATE OR REPLACE the artifact — the version is now present. A self-deployed
--      module (chunk_count == 0) SKIPS this step: its own installer deployed the
--      backing script.
--   3. EXECUTE SCRIPT PREPROC.RULE_ENSURE(scope_type, scope_value, phase,
--      script_name, function_name, NULL, TRUE) — idempotent rule registration with
--      the auto-refresh suppressed; RULE_ENSURE validates the script exists first.
--   4. EXECUTE SCRIPT PREPROC.MODULE_REGISTER(name, version, source_repo,
--      source_ref, sha256, script_name, function_name, phase, deploy_mode,
--      source_url, library_version) — idempotent provenance upsert. sha256 is
--      NULL for a self-deployed module; source_url is the effective source and
--      library_version the resolved release (the `latest` audit anchor).
--   5. A single EXECUTE SCRIPT PREPROC.REFRESH when the rule was freshly created
--      (parity with deploy_module's `if do_refresh and rule_created`): RESOLUTION
--      depends only on CONFIG, so an idempotent re-install that changed no rule
--      needs no rebuild.
--
-- Idempotency / never-mutate-a-live-version (ADR-046)
--   RULE_ENSURE and MODULE_REGISTER are both idempotent, so re-installing the
--   identical version writes no second rule or provenance row. CREATE OR REPLACE
--   of the same PREPROC_RT.<NAME>_V<N> body is a no-op in effect. The script never
--   mutates an existing live version — an update deploys a new _V<N+1>.
--
-- Result contract
--   Created RETURNS TABLE: (module, version, phase, status, library_version), one
--   row. library_version is the concrete library release the resolved index
--   declared (NULL when the index carried none), so the operator sees which
--   release a `latest` install landed on.
--     status = 'installed'        a fresh library-deployed install
--            = 'already-current'  an idempotent library-deployed re-install
--            = 'registered-only'  a self-deployed module (rule + provenance, no
--                                 CREATE)
-- =============================================================================

-- The CREATE header declares the typed parameters; luacheck cannot see it, so
-- declare them (and the Exasol `null` sentinel is already a project read_global).
-- luacheck: read globals source module_name scope

local RESOLVER = "PREPROC.MODULE_RESOLVER_V1"
local VALID_SCOPE_TYPES = {ROLE = true, USER = true}

-- Resolver EMITS columns are read positionally (1-based) in this fixed order.
local COL_STATUS = 1
local COL_ERROR_KIND = 2
local COL_ERROR_MESSAGE = 3
local COL_NAME = 4
local COL_VERSION = 5
local COL_PHASE = 6
local COL_SCRIPT_NAME = 7
local COL_FUNCTION_NAME = 8
local COL_DEPLOY_MODE = 9
local COL_SHA256 = 10
local COL_SOURCE_REPO = 13
local COL_SOURCE_REF = 14
local COL_LIBRARY_VERSION = 15
local COL_CHUNK_INDEX = 16
local COL_CHUNK_COUNT = 17
local COL_ARTIFACT_CHUNK = 18

-- A SQL NULL parameter may arrive as Lua nil or the Exasol `null` sentinel.
local function is_null(value)
    return value == nil or value == null
end

-- Single-quote a value as a SQL string literal, doubling embedded quotes so an
-- operator-supplied name cannot break out of the literal.
local function sql_str(value)
    return "'" .. string.gsub(tostring(value), "'", "''") .. "'"
end

-- SQL literal for a nullable string: the NULL keyword when absent.
local function sql_nullable(value)
    if is_null(value) then
        return "NULL"
    end
    return sql_str(value)
end

-- Parse the REQUIRED scope 'TYPE:value' into (scope_type, scope_value). The type
-- is upper-cased and validated; the value is preserved verbatim (parity with the
-- CLI's _parse_scope_arg, which upper-cases only the type).
local function parse_scope(raw)
    if is_null(raw) then
        error("PREPROC.MODULE_INSTALL: scope is required "
            .. "(expected 'ROLE:<name>' or 'USER:<name>')")
    end
    local text = tostring(raw)
    local colon = string.find(text, ":", 1, true)
    if colon == nil then
        error("PREPROC.MODULE_INSTALL: invalid scope " .. sql_str(text)
            .. " (expected 'ROLE:<name>' or 'USER:<name>')")
    end
    local scope_type = string.upper(string.sub(text, 1, colon - 1))
    local scope_value = string.sub(text, colon + 1)
    if scope_type == "" or scope_value == "" then
        error("PREPROC.MODULE_INSTALL: invalid scope " .. sql_str(text)
            .. " (expected 'ROLE:<name>' or 'USER:<name>')")
    end
    if not VALID_SCOPE_TYPES[scope_type] then
        error("PREPROC.MODULE_INSTALL: invalid scope type " .. sql_str(scope_type)
            .. " (expected ROLE or USER)")
    end
    return scope_type, scope_value
end

-- Remove any interactive-client lone `/` terminator line and trim surrounding
-- whitespace (parity with deploy.strip_script_terminator). The resolver verified
-- the sha256 over the raw bytes before chunking, so stripping here is safe.
local function strip_terminator(text)
    local kept = {}
    for line in string.gmatch(text .. "\n", "([^\n]*)\n") do
        if not string.find(line, "^%s*/%s*$") then
            kept[#kept + 1] = line
        end
    end
    local joined = table.concat(kept, "\n")
    joined = string.gsub(joined, "^%s+", "")
    joined = string.gsub(joined, "%s+$", "")
    return joined
end

-- Reassemble the artifact CREATE text from the resolver's ordered chunks.
local function reassemble_artifact(rows, chunk_count, module_name)
    local chunks = {}
    for _, row in ipairs(rows) do
        local idx = tonumber(row[COL_CHUNK_INDEX])
        if idx ~= nil then
            chunks[idx] = row[COL_ARTIFACT_CHUNK]
        end
    end
    local ordered = {}
    for i = 0, chunk_count - 1 do
        if is_null(chunks[i]) then
            error("PREPROC.MODULE_INSTALL: missing artifact chunk " .. tostring(i)
                .. " of " .. tostring(chunk_count) .. " for module " .. sql_str(module_name))
        end
        ordered[#ordered + 1] = chunks[i]
    end
    return strip_terminator(table.concat(ordered))
end

-- --- Step 0: parse + validate the scope BEFORE touching the resolver ----------
local scope_type, scope_value = parse_scope(scope)

-- --- Step 1: resolve + verify (read-only; runs before any write) --------------
local res_ok, res = pquery("SELECT " .. RESOLVER .. "(:src, :mod)",
    {src = source, mod = module_name})
if not res_ok then
    error("PREPROC.MODULE_INSTALL: resolver invocation failed for module "
        .. sql_str(module_name) .. " from source " .. sql_str(source)
        .. ": " .. tostring(res.error_message))
end
if res[1] == nil then
    error("PREPROC.MODULE_INSTALL: resolver returned no rows for module "
        .. sql_str(module_name) .. " from source " .. sql_str(source))
end
for _, row in ipairs(res) do
    if tostring(row[COL_STATUS]) == "ERROR" then
        error("PREPROC.MODULE_INSTALL: cannot install module " .. sql_str(module_name)
            .. " from source " .. sql_str(source)
            .. ": [" .. tostring(row[COL_ERROR_KIND]) .. "] " .. tostring(row[COL_ERROR_MESSAGE]))
    end
end

-- Metadata is identical on every emitted row; read it from the first.
local head = res[1]
local name = head[COL_NAME]
local version = tonumber(head[COL_VERSION])
local phase = head[COL_PHASE]
local script_name = head[COL_SCRIPT_NAME]
local function_name = head[COL_FUNCTION_NAME]
local deploy_mode = head[COL_DEPLOY_MODE]
local sha256 = head[COL_SHA256]
local source_repo = head[COL_SOURCE_REPO]
local source_ref = head[COL_SOURCE_REF]
local library_version = head[COL_LIBRARY_VERSION]
local chunk_count = tonumber(head[COL_CHUNK_COUNT]) or 0

-- --- Step 2: library-deployed → deploy the versioned script -------------------
-- self-deployed (chunk_count == 0) skips this entirely (parity with deploy.py).
if chunk_count >= 1 then
    local artifact_text = reassemble_artifact(res, chunk_count, module_name)
    local open_ok, open_res = pquery("OPEN SCHEMA PREPROC_RT")
    if not open_ok then
        error("PREPROC.MODULE_INSTALL: OPEN SCHEMA PREPROC_RT failed: "
            .. tostring(open_res.error_message))
    end
    local create_ok, create_res = pquery(artifact_text)
    if not create_ok then
        error("PREPROC.MODULE_INSTALL: deploying module script " .. tostring(script_name)
            .. " failed: " .. tostring(create_res.error_message))
    end
end

-- --- Step 3: idempotently register the activation rule (refresh suppressed) ---
local rule_ok, rule_res = pquery("EXECUTE SCRIPT PREPROC.RULE_ENSURE("
    .. sql_str(scope_type) .. ", " .. sql_str(scope_value) .. ", "
    .. sql_str(phase) .. ", " .. sql_str(script_name) .. ", "
    .. sql_str(function_name) .. ", NULL, TRUE)")
if not rule_ok then
    error("PREPROC.MODULE_INSTALL: RULE_ENSURE failed: " .. tostring(rule_res.error_message))
end
local rule_created = (rule_res[1] ~= nil and rule_res[1][2] == true)

-- --- Step 4: idempotently upsert the provenance row ---------------------------
-- `source` is the effective source the operator/default resolved from (its
-- audit value: reveals whether a mutable `latest` tag, a pinned vX.Y.Z, or a
-- bucketfs: path was used); library_version is the concrete release the index
-- self-declared, so a `latest` install is auditable back to a fixed version.
local reg_ok, reg_res = pquery("EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
    .. sql_str(name) .. ", " .. string.format("%d", version) .. ", "
    .. sql_nullable(source_repo) .. ", " .. sql_nullable(source_ref) .. ", "
    .. sql_nullable(sha256) .. ", " .. sql_str(script_name) .. ", "
    .. sql_str(function_name) .. ", " .. sql_str(phase) .. ", "
    .. sql_str(deploy_mode) .. ", " .. sql_nullable(source) .. ", "
    .. sql_nullable(library_version) .. ")")
if not reg_ok then
    error("PREPROC.MODULE_INSTALL: MODULE_REGISTER failed: " .. tostring(reg_res.error_message))
end
local provenance_created = (reg_res[1] ~= nil and reg_res[1][4] == true)

-- --- Step 5: the single refresh (only when a rule was freshly created) --------
if rule_created then
    local refresh_ok, refresh_res = pquery("EXECUTE SCRIPT PREPROC.REFRESH")
    if not refresh_ok then
        error("PREPROC.MODULE_INSTALL: REFRESH failed: " .. tostring(refresh_res.error_message))
    end
end

-- --- Step 6: report the outcome -----------------------------------------------
local status
if chunk_count == 0 then
    status = "registered-only"
elseif rule_created or provenance_created then
    status = "installed"
else
    status = "already-current"
end

-- `module` is a reserved word in a column-description string, so the output
-- column is quoted to carry the spec's `module | version | phase | status`
-- shape, extended with the resolved library_version so the operator sees which
-- concrete release a `latest` install landed on. Coerce a missing version to the
-- `null` sentinel (a Lua nil would shorten the row and break the column count).
local library_version_out = is_null(library_version) and null or library_version
exit(
    {{name, version, phase, status, library_version_out}},
    '"module" VARCHAR(256), version DECIMAL(9,0), phase VARCHAR(32), status VARCHAR(32), '
        .. 'library_version VARCHAR(64)'
)
