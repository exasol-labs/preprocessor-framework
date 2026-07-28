-- =============================================================================
-- MODULE_RECLAIM guarded module version reclaim  (PREPROC.MODULE_RECLAIM)
-- =============================================================================
-- Role
--   The RECLAIM step of the module lifecycle: the physical teardown of every
--   object the retired version created, plus the removal of the version's
--   provenance row. It is GUARDED and is NEVER the same flow as retirement
--   (PREPROC.MODULE_RETIRE only marks status = 'retired'; reclaim is a separate,
--   later admin call), mirroring the profile never-same-refresh-drop invariant
--   (ADR-022): a version is dropped only after it has been retired AND is provably
--   unreachable. Reclaim touches only PREPROC.MODULES (delete) and the catalog
--   (drop); it changes no CONFIG rule (there is provably none — see guard 1), so
--   it issues no REFRESH_CORE rebuild.
--
-- The reclaim safety argument (why dropping <NAME>_V<N> cannot strand a caller)
--   A caller can end up importing PREPROC_RT.<NAME>_V<N> by exactly two paths, and
--   both are closed by the two guards below before any DROP:
--
--     GUARD 1 — no ENABLED CONFIG rule names the script.
--       MASTER dispatches the CURRENT pipeline resolved from enabled CONFIG rules
--       (PREPROC.RESOLUTION is built from enabled rows only). If no enabled rule
--       names <NAME>_V<N>, MASTER never imports it for any live dispatch. A
--       DISABLED rule cannot dispatch (it is absent from RESOLUTION and produces
--       no profile), so it does not block — the guard checks enabled = TRUE, the
--       exact reachability condition.
--
--     GUARD 2 — no NON-ORPHANED (live) PROFILES manifest bakes the script.
--       A convenience-mode session may be pinned to a baked profile
--       PREPROC_RT.P_<hash> that statically imports <NAME>_V<N>. A LIVE (non-
--       orphaned) profile is the only kind that still imports its baked scripts,
--       so a live profile baking <NAME>_V<N> blocks the drop. An ORPHANED profile
--       does NOT block: REFRESH_CORE's RETIRE step replaced its body with the
--       MASTER stub (MASTER's own body), which imports NOTHING statically — it
--       re-runs the live resolve -> dispatch -> swap for the session's next
--       statement. With guard 1 already holding (no enabled rule names V<N>), that
--       re-resolve targets the current version, never V<N>. So even while the
--       orphaned P_<hash> script (and sessions pinned to it) still physically
--       exist, dropping V<N> is safe. This is the generational safety inherited
--       from the profile lifecycle: a V<N>-baking profile must first be orphaned
--       (converted to a stub by a refresh) before this guard passes.
--
--   Both guards are evaluated against the module's ENTRY script (the row's
--   script_name), not against its companions: a rule names an entry script and a
--   profile bakes an entry script, so the entry script's reachability is the
--   reachability of the whole version.
--
--   Ordering consequence: the update flow MUST repoint the rule off V<N> and
--   refresh (which orphans the V<N>-baking profile) BEFORE reclaiming V<N>. If it
--   has not, guard 1 (rule still enabled) or guard 2 (profile still live) blocks
--   the drop and reclaim is a safe no-op that leaves the retired row intact.
--
-- Manifest match (guard 2) is literal, not LIKE
--   PROFILES.manifest is the '||'-joined LISTAGG of
--   'phase|ordinal|script_name|function_name|client_pattern' entries, so a
--   script_name always appears flanked by '|'. The needle '|'||script_name||'|'
--   is matched with INSTR (wildcard-free): a LIKE would treat the '_' characters
--   in the qualified script name as single-char wildcards and over-match (V1 vs
--   V12, etc.). The flanking '|' prevents a prefix false-match.
--
-- Which objects this drops (the recorded inventory, and nothing else)
--   PREPROC.MODULES.objects records the version's object inventory as it was
--   declared AND parse-verified at install time (ADR-055), so it is exactly the
--   set of objects the module created. That inventory — NOT a schema name — is
--   the ownership boundary (ADR-057): an artifact may legitimately create objects
--   in any schema the installing admin can write, so an object outside PREPROC_RT
--   IS reclaimed when the inventory records it, and an object absent from the
--   inventory is NEVER dropped, whatever schema it lives in and however closely
--   its name resembles a companion's. A NULL inventory — a row written before the
--   composite delta, or a single-statement module — denotes the single LUA SCRIPT
--   named by the row's script_name, which is exactly what
--   preproc.module.registry derives for a schema_version 1 index entry, so the
--   CLI and this script read such a row identically. An object already absent is
--   the normal idempotent case and still reclaims the row.
--
--   Generational isolation is what makes the widened boundary safe across
--   versions: every object of a version carries the module's _V<N> suffix
--   (ADR-056), so a v1 inventory and a v2 inventory share no object name and
--   reclaiming v1 cannot touch a live v2 companion — not even one whose body is
--   byte-identical between the two generations, because the drop set is resolved
--   from names, never from bodies.
--
-- Inventory wire format (preproc.module.registry.encode_objects)
--   Newline-separated TYPE|NAME lines, one per object, in FILE order:
--     LUA SCRIPT|PREPROC_RT.CONTROL_DATABASE_V1
--     PYTHON3 SCALAR SCRIPT|PREPROC_RT.CONFD_JOB_V1
--     TABLE|PREPROC_RT.CONFD_STATE_V1
--   Each line splits on its FIRST '|' ('^(.-)|(.*)$' — non-greedy type, greedy
--   name), which is exactly registry.decode_objects's str.partition. Delimiter
--   safety comes from validation, not escaping: manifest.py rejects a declared
--   object type or name containing a newline, a carriage return, or a '|', so
--   nothing here needs unescaping and a name can never straddle two lines.
--
-- Reverse file order
--   The recorded inventory is the undo list. Install runs the artifact's
--   statements forward, so a later object may depend on an earlier one; teardown
--   therefore runs BACKWARD, removing a dependent before the object it depends
--   on. This is what lets a module that creates its own schema be reclaimed at
--   all: the schema is dropped RESTRICT (never CASCADE — see below), which
--   succeeds only once the objects the inventory placed inside it are gone.
--
-- DROP forms and quoting (identical to the CLI's, preproc.module.deploy.drop_sql)
--   A type ending in SCRIPT drops with DROP SCRIPT IF EXISTS — every Exasol script
--   language shares the one DROP verb — and each remaining admitted type maps to
--   its own DROP ... IF EXISTS verb. IF EXISTS keeps every drop idempotent, which
--   matters because reclaim re-runs over inventories that may already be partly
--   gone.
--
--   An identifier cannot be bound as a query parameter, so the DROP is built by
--   concatenation and per-part quoting is what makes it injection-proof whatever
--   the recorded name holds: each part is wrapped in '"' with any embedded '"'
--   doubled, yielding "PREPROC_RT"."FOO_V1". This is lossless for the canonical
--   names MODULE_REGISTER stores (an unquoted identifier folded to upper case
--   names the same object as its quoted upper-case form). A qualified name is
--   split on its FIRST '.' only — safe because preproc.module.manifest rejects a
--   dot inside a quoted SCHEMA part at manifest parse time, so only the OBJECT
--   part may legitimately carry one, and the first-dot split always lands on the
--   true schema/object boundary; SCHEMA, VIRTUAL SCHEMA, and CONNECTION are
--   unqualified names and are not split at all.
--
--   A SCHEMA is dropped RESTRICT (Exasol's default). Reclaim must never destroy
--   data it does not own, so a schema still holding objects outside the inventory
--   surfaces as a reported drop failure rather than being CASCADE-emptied. A
--   VIRTUAL SCHEMA is the deliberate exception: it always holds virtual tables, so
--   RESTRICT could never drop one, and CASCADE removes only the local metadata,
--   never anything at the remote source.
--
-- Pre-flight, then drop-then-verify, then delete (mirrors REFRESH_CORE reclaim)
--   Exasol DDL implicitly commits and cannot be rolled back, so every inventory
--   entry is resolved to its DROP statement and its catalog probe BEFORE the first
--   DROP: an entry this script cannot render is a corrupt inventory and aborts
--   while the database is still untouched, rather than half-way through a
--   teardown that cannot be undone.
--
--   Past that pre-flight reclaim is inherently best-effort. Every remaining object
--   is still attempted after one fails, because the failure is not undoable and
--   skipping the rest would only widen the leak. The provenance row is the sole
--   record that a retired version still needs reclaiming, so it is deleted ONLY
--   after every object it named reads as absent from object_present's probe. Any
--   object left present (a transient lock, a schema RESTRICT refused) leaves the
--   row intact so the next reclaim retries, rather than stranding untracked
--   objects. Partial removal is therefore possible and is reported as
--   drop_failed; that is the honest outcome, not a rollback.
--
--   object_present's guarantee is bounded by what EXA_ALL_OBJECTS can see, not
--   the true state of the catalog: EXA_ALL_OBJECTS is privilege-filtered, so an
--   inventory object in a foreign schema whose grant was revoked (or never held)
--   reads as absent even though it still physically exists. In that case reclaim
--   reports "reclaimed" and deletes the provenance row while the object survives
--   untracked. This is the same privilege boundary every other EXA_ALL_* probe in
--   this script (and the CLI's own catalog reads) is subject to; it is not
--   special-cased here because there is no privilege-blind alternative catalog
--   view to probe instead.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py; bare
-- names, no SQL types)
--   name     VARCHAR(128)  module name
--   version  DECIMAL(9,0)  the retired version to reclaim
--
-- Result contract
--   Created RETURNS TABLE: (name, version, dropped, reclaimed, reason).
--     dropped   — at least one recorded object was present and every present one
--                 was dropped and confirmed absent.
--     reclaimed — the provenance row was removed.
--     reason    — 'reclaimed' | 'blocked_by_rule' | 'blocked_by_profile' |
--                 'not_retired' | 'not_found' | 'drop_failed'.
-- =============================================================================

-- The CREATE header declares the typed parameters; luacheck cannot see it.
-- luacheck: read globals name version

local RESULT_SPEC =
    "name VARCHAR(128), version DECIMAL(9,0), dropped BOOLEAN, reclaimed BOOLEAN, reason VARCHAR(64)"

-- A NULL recorded inventory denotes this one object type named by script_name,
-- matching preproc.module.manifest.ENTRY_OBJECT_TYPE.
local ENTRY_OBJECT_TYPE = "LUA SCRIPT"

-- Every Exasol script language shares the one DROP verb, so the type suffix (not
-- the language) selects it: 'LUA SCRIPT', 'PYTHON3 SCALAR SCRIPT', 'SCRIPT', ...
local SCRIPT_TYPE_SUFFIX = "SCRIPT"
local SCRIPT_DROP = "DROP SCRIPT IF EXISTS"

-- The remaining object types the module contract admits (preproc.module.artifact
-- closes the set), each with the DROP form preproc.module.deploy.drop_sql uses.
local DROP_VERBS = {
    TABLE = "DROP TABLE IF EXISTS",
    VIEW = "DROP VIEW IF EXISTS",
    FUNCTION = "DROP FUNCTION IF EXISTS",
    CONNECTION = "DROP CONNECTION IF EXISTS",
    SCHEMA = "DROP SCHEMA IF EXISTS",
    ["VIRTUAL SCHEMA"] = "DROP VIRTUAL SCHEMA IF EXISTS",
}

-- Types whose name is a single unqualified identifier (never schema-qualified).
local UNQUALIFIED_TYPES = {SCHEMA = true, ["VIRTUAL SCHEMA"] = true, CONNECTION = true}

local CASCADE_TYPES = {["VIRTUAL SCHEMA"] = true}

local function is_script_type(object_type)
    return string.sub(object_type, -#SCRIPT_TYPE_SUFFIX) == SCRIPT_TYPE_SUFFIX
end

local function quote_part(part)
    local escaped = string.gsub(part, '"', '""')
    return '"' .. escaped .. '"'
end

-- Split a recorded name into (schema, object). Returns nil for the schema of an
-- unqualified type, and (nil, nil) for a name this script cannot render.
local function split_qualified(object_type, object_name)
    if object_name == "" then
        return nil, nil
    end
    if UNQUALIFIED_TYPES[object_type] then
        return nil, object_name
    end
    local dot = string.find(object_name, ".", 1, true)
    if dot == nil or dot == 1 or dot == #object_name then
        return nil, nil
    end
    return string.sub(object_name, 1, dot - 1), string.sub(object_name, dot + 1)
end

-- EXA_ALL_OBJECTS's OBJECT_TYPE for an inventory type: one 'SCRIPT' for every
-- script language, and a virtual schema is catalogued as a SCHEMA.
local function catalog_object_type(object_type)
    if is_script_type(object_type) then
        return "SCRIPT"
    end
    if object_type == "VIRTUAL SCHEMA" then
        return "SCHEMA"
    end
    return object_type
end

-- Parse PREPROC.MODULES.objects into inventory entries in FILE order. A NULL or
-- empty inventory falls back to the single entry script named by script_name.
local function parse_inventory(recorded, entry_name)
    if recorded == nil or recorded == null then
        return {{type = ENTRY_OBJECT_TYPE, name = entry_name}}
    end
    local entries = {}
    for line in string.gmatch(tostring(recorded), "[^\n]+") do
        local object_type, object_name = string.match(line, "^(.-)|(.*)$")
        if object_type == nil then
            error("PREPROC.MODULE_RECLAIM: malformed recorded inventory: line carries no '|' "
                .. "delimiter: " .. line)
        end
        entries[#entries + 1] = {type = object_type, name = object_name}
    end
    if #entries == 0 then
        return {{type = ENTRY_OBJECT_TYPE, name = entry_name}}
    end
    return entries
end

-- Resolve one inventory entry to its DROP statement and catalog probe, or raise.
-- Raising here is the pre-flight: it happens before any DROP, so a corrupt
-- inventory leaves the database and the provenance row untouched.
local function resolve_object(entry)
    local verb
    if is_script_type(entry.type) then
        verb = SCRIPT_DROP
    else
        verb = DROP_VERBS[entry.type]
    end
    if verb == nil then
        error("PREPROC.MODULE_RECLAIM: recorded inventory names an object type with no DROP "
            .. "form: " .. tostring(entry.type))
    end
    local schema, object = split_qualified(entry.type, entry.name)
    if object == nil then
        error("PREPROC.MODULE_RECLAIM: recorded inventory object name is not usable for a "
            .. tostring(entry.type) .. ": " .. tostring(entry.name))
    end
    local target
    if schema == nil then
        target = quote_part(object)
    else
        target = quote_part(schema) .. "." .. quote_part(object)
    end
    local cascade = ""
    if CASCADE_TYPES[entry.type] then
        cascade = " CASCADE"
    end
    return {
        type = entry.type,
        catalog_type = catalog_object_type(entry.type),
        schema = schema,
        object = object,
        drop_sql = verb .. " " .. target .. cascade,
    }
end

-- Catalog presence of one resolved object. EXA_ALL_OBJECTS covers every admitted
-- schema object and the schemas themselves; a CONNECTION is not a schema object
-- and appears only in EXA_ALL_CONNECTIONS. The name is BOUND, never concatenated,
-- so a recorded name cannot reshape the probe.
local function object_present(resolved)
    local sql, binds
    if resolved.type == "CONNECTION" then
        sql = "SELECT COUNT(*) AS c FROM EXA_ALL_CONNECTIONS WHERE CONNECTION_NAME = :n"
        binds = {n = resolved.object}
    elseif resolved.schema == nil then
        sql = [[SELECT COUNT(*) AS c FROM EXA_ALL_OBJECTS
                  WHERE OBJECT_TYPE = 'SCHEMA' AND ROOT_NAME IS NULL AND OBJECT_NAME = :n]]
        binds = {n = resolved.object}
    else
        sql = [[SELECT COUNT(*) AS c FROM EXA_ALL_OBJECTS
                  WHERE OBJECT_TYPE = :t AND ROOT_NAME = :s AND OBJECT_NAME = :n]]
        binds = {t = resolved.catalog_type, s = resolved.schema, n = resolved.object}
    end
    local ok, res = pquery(sql, binds)
    if not ok then
        error("PREPROC.MODULE_RECLAIM: catalog check failed for '" .. resolved.drop_sql
            .. "': " .. tostring(res.error_message))
    end
    return (tonumber(res[1][1]) or 0) ~= 0
end

-- --- Step 1: read the provenance row ------------------------------------------
local row_ok, row_res = pquery(
    "SELECT script_name, status, objects FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not row_ok then
    error("PREPROC.MODULE_RECLAIM: provenance read failed: " .. tostring(row_res.error_message))
end
if row_res[1] == nil then
    -- Nothing recorded (already reclaimed or never registered): idempotent no-op.
    exit({{name, version, false, false, "not_found"}}, RESULT_SPEC)
end
local script_name = tostring(row_res[1][1])
local status = tostring(row_res[1][2])
local recorded_objects = row_res[1][3]

-- --- Step 2: only a retired version is reclaimable ----------------------------
-- A deployed (live) version must be retired first; reclaim never drops a live one.
if status ~= "retired" then
    exit({{name, version, false, false, "not_retired"}}, RESULT_SPEC)
end

-- --- Step 3: GUARD 1 — no enabled CONFIG rule names the script -----------------
local rule_ok, rule_res = pquery(
    "SELECT COUNT(*) AS c FROM PREPROC.CONFIG WHERE enabled = TRUE AND script_name = :sn",
    {sn = script_name}
)
if not rule_ok then
    error("PREPROC.MODULE_RECLAIM: rule check failed: " .. tostring(rule_res.error_message))
end
if (tonumber(rule_res[1][1]) or 0) ~= 0 then
    exit({{name, version, false, false, "blocked_by_rule"}}, RESULT_SPEC)
end

-- --- Step 4: GUARD 2 — no live (non-orphaned) profile bakes the script ---------
local needle = "|" .. script_name .. "|"
local prof_ok, prof_res = pquery(
    [[SELECT COUNT(*) AS c FROM PREPROC.PROFILES
        WHERE orphaned_at IS NULL AND INSTR(manifest, :needle) > 0]],
    {needle = needle}
)
if not prof_ok then
    error("PREPROC.MODULE_RECLAIM: profile check failed: " .. tostring(prof_res.error_message))
end
if (tonumber(prof_res[1][1]) or 0) ~= 0 then
    exit({{name, version, false, false, "blocked_by_profile"}}, RESULT_SPEC)
end

-- --- Step 5: pre-flight the recorded inventory (no DDL yet) --------------------
local inventory = parse_inventory(recorded_objects, script_name)
local drop_set = {}
for i = 1, #inventory do
    drop_set[i] = resolve_object(inventory[i])
end

-- --- Step 6: drop the inventory in REVERSE file order -------------------------
local dropped = false
local failed = 0
for i = #drop_set, 1, -1 do
    local target = drop_set[i]
    if object_present(target) then
        local drop_ok = pquery(target.drop_sql)
        if not drop_ok or object_present(target) then
            -- Left present: keep going (best-effort) and report drop_failed below.
            failed = failed + 1
        else
            dropped = true
        end
    end
end
if failed ~= 0 then
    -- At least one recorded object survives; leave the provenance row for retry.
    exit({{name, version, false, false, "drop_failed"}}, RESULT_SPEC)
end

-- --- Step 7: remove the provenance row (reclamation recorded by deletion) ------
local del_ok, del_res = pquery(
    "DELETE FROM PREPROC.MODULES WHERE name = :n AND version = :v",
    {n = name, v = version}
)
if not del_ok then
    error("PREPROC.MODULE_RECLAIM: provenance delete failed: " .. tostring(del_res.error_message))
end

local commit_ok, commit_res = pquery("COMMIT")
if not commit_ok then
    error("PREPROC.MODULE_RECLAIM: commit failed: " .. tostring(commit_res.error_message))
end

exit({{name, version, dropped, true, "reclaimed"}}, RESULT_SPEC)
