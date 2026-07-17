-- =============================================================================
-- RULE_ENSURE idempotent preprocessor rule installer  (PREPROC.RULE_ENSURE)
-- =============================================================================
-- Role
--   The declarative, idempotent sibling of PREPROC.RULE_ADD. RULE_ADD always
--   allocates a NEW rule_id and inserts a row (documented allocate-new
--   behaviour, unchanged and still separately tested). RULE_ENSURE instead
--   converges on the natural rule key: it inserts a rule only when no ENABLED
--   rule with the same (scope_type, scope_value, phase, script_name,
--   function_name, client_pattern) already exists, and returns the existing
--   rule_id otherwise. This is the surface `preproc module sync` uses so a
--   declarative re-apply never stacks duplicates — closing BUG-008, where an
--   identical rule re-added N times made a TRANSLATE/REWRITE module run N times.
--
--   Validation and auto-refresh mirror RULE_ADD exactly, so the two surfaces are
--   interchangeable except for the idempotency: every input is validated BEFORE
--   any write, so a rejected call leaves CONFIG exactly as it was, and on the
--   insert path the shared REFRESH_CORE module re-materialises RESOLUTION.
--
-- Parameters (declared in the CREATE OR REPLACE LUA SCRIPT header, deployed by
-- install.py / bundle.py — not in this body; Exasol LUA SCRIPT parameters are
-- bare names with no SQL types)
--   scope_type      VARCHAR(4)    'ROLE' or 'USER'
--   scope_value     VARCHAR(128)  the role or user name the rule scopes to
--   phase           VARCHAR(32)   'TRANSLATE', 'EXPAND', or 'REWRITE'
--   script_name     VARCHAR(128)  fully-qualified SCHEMA.NAME of the rule module
--   function_name   VARCHAR(128)  entry function within that module
--   client_pattern  VARCHAR(256)  client-app substring, or SQL NULL for apply-always
--   norefresh       BOOLEAN       TRUE to skip the auto-refresh (caller refreshes)
--   The parameters are directly accessible as Lua variables of the same name.
--
-- Natural rule key (the idempotency key)
--   (scope_type, scope_value, phase, script_name, function_name, client_pattern),
--   matched over ENABLED rows only (enabled = TRUE). A DISABLED row with the same
--   key does not satisfy the ensure — the contract is "an enabled rule for this
--   tuple exists", so a match requires enabled = TRUE. client_pattern is compared
--   NULL-safely: a NULL pattern matches only a NULL pattern (client_pattern IS
--   NULL), never a non-NULL one, so the SQL is issued in two shapes rather than
--   relying on ambiguous NULL-bind equality. If several enabled rows somehow
--   match, the LOWEST rule_id is returned (deterministic, mirroring REFRESH_CORE's
--   lowest-rule_id-wins dedup).
--
-- Validation order (the invariant that keeps a rejected call side-effect free)
--   phase, then scope_type, then script existence — all BEFORE the match query
--   and any DML, identical to RULE_ADD. Each raises via error() naming the
--   offending value; no COMMIT has occurred, so CONFIG is unchanged on rejection.
--
-- rule_id allocation (only on the insert path)
--   Identical to RULE_ADD: MAX(rule_id) + 1 over operator space (rule_id < 9000),
--   1 for an empty table, and an error rather than crossing into the reserved
--   9000-9999 demo band.
--
-- Refresh / atomicity
--   * Match found: no CONFIG change, so NO refresh and NO commit are issued — the
--     existing RESOLUTION already reflects the rule. The existing rule_id is
--     returned with created = false.
--   * No match: the single INSERT is followed by rc.rebuild() (REFRESH_CORE),
--     which stamps SETTINGS and COMMITs the new rule together with the rebuilt
--     RESOLUTION. Under norefresh the INSERT is committed on its own so it
--     persists; RESOLUTION is left for a later refresh. created = true.
--
-- Result contract
--   Created RETURNS TABLE: (rule_id, created). rule_id is the existing id on a
--   match or the newly allocated id on an insert; created distinguishes the two
--   so the declarative caller can report converge outcomes without a re-query.
-- =============================================================================

-- rc is bound at runtime by import("PREPROC.REFRESH_CORE", "rc"); luacheck cannot
-- see import's binding, so declare it as a global here.
-- luacheck: globals rc
-- The CREATE header (added by install.py / bundle.py) declares the typed
-- parameters, which Exasol injects as read-only globals; luacheck cannot see that
-- header, so declare them as read-only globals here.
-- luacheck: read globals scope_type scope_value phase script_name function_name client_pattern norefresh

local RESERVED_DEMO_FLOOR = 9000

local VALID_PHASES = {TRANSLATE = true, EXPAND = true, REWRITE = true}
local VALID_SCOPE_TYPES = {ROLE = true, USER = true}

-- --- Step 1: validate phase ---------------------------------------------------
if not VALID_PHASES[phase] then
    error(
        "PREPROC.RULE_ENSURE: invalid phase: " .. tostring(phase)
            .. " (expected TRANSLATE, EXPAND, or REWRITE)"
    )
end

-- --- Step 2: validate scope_type ----------------------------------------------
if not VALID_SCOPE_TYPES[scope_type] then
    error(
        "PREPROC.RULE_ENSURE: invalid scope_type: " .. tostring(scope_type)
            .. " (expected ROLE or USER)"
    )
end

-- --- Step 3: validate the referenced script exists ----------------------------
-- Read the dictionary under invoker rights; the fully-qualified SCHEMA.NAME must
-- name a deployed script. Rejecting here keeps a typo'd rule out of CONFIG.
local chk_ok, chk_res = pquery(
    "SELECT COUNT(*) AS c FROM EXA_ALL_SCRIPTS"
        .. " WHERE SCRIPT_SCHEMA || '.' || SCRIPT_NAME = :sn",
    {sn = script_name}
)
if not chk_ok then
    error(
        "PREPROC.RULE_ENSURE: script existence check failed: "
            .. tostring(chk_res.error_message)
    )
end
if chk_res[1][1] == 0 then
    error("PREPROC.RULE_ENSURE: script not found: " .. tostring(script_name))
end

-- --- Step 4: normalise the client_pattern null-ness ---------------------------
-- A SQL NULL parameter may arrive as Lua nil or the Exasol `null` sentinel; treat
-- both as "no pattern" so the match and insert are issued in the correct shape.
local cp_is_null = (client_pattern == nil or client_pattern == null)

-- --- Step 5: look for an existing enabled rule on the natural key --------------
-- MIN(rule_id) yields the lowest matching id, or the `null` sentinel when nothing
-- matches. client_pattern is compared NULL-safely by issuing the IS-NULL variant
-- when there is no pattern and the equality variant otherwise.
local match_ok, match_res
if cp_is_null then
    match_ok, match_res = pquery(
        [[SELECT MIN(rule_id) AS rid FROM PREPROC.CONFIG
            WHERE enabled = TRUE
              AND scope_type = :st AND scope_value = :sv
              AND phase = :ph AND script_name = :sn AND function_name = :fn
              AND client_pattern IS NULL]],
        {st = scope_type, sv = scope_value, ph = phase, sn = script_name, fn = function_name}
    )
else
    match_ok, match_res = pquery(
        [[SELECT MIN(rule_id) AS rid FROM PREPROC.CONFIG
            WHERE enabled = TRUE
              AND scope_type = :st AND scope_value = :sv
              AND phase = :ph AND script_name = :sn AND function_name = :fn
              AND client_pattern = :cp]],
        {
            st = scope_type, sv = scope_value, ph = phase,
            sn = script_name, fn = function_name, cp = client_pattern,
        }
    )
end
if not match_ok then
    error("PREPROC.RULE_ENSURE: match query failed: " .. tostring(match_res.error_message))
end

local existing_id = match_res[1][1]
if existing_id ~= nil and existing_id ~= null then
    -- Idempotent no-op: an identical enabled rule already exists. Nothing is
    -- inserted, no refresh and no commit are issued, and the existing id is
    -- returned so the declarative caller re-uses it.
    exit({{existing_id, false}}, "rule_id DECIMAL(18,0), created BOOLEAN")
end

-- --- Step 6: allocate a non-colliding rule_id in operator space ---------------
local id_ok, id_res = pquery(
    "SELECT COALESCE(MAX(rule_id), 0) + 1 AS next_id"
        .. " FROM PREPROC.CONFIG WHERE rule_id < :floor",
    {floor = RESERVED_DEMO_FLOOR}
)
if not id_ok then
    error("PREPROC.RULE_ENSURE: rule_id allocation failed: " .. tostring(id_res.error_message))
end
local rule_id = id_res[1][1]
if rule_id >= RESERVED_DEMO_FLOOR then
    error(
        "PREPROC.RULE_ENSURE: operator rule_id space exhausted below "
            .. tostring(RESERVED_DEMO_FLOOR)
    )
end

-- --- Step 7: insert the enabled rule ------------------------------------------
-- enabled = TRUE and version = 1 for a freshly inserted rule; client_pattern is
-- inserted as a NULL literal when absent (bind-safe) or bound otherwise.
local ins_ok, ins_res
if cp_is_null then
    ins_ok, ins_res = pquery(
        [[INSERT INTO PREPROC.CONFIG
            (rule_id, scope_type, scope_value, client_pattern,
             phase, script_name, function_name, enabled, version)
          VALUES
            (:rule_id, :scope_type, :scope_value, NULL,
             :phase, :script_name, :function_name, TRUE, 1)]],
        {
            rule_id = rule_id, scope_type = scope_type, scope_value = scope_value,
            phase = phase, script_name = script_name, function_name = function_name,
        }
    )
else
    ins_ok, ins_res = pquery(
        [[INSERT INTO PREPROC.CONFIG
            (rule_id, scope_type, scope_value, client_pattern,
             phase, script_name, function_name, enabled, version)
          VALUES
            (:rule_id, :scope_type, :scope_value, :cp,
             :phase, :script_name, :function_name, TRUE, 1)]],
        {
            rule_id = rule_id, scope_type = scope_type, scope_value = scope_value,
            cp = client_pattern, phase = phase, script_name = script_name,
            function_name = function_name,
        }
    )
end
if not ins_ok then
    error("PREPROC.RULE_ENSURE: insert failed: " .. tostring(ins_res.error_message))
end

-- --- Step 8: refresh (or commit-only under norefresh) -------------------------
if norefresh == true then
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error("PREPROC.RULE_ENSURE: commit failed: " .. tostring(commit_res.error_message))
    end
else
    import("PREPROC.REFRESH_CORE", "rc")
    rc.rebuild()
end

-- --- Step 9: report the allocated rule_id -------------------------------------
exit({{rule_id, true}}, "rule_id DECIMAL(18,0), created BOOLEAN")
