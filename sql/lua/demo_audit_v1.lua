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
-- User identity from engine metadata (no query)
--   The user is exa.meta.current_user -- the executing user the engine already
--   exposes to every script -- never parsed from the submitted statement text and
--   never fetched with a per-statement SELECT CURRENT_USER. Reading metadata is a
--   field access, not a database round-trip.
-- =============================================================================

-- luacheck: globals tag

-- tag(sqltext) -> the statement with an appended
-- /* PREPROC_AUDIT user=<CURRENT_USER> */ comment. Always returns a string.
function tag(sqltext)
    return sqltext .. " /* PREPROC_AUDIT user=" .. tostring(exa.meta.current_user) .. " */"
end
