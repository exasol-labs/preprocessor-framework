-- =============================================================================
-- EXAMPLE preprocessor add-on: confd-admin  (CONTROL DATABASE / CONTROL BUCKETFS)
-- =============================================================================
-- Adds a generic ConfD gateway UDF and the `CONTROL DATABASE …` / `CONTROL
-- BUCKETFS …` SQL command surfaces to an installed preprocessor-framework. It
-- deploys five objects and self-registers two DBA-scoped EXPAND rules, then
-- refreshes — so the commands work as soon as the file finishes (for DBA-role
-- holders). Origin: the companion exa-confd-admin project; vendored here as a
-- complete, runnable example of a real-world command preprocessor.
--
-- Deploys: PREPROC_RT.CONFD_JOB_V1 (Python3 ConfD gateway UDF),
--   PREPROC_RT.CONTROL_DATABASE_V1 + PREPROC_RT.CONTROL_BUCKETFS_V1 (EXPAND
--   grammar modules), PREPROC.ADMIN_CONFD_JOB (the EXECUTE SCRIPT target), and
--   two DBA-scoped EXPAND rules in PREPROC.CONFIG.
--
-- Prerequisites:
--   1. The preprocessor-framework is installed (run release.sql — PREPROC /
--      PREPROC_RT schemas exist, MASTER deployed).
--   2. A ConfD user exists and a CONNECTION object named CONFD_CONNECTION points
--      at the ConfD XML-RPC endpoint:
--        CREATE CONNECTION CONFD_CONNECTION
--            TO 'https://<host>:20003'
--            USER '<confd_user>' IDENTIFIED BY '<password>';
--   3. The operator holds the DBA role.
--
-- Run this whole file in EXAplus or DbVisualiser (the `/` delimiter is required).
-- It does NOT activate the preprocessor; that remains a separate DBA decision.
-- =============================================================================

OPEN SCHEMA PREPROC_RT;

--/
CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PREPROC_RT.CONFD_JOB_V1(connection_name VARCHAR(128), job_name VARCHAR(128), params_json VARCHAR(2000000)) RETURNS VARCHAR(2000000) AS
# =============================================================================
# CONFD_JOB_V1  (PREPROC_RT.CONFD_JOB_V1)  — Python3 SCALAR UDF body
# =============================================================================
# Generic gateway to Exasol ConfD. Given the name of a CONNECTION object, a
# ConfD job name, and a JSON parameter object, it invokes the job over ConfD's
# XML-RPC interface and returns the result as a JSON string.
#
# This file is BOTH:
#   * a normal importable Python module (so the logic is unit-testable without a
#     database — see tests/test_confd_job.py), and
#   * the verbatim body embedded into the CREATE PYTHON3 SCALAR SCRIPT by
#     build.py. The `exa` runtime object is only referenced inside run(), so the
#     module imports cleanly outside Exasol; the testable logic lives in the
#     dependency-injected helper _execute().
#
# Return contract — the ERR: sentinel
#   run() NEVER raises. On success it returns a JSON document. On ANY failure it
#   returns 'ERR:<message>'. The calling Lua admin script (PREPROC.ADMIN_CONFD_JOB)
#   checks for the exact ERR: prefix and re-raises it as a loud script error, so
#   a failed ConfD job surfaces clearly instead of corrupting a result set.
#
# Credentials / endpoint
#   Read from the named CONNECTION object via exa.get_connection(). Expected shape:
#     CREATE CONNECTION CONFD_CONNECTION
#         TO 'https://<host>:20003'
#         USER '<confd_user>' IDENTIFIED BY '<password>';
#   .address supplies scheme+host+port (scheme defaults to https if omitted),
#   .user / .password supply basic-auth credentials, injected into the URL.
#
# db_name inference (opt-in)
#   ConfD database jobs take a db_name; cluster jobs (BucketFS, and
#   db_backup_change_expiration) must NOT receive one. So inference is OPT-IN:
#   the caller signals "fill in the current database" by passing db_name as an
#   empty string ('{"db_name":""}'); the UDF then substitutes
#   exa.meta.database_name — the database this UDF is itself running in. A job
#   whose params omit db_name entirely never receives it. The curated grammar
#   adds the empty-string marker only for jobs that take db_name. Assumption: the
#   SQL-session database name equals the ConfD database name (single-DB).
#
# TLS
#   ConfD ships a self-signed certificate by default, so an unverified SSL
#   context is used. Tighten this if your deployment installs a trusted cert.
# =============================================================================
import json
import ssl
import xmlrpc.client
from urllib.parse import quote as _urlquote


def _build_endpoint(address, user, password):
    """Build the XML-RPC URL 'scheme://user:password@host:port' from a
    CONNECTION's address plus credentials.

    The address may or may not carry a scheme and may already embed credentials
    (which are stripped and replaced). ConfD speaks HTTPS, so a bare host:port
    address is assumed to be https.
    """
    addr = address or ""
    if "://" in addr:
        scheme, rest = addr.split("://", 1)
    else:
        scheme, rest = "https", addr
    # Drop any credentials already present in the address; we re-inject ours.
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return "{}://{}:{}@{}".format(
        scheme,
        _urlquote(user or "", safe=""),
        _urlquote(password or "", safe=""),
        rest,
    )


def _prepare_params(params_json, default_db_name):
    """Parse the JSON parameter object and fill an opted-in db_name.

    db_name inference is OPT-IN: the current database name is substituted only
    when the caller includes db_name as an empty string ('{"db_name":""}'). This
    is deliberate — most database jobs take db_name, but some ConfD jobs must NOT
    receive it (e.g. db_backup_change_expiration, and every BucketFS job, which
    are not scoped to a database). The curated SQL grammar adds the empty-string
    marker only for the jobs that take db_name, so a job that doesn't take one
    never gets an unexpected parameter.

    An empty/blank/None params_json is treated as an empty object. Raises
    ValueError if the JSON is not an object (dict).
    """
    params = {}
    if params_json is not None and str(params_json).strip() != "":
        params = json.loads(params_json)
    if not isinstance(params, dict):
        raise ValueError("params_json must be a JSON object")
    if "db_name" in params and not params["db_name"]:
        params["db_name"] = default_db_name
    return params


def _execute(connection, default_db_name, job_name, params_json, proxy_factory=None):
    """Pure, dependency-injected core: prepare params, build the endpoint, call
    the ConfD job, and return the result as JSON. Tests drive this directly with
    a fake connection and proxy_factory; run() wires in the live Exasol objects.
    """
    params = _prepare_params(params_json, default_db_name)
    endpoint = _build_endpoint(connection.address, connection.user, connection.password)
    if proxy_factory is None:
        sslctx = ssl._create_unverified_context()

        def proxy_factory(url):
            return xmlrpc.client.ServerProxy(url, context=sslctx, allow_none=True)

    server = proxy_factory(endpoint)
    result = server.job_exec(job_name, {"params": params})
    return json.dumps(result, default=str)


def run(ctx):
    try:
        connection = exa.get_connection(ctx.connection_name)  # noqa: F821  (exa: Exasol runtime)
        return _execute(
            connection,
            exa.meta.database_name,  # noqa: F821
            ctx.job_name,
            ctx.params_json,
        )
    except Exception as e:  # never raise: the ERR: sentinel is the contract
        return "ERR:" + str(e)
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_DATABASE_V1() AS
-- =============================================================================
-- CONTROL_DATABASE_V1  (PREPROC_RT.CONTROL_DATABASE_V1)  — EXPAND-phase module
-- =============================================================================
-- The curated SQL surface for ConfD-backed database administration. It parses
-- the `CONTROL DATABASE <verb> <object> …` keyword grammar and rewrites each
-- recognised command into exactly one
-- `EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB(job, params)` call. Commands:
--
--   CONTROL DATABASE SHOW INFO                                          -> db_info
--   CONTROL DATABASE SHOW BACKUPS                                       -> db_backup_list
--   CONTROL DATABASE SHOW BACKUP PROGRESS                               -> db_backup_progress
--   CONTROL DATABASE ABORT BACKUP                                       -> db_backup_abort
--   CONTROL DATABASE DELETE UNUSABLE BACKUPS                            -> db_backup_delete_unusable
--   CONTROL DATABASE DELETE BACKUP '<id>' [, '<id>' …]                  -> db_backups_delete
--   CONTROL DATABASE START BACKUP TO VOLUME <vol>
--       [WITH LEVEL <n>] [AND EXPIRATION '<t>']                         -> db_backup_start
--   CONTROL DATABASE ADD BACKUP SCHEDULE '<name>' TO VOLUME <vol>
--       [WITH LEVEL <n>] [AT '<cron>'] [AND EXPIRATION '<t>']
--       [ENABLED|DISABLED]                                              -> db_backup_add_schedule
--   CONTROL DATABASE MODIFY BACKUP SCHEDULE '<name>'
--       [AT '<cron>'] [WITH EXPIRATION '<t>'] [ENABLED|DISABLED]        -> db_backup_modify_schedule
--   CONTROL DATABASE MODIFY BACKUP EXPIRATION ON VOLUME <vol>
--       FOR '<backup_files>' TO '<YYYY-MM-DD hh:mm:ss>'                 -> db_backup_change_expiration
--
-- Conventions
--   * The database NAME is never typed. Jobs that take a db_name carry the
--     marker "db_name":"" in their params; the UDF substitutes the current
--     database. db_backup_change_expiration is NOT scoped to a database, so it
--     omits the marker (see the UDF's opt-in inference).
--   * String arguments (schedule names, backup ids, file prefixes, times, cron
--     specs, expirations) are single-quoted. Volume names are bare identifiers
--     (matching `TO VOLUME <vol>`). LEVEL defaults to 0 (full backup).
--   * <cron> is a 5-field crontab string 'min hour day month weekday'
--     (e.g. '0 2 * * 0' = 02:00 every Sunday).
--
-- Follows the framework's EXPAND authoring contract: return nil to decline
-- (fail-open), a single string to accept (terminal); export a top-level global
-- `expand`; no defensive pcall; capture gsub's result into one local.
-- Scoped to the DBA role via the seeded CONFIG rule.
-- =============================================================================

-- luacheck: globals expand

local SENTINEL = "\1"

-- Replace every single-quoted '...' literal with a placeholder token
-- \1<index>\1 (padded with spaces so it tokenises cleanly), returning the
-- de-quoted text and the ordered list of literal values. This lets the rest of
-- the parser tokenise on whitespace without quoted values (which may contain
-- spaces, commas, or '=') breaking the scan.
local function extract_literals(text)
    local literals = {}
    local out = text:gsub("'([^']*)'", function(value)
        literals[#literals + 1] = value
        return " " .. SENTINEL .. #literals .. SENTINEL .. " "
    end)
    return out, literals
end

-- Tokenise on whitespace, first isolating ',' and '=' as their own tokens so
-- `key=value` and `a,b` split regardless of surrounding spaces.
local function tokenize(text)
    local spaced = text:gsub("[,=]", " %0 ")
    local tokens = {}
    for word in spaced:gmatch("%S+") do
        tokens[#tokens + 1] = word
    end
    return tokens
end

-- If token is a literal placeholder, return its value; else nil.
local function as_literal(token, literals)
    if not token then
        return nil
    end
    local idx = token:match("^" .. SENTINEL .. "(%d+)" .. SENTINEL .. "$")
    if idx then
        return literals[tonumber(idx)]
    end
    return nil
end

-- Resolve a token to its string value: the literal value if it is a placeholder,
-- otherwise the bare token text unchanged (e.g. a volume name or integer).
local function resolve(token, literals)
    local value = as_literal(token, literals)
    if value ~= nil then
        return value
    end
    return token
end

-- Uppercase a token for case-insensitive keyword comparison (nil-safe).
local function U(token)
    return token and string.upper(token) or nil
end

-- Escape a value for embedding as a SQL single-quoted string literal.
local function sql_quote(value)
    local escaped = value:gsub("'", "''")
    return "'" .. escaped .. "'"
end

-- Escape a value for embedding inside a JSON double-quoted string.
local function json_escape(value)
    local s = value:gsub("\\", "\\\\")
    local t = s:gsub('"', '\\"')
    return t
end

-- Format a "key":"value" JSON string pair.
local function json_str(key, value)
    return '"' .. key .. '":"' .. json_escape(value) .. '"'
end

-- Build a JSON object from a list of preformatted "key":value parts.
local function build_json(parts)
    return "{" .. table.concat(parts, ",") .. "}"
end

-- Build a JSON object that opts in to db_name inference (UDF fills it in).
local function db_params(parts)
    local p = { '"db_name":""' }
    for _, part in ipairs(parts) do
        p[#p + 1] = part
    end
    return build_json(p)
end

-- The single emitted statement: one EXECUTE SCRIPT call. The job name is a
-- trusted constant; params_json is carried as a SQL string literal.
local function emit(job, params_json)
    return "EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB('"
        .. job .. "', " .. sql_quote(params_json) .. ")"
end

-- Append the 5 cron fields from a crontab string, if exactly 5 are present.
local function add_cron(parts, cron)
    if not cron then
        return
    end
    local fields = {}
    for word in cron:gmatch("%S+") do
        fields[#fields + 1] = word
    end
    if #fields == 5 then
        local names = { "minute", "hour", "day", "month", "weekday" }
        for k = 1, 5 do
            parts[#parts + 1] = json_str(names[k], fields[k])
        end
    end
end

-- A bare integer level token, or "0" (full backup) as the default.
local function level_or_default(level)
    if level and level:match("^%d+$") then
        return '"level":' .. level
    end
    return '"level":0'
end

-- --- per-command parsers (each returns the emit string, or nil to decline) ---

-- START BACKUP TO VOLUME <vol> [WITH LEVEL <n>] [AND EXPIRATION '<t>']
local function parse_start(t, i, literals)
    local volume, level, expire
    while i <= #t do
        local u = U(t[i])
        if u == "VOLUME" and t[i + 1] then
            volume = resolve(t[i + 1], literals)
            i = i + 2
        elseif u == "LEVEL" and t[i + 1] then
            level = resolve(t[i + 1], literals)
            i = i + 2
        elseif u == "EXPIRATION" and t[i + 1] then
            expire = as_literal(t[i + 1], literals)
            i = i + 2
        else
            i = i + 1
        end
    end
    local parts = {}
    if volume then
        parts[#parts + 1] = json_str("backup_volume_name", volume)
    end
    parts[#parts + 1] = level_or_default(level)
    if expire then
        parts[#parts + 1] = json_str("expire", expire)
    end
    return emit("db_backup_start", db_params(parts))
end

-- DELETE BACKUP '<id>' [, '<id>' …]  (each id is a quoted literal)
local function parse_delete_backups(t, i, literals)
    local ids = {}
    while i <= #t do
        local value = as_literal(t[i], literals)
        if value ~= nil then
            ids[#ids + 1] = '"' .. json_escape(value) .. '"'
        end
        i = i + 1
    end
    if #ids == 0 then
        return nil
    end
    local parts = { '"backup_list":[' .. table.concat(ids, ",") .. "]" }
    return emit("db_backups_delete", db_params(parts))
end

-- ADD BACKUP SCHEDULE '<name>' TO VOLUME <vol> [WITH LEVEL <n>] [AT '<cron>']
--     [AND EXPIRATION '<t>'] [ENABLED|DISABLED]
local function parse_add_schedule(t, i, literals)
    local name = as_literal(t[i], literals)
    if not name then
        return nil
    end
    i = i + 1
    local volume, level, cron, expire, enabled
    while i <= #t do
        local u = U(t[i])
        if u == "VOLUME" and t[i + 1] then
            volume = resolve(t[i + 1], literals)
            i = i + 2
        elseif u == "LEVEL" and t[i + 1] then
            level = resolve(t[i + 1], literals)
            i = i + 2
        elseif u == "AT" and t[i + 1] then
            cron = as_literal(t[i + 1], literals)
            i = i + 2
        elseif u == "EXPIRATION" and t[i + 1] then
            expire = as_literal(t[i + 1], literals)
            i = i + 2
        elseif u == "ENABLED" then
            enabled = true
            i = i + 1
        elseif u == "DISABLED" then
            enabled = false
            i = i + 1
        else
            i = i + 1
        end
    end
    local parts = { json_str("backup_name", name) }
    if volume then
        parts[#parts + 1] = json_str("backup_volume_name", volume)
    end
    parts[#parts + 1] = level_or_default(level)
    add_cron(parts, cron)
    if expire then
        parts[#parts + 1] = json_str("expire", expire)
    end
    if enabled ~= nil then
        parts[#parts + 1] = '"enabled":' .. tostring(enabled)
    end
    return emit("db_backup_add_schedule", db_params(parts))
end

-- MODIFY BACKUP SCHEDULE '<name>' [AT '<cron>'] [WITH EXPIRATION '<t>']
--     [ENABLED|DISABLED]
local function parse_modify_schedule(t, i, literals)
    local name = as_literal(t[i], literals)
    if not name then
        return nil
    end
    i = i + 1
    local cron, expire, enabled
    while i <= #t do
        local u = U(t[i])
        if u == "AT" and t[i + 1] then
            cron = as_literal(t[i + 1], literals)
            i = i + 2
        elseif u == "EXPIRATION" and t[i + 1] then
            expire = as_literal(t[i + 1], literals)
            i = i + 2
        elseif u == "ENABLED" then
            enabled = true
            i = i + 1
        elseif u == "DISABLED" then
            enabled = false
            i = i + 1
        else
            i = i + 1
        end
    end
    local parts = { json_str("backup_name", name) }
    add_cron(parts, cron)
    if expire then
        parts[#parts + 1] = json_str("expire", expire)
    end
    if enabled ~= nil then
        parts[#parts + 1] = '"enabled":' .. tostring(enabled)
    end
    return emit("db_backup_modify_schedule", db_params(parts))
end

-- MODIFY BACKUP EXPIRATION ON VOLUME <vol> FOR '<files>' TO '<time>'
-- NOTE: db_backup_change_expiration is NOT db-scoped — no db_name marker.
local function parse_change_expiration(t, i, literals)
    local volume, files, etime
    while i <= #t do
        local u = U(t[i])
        if u == "VOLUME" and t[i + 1] then
            volume = resolve(t[i + 1], literals)
            i = i + 2
        elseif u == "FOR" and t[i + 1] then
            files = as_literal(t[i + 1], literals)
            i = i + 2
        elseif u == "TO" and t[i + 1] then
            etime = as_literal(t[i + 1], literals)
            i = i + 2
        else
            i = i + 1
        end
    end
    if not (volume and files and etime) then
        return nil
    end
    local parts = {
        json_str("backup_volume_name", volume),
        json_str("backup_files", files),
        json_str("expire_time", etime),
    }
    return emit("db_backup_change_expiration", build_json(parts))
end

-- expand(sqltext) -> rewritten EXECUTE SCRIPT for a recognised CONTROL DATABASE
-- command, else nil (not ours, or an unrecognised subcommand).
function expand(sqltext)
    if type(sqltext) ~= "string" then
        return nil
    end

    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    local without_semicolon = trimmed:gsub(";%s*$", "")
    local clean = without_semicolon:gsub("^%s*(.-)%s*$", "%1")

    local rest = clean:match(
        "^[Cc][Oo][Nn][Tt][Rr][Oo][Ll]%s+[Dd][Aa][Tt][Aa][Bb][Aa][Ss][Ee]%s+(.*)$"
    )
    if not rest then
        return nil
    end

    local literals
    rest, literals = extract_literals(rest)
    local t = tokenize(rest)
    if #t == 0 then
        return nil
    end

    local v1 = U(t[1])
    local v2 = U(t[2])

    if v1 == "SHOW" then
        if v2 == "INFO" and #t == 2 then
            return emit("db_info", db_params({}))
        elseif v2 == "BACKUPS" and #t == 2 then
            return emit("db_backup_list", db_params({}))
        elseif v2 == "BACKUP" and U(t[3]) == "PROGRESS" and #t == 3 then
            return emit("db_backup_progress", db_params({}))
        end
        return nil
    end

    if v1 == "START" and v2 == "BACKUP" then
        return parse_start(t, 3, literals)
    end

    if v1 == "ABORT" and v2 == "BACKUP" and #t == 2 then
        return emit("db_backup_abort", db_params({}))
    end

    if v1 == "DELETE" then
        if v2 == "UNUSABLE" and U(t[3]) == "BACKUPS" and #t == 3 then
            return emit("db_backup_delete_unusable", db_params({}))
        elseif v2 == "BACKUP" then
            return parse_delete_backups(t, 3, literals)
        end
        return nil
    end

    if v1 == "ADD" and v2 == "BACKUP" and U(t[3]) == "SCHEDULE" then
        return parse_add_schedule(t, 4, literals)
    end

    if v1 == "MODIFY" and v2 == "BACKUP" then
        if U(t[3]) == "SCHEDULE" then
            return parse_modify_schedule(t, 4, literals)
        elseif U(t[3]) == "EXPIRATION" then
            return parse_change_expiration(t, 4, literals)
        end
        return nil
    end

    return nil
end
/

--/
CREATE OR REPLACE LUA SCRIPT PREPROC_RT.CONTROL_BUCKETFS_V1() AS
-- =============================================================================
-- CONTROL_BUCKETFS_V1  (PREPROC_RT.CONTROL_BUCKETFS_V1)  — EXPAND-phase module
-- =============================================================================
-- The curated SQL surface for ConfD-backed BucketFS administration. It parses
-- the `CONTROL BUCKETFS <verb> <object> …` grammar and rewrites each recognised
-- command into one `EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB(job, params)` call.
--
--   CONTROL BUCKETFS SHOW SERVICES                                      -> bucketfs_list
--   CONTROL BUCKETFS SHOW SERVICE '<name>'                              -> bucketfs_info
--   CONTROL BUCKETFS DELETE SERVICE '<name>'                            -> bucketfs_delete
--   CONTROL BUCKETFS ADD SERVICE '<name>' WITH <options>                -> bucketfs_add
--   CONTROL BUCKETFS MODIFY SERVICE '<name>' WITH <options>             -> bucketfs_modify
--   CONTROL BUCKETFS ADD BUCKET '<bucket>' TO SERVICE '<svc>'
--       [PUBLIC|PRIVATE] [WITH <options>]                               -> bucket_add
--   CONTROL BUCKETFS MODIFY BUCKET '<bucket>' IN SERVICE '<svc>'
--       [PUBLIC|PRIVATE] [WITH <options>]                               -> bucket_modify
--   CONTROL BUCKETFS DELETE BUCKET '<bucket>' FROM SERVICE '<svc>'      -> bucket_delete
--
-- Options (the `WITH key = value [, …]` tail) map 1:1 to the documented ConfD
-- parameter names, so they are self-documenting against the ConfD job pages:
--   service:  http_port, https_port, owner ('user group'), sync_period,
--             sync_key, path, mode, bucketvolume
--   bucket:   read_password, write_password
-- Ports and sync_period are emitted as JSON numbers when integral; owner is
-- split on whitespace into a 2-element JSON array (ConfD wants a [user, group]
-- tuple). additional_files is intentionally not exposed in v1.
--
-- BucketFS jobs are NOT database-scoped, so params NEVER carry a db_name marker.
-- Identifiers (service/bucket names) are single-quoted; the public flag is the
-- PUBLIC|PRIVATE keyword (required by ConfD on ADD BUCKET).
--
-- Follows the framework's EXPAND authoring contract: return nil to decline
-- (fail-open), a single string to accept (terminal); export a top-level global
-- `expand`; no defensive pcall. Scoped to the DBA role via the seeded rule.
-- =============================================================================

-- luacheck: globals expand

local SENTINEL = "\1"

-- Replace single-quoted '...' literals with placeholder tokens (see
-- CONTROL_DATABASE_V1 for the rationale): de-quoting first lets the parser
-- tokenise on whitespace without quoted values breaking the scan.
local function extract_literals(text)
    local literals = {}
    local out = text:gsub("'([^']*)'", function(value)
        literals[#literals + 1] = value
        return " " .. SENTINEL .. #literals .. SENTINEL .. " "
    end)
    return out, literals
end

local function tokenize(text)
    local spaced = text:gsub("[,=]", " %0 ")
    local tokens = {}
    for word in spaced:gmatch("%S+") do
        tokens[#tokens + 1] = word
    end
    return tokens
end

local function as_literal(token, literals)
    if not token then
        return nil
    end
    local idx = token:match("^" .. SENTINEL .. "(%d+)" .. SENTINEL .. "$")
    if idx then
        return literals[tonumber(idx)]
    end
    return nil
end

local function resolve(token, literals)
    local value = as_literal(token, literals)
    if value ~= nil then
        return value
    end
    return token
end

local function U(token)
    return token and string.upper(token) or nil
end

local function sql_quote(value)
    local escaped = value:gsub("'", "''")
    return "'" .. escaped .. "'"
end

local function json_escape(value)
    local s = value:gsub("\\", "\\\\")
    local t = s:gsub('"', '\\"')
    return t
end

local function json_str(key, value)
    return '"' .. key .. '":"' .. json_escape(value) .. '"'
end

-- Emit a number when the value is an unsigned integer, else a JSON string.
local function json_num_or_str(key, value)
    if value:match("^%d+$") then
        return '"' .. key .. '":' .. value
    end
    return json_str(key, value)
end

-- Split a value on whitespace into a JSON array of strings (for owner tuples).
local function json_words_array(key, value)
    local items = {}
    for word in value:gmatch("%S+") do
        items[#items + 1] = '"' .. json_escape(word) .. '"'
    end
    return '"' .. key .. '":[' .. table.concat(items, ",") .. "]"
end

local function build_json(parts)
    return "{" .. table.concat(parts, ",") .. "}"
end

local function emit(job, params_json)
    return "EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB('"
        .. job .. "', " .. sql_quote(params_json) .. ")"
end

-- Index of the first token equal (case-insensitively) to kw at/after `from`.
local function index_of(t, kw, from)
    for j = from, #t do
        if U(t[j]) == kw then
            return j
        end
    end
    return nil
end

-- Collect a `key = value [, …]` option list starting at token index i into a
-- table keyed by lowercased option name. Stops at the first malformed pair.
local function collect_options(t, i, literals)
    local opts = {}
    while i <= #t do
        if t[i] == "," then
            i = i + 1
        elseif t[i + 1] == "=" and t[i + 2] then
            opts[string.lower(t[i])] = resolve(t[i + 2], literals)
            i = i + 3
        else
            break
        end
    end
    return opts
end

-- Append known service options (bucketfs_add / bucketfs_modify) to parts.
local function append_service_opts(parts, opts)
    if opts.http_port then
        parts[#parts + 1] = json_num_or_str("http_port", opts.http_port)
    end
    if opts.https_port then
        parts[#parts + 1] = json_num_or_str("https_port", opts.https_port)
    end
    if opts.owner then
        parts[#parts + 1] = json_words_array("owner", opts.owner)
    end
    if opts.sync_period then
        parts[#parts + 1] = json_num_or_str("sync_period", opts.sync_period)
    end
    if opts.sync_key then
        parts[#parts + 1] = json_str("sync_key", opts.sync_key)
    end
    if opts.path then
        parts[#parts + 1] = json_str("path", opts.path)
    end
    if opts.mode then
        parts[#parts + 1] = json_str("mode", opts.mode)
    end
    if opts.bucketvolume then
        parts[#parts + 1] = json_str("bucketvolume", opts.bucketvolume)
    end
end

-- Append known bucket options (bucket_add / bucket_modify) to parts.
local function append_bucket_opts(parts, opts)
    if opts.read_password then
        parts[#parts + 1] = json_str("read_password", opts.read_password)
    end
    if opts.write_password then
        parts[#parts + 1] = json_str("write_password", opts.write_password)
    end
end

-- --- per-command parsers (each returns the emit string, or nil to decline) ---

-- ADD|MODIFY SERVICE '<name>' WITH <options>
local function parse_service(t, literals, job)
    local name = as_literal(t[3], literals)
    if not name then
        return nil
    end
    local parts = { json_str("bucketfs_name", name) }
    local wi = index_of(t, "WITH", 4)
    if wi then
        append_service_opts(parts, collect_options(t, wi + 1, literals))
    end
    return emit(job, build_json(parts))
end

-- ADD BUCKET '<bucket>' TO SERVICE '<svc>' [PUBLIC|PRIVATE] [WITH <options>]
-- MODIFY BUCKET '<bucket>' IN SERVICE '<svc>' [PUBLIC|PRIVATE] [WITH <options>]
local function parse_bucket(t, literals, job)
    local bucket = as_literal(t[3], literals)
    if not bucket then
        return nil
    end
    local si = index_of(t, "SERVICE", 4)
    if not si then
        return nil
    end
    local svc = as_literal(t[si + 1], literals)
    if not svc then
        return nil
    end
    local parts = {
        json_str("bucket_name", bucket),
        json_str("bucketfs_name", svc),
    }
    if index_of(t, "PUBLIC", 4) then
        parts[#parts + 1] = '"public":true'
    elseif index_of(t, "PRIVATE", 4) then
        parts[#parts + 1] = '"public":false'
    end
    local wi = index_of(t, "WITH", 4)
    if wi then
        append_bucket_opts(parts, collect_options(t, wi + 1, literals))
    end
    return emit(job, build_json(parts))
end

-- DELETE BUCKET '<bucket>' FROM SERVICE '<svc>'
local function parse_bucket_delete(t, literals)
    local bucket = as_literal(t[3], literals)
    if not bucket then
        return nil
    end
    local si = index_of(t, "SERVICE", 4)
    if not si then
        return nil
    end
    local svc = as_literal(t[si + 1], literals)
    if not svc then
        return nil
    end
    local parts = {
        json_str("bucketfs_name", svc),
        json_str("bucket_name", bucket),
    }
    return emit("bucket_delete", build_json(parts))
end

-- expand(sqltext) -> rewritten EXECUTE SCRIPT for a recognised CONTROL BUCKETFS
-- command, else nil.
function expand(sqltext)
    if type(sqltext) ~= "string" then
        return nil
    end

    local trimmed = sqltext:gsub("^%s*(.-)%s*$", "%1")
    local without_semicolon = trimmed:gsub(";%s*$", "")
    local clean = without_semicolon:gsub("^%s*(.-)%s*$", "%1")

    local rest = clean:match(
        "^[Cc][Oo][Nn][Tt][Rr][Oo][Ll]%s+[Bb][Uu][Cc][Kk][Ee][Tt][Ff][Ss]%s+(.*)$"
    )
    if not rest then
        return nil
    end

    local literals
    rest, literals = extract_literals(rest)
    local t = tokenize(rest)
    if #t == 0 then
        return nil
    end

    local v1 = U(t[1])
    local v2 = U(t[2])

    if v1 == "SHOW" then
        if v2 == "SERVICES" and #t == 2 then
            return emit("bucketfs_list", "{}")
        elseif v2 == "SERVICE" then
            local name = as_literal(t[3], literals)
            if name and #t == 3 then
                return emit("bucketfs_info", build_json({ json_str("bucketfs_name", name) }))
            end
        end
        return nil
    end

    if v1 == "DELETE" then
        if v2 == "SERVICE" then
            local name = as_literal(t[3], literals)
            if name and #t == 3 then
                return emit("bucketfs_delete", build_json({ json_str("bucketfs_name", name) }))
            end
            return nil
        elseif v2 == "BUCKET" then
            return parse_bucket_delete(t, literals)
        end
        return nil
    end

    if v1 == "ADD" then
        if v2 == "SERVICE" then
            return parse_service(t, literals, "bucketfs_add")
        elseif v2 == "BUCKET" then
            return parse_bucket(t, literals, "bucket_add")
        end
        return nil
    end

    if v1 == "MODIFY" then
        if v2 == "SERVICE" then
            return parse_service(t, literals, "bucketfs_modify")
        elseif v2 == "BUCKET" then
            return parse_bucket(t, literals, "bucket_modify")
        end
        return nil
    end

    return nil
end
/

OPEN SCHEMA PREPROC;

--/
CREATE OR REPLACE LUA SCRIPT PREPROC.ADMIN_CONFD_JOB(job_name, params_json) RETURNS TABLE AS
-- =============================================================================
-- ADMIN_CONFD_JOB  (PREPROC.ADMIN_CONFD_JOB)  — Lua admin script body
-- =============================================================================
-- The single internal entry point through which every CONTROL command reaches
-- ConfD. The CONTROL_DATABASE_V1 EXPAND module rewrites each recognised
-- `CONTROL DATABASE …` statement into exactly one
-- `EXECUTE SCRIPT PREPROC.ADMIN_CONFD_JOB(job, params)` call against this
-- script. This script:
--   1. resolves the ConfD CONNECTION object name (a constant — see below),
--   2. calls the generic UDF PREPROC_RT.CONFD_JOB_V1(connection, job, params),
--   3. converts the UDF's ERR: sentinel into a loud script error, and
--   4. returns the ConfD job's JSON result as a single-row table.
--
-- Why a generic admin script (not one per job)
--   ConfD has dozens of jobs and a uniform call shape. Keeping ONE admin script
--   that takes (job_name, params_json) means new commands are added purely in
--   the grammar — no new database object per job. The curated SQL surface
--   (CONTROL DATABASE …) is enforced by the EXPAND module, not here.
--
-- Why raising here is correct (unlike a dispatched module)
--   A preprocessor MODULE must never raise (MASTER's pcall would silently skip
--   it). This is NOT a module — it is an EXECUTE SCRIPT target invoked after the
--   statement has already been rewritten. A failed ConfD job MUST surface as a
--   visible error to the operator, so this script re-raises the UDF's ERR:
--   sentinel via error().
--
-- Parameters (declared in the CREATE OR REPLACE LUA SCRIPT header by build.py)
--   job_name     the ConfD job to run, e.g. 'db_backup_start'
--   params_json  JSON object of job parameters, e.g. '{"backup_volume_name":"v","level":0}'
--                NULL is normalised to '{}'. db_name is injected by the UDF when
--                absent, so the curated commands pass only the job-specific args.
-- =============================================================================

-- luacheck: read globals job_name params_json

-- The CONNECTION object the DBA must create as a prerequisite (see docs). To
-- point at a different ConfD endpoint, change this constant and redeploy.
local CONFD_CONNECTION = "CONFD_CONNECTION"

-- Normalise a NULL/absent params object to an empty JSON object so the UDF's
-- json.loads path is exercised uniformly.
local pj = params_json
if pj == null or pj == nil then
    pj = "{}"
end

-- Invoke the generic ConfD gateway UDF. Named binding keeps the values out of
-- the SQL text.
-- The column alias must NOT be RESULT: it is a reserved keyword in Exasol and
-- would make this SELECT a syntax error. The value is read positionally below.
local ok, res = pquery(
    "SELECT PREPROC_RT.CONFD_JOB_V1(:c, :j, :p) AS confd_result",
    { c = CONFD_CONNECTION, j = job_name, p = pj }
)
if not ok then
    error("PREPROC.ADMIN_CONFD_JOB: UDF call failed: " .. tostring(res.error_message))
end

local result = res[1][1]
if result == null or result == nil then
    error(
        "PREPROC.ADMIN_CONFD_JOB: ConfD job '" .. tostring(job_name)
            .. "' returned no value"
    )
end

-- The UDF never raises; a failure arrives as the ERR:<message> sentinel. Re-raise
-- it here so the operator sees a clear error rather than an ERR: string in a
-- result column.
local result_str = tostring(result)
if string.sub(result_str, 1, 4) == "ERR:" then
    error(
        "PREPROC.ADMIN_CONFD_JOB: ConfD job '" .. tostring(job_name)
            .. "' failed: " .. string.sub(result_str, 5)
    )
end

-- Success: return the JSON result as a single-row, single-column table. The
-- output column is named CONFD_RESULT, not RESULT: RESULT is a reserved keyword
-- in Exasol and is rejected as a column name in the table description string.
exit({{result_str}}, "confd_result VARCHAR(2000000)")
/


-- Register the two command surfaces with the framework, scoped to the built-in
-- DBA role. The PREPROC command sugar registers each rule and auto-refreshes; it
-- requires the preprocessor to be active for your session and that you hold
-- PREPROC_ADMIN. A user must HOLD the DBA role (GRANT DBA TO <user>) for these
-- rules to resolve to them — the implicit superuser SYS does not appear in the
-- role-grant graph PREPROC.REFRESH walks, so operate as a DBA-role holder.

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.CONTROL_DATABASE_V1 FUNCTION expand;

PREPROC ADD RULE FOR ROLE DBA PHASE EXPAND SCRIPT PREPROC_RT.CONTROL_BUCKETFS_V1 FUNCTION expand;

-- If the preprocessor is not active yet, call the admin script directly instead:
--   EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'DBA', 'EXPAND', 'PREPROC_RT.CONTROL_DATABASE_V1', 'expand', NULL);
--   EXECUTE SCRIPT PREPROC.RULE_ADD('ROLE', 'DBA', 'EXPAND', 'PREPROC_RT.CONTROL_BUCKETFS_V1', 'expand', NULL);
