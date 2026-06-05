-- =============================================================================
-- DEMO_AUDIT_V1 preprocessor module  (PREPROC_RT.DEMO_AUDIT_V1)
-- =============================================================================
-- Canonical authoring reference for REWRITE-phase modules. Read demo_ping_v1
-- for the full authoring contract; this file applies that contract to a
-- behaviour-annotating REWRITE rule.
--
-- REWRITE-phase return contract
--   On REWRITE the dispatcher runs every matching rule and threads the returned
--   string forward (run-all threading), not first-match-terminal. This module
--   ALWAYS applies: it returns the annotated statement string on every path and
--   never returns nil.
--
-- What it does (and explicitly does not do)
--   It appends an SQL comment  /* PREPROC_AUDIT user=<CURRENT_USER> */  to the
--   statement so the demo can show, at a glance, that a REWRITE rule fired and
--   for which user. The result is a single valid Exasol statement that runs
--   identically in meaning to the original. This is a DEMONSTRATION module, not
--   an audit-logging feature: it writes nothing to any table (per-statement
--   audit writes are out of scope -- transaction-conflict hazard).
--
-- User identity derived inside the module
--   The user identity is the CURRENT_USER the framework runs under, obtained via
--   pquery here -- never parsed from the submitted statement text.
--
-- No defensive pcall
--   MASTER wraps this call in pcall (fail-open at the rule level). Reading
--   pquery's ok flag is ordinary control flow; a genuine throw is left to
--   propagate so MASTER's pcall can skip the rule visibly.
-- =============================================================================

-- luacheck: globals tag

-- tag(sqltext) -> the statement with an appended
-- /* PREPROC_AUDIT user=<CURRENT_USER> */ comment. Always returns a string.
function tag(sqltext)
    local user = "UNKNOWN"
    local ok, result = pquery("SELECT CURRENT_USER")
    if ok and result ~= nil and result[1] ~= nil and result[1][1] ~= null then
        user = tostring(result[1][1])
    end
    return sqltext .. " /* PREPROC_AUDIT user=" .. user .. " */"
end
