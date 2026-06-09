-- =============================================================================
-- REFRESH_CORE shared resolution rebuild  (PREPROC.REFRESH_CORE)
-- =============================================================================
-- Role
--   The single source of truth for the resolution rebuild. Materialises the
--   scoped rules in PREPROC.CONFIG (ROLE- and USER-scoped) into the flat,
--   per-user PREPROC.RESOLUTION table that PREPROC_RT.MY_PIPELINE reads, then
--   stamps PREPROC.SETTINGS and commits once. Transitive nested-role expansion
--   is performed in native Exasol SQL (CONNECT BY); this module only
--   orchestrates: it issues the SQL, owns the transaction, and commits.
--
--   Factored out of refresh.lua so that PREPROC.REFRESH and every mutating
--   admin script (RULE_ADD/DROP/ENABLE/DISABLE, SET_MODE) run the IDENTICAL
--   rebuild instead of duplicating the SQL or nesting EXECUTE SCRIPT
--   PREPROC.REFRESH (fragile under Exasol). Callers import this module and
--   invoke rebuild(); they own their own result reporting (exit()).
--
-- Import-loading contract
--   This script is deployed as a plain importable LUA SCRIPT (no RETURNS TABLE).
--   A caller loads it and runs the rebuild via:
--
--     import("PREPROC.REFRESH_CORE", "rc")
--     local rows_inserted = rc.rebuild()
--
--   import() executes this body in the CALLER'S context, so the Exasol globals
--   pquery / error / null are bound. The exported function MUST be declared at
--   top level with plain global function syntax (function rebuild() ... end) so
--   that import() finds it in the module table; a local function would be
--   invisible to the caller.
--
-- Privilege model (invoker rights)
--   Exasol Lua scripts run with INVOKER rights. Reading EXA_DBA_ROLE_PRIVS /
--   EXA_DBA_USERS requires SELECT ANY DICTIONARY, granted to PREPROC_ADMIN. The
--   fingerprint read happens FIRST, so a non-admin caller lacking the privilege
--   fails before any write, leaving PREPROC.RESOLUTION unchanged.
--
-- Resolution model (additive UNION, SQL-privilege style)
--   A user's resolved pipeline is the UNION (superset) of every enabled
--   ROLE-derived rule (via the transitive role closure) and every enabled
--   USER-scoped rule that applies to that user. There is no override or
--   exclusion. Identical (phase, script, function) rows reaching a user by
--   multiple paths are deduplicated, the lowest contributing rule_id winning
--   the ordinal. Ordering within a phase is pure rule_id ascending; scope type
--   is ignored.
--
-- Profile hash manifest (content-addressed profile scripts)
--   After RESOLUTION is reinserted, every row of a user's chain is stamped with a
--   profile_hash: HASH_MD5 over the user's ordered manifest. The manifest is the
--   per-user LISTAGG of 'phase|ordinal|script_name|function_name|client_pattern'
--   joined with '||', ordered by (phase, ordinal). It is deterministic and
--   order-sensitive: two users with identical rules in a different ordinal order,
--   or differing client_patterns, hash to distinct profiles. The same manifest is
--   stored in PREPROC.PROFILES and the hash names the generated PREPROC_RT.P_<hash>
--   profile script, so a session pinned to a hash always finds a matching script.
--
-- Atomicity (the invariant this module exists to centralise)
--   The fingerprint is captured FIRST (so STATUS, recomputing the identical SQL
--   afterwards, reports "current"). Then the rebuild -- DELETE all of
--   RESOLUTION, reinsert resolved rows, stamp each row's profile_hash, generate
--   the per-hash profile scripts, upsert PREPROC.PROFILES, retire then reclaim
--   stale profiles, stamp last_refresh + fingerprint -- runs as one transaction
--   committed once at the end. No reader can observe an emptied RESOLUTION
--   paired with an old last_refresh, nor a populated RESOLUTION paired with a
--   stale stamp, nor a profile_hash that references a not-yet-created profile
--   script.
--
-- Profile lifecycle (generate -> retire -> reclaim, in fixed order)
--   A profile script outlives the chain that created it: a session may be
--   pinned (via convenience-mode self-swap) to PREPROC_RT.P_<hash> long after
--   the rules that produced <hash> are gone. The rebuild therefore manages the
--   full lifecycle, in this strict phase order, all before the single COMMIT:
--
--     GENERATE  CREATE OR REPLACE the real baked body for every CURRENT chain,
--               then clear orphaned_at for any re-adopted hash (a hash that had
--               been retired but is referenced by a current RESOLUTION row again
--               -- its real body is restored above and it must no longer be
--               treated as orphaned). After this, PROFILES is upserted for the
--               current hashes.
--     RETIRE    For each PROFILES hash NOT referenced by any current RESOLUTION
--               row AND not already retired (orphaned_at IS NULL), CREATE OR
--               REPLACE the script with the retirement STUB body (PREPROC_RT.
--               MASTER's own body, read from the catalog) and stamp orphaned_at
--               = CURRENT_TIMESTAMP. A pinned session that next executes a
--               statement re-runs the full MASTER resolve -> dispatch -> swap and
--               re-points itself -- no untransformed gap. Already-retired hashes
--               are excluded by the orphaned_at filter so their original
--               orphaned_at is never re-stamped.
--     RECLAIM   For each retired hash that is provably unpinned, DROP SCRIPT
--               PREPROC_RT.P_<hash> and delete its PROFILES row.
--
--   Never-same-refresh-drop (the hard safety invariant)
--     A profile is NEVER dropped on the refresh that orphans it. RETIRE only
--     stamps; RECLAIM only drops hashes retired on a PRIOR refresh. This is
--     enforced by construction: reclaim_cutoff is captured as CURRENT_TIMESTAMP
--     at the TOP of rebuild() (before any retire), and RECLAIM considers only
--     hashes whose orphaned_at < reclaim_cutoff -- strictly before this refresh
--     began. A hash retired in THIS rebuild has orphaned_at >= reclaim_cutoff
--     and is excluded regardless of session visibility, so it cannot be dropped
--     in the same refresh that orphaned it.
--
--   Login-time reclaim criterion (provably unpinned)
--     A retired hash is reclaimable only when no active session could still be
--     pinned to it. A session pinned to P_<hash> must have logged in BEFORE the
--     hash was retired (you cannot pin to a script that was already a stub at
--     login -- the self-swap targets the current target, not a stub). So the
--     criterion is: NO active EXA_ALL_SESSIONS row has LOGIN_TIME < orphaned_at.
--     Zero active sessions older than orphaned_at => reclaimable (the NOT EXISTS
--     is vacuously true).
--
--   DDL implicit-commit reasoning (why reclaim cannot strand a reader)
--     Each CREATE OR REPLACE / DROP SCRIPT implicitly commits preceding DML, so
--     the single-COMMIT story is nuanced. The invariant that MUST hold is: no
--     COMMITTED RESOLUTION row references a hash whose script was dropped.
--     RECLAIM drops only hashes ABSENT from the current RESOLUTION (orphaned on a
--     prior refresh, not re-adopted), and the current RESOLUTION was already
--     INSERTed above, so a reader resolving the current RESOLUTION never names a
--     reclaimed hash. The DROP's implicit commit of the pending PROFILES delete
--     is therefore safe: the committed RESOLUTION and the dropped script are
--     disjoint by hash.
--
-- Return contract
--   rebuild() returns rows_inserted (a number), the authoritative count of rows
--   materialised into RESOLUTION. It does NOT call exit(): result reporting (and
--   reading last_refresh back) belongs to each caller, because the result shape
--   differs per caller (REFRESH returns rows_inserted + last_refresh; an admin
--   script returns its own outcome). On any failure rebuild() raises via error()
--   so the caller's transaction is left uncommitted up to that point.
-- =============================================================================

-- luacheck: globals rebuild

-- State fingerprint over the resolution inputs (the user universe and the full
-- role-grant graph). Shared verbatim with PREPROC.STATUS so writer and reader
-- compute it identically: an MD5 over the '|'-joined, sorted identifiers.
local FINGERPRINT_SQL = [[
    SELECT HASH_MD5(
        LISTAGG(ident, '|') WITHIN GROUP (ORDER BY ident)
    ) AS fp
    FROM (
        SELECT USER_NAME AS ident FROM EXA_DBA_USERS
        UNION ALL
        SELECT GRANTED_ROLE || ':' || GRANTEE AS ident FROM EXA_DBA_ROLE_PRIVS
    ) t
]]

-- Transitive role-to-user closure JOIN enabled CONFIG, UNIONed with USER-scoped
-- rules, deduplicated to one row per (user, client_pattern, phase, script,
-- function) keeping the lowest contributing rule_id, with a per-(user, phase)
-- ordinal in rule_id-ascending order.
--
-- Closure semantics: in EXA_DBA_ROLE_PRIVS, GRANTED_ROLE is granted to GRANTEE.
-- Walking PRIOR GRANTEE = GRANTED_ROLE descends the grant chain to arbitrary
-- nesting depth. There is deliberately NO START WITH: Exasol forbids IN
-- subqueries outside WHERE clauses, so every row roots a chain and the full
-- (origin_role, transitive_grantee) closure is produced; the JOIN against
-- enabled ROLE-scoped CONFIG then keeps only the origins that matter. Joining
-- EXA_DBA_USERS on USER_NAME = GRANTEE keeps only the users at the leaves
-- (other roles in the chain are intermediate, not output). CONNECT BY NOCYCLE
-- is defensive: a role-grant cycle would otherwise abort the whole refresh.
--
-- PUBLIC handling: every user implicitly holds the built-in PUBLIC role, but
-- that membership is automatic and never materialises as a row in
-- EXA_DBA_ROLE_PRIVS. The closure source therefore UNIONs in a synthetic
-- 'PUBLIC' -> user edge for every user in EXA_DBA_USERS, so the same closure
-- resolves a rule scoped to PUBLIC (origin_role = 'PUBLIC') to ALL users, and
-- also resolves any real role granted TO PUBLIC to all users transitively (its
-- chain reaches the synthetic PUBLIC node and fans out to every user).
local RESOLUTION_SQL = [[
    WITH scoped_rules AS (
        SELECT
            closure.grantee      AS user_name,
            cfg.client_pattern   AS client_pattern,
            cfg.phase            AS phase,
            cfg.script_name      AS script_name,
            cfg.function_name    AS function_name,
            cfg.rule_id          AS rule_id
        FROM (
            SELECT CONNECT_BY_ROOT GRANTED_ROLE AS origin_role,
                   GRANTEE                      AS grantee
            FROM (
                SELECT GRANTED_ROLE, GRANTEE FROM EXA_DBA_ROLE_PRIVS
                UNION ALL
                SELECT 'PUBLIC' AS GRANTED_ROLE, USER_NAME AS GRANTEE
                FROM EXA_DBA_USERS
            )
            CONNECT BY NOCYCLE PRIOR GRANTEE = GRANTED_ROLE
        ) closure
        JOIN PREPROC.CONFIG cfg
            ON cfg.scope_type = 'ROLE'
           AND cfg.enabled = TRUE
           AND cfg.scope_value = closure.origin_role
        JOIN EXA_DBA_USERS u
            ON u.user_name = closure.grantee
        UNION ALL
        SELECT
            cfg.scope_value      AS user_name,
            cfg.client_pattern   AS client_pattern,
            cfg.phase            AS phase,
            cfg.script_name      AS script_name,
            cfg.function_name    AS function_name,
            cfg.rule_id          AS rule_id
        FROM PREPROC.CONFIG cfg
        WHERE cfg.scope_type = 'USER' AND cfg.enabled = TRUE
    ),
    deduped AS (
        SELECT
            user_name,
            client_pattern,
            phase,
            script_name,
            function_name,
            MIN(rule_id) AS min_rule_id
        FROM scoped_rules
        GROUP BY user_name, client_pattern, phase, script_name, function_name
    )
    SELECT
        user_name,
        client_pattern,
        phase,
        script_name,
        function_name,
        ROW_NUMBER() OVER (
            PARTITION BY user_name, phase
            ORDER BY min_rule_id ASC
        ) AS ordinal
    FROM deduped
]]

-- Per-user manifest fragment, reused for both the profile_hash UPDATE and the
-- PROFILES upsert so the stored manifest and the hash describe the identical
-- ordered chain. ORDER BY (phase, ordinal) makes the manifest deterministic and
-- order-sensitive; COALESCE(client_pattern, '') keeps a NULL pattern stable.
local MANIFEST_EXPR = [[
    LISTAGG(
        phase || '|' || CAST(ordinal AS VARCHAR(20)) || '|' ||
        script_name || '|' || function_name || '|' ||
        COALESCE(client_pattern, ''), '||'
    ) WITHIN GROUP (ORDER BY phase, ordinal)
]]

-- Stamp every RESOLUTION row of a user with HASH_MD5 over that user's manifest.
-- Exasol rejects a correlated scalar subquery in an UPDATE ... SET that reads the
-- target table, so the per-user hash is computed once in a USING-derived table
-- and joined back via MERGE on user_name. The aggregate yields one hash per user,
-- so all rows in a chain carry the same profile_hash; users with no rows are
-- untouched (no empty-chain hash).
local HASH_UPDATE_SQL = [[
    MERGE INTO PREPROC.RESOLUTION r
    USING (
        SELECT user_name, HASH_MD5(]] .. MANIFEST_EXPR .. [[) AS h
        FROM PREPROC.RESOLUTION
        GROUP BY user_name
    ) s
    ON r.user_name = s.user_name
    WHEN MATCHED THEN UPDATE SET r.profile_hash = s.h
]]

-- One (profile_hash, manifest) row per distinct chain. Computed per user then
-- DISTINCT-collapsed so two users sharing a chain yield a single profile.
local PROFILE_ROWS_SQL = [[
    SELECT DISTINCT profile_hash, manifest
    FROM (
        SELECT profile_hash, ]] .. MANIFEST_EXPR .. [[ AS manifest
        FROM PREPROC.RESOLUTION
        WHERE profile_hash IS NOT NULL
        GROUP BY profile_hash, user_name
    )
]]

-- The distinct rule rows of one chain, in dispatch order. Selected per hash and
-- serialised into the baked rule table of that hash's profile script. DISTINCT
-- collapses the (per-user) RESOLUTION rows of a shared chain to the single set
-- of rules that defines the chain; ORDER BY (phase, ordinal) makes the baked
-- table deterministic and yields ordinal-ascending order within each phase, on
-- which the generated dispatch loop relies.
local PROFILE_RULES_SQL = [[
    SELECT DISTINCT phase, ordinal, script_name, function_name, client_pattern
    FROM PREPROC.RESOLUTION
    WHERE profile_hash = :hash
    ORDER BY phase, ordinal
]]

-- Profile script body, assembled from PROFILE_BODY_PREFIX, a CLIENT section, and
-- PROFILE_BODY_DISPATCH bracketing the baked rule table. The body is a STATIC
-- mirror of the MASTER dispatcher (guard, optional client lookup, the
-- TRANSLATE->EXPAND->REWRITE loop with per-rule pcall fail-open and EXPAND
-- first-match-terminal) but reads its rules from the baked `rules` literal
-- instead of querying PREPROC_RT.MY_PIPELINE -- that absent pquery per statement
-- is the whole point of the zero-overhead session path.
--
-- The client section is chosen per chain. PROFILE_BODY_CLIENT (which queries
-- EXA_ALL_SESSIONS for the connecting client) is emitted ONLY when at least one
-- baked rule carries a client_pattern; otherwise PROFILE_BODY_NOCLIENT is used,
-- making client_matches unconditionally true and issuing NO per-statement client
-- query. The common case -- no client_pattern rules -- therefore costs no client
-- lookup at all. The connecting client cannot change within a session and Exasol
-- exposes it only via EXA_ALL_SESSIONS (exa.meta carries no client field) and a
-- preprocessor script keeps no cross-statement state, so when a client_pattern
-- IS present the query is unavoidable per statement.
--
-- Stored as level-1 long strings (`[=[ ... ]=]`) so the embedded level-0
-- (`[[ ... ]]`) SQL literal needs no escaping. PROFILE_BODY_PREFIX's first line is
-- the luacheck globals declaration so the generated body validates standalone.
--
-- client_matches here compares against Lua `nil` (the baked table stores nil for
-- a NULL pattern), NOT the Exasol `null` sentinel MASTER tests, because the rule
-- data is a static Lua table, not a live view row.
local PROFILE_BODY_PREFIX = [=[-- luacheck: globals sqlparsing pquery null import
local sqltext = sqlparsing.getsqltext()
if type(sqltext) ~= "string" then return end
local upper = string.upper(sqltext)
if string.find(upper, "^%s*ALTER%s+SESSION")
    or string.find(upper, "^%s*ALTER%s+SYSTEM") then
    return
end
local rules = {]=]

-- CLIENT section, with the EXA_ALL_SESSIONS lookup. Used only for chains that
-- have at least one client_pattern rule.
local PROFILE_BODY_CLIENT = [=[local current_client = ""
local cc_pcall_ok, cc_ok, cc_res = pcall(pquery, [[
    SELECT CLIENT FROM EXA_ALL_SESSIONS WHERE SESSION_ID = CURRENT_SESSION
]])
if cc_pcall_ok and cc_ok and cc_res ~= nil
    and cc_res[1] ~= nil and cc_res[1][1] ~= null then
    current_client = tostring(cc_res[1][1])
end
local function client_matches(cp)
    if cp == nil then return true end
    return string.find(current_client, cp, 1, true) ~= nil
end
]=]

-- CLIENT section, no lookup. Used when no baked rule has a client_pattern, so the
-- connecting client is never needed and no per-statement query is issued.
-- client_matches takes no parameter (the dispatch loop still calls it with one,
-- which Lua ignores) so the standalone body lints clean with no unused argument.
local PROFILE_BODY_NOCLIENT = [=[local function client_matches()
    return true
end
]=]

-- DISPATCH section: the per-rule import/pcall helpers and the fixed-order phase
-- loop, shared by both client variants.
local PROFILE_BODY_DISPATCH = [=[local function dispatch_rule(sn, fn, text)
    local ok, t = pcall(function()
        import(sn, "preproc_module")
        return preproc_module[fn](text)
    end)
    if ok and type(t) == "string" then return t end
    return text
end
local function dispatch_expand_rule(sn, fn, text)
    local ok, t = pcall(function()
        import(sn, "preproc_module")
        return preproc_module[fn](text)
    end)
    if ok and type(t) == "string" then return t end
    return nil
end
local phase_order = {"TRANSLATE", "EXPAND", "REWRITE"}
for _, phase in ipairs(phase_order) do
    for _, rule in ipairs(rules) do
        if rule.phase == phase and client_matches(rule.client_pattern) then
            if phase == "EXPAND" then
                local expanded = dispatch_expand_rule(rule.script_name, rule.function_name, sqltext)
                if expanded ~= nil then
                    sqltext = expanded
                    break
                end
            else
                sqltext = dispatch_rule(rule.script_name, rule.function_name, sqltext)
            end
        end
    end
end
if sqltext ~= sqlparsing.getsqltext() then
    sqlparsing.setsqltext(sqltext)
end
]=]

-- The deployed body of PREPROC_RT.MASTER, read back from the catalog. The
-- retirement stub IS this body verbatim, so a retired profile is byte-for-byte
-- identical to MASTER: a session still pinned to P_<hash> re-runs the full
-- resolve -> dispatch -> swap for its NEXT statement, correctly transforming
-- that statement under the CURRENT rules and re-pointing itself via ALTER SESSION
-- to the correct current target. Sourcing the stub from the single deployed
-- MASTER (rather than a copy baked here) keeps ONE source of truth and makes
-- "stub == MASTER" true by construction; a change to the dispatch contract in
-- master.lua propagates to every stub created or refreshed afterwards. The
-- import("MASTER_CORE") wrapper was abandoned: dynamic import() works only at
-- top-level preprocessor scope, so the dispatch logic lives inline in MASTER and
-- the stub must carry that same inline body.
--
-- SCRIPT_TEXT is the full CREATE statement: its single first line is
-- `CREATE LUA SCRIPT "MASTER" () RETURNS ROWCOUNT AS`, and the script body
-- (which is what a CREATE OR REPLACE ... AS expects) is everything after the
-- first newline. The empty parameter list guarantees the header is one line, so
-- splitting on the first newline is a robust strip.
local MASTER_TEXT_SQL = [[
    SELECT SCRIPT_TEXT FROM EXA_ALL_SCRIPTS
    WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND SCRIPT_NAME = 'MASTER'
]]

-- master_stub_body() -> the MASTER script body to deploy as a retirement stub.
--   Reads PREPROC_RT.MASTER's catalog text and strips the CREATE header,
--   leaving the body. Raises if MASTER is absent or its text is unreadable, so a
--   refresh that cannot source a faithful stub fails loudly (uncommitted) rather
--   than installing a broken or empty stub.
--
--   The header ends with the keyword AS as the last token on its line; the body
--   starts on the next line. Anchoring the strip on that ` AS\n` (rather than the
--   first newline) tolerates a header that some Exasol version might render
--   across more than one line -- a first-newline strip would leave a dangling
--   `RETURNS ROWCOUNT AS` fragment and produce a syntactically broken stub. As a
--   final guard, the stripped body MUST NOT still begin with a CREATE/header
--   remnant, so a layout this anchor did not anticipate fails loudly here instead
--   of deploying a broken stub.
local function master_stub_body()
    local ok, res = pquery(MASTER_TEXT_SQL)
    if not ok then
        error("PREPROC.REFRESH_CORE: reading MASTER script text failed: "
            .. tostring(res.error_message))
    end
    if res[1] == nil or res[1][1] == null then
        error("PREPROC.REFRESH_CORE: PREPROC_RT.MASTER text not found; "
            .. "cannot source retirement stub body")
    end
    local master_text = tostring(res[1][1])
    -- Find the first ` AS` that is the last token on its line (the CREATE header
    -- terminator); the body is everything after that line's newline.
    local _, as_newline = string.find(master_text, "%sAS%s-\n")
    if as_newline == nil then
        error("PREPROC.REFRESH_CORE: MASTER script text has no 'AS' header terminator")
    end
    local body = string.sub(master_text, as_newline + 1)
    if string.find(body, "^%s*CREATE")
        or string.find(string.upper(body), "^%s*RETURNS") then
        error("PREPROC.REFRESH_CORE: stripped MASTER body still carries a CREATE header remnant")
    end
    return body
end

-- build_profile_body(rule_rows) -> Lua source for one P_<hash> script.
--   Serialises the chain's rule rows into the baked `rules` table and splices it
--   between the dispatch-template halves. Each row becomes a Lua table element
--   {phase=..,ordinal=..,script_name=..,function_name=..,client_pattern=..}.
--   string.format("%q") double-quotes and escapes every string field, so an odd
--   script/function name (operator-supplied) cannot break out of the literal; a
--   NULL client_pattern (Exasol `null`) is emitted as the Lua keyword nil so the
--   generated client_matches treats it as apply-always. The CLIENT section is
--   included only when at least one rule carries a client_pattern, so a chain
--   with no client patterns generates a profile that issues no per-statement
--   EXA_ALL_SESSIONS client query.
local function build_profile_body(rule_rows)
    local rule_parts = {}
    local has_client_pattern = false
    for _, r in ipairs(rule_rows) do
        local cp
        if r[5] == null then
            cp = "nil"
        else
            cp = string.format("%q", tostring(r[5]))
            has_client_pattern = true
        end
        table.insert(rule_parts, string.format(
            "{phase=%q,ordinal=%d,script_name=%q,function_name=%q,client_pattern=%s}",
            tostring(r[1]), tonumber(r[2]), tostring(r[3]), tostring(r[4]), cp
        ))
    end
    local client_section = PROFILE_BODY_NOCLIENT
    if has_client_pattern then
        client_section = PROFILE_BODY_CLIENT
    end
    return PROFILE_BODY_PREFIX
        .. table.concat(rule_parts, ",\n")
        .. "}\n"
        .. client_section
        .. PROFILE_BODY_DISPATCH
end

-- rebuild() -> rows_inserted
--   Performs the atomic resolution rebuild and returns the authoritative count
--   of rows materialised into PREPROC.RESOLUTION. Raises via error() on any
--   failure; never calls exit() (the caller owns result reporting). The step
--   ordering below IS the atomicity contract and the profile lifecycle contract
--   and must not be reordered:
--
--     fingerprint -> reclaim_cutoff -> DELETE/INSERT RESOLUTION -> hash UPDATE
--       -> GENERATE (real bodies + clear re-adopted orphaned_at)
--       -> PROFILES upsert -> RETIRE -> RECLAIM -> stamp SETTINGS -> COMMIT
--
--   Profile codegen (hash stamp, CREATE SCRIPT, PROFILES upsert) runs after the
--   RESOLUTION insert and before the stamp + COMMIT, so no committed RESOLUTION
--   row ever references a profile script that does not yet exist. RETIRE runs
--   after GENERATE + upsert so a re-adopted hash is already restored to its real
--   body and is excluded from retirement. RECLAIM runs last (before the stamp)
--   so it sees the final orphaned_at set, and only drops hashes orphaned on a
--   prior refresh (orphaned_at < reclaim_cutoff) -- see the module header for the
--   never-same-refresh-drop and login-time reclaim reasoning.
function rebuild()
    -- --- Step 1: capture the fingerprint of the current input state ----------
    -- Run before the rebuild so the stamped fingerprint describes exactly the
    -- dictionary state the resolution was built from. A failure here (e.g. a
    -- non-admin caller lacking SELECT ANY DICTIONARY) surfaces before any write.
    local fp_ok, fp_res = pquery(FINGERPRINT_SQL)
    if not fp_ok then
        error("PREPROC.REFRESH_CORE: fingerprint computation failed: " .. tostring(fp_res.error_message))
    end
    local fingerprint = fp_res[1][1]
    if fingerprint == null then
        fingerprint = ""
    end

    -- --- Step 1b: capture the reclaim cutoff (BEFORE any write or retire) -----
    -- The instant this refresh began. RECLAIM (Step 8) only drops hashes whose
    -- orphaned_at < this cutoff -- i.e. hashes retired on a PRIOR refresh. Any
    -- hash retired by THIS refresh's RETIRE phase is stamped with a later
    -- CURRENT_TIMESTAMP and is therefore excluded, making never-same-refresh-drop
    -- true by construction, independent of which sessions EXA_ALL_SESSIONS shows.
    local cutoff_ok, cutoff_res = pquery("SELECT CURRENT_TIMESTAMP AS c")
    if not cutoff_ok then
        error("PREPROC.REFRESH_CORE: capturing reclaim cutoff failed: " .. tostring(cutoff_res.error_message))
    end
    local reclaim_cutoff = cutoff_res[1][1]

    -- --- Step 2: atomic rebuild (single transaction, committed once) ---------
    local del_ok, del_res = pquery("DELETE FROM PREPROC.RESOLUTION")
    if not del_ok then
        error("PREPROC.REFRESH_CORE: clearing RESOLUTION failed: " .. tostring(del_res.error_message))
    end

    local ins_ok, ins_res = pquery(
        [[INSERT INTO PREPROC.RESOLUTION
            (user_name, client_pattern, phase, script_name, function_name, ordinal)
        ]] .. RESOLUTION_SQL
    )
    if not ins_ok then
        error("PREPROC.REFRESH_CORE: inserting resolved rows failed: " .. tostring(ins_res.error_message))
    end
    local row_count = ins_res.rows_inserted

    -- Count back the materialised rows as the reported outcome. rows_inserted is
    -- the authoritative count; the COUNT(*) is defence-in-depth should the driver
    -- not populate rows_inserted for an INSERT...SELECT.
    if row_count == nil then
        local cnt_ok, cnt_res = pquery("SELECT COUNT(*) AS c FROM PREPROC.RESOLUTION")
        if not cnt_ok then
            error("PREPROC.REFRESH_CORE: counting resolved rows failed: " .. tostring(cnt_res.error_message))
        end
        row_count = cnt_res[1][1]
    end

    -- --- Step 3: stamp each RESOLUTION row with its chain's profile_hash ------
    -- Runs after the INSERT and before any codegen so the freshly materialised
    -- rows carry the content-addressed hash that names their profile script.
    local hash_ok, hash_res = pquery(HASH_UPDATE_SQL)
    if not hash_ok then
        error("PREPROC.REFRESH_CORE: stamping profile hashes failed: " .. tostring(hash_res.error_message))
    end

    -- --- Step 4: gather the distinct (hash, manifest) profiles to generate ----
    local prof_ok, prof_res = pquery(PROFILE_ROWS_SQL)
    if not prof_ok then
        error("PREPROC.REFRESH_CORE: collecting profile chains failed: " .. tostring(prof_res.error_message))
    end

    -- --- Step 5: GENERATE the per-hash profile scripts ------------------------
    -- CREATE OR REPLACE is idempotent: an unchanged chain re-creates the same
    -- P_<hash> with the same body, so a re-refresh never drops a pinned script.
    -- The hash is HASH_MD5 hex from the dictionary (not user input), so it is a
    -- safe identifier suffix. The body bakes the chain's rules into a static Lua
    -- table dispatched by an inline mirror of MASTER, so a session pinned to
    -- P_<hash> runs no per-statement MY_PIPELINE lookup.
    for _, prof_row in ipairs(prof_res) do
        local hash = tostring(prof_row[1])

        -- Step 5a: read the chain's rules in dispatch (phase, ordinal) order.
        local rules_ok, rules_res = pquery(PROFILE_RULES_SQL, {hash = hash})
        if not rules_ok then
            error("PREPROC.REFRESH_CORE: reading rules for P_" .. hash
                .. " failed: " .. tostring(rules_res.error_message))
        end

        -- Step 5b/5c: serialise the rules into the baked table and splice it
        -- into the dispatch template to form the full script body.
        local script_body = build_profile_body(rules_res)

        local create_ok, create_res = pquery(
            "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_" .. hash .. " AS\n" .. script_body
        )
        if not create_ok then
            error("PREPROC.REFRESH_CORE: creating profile script P_" .. hash
                .. " failed: " .. tostring(create_res.error_message))
        end
    end

    -- --- Step 5d: clear orphaned_at for re-adopted hashes --------------------
    -- A hash that was retired on a prior refresh (orphaned_at non-NULL) but is
    -- referenced by a current RESOLUTION row again has just had its REAL baked
    -- body restored by the GENERATE loop above. It is no longer orphaned, so
    -- clear orphaned_at back to NULL. This is set-based over every re-adopted
    -- hash and runs BEFORE RETIRE, so RETIRE (which targets hashes NOT in the
    -- current RESOLUTION) cannot re-retire it, and RECLAIM cannot drop it.
    local readopt_ok, readopt_res = pquery([[
        UPDATE PREPROC.PROFILES SET orphaned_at = NULL
        WHERE orphaned_at IS NOT NULL
          AND profile_hash IN (
              SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION
              WHERE profile_hash IS NOT NULL
          )
    ]])
    if not readopt_ok then
        error("PREPROC.REFRESH_CORE: clearing re-adopted orphaned_at failed: "
            .. tostring(readopt_res.error_message))
    end

    -- --- Step 6: upsert PREPROC.PROFILES for each current chain ---------------
    -- MERGE inserts a row for a newly-seen hash and is a no-op for one already
    -- recorded (re-adopt clears orphaned_at in Group 6, not here).
    for _, prof_row in ipairs(prof_res) do
        local hash = tostring(prof_row[1])
        local manifest = prof_row[2]
        local merge_ok, merge_res = pquery(
            [[MERGE INTO PREPROC.PROFILES p
                USING (SELECT :hash AS profile_hash, :manifest AS manifest,
                              CURRENT_TIMESTAMP AS created_at) s
                ON p.profile_hash = s.profile_hash
                WHEN NOT MATCHED THEN
                    INSERT (profile_hash, manifest, created_at)
                    VALUES (s.profile_hash, s.manifest, s.created_at)]],
            {hash = hash, manifest = manifest}
        )
        if not merge_ok then
            error("PREPROC.REFRESH_CORE: upserting PROFILES for P_" .. hash
                .. " failed: " .. tostring(merge_res.error_message))
        end
    end

    -- --- Step 7: RETIRE profiles whose chains are gone -----------------------
    -- A PROFILES hash NOT referenced by any current RESOLUTION row, and not
    -- already retired (orphaned_at IS NULL), is orphaned by this refresh. Rather
    -- than drop it (a session may still be pinned to it), replace its body with
    -- the MASTER stub body (MASTER's own inline body, read from the catalog) and
    -- stamp orphaned_at = CURRENT_TIMESTAMP. The
    -- WHERE clause excludes already-retired hashes, so their original orphaned_at
    -- is never re-stamped. The NOT IN subquery filters profile_hash IS NOT NULL
    -- so a stray NULL cannot make NOT IN vacuously empty.
    local retire_ok, retire_res = pquery([[
        SELECT profile_hash FROM PREPROC.PROFILES
        WHERE orphaned_at IS NULL
          AND profile_hash NOT IN (
              SELECT DISTINCT profile_hash FROM PREPROC.RESOLUTION
              WHERE profile_hash IS NOT NULL
          )
    ]])
    if not retire_ok then
        error("PREPROC.REFRESH_CORE: selecting profiles to retire failed: "
            .. tostring(retire_res.error_message))
    end
    -- Source the stub body once (it is MASTER's body, identical for every stub)
    -- and only when there is at least one hash to retire, so a refresh that
    -- retires nothing never reads the catalog.
    local stub_body = nil
    if retire_res[1] ~= nil then
        stub_body = master_stub_body()
    end
    for _, retire_row in ipairs(retire_res) do
        local hash = tostring(retire_row[1])

        local stub_ok, stub_res = pquery(
            "CREATE OR REPLACE LUA SCRIPT PREPROC_RT.P_" .. hash .. " AS\n" .. stub_body
        )
        if not stub_ok then
            error("PREPROC.REFRESH_CORE: retiring profile script P_" .. hash
                .. " failed: " .. tostring(stub_res.error_message))
        end

        local stamp_ok, stamp_res = pquery(
            "UPDATE PREPROC.PROFILES SET orphaned_at = CURRENT_TIMESTAMP WHERE profile_hash = :hash",
            {hash = hash}
        )
        if not stamp_ok then
            error("PREPROC.REFRESH_CORE: stamping orphaned_at for P_" .. hash
                .. " failed: " .. tostring(stamp_res.error_message))
        end
    end

    -- --- Step 8: RECLAIM provably-unpinned retired profiles ------------------
    -- A retired hash is reclaimable when it was retired on a PRIOR refresh
    -- (orphaned_at < reclaim_cutoff -- excludes anything RETIRE just stamped,
    -- enforcing never-same-refresh-drop) AND no active session could still be
    -- pinned to it (no EXA_ALL_SESSIONS row logged in before it was retired). A
    -- pinned session must have logged in before the orphaning, so LOGIN_TIME <
    -- orphaned_at is exactly the could-be-pinned test; zero qualifying sessions
    -- makes the NOT EXISTS vacuously true => reclaimable.
    local reclaim_ok, reclaim_res = pquery([[
        SELECT p.profile_hash FROM PREPROC.PROFILES p
        WHERE p.orphaned_at IS NOT NULL
          AND p.orphaned_at < :cutoff
          AND NOT EXISTS (
              SELECT 1 FROM EXA_ALL_SESSIONS s
              WHERE s.LOGIN_TIME < p.orphaned_at
          )
    ]], {cutoff = reclaim_cutoff})
    if not reclaim_ok then
        error("PREPROC.REFRESH_CORE: selecting profiles to reclaim failed: "
            .. tostring(reclaim_res.error_message))
    end
    for _, reclaim_row in ipairs(reclaim_res) do
        local hash = tostring(reclaim_row[1])

        -- DROP the script first, then delete the PROFILES row ONLY once the
        -- script is confirmed gone. The PROFILES row is the sole record that a
        -- retired hash still needs reclaiming; deleting it before the DROP truly
        -- succeeded would strand the script forever (orphaned, untracked, never
        -- re-examined). A DROP failure because the script is already absent is
        -- the normal idempotent case and still reclaims; a DROP failure with the
        -- script still present (e.g. a transient lock) leaves the PROFILES row
        -- and its orphaned_at intact, so the hash is retried on the next refresh.
        -- Both the DROP and the delete target a hash absent from the current
        -- (already-inserted) RESOLUTION, so no committed reader can name a
        -- reclaimed hash regardless of the DROP's implicit commit.
        pquery("DROP SCRIPT PREPROC_RT.P_" .. hash)

        local still_ok, still_res = pquery(
            [[SELECT COUNT(*) AS c FROM EXA_ALL_SCRIPTS
              WHERE SCRIPT_SCHEMA = 'PREPROC_RT' AND UPPER(SCRIPT_NAME) = UPPER(:n)]],
            {n = "P_" .. hash}
        )
        if not still_ok then
            error("PREPROC.REFRESH_CORE: verifying drop of P_" .. hash
                .. " failed: " .. tostring(still_res.error_message))
        end
        if still_res[1][1] == 0 then
            local del_prof_ok, del_prof_res = pquery(
                "DELETE FROM PREPROC.PROFILES WHERE profile_hash = :hash",
                {hash = hash}
            )
            if not del_prof_ok then
                error("PREPROC.REFRESH_CORE: deleting PROFILES row for P_" .. hash
                    .. " failed: " .. tostring(del_prof_res.error_message))
            end
        end
    end

    -- --- Step 9: stamp SETTINGS, then the single COMMIT ----------------------
    local stamp_ok, stamp_res = pquery(
        "UPDATE PREPROC.SETTINGS SET last_refresh = CURRENT_TIMESTAMP, state_fingerprint = :fp",
        {fp = fingerprint}
    )
    if not stamp_ok then
        error("PREPROC.REFRESH_CORE: stamping SETTINGS failed: " .. tostring(stamp_res.error_message))
    end

    -- Exasol Lua scripts have no commit() global; COMMIT is issued as SQL.
    local commit_ok, commit_res = pquery("COMMIT")
    if not commit_ok then
        error("PREPROC.REFRESH_CORE: commit failed: " .. tostring(commit_res.error_message))
    end

    return row_count
end
