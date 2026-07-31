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
--   operator grants it — ACCESS on the BucketFS CONNECTION: the credential
--   grant that makes Exasol mount the read-protected bucket, not a name the
--   source carries. Mount visibility follows the CALLING session, not the
--   script owner, so this INVOKER-rights entry point only ever sees what the
--   caller was granted. This is exactly the boundary REFRESH_CORE already
--   uses to CREATE the PREPROC_RT.P_<hash> profile scripts at refresh time.
--   The resolver UDF is admin-only (never PUBLIC), so a non-admin cannot
--   reach BucketFS / the network through it, and a non-admin caller fails at
--   the first privileged access.
--
-- Parameters (declared in the CREATE header added by install.py / bundle.py; bare
-- names, no SQL types)
--   source       VARCHAR  a `bucketfs:<service>/<bucket>/<path>` location or
--                         an `https:` source (see the resolver)
--   module_name  VARCHAR  the module to install
--   scope        VARCHAR  REQUIRED `TYPE:value`, TYPE in {ROLE, USER}; targets
--                         RULE ACTIVATION (who receives the transform), NOT
--                         install permission
--
-- Sequence (mirrors deploy.deploy_module exactly, in a fixed order that keeps the
-- CLI's "leave the DB consistent on failure" invariant)
--   1. Drive the resolver (SELECT PREPROC.MODULE_RESOLVER_V1(source, module_name),
--      read once, NO FROM). ANY emitted row with status = 'ERROR' aborts HERE via
--      error() — BEFORE any DB write. A sha256 mismatch, an artifact whose object
--      inventory disagrees with the index entry's declaration, module-not-found,
--      unreadable source, or unreachable-HTTPS-host / network error therefore
--      never leaves a half-installed database (parity with
--      deploy.fetch_and_verify_artifact).
--   2. PRE-FLIGHT, for a library-deployed module (chunk_count >= 1): reassemble
--      artifact_chunk in chunk_index order, encode the resolved inventory and
--      confirm it fits the provenance column, split the artifact into its 1..N
--      statements, and pair every statement with the inventory object it creates
--      — all of it BEFORE the first DDL. A split the grammar rejects, an
--      inventory wider than PREPROC.MODULES.objects (which step 5 could not then
--      write, leaving the module live with no provenance row), or a statement list
--      that does not line up with the resolved inventory in either direction,
--      aborts HERE. This extends step 1's abort-before-write gate and is the
--      primary all-or-nothing mechanism (ADR-059). A self-deployed module
--      (chunk_count == 0) has no artifact and skips steps 2 and 3: its own
--      installer deployed the backing script.
--   3. OPEN SCHEMA PREPROC_RT once, then execute the pre-flighted statements in
--      FILE order — the version is now present. Every object name is fully
--      qualified, so the open schema is compilation context only and a statement
--      may create its object in any schema the caller can write (ADR-057).
--   4. EXECUTE SCRIPT PREPROC.RULE_ENSURE(scope_type, scope_value, phase,
--      script_name, function_name, NULL, TRUE) — idempotent rule registration with
--      the auto-refresh suppressed; RULE_ENSURE validates the script exists first.
--   5. EXECUTE SCRIPT PREPROC.MODULE_REGISTER(name, version, source_repo,
--      source_ref, sha256, script_name, function_name, phase, deploy_mode,
--      source_url, library_version, objects) — idempotent provenance upsert.
--      sha256 is NULL for a self-deployed module; source_url is the effective
--      source and library_version the resolved release (the `latest` audit
--      anchor); objects is the deployed inventory, which is what lets reclaim
--      drop every object of the version later.
--   6. A single EXECUTE SCRIPT PREPROC.REFRESH when the rule was freshly created
--      (parity with deploy_module's `if do_refresh and rule_created`): RESOLUTION
--      depends only on CONFIG, so an idempotent re-install that changed no rule
--      needs no rebuild.
--
-- Atomicity is CONSTRUCTED, not inherited (ADR-059)
--   Exasol DDL implicitly commits and cannot be rolled back — refresh_core.lua:111,
--   117,742 and module_reclaim.lua:68 all document this and work around it — so a
--   transaction around a composite artifact would be a false guarantee. The CLI's
--   deploy.deploy_artifact builds all-or-nothing from the same four mechanisms, so
--   both paths reach a byte-identical end state:
--     * ABORT-BEFORE-WRITE PRE-FLIGHT (steps 1 and 2), so the overwhelming
--       majority of failures never reach a write at all.
--     * COMPENSATING CLEANUP: on a failure at statement k the objects created by
--       statements 1..k-1 are dropped in REVERSE creation order (a companion may
--       depend on an earlier object, so backwards is the only order that can
--       succeed). Every drop is attempted even after one fails and none is
--       re-raised: this compensates an error that already happened and MUST NOT
--       replace it. A SCHEMA is dropped RESTRICT, never CASCADE — best-effort
--       compensation must never destroy data, so a non-empty schema survives as a
--       reported cleanup failure.
--     * IDEMPOTENT RE-RUN: every artifact statement is a CREATE OR REPLACE, so
--       re-running a part-way failed install converges rather than colliding —
--       including when compensating cleanup could not drop everything it created.
--     * RULE REGISTRATION LAST: the rule is DML and rollback-able, so a module
--       whose statements deployed but whose rule was never registered is INERT —
--       the grammar never activates — rather than half-active.
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
local COL_OBJECTS = 19

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

-- Reassemble the artifact text from the resolver's ordered chunks, VERBATIM.
-- Nothing is stripped here: the artifact is a statement sequence, and only the
-- splitter below knows which lone `/` lines are block terminators. Removing them
-- up front would leave every `--/` block unterminated.
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
    return table.concat(ordered)
end

-- --- The shared artifact statement grammar (ADR-058) --------------------------
-- The Lua half of the module-artifact statement splitter. The CANONICAL half is
-- src/preproc/module/artifact.py `split_statements` (embedded verbatim into the
-- resolver UDF, so the two Python sites cannot diverge); this is the only OTHER
-- implementation, and both are pinned to the same conformance corpus
-- (tests/fixtures/artifact_split_corpus.json) because the CLI path and this
-- in-database path MUST reach a byte-identical end state.
--
-- The grammar, exactly as the canonical half defines it:
--   * `\r\n` and a lone `\r` normalise to `\n` BEFORE anything else.
--   * A marker line is a COMPLETE physical line whose content, ignoring
--     surrounding whitespace, is exactly `--/` (block open) or `/` (block close).
--     `SELECT 1; --/` is a statement plus a comment, not a marker; after a
--     mid-line `;` the remainder is no longer a fresh line and gets no marker
--     check.
--   * A block body is NEVER scanned. Only a lone `/` line closes it, whatever the
--     body contains, which is what preserves a body whose own syntax needs a
--     trailing `;` (CREATE FUNCTION ... END;).
--   * Outside a block the statement ends at the first `;` falling outside
--     `'...'` (where `''` is the escape), `"..."`, `--` and `/* */`. The `;` is
--     dropped, and several statements may share one line.
--   * Outside a block a lone `/` line ends a pending statement, and is discarded
--     when nothing is pending — which is what keeps a pre-composite,
--     single-statement artifact valid unchanged.
--   * A `--/` opening while a statement is still pending, and a block the
--     artifact never closes, are both errors.
--
-- tests/_lua_splitter.py extracts everything between the two sentinels below
-- VERBATIM to drive the conformance corpus, so the tested source and the
-- deployed source are the same bytes.
-- BEGIN ARTIFACT SPLITTER
local OPEN_MARKER_PATTERN = "^%s*%-%-/%s*$"
local CLOSE_MARKER_PATTERN = "^%s*/%s*$"

-- The only characters that can end a statement or change the scan state: the
-- terminator, the two quote openers, and the first character of `--` and `/*`.
-- Scanning jumps between them instead of walking every character, so a 2,000,000
-- character artifact costs one C-level search per significant character.
local SIGNIFICANT_CHARS = "[%-/'\";]"
local NON_WHITESPACE = "[^%s]"
local LAST_NON_WHITESPACE = "[^%s]%s*$"

local SCAN_CLEAN = 0
local SCAN_SINGLE_QUOTE = 1
local SCAN_DOUBLE_QUOTE = 2
local SCAN_BLOCK_COMMENT = 3

local function normalize_newlines(text)
    local normalized = string.gsub(text, "\r\n", "\n")
    normalized = string.gsub(normalized, "\r", "\n")
    return normalized
end

-- Cut text into its physical lines, keeping a trailing empty line so the line
-- sequence matches the canonical implementation's split on "\n" exactly.
local function split_lines(text)
    local lines = {}
    local start = 1
    while true do
        local newline = string.find(text, "\n", start, true)
        if newline == nil then
            lines[#lines + 1] = string.sub(text, start)
            return lines
        end
        lines[#lines + 1] = string.sub(text, start, newline - 1)
        start = newline + 1
    end
end

-- Trim the whitespace `%s` matches — space, \t, \n, \v, \f, \r — which is the
-- explicit character set the canonical implementation strips with.
local function strip_whitespace(text)
    local first = string.find(text, NON_WHITESPACE)
    if first == nil then
        return ""
    end
    local last = string.find(text, LAST_NON_WHITESPACE)
    return string.sub(text, first, last)
end

local function is_marker_line(line, marker_pattern)
    return string.find(line, marker_pattern) ~= nil
end

local function collect_statement(statements, pending)
    local statement = strip_whitespace(table.concat(pending, "\n"))
    if statement ~= "" then
        statements[#statements + 1] = statement
    end
end

-- Scan `line` from `start` for the `;` that ends a statement, returning
-- (state, has_code, cut). `state` carries string and block-comment context into
-- the next line. `has_code` reports whether the scanned span contributed any
-- non-whitespace outside a comment, which is what tells an unterminated
-- statement from inter-statement filler. `cut` is the position of the terminating
-- `;`, or nil when the line does not end the statement.
local function scan_line(line, state, start)
    local index = start
    local length = #line
    local has_code = false
    while index <= length do
        if state == SCAN_SINGLE_QUOTE then
            local close = string.find(line, "'", index, true)
            if close == nil then
                return state, has_code, nil
            end
            state = SCAN_CLEAN
            index = close + 1
        elseif state == SCAN_DOUBLE_QUOTE then
            local close = string.find(line, '"', index, true)
            if close == nil then
                return state, has_code, nil
            end
            state = SCAN_CLEAN
            index = close + 1
        elseif state == SCAN_BLOCK_COMMENT then
            local close = string.find(line, "*/", index, true)
            if close == nil then
                return state, has_code, nil
            end
            state = SCAN_CLEAN
            index = close + 2
        else
            local at = string.find(line, SIGNIFICANT_CHARS, index)
            if not has_code then
                -- Every significant character is itself non-whitespace, so this
                -- search never runs past `at`: it only reports whether the span
                -- skipped over on the way there carried any code.
                local code_at = string.find(line, NON_WHITESPACE, index)
                has_code = code_at ~= nil and (at == nil or code_at < at)
            end
            if at == nil then
                return state, has_code, nil
            end
            local char = string.sub(line, at, at)
            local pair = string.sub(line, at, at + 1)
            if char == ";" then
                return SCAN_CLEAN, has_code, at
            elseif pair == "--" then
                return state, has_code, nil
            elseif pair == "/*" then
                state = SCAN_BLOCK_COMMENT
                index = at + 2
            else
                has_code = true
                if char == "'" then
                    state = SCAN_SINGLE_QUOTE
                elseif char == '"' then
                    state = SCAN_DOUBLE_QUOTE
                end
                index = at + 1
            end
        end
    end
    return state, has_code, nil
end

-- Cut an artifact into its statements, in file order. Returns the statement
-- list, in which every statement is stripped of surrounding whitespace and
-- carries neither its `;` terminator nor any marker line; a statement stripping
-- to empty is not yielded. Returns nil plus a message when the block structure
-- is invalid, so a caller pre-flighting an install aborts before its first write:
--     local statements, split_error = split_statements(artifact_text)
--     if statements == nil then error(split_error) end
local function split_statements(artifact_text)
    local statements = {}
    local pending = {}
    local pending_has_code = false
    local state = SCAN_CLEAN
    local in_block = false

    for _, line in ipairs(split_lines(normalize_newlines(artifact_text))) do
        if in_block then
            if is_marker_line(line, CLOSE_MARKER_PATTERN) then
                in_block = false
                collect_statement(statements, pending)
                pending = {}
            else
                pending[#pending + 1] = line
            end
        elseif state == SCAN_CLEAN and is_marker_line(line, OPEN_MARKER_PATTERN) then
            if pending_has_code then
                return nil, "PREPROC.MODULE_INSTALL: a '--/' block opened while the preceding "
                    .. "statement is still unterminated; terminate it with ';' or wrap it in "
                    .. "'--/' ... '/'"
            end
            pending = {}
            in_block = true
        elseif state == SCAN_CLEAN and is_marker_line(line, CLOSE_MARKER_PATTERN) then
            if pending_has_code then
                collect_statement(statements, pending)
            end
            pending = {}
            pending_has_code = false
        else
            local start = 1
            while true do
                local has_code, cut
                state, has_code, cut = scan_line(line, state, start)
                if has_code then
                    pending_has_code = true
                end
                if cut == nil then
                    pending[#pending + 1] = string.sub(line, start)
                    break
                end
                pending[#pending + 1] = string.sub(line, start, cut - 1)
                collect_statement(statements, pending)
                pending = {}
                pending_has_code = false
                start = cut + 1
            end
        end
    end

    if in_block then
        return nil, "PREPROC.MODULE_INSTALL: unterminated '--/' block: the artifact ends with "
            .. "no closing lone '/' line"
    end
    if pending_has_code then
        collect_statement(statements, pending)
    end
    return statements
end
-- END ARTIFACT SPLITTER

-- --- The object inventory: wire form, pre-flight, and the undo list -----------
-- The resolver derives the artifact's object inventory in FILE order and verifies
-- it against the index entry's declaration in both directions (ADR-055), so this
-- script never re-derives it: it decodes what the resolver emitted, lines it up
-- with the split statements, and uses it as the compensating-cleanup undo list.

local OBJECT_DELIMITER = "|"
local OBJECT_LINE_PATTERN = "^(.-)|(.*)$"

-- The width of PREPROC.MODULES.objects (sql/install/22_modules_table.sql), of
-- MODULE_REGISTER's CAST, and of the resolver's own `objects` column. A PINNED
-- copy of preproc.module.registry.OBJECTS_MAX_CHARS, which this script cannot
-- import; tests/test_module_registry.py asserts every pinned copy still equals
-- the canonical constant, because each one gates a refusal and the narrowest
-- copy would silently become the real limit. Nothing in the module contract caps
-- the object count, so a large subsystem module reaches an encoding no column can
-- hold, and the provenance row is written LAST -- after
-- every CREATE and after RULE_ENSURE. Discovering the width at MODULE_REGISTER
-- would therefore leave the module live and DISPATCHING with no provenance row:
-- neither retirable nor reclaimable, the exact inverse of the "a partial install
-- is inert" guarantee. So it is a pre-flight check, like the split and the
-- inventory pairing. The resolver checks the same limit before it emits (its own
-- column is this wide too), which is where an over-wide inventory is normally
-- caught; this second check is what stands between a truncating transport and an
-- install whose recorded undo list silently lost its tail.
local OBJECTS_MAX_CHARS = 200000

-- Bytes that continue a UTF-8 character rather than starting one; `#` counts
-- BYTES and Lua 5.1 has no utf8 library, but an Exasol VARCHAR(n) counts
-- CHARACTERS.
local UTF8_CONTINUATION = "[\128-\191]"

-- Types Exasol names with a single unqualified identifier, so cleanup must not
-- split them on a dot (parity with artifact.UNQUALIFIED_TYPES).
local UNQUALIFIED_TYPES = {SCHEMA = true, ["VIRTUAL SCHEMA"] = true, CONNECTION = true}

-- Every script language and mode ends in SCRIPT ('LUA SCRIPT', 'PYTHON3 SCALAR
-- SCRIPT', ...) and they all drop with the one verb, so the suffix classifies
-- them without enumerating an installation-defined language list.
local SCRIPT_TYPE_SUFFIX = "SCRIPT"
local SCRIPT_DROP = "DROP SCRIPT IF EXISTS"

-- A SCHEMA is dropped RESTRICT (Exasol's default): compensating cleanup is
-- best-effort and must never destroy data, and a `CREATE SCHEMA IF NOT EXISTS`
-- may have found the schema already there. A non-empty schema therefore survives
-- as a reported cleanup failure. A VIRTUAL SCHEMA is the deliberate exception —
-- it always holds virtual tables, so RESTRICT could never drop one, and CASCADE
-- removes only the local metadata, never anything at the remote source.
local DROP_VERBS = {
    TABLE = "DROP TABLE IF EXISTS",
    VIEW = "DROP VIEW IF EXISTS",
    FUNCTION = "DROP FUNCTION IF EXISTS",
    CONNECTION = "DROP CONNECTION IF EXISTS",
    SCHEMA = "DROP SCHEMA IF EXISTS",
    ["VIRTUAL SCHEMA"] = "DROP VIRTUAL SCHEMA IF EXISTS",
}
local CASCADE_TYPES = {["VIRTUAL SCHEMA"] = true}

-- The characters the database's single-statement API takes no trailing run of.
-- pyexasol's own _format_query ends in rstrip(" \n;"), and the CLI path relies on
-- it, so the in-database path applies the identical normalisation before pquery
-- (which does NOT) or the two paths would send different text for a block-wrapped
-- body that legitimately carries its own trailing `;`.
local WIRE_TRAILING = "[^ \n;][ \n;]*$"

local CREATE_KEYWORD = "CREATE"
local WORD_CHAR = "[%w_]"
-- The `%s` class matches exactly the characters the canonical derivation strips
-- with (space, \t, \n, \v, \f, \r). Declared here rather than reused from the
-- pinned splitter block above, which stays self-contained.
local CODE_CHAR = "[^%s]"

-- Decode the resolver's `objects` column into an ordered {type, name} list. The
-- wire form is newline-separated `TYPE|NAME` lines with no trailing newline, split
-- on the FIRST `|` — registry.encode_objects's format exactly. Delimiter safety
-- comes from validation, not escaping: manifest.py rejects `\n`, `\r`, or `|` in a
-- declared type or name, so no value here can carry one. Signals a malformed
-- encoding as (nil, message) so a caller aborts before its first write rather
-- than installing with an undo list it cannot trust.
local function decode_objects(raw)
    if is_null(raw) then
        return {}
    end
    local objects = {}
    for line in string.gmatch(tostring(raw) .. "\n", "([^\n]*)\n") do
        if line ~= "" then
            local obj_type, obj_name = string.match(line, OBJECT_LINE_PATTERN)
            if obj_type == nil then
                return nil, "PREPROC.MODULE_INSTALL: malformed object inventory from the resolver: "
                    .. "line " .. sql_str(line) .. " carries no "
                    .. sql_str(OBJECT_DELIMITER) .. " delimiter"
            end
            objects[#objects + 1] = {type = obj_type, name = obj_name}
        end
    end
    return objects
end

-- Re-encode an inventory into the same wire form, so the provenance row this
-- script writes is byte-identical to registry.encode_objects's output whatever
-- formatting slack the transport allowed.
local function encode_objects(objects)
    if #objects == 0 then
        return nil
    end
    local lines = {}
    for _, obj in ipairs(objects) do
        lines[#lines + 1] = obj.type .. OBJECT_DELIMITER .. obj.name
    end
    return table.concat(lines, "\n")
end

-- The CHARACTER length of a UTF-8 string, which is what the VARCHAR the encoded
-- inventory is bound for counts. Discounting continuation bytes rather than
-- taking `#` is what keeps this check in step with the resolver's and the CLI's,
-- both of which count characters: a quoted object identifier may legitimately
-- hold non-ASCII (manifest.py rejects only '\n', '\r', and '|'), and counting
-- bytes would refuse an install those two paths accept.
local function char_length(text)
    local _, continuations = string.gsub(text, UTF8_CONTINUATION, "")
    return #text - continuations
end

local function object_label(obj)
    return obj.type .. " " .. obj.name
end

local function quote_ident(part)
    return '"' .. string.gsub(part, '"', '""') .. '"'
end

-- Quote an inventory name back into a SQL identifier. The inventory name is
-- CANONICAL, not quoted — an unquoted identifier folded to upper case, a quoted
-- one kept verbatim with its quotes stripped — so quoting every part on the way
-- out is what makes it safe whatever it holds (a reserved word, a lower-case
-- letter, punctuation), and it is lossless. Only the schema separator is
-- re-split, on the FIRST dot only — safe because manifest.py's
-- `_validate_objects` rejects a dot inside a quoted SCHEMA part at parse time;
-- a dot in the OBJECT part (the tail this split absorbs) is unaffected and
-- survives.
local function quote_qualified(obj)
    local dot = nil
    if not UNQUALIFIED_TYPES[obj.type] then
        dot = string.find(obj.name, ".", 1, true)
    end
    if dot == nil then
        return quote_ident(obj.name)
    end
    return quote_ident(string.sub(obj.name, 1, dot - 1)) .. "."
        .. quote_ident(string.sub(obj.name, dot + 1))
end

-- The DROP ... IF EXISTS that removes one inventory object, or nil for a type
-- with no known DROP form. IF EXISTS keeps the drop idempotent, which matters
-- because cleanup re-runs over an inventory that may be partly gone already.
local function drop_sql(obj)
    local verb
    if string.sub(obj.type, -#SCRIPT_TYPE_SUFFIX) == SCRIPT_TYPE_SUFFIX then
        verb = SCRIPT_DROP
    else
        verb = DROP_VERBS[obj.type]
    end
    if verb == nil then
        return nil
    end
    return verb .. " " .. quote_qualified(obj) .. (CASCADE_TYPES[obj.type] and " CASCADE" or "")
end

-- Strip the trailing terminator run pyexasol strips, so both paths send identical
-- text. The last significant character is located directly rather than by
-- repeatedly trimming the tail, which keeps a 2,000,000-character body linear.
local function wire_form(statement)
    local last = string.find(statement, WIRE_TRAILING)
    if last == nil then
        return ""
    end
    return string.sub(statement, 1, last)
end

-- Where a statement's first real token starts, past any leading `--` or `/* */`
-- comment (parity with the canonical derivation's _strip_leading_comments), or
-- nil for a statement that is only comment and whitespace. The scan carries an
-- index instead of re-slicing so a long comment header cannot make it quadratic.
local function code_start(statement)
    local index = 1
    while true do
        local first = string.find(statement, CODE_CHAR, index)
        if first == nil then
            return nil
        end
        index = first
        local pair = string.sub(statement, index, index + 1)
        if pair == "--" then
            local newline = string.find(statement, "\n", index, true)
            if newline == nil then
                return nil
            end
            index = newline + 1
        elseif pair == "/*" then
            local close = string.find(statement, "*/", index + 2, true)
            if close == nil then
                return nil
            end
            index = close + 2
        else
            return index
        end
    end
end

-- Whether a statement creates an object: whether its first real token is the
-- CREATE keyword. The canonical derivation admits every CREATE form the module
-- contract allows and REJECTS every other one, and the resolver already ran it
-- over this very artifact, so a leading CREATE marks exactly the statements that
-- contributed an inventory entry — INSERT, GRANT and COMMIT contribute none and
-- so are correctly left out of the compensating-cleanup undo list.
local function creates_object(statement)
    local start = code_start(statement)
    if start == nil then
        return false
    end
    local head_word = string.sub(statement, start, start + #CREATE_KEYWORD - 1)
    if string.upper(head_word) ~= CREATE_KEYWORD then
        return false
    end
    local after = string.sub(statement, start + #CREATE_KEYWORD, start + #CREATE_KEYWORD)
    return after == "" or string.find(after, WORD_CHAR) == nil
end

-- Pair every statement with the inventory object it creates, in file order, and
-- return the executable plan. This is the pre-flight gate for the artifact body:
-- holding a plan is the proof that the statement list and the resolved inventory
-- line up in BOTH directions, which is also what would catch a Python-vs-Lua
-- splitter divergence — before the first DDL rather than mid-install. Signals a
-- disagreement as (nil, message) exactly as the splitter does.
local function plan_statements(statements, objects)
    local plan = {}
    local next_object = 1
    for index, statement in ipairs(statements) do
        local creates = nil
        if creates_object(statement) then
            creates = objects[next_object]
            if creates == nil then
                return nil, "PREPROC.MODULE_INSTALL: artifact statement " .. tostring(index)
                    .. " creates an object but the resolved inventory declares only "
                    .. tostring(#objects) .. " object(s)"
            end
            next_object = next_object + 1
        end
        plan[index] = {sql = wire_form(statement), creates = creates}
    end
    if next_object <= #objects then
        return nil, "PREPROC.MODULE_INSTALL: the resolved inventory declares "
            .. sql_str(object_label(objects[next_object]))
            .. " but no artifact statement creates it"
    end
    return plan
end

-- Best-effort drop of the objects created so far, in REVERSE creation order —
-- a companion may depend on an earlier object, so backwards is the only order
-- that can succeed. Every drop runs in its own pcall and every one is attempted
-- even after an earlier failure; nothing is re-raised, because this compensates
-- an error that has already happened and must never replace it. Returns the
-- labels dropped and the labels that could not be, both in attempt order.
local function drop_created(created)
    local dropped = {}
    local failures = {}
    for index = #created, 1, -1 do
        local obj = created[index]
        local statement = drop_sql(obj)
        if statement == nil then
            failures[#failures + 1] = object_label(obj)
                .. " (no DROP form is known for object type " .. sql_str(obj.type) .. ")"
        else
            local protected, drop_ok, drop_res = pcall(pquery, statement)
            if protected and drop_ok then
                dropped[#dropped + 1] = object_label(obj)
            elseif protected then
                failures[#failures + 1] = object_label(obj)
                    .. " (" .. tostring(drop_res.error_message) .. ")"
            else
                failures[#failures + 1] = object_label(obj) .. " (" .. tostring(drop_ok) .. ")"
            end
        end
    end
    return dropped, failures
end

-- Compose the operator-facing failure message: the real error FIRST, the cleanup
-- outcome after, mirroring deploy._deploy_error_message so both paths report a
-- mid-artifact failure the same way.
local function artifact_error(index, total, creates, cause, dropped, failures)
    local target = creates and (" (" .. object_label(creates) .. ")") or ""
    local parts = {
        "PREPROC.MODULE_INSTALL: module artifact statement " .. tostring(index) .. " of "
            .. tostring(total) .. " failed" .. target .. ": " .. tostring(cause),
    }
    if #dropped > 0 then
        parts[#parts + 1] = "compensating cleanup dropped " .. tostring(#dropped)
            .. " object(s) in reverse: " .. table.concat(dropped, ", ")
    end
    if #failures > 0 then
        parts[#parts + 1] = "compensating cleanup could NOT drop "
            .. table.concat(failures, "; ")
            .. " — the module is partially present but inert (no rule was registered) and "
            .. "re-running the install converges"
    end
    return table.concat(parts, ". ")
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

-- --- Step 2: pre-flight the artifact — the last point before any DDL ----------
-- self-deployed (chunk_count == 0) has no artifact and skips this entirely
-- (parity with deploy.py), leaving objects NULL in the provenance row.
local plan, encoded_objects
if chunk_count >= 1 then
    local objects, decode_error, plan_error
    objects, decode_error = decode_objects(head[COL_OBJECTS])
    if objects == nil then
        error(decode_error)
    end
    -- Encoded HERE, not at step 5, so the width of the row step 5 will write is
    -- settled while nothing has been written yet.
    encoded_objects = encode_objects(objects)
    if encoded_objects ~= nil then
        local encoded_length = char_length(encoded_objects)
        if encoded_length > OBJECTS_MAX_CHARS then
            error("PREPROC.MODULE_INSTALL: the resolved object inventory for module "
                .. sql_str(module_name) .. " encodes to " .. tostring(encoded_length)
                .. " characters, over the " .. tostring(OBJECTS_MAX_CHARS)
                .. "-character limit of PREPROC.MODULES.objects; the provenance row could "
                .. "not be written, and a module live with no provenance row can be neither "
                .. "retired nor reclaimed, so the install is refused before any DDL -- split "
                .. "the module into smaller modules or shorten its object names")
        end
    end
    local statements, split_error = split_statements(
        reassemble_artifact(res, chunk_count, module_name))
    if statements == nil then
        error(split_error)
    end
    plan, plan_error = plan_statements(statements, objects)
    if plan == nil then
        error(plan_error)
    end
end

-- --- Step 3: execute the pre-flighted statements in FILE order ----------------
-- Exasol compiles a script against an active schema, so PREPROC_RT is opened once
-- before the first statement; every object name is fully qualified, so the open
-- schema is compilation context only. Statements carry NO query parameters — a Lua
-- or Python body is full of braces and colons that a parameterised send would try
-- to substitute.
if plan ~= nil then
    local open_ok, open_res = pquery("OPEN SCHEMA PREPROC_RT")
    if not open_ok then
        error("PREPROC.MODULE_INSTALL: OPEN SCHEMA PREPROC_RT failed: "
            .. tostring(open_res.error_message))
    end
    local created = {}
    for index, statement in ipairs(plan) do
        local stmt_ok, stmt_res = pquery(statement.sql)
        if not stmt_ok then
            local dropped, failures = drop_created(created)
            error(artifact_error(index, #plan, statement.creates,
                stmt_res.error_message, dropped, failures))
        end
        if statement.creates ~= nil then
            created[#created + 1] = statement.creates
        end
    end
end

-- --- Step 4: idempotently register the activation rule (refresh suppressed) ---
-- LAST of the write steps that matter: the rule is DML, so until it exists the
-- module is inert whatever the artifact left behind.
local rule_ok, rule_res = pquery("EXECUTE SCRIPT PREPROC.RULE_ENSURE("
    .. sql_str(scope_type) .. ", " .. sql_str(scope_value) .. ", "
    .. sql_str(phase) .. ", " .. sql_str(script_name) .. ", "
    .. sql_str(function_name) .. ", NULL, TRUE)")
if not rule_ok then
    error("PREPROC.MODULE_INSTALL: RULE_ENSURE failed: " .. tostring(rule_res.error_message))
end
local rule_created = (rule_res[1] ~= nil and rule_res[1][2] == true)

-- --- Step 5: idempotently upsert the provenance row ---------------------------
-- `source` is the effective source the operator/default resolved from (its
-- audit value: reveals whether a mutable `latest` tag, a pinned vX.Y.Z, or a
-- bucketfs: location was used); library_version is the concrete release the
-- index self-declared, so a `latest` install is auditable back to a fixed
-- version. The column records the source exactly as given, with no re-parse
-- and no migration of older rows: one written before the location grammar
-- still reads bucketfs:<CONNECTION>/<path>, which is no longer resolvable and
-- is kept only as the historical record. The inventory is recorded LAST of
-- the arguments because the database never retains the artifact, so this
-- column is the only thing reclaim can later drop from.
local reg_ok, reg_res = pquery("EXECUTE SCRIPT PREPROC.MODULE_REGISTER("
    .. sql_str(name) .. ", " .. string.format("%d", version) .. ", "
    .. sql_nullable(source_repo) .. ", " .. sql_nullable(source_ref) .. ", "
    .. sql_nullable(sha256) .. ", " .. sql_str(script_name) .. ", "
    .. sql_str(function_name) .. ", " .. sql_str(phase) .. ", "
    .. sql_str(deploy_mode) .. ", " .. sql_nullable(source) .. ", "
    .. sql_nullable(library_version) .. ", " .. sql_nullable(encoded_objects) .. ")")
if not reg_ok then
    error("PREPROC.MODULE_INSTALL: MODULE_REGISTER failed: " .. tostring(reg_res.error_message))
end
local provenance_created = (reg_res[1] ~= nil and reg_res[1][4] == true)

-- --- Step 6: the single refresh (only when a rule was freshly created) --------
if rule_created then
    local refresh_ok, refresh_res = pquery("EXECUTE SCRIPT PREPROC.REFRESH")
    if not refresh_ok then
        error("PREPROC.MODULE_INSTALL: REFRESH failed: " .. tostring(refresh_res.error_message))
    end
end

-- --- Step 7: report the outcome -----------------------------------------------
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
