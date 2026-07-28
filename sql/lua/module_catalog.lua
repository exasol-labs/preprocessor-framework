-- =============================================================================
-- MODULE_CATALOG source discovery listing  (PREPROC.MODULE_CATALOG)
-- =============================================================================
-- Role
--   Read-only DB-side analogue of `preproc module list --registry`: lists every
--   module a source's registry/index.json advertises -- name, version, phase,
--   deploy_mode -- alongside an `installed?` flag joined against the existing
--   PREPROC_RT.INSTALLED_MODULES provenance view (ADR-046), so a DBA can see
--   what a source offers and what is already installed before committing to
--   PREPROC.MODULE_INSTALL. It NEVER writes: it touches no row of
--   PREPROC.CONFIG, PREPROC.SETTINGS, PREPROC.RESOLUTION, or PREPROC.MODULES --
--   every statement it issues is a SELECT.
--
-- Parameters (declared in the CREATE OR REPLACE LUA SCRIPT header, deployed by
-- install.py / bundle.py -- not in this body)
--   source   VARCHAR(2000)  bucketfs:CONN/path or https://.../registry/index.json
--   The parameter is directly accessible as the Lua variable of the same name.
--
-- Drives the resolver in index-metadata-only mode
--   MODULE_CATALOG calls PREPROC.MODULE_RESOLVER_V1(source, '*') -- the additive
--   list-all sentinel that emits one metadata-only row per advertised module
--   (no artifact fetch, no sha256 verification of bodies) instead of resolving
--   one named module. Any 'ERROR' row (bad_source, source_unreadable,
--   network_error, ...) aborts the whole call via error(), naming the source --
--   there is nothing partial to report when the index itself could not be read,
--   so MODULE_CATALOG returns no rows in that case.
--
-- installed? flag  (deployed, not retired)
--   A second read of PREPROC_RT.INSTALLED_MODULES filtered to status = 'deployed'
--   builds a (name, version) lookup set. A version whose only PREPROC.MODULES
--   row is 'retired' is therefore absent from the lookup and reported as NOT
--   installed, even though a provenance row for it exists.
--
-- Admin-only
--   MODULE_CATALOG inherits the same PREPROC_ADMIN boundary as MODULE_INSTALL
--   because the resolver UDF it drives is admin-only and PREPROC carries no
--   PUBLIC EXECUTE grant.
--
-- Result contract
--   Created RETURNS TABLE: one row per advertised module with columns
--     name, version, phase, deploy_mode, installed, object_count, objects
--   emitted via exit(result_table, column_spec). A source advertising zero
--   modules returns an empty result set (not an error).
--
-- object_count / objects  (the pre-install review gate -- ADR-057)
--   The resolver's list-all ('*') row already carries the entry's DECLARED
--   object inventory in its `objects` column (schema_version-1 script_name
--   fallback already applied there -- see module_resolver_v1.sql), so this
--   script reports it VERBATIM rather than re-deriving anything: `objects` is
--   passed through unchanged (the same newline-separated 'TYPE|NAME' wire
--   format PREPROC.MODULES.objects and PREPROC_RT.INSTALLED_MODULES use, and
--   the same format module_reclaim.lua parses) and `object_count` is the
--   number of lines in it. A NULL `objects` (a self-deployed / no-artifact
--   entry) reports object_count 0 -- there is no artifact to enumerate.
--   Because this is the declared inventory as written in the entry, an object
--   outside PREPROC_RT or PREPROC appears exactly as declared, schema and all
--   -- there is nothing to hide it, and MODULE_CATALOG creates none of the
--   objects it reports.
-- =============================================================================

-- The CREATE header declares the typed parameter; luacheck cannot see it, so
-- declare it as a read-only global here.
-- luacheck: read globals source

local LIST_ALL_SENTINEL = "*"

-- Count the newline-separated 'TYPE|NAME' lines in a resolver `objects` value.
-- A NULL/absent inventory (self-deployed / no-artifact entry) counts as 0.
local function object_count(objects_raw)
    if objects_raw == nil or objects_raw == null then
        return 0
    end
    local count = 0
    for _ in string.gmatch(tostring(objects_raw), "[^\n]+") do
        count = count + 1
    end
    return count
end

-- --- Step 1: drive the resolver in index-metadata-only mode -------------------
-- No FROM clause: a SET script over a single-row driving input forms exactly
-- one group, so PREPROC.MODULE_RESOLVER_V1 runs once per call.
local res_ok, res_rows = pquery(
    "SELECT PREPROC.MODULE_RESOLVER_V1(:src, :mod)",
    {src = source, mod = LIST_ALL_SENTINEL}
)
if not res_ok then
    error("PREPROC.MODULE_CATALOG: resolver call failed: " .. tostring(res_rows.error_message))
end

-- Scan EVERY resolver row for an ERROR before reporting anything (symmetry with
-- PREPROC.MODULE_INSTALL's abort-before-write loop). An index read failure emits
-- its single ERROR row first, but scanning all rows keeps the two callers
-- identical rather than trusting the error to always be row 1.
for i = 1, #res_rows do
    if tostring(res_rows[i][1]) == "ERROR" then
        error(
            "PREPROC.MODULE_CATALOG: cannot resolve source " .. tostring(source) .. ": "
                .. tostring(res_rows[i][3])
        )
    end
end

-- --- Step 2: read deployed provenance for the installed? lookup ---------------
local ins_ok, ins_rows = pquery(
    "SELECT name, version FROM PREPROC_RT.INSTALLED_MODULES WHERE status = 'deployed'"
)
if not ins_ok then
    error("PREPROC.MODULE_CATALOG: provenance read failed: " .. tostring(ins_rows.error_message))
end
local installed = {}
for i = 1, #ins_rows do
    local r = ins_rows[i]
    installed[r[1] .. "@" .. tostring(r[2])] = true
end

-- --- Step 3: marshal the catalog rows, joining the installed? flag ------------
-- Resolver EMITS order: status(1) error_kind(2) error_message(3) name(4)
-- version(5) phase(6) script_name(7) function_name(8) deploy_mode(9) sha256(10)
-- suggested_scope_type(11) suggested_scope_value(12) source_repo(13)
-- source_ref(14) library_version(15) chunk_index(16) chunk_count(17)
-- artifact_chunk(18) objects(19).
local rows = {}
for i = 1, #res_rows do
    local r = res_rows[i]
    local name, version, phase, deploy_mode, objects = r[4], r[5], r[6], r[9], r[19]
    -- `objects` sits in the LAST column position: a Lua nil here (as opposed to
    -- the `null` sentinel) would shorten this row's table constructor and break
    -- the column count, exactly the reason module_install.lua coerces its own
    -- trailing nullable column the same way.
    local objects_out = objects == nil and null or objects
    rows[i] = {
        name,
        version,
        phase,
        deploy_mode,
        installed[name .. "@" .. tostring(version)] == true,
        object_count(objects),
        objects_out,
    }
end

exit(
    rows,
    "name VARCHAR(256), version DECIMAL(9,0), phase VARCHAR(32),"
        .. " deploy_mode VARCHAR(32), installed BOOLEAN,"
        .. " object_count DECIMAL(9,0), objects VARCHAR(200000)"
)
