-- =============================================================================
-- DEMO_TRANSLATE_V1 preprocessor module  (PREPROC_RT.DEMO_TRANSLATE_V1)
-- =============================================================================
-- Canonical authoring reference for TRANSLATE-phase modules. Read demo_ping_v1
-- for the full authoring contract; this file applies that contract to a
-- translation rule that calls a companion Python3 UDF.
--
-- TRANSLATE-phase return contract (differs from EXPAND)
--   On TRANSLATE the dispatcher runs every matching rule and threads the
--   returned string forward (run-all threading), not first-match-terminal. The
--   function therefore returns a string on EVERY path: the transpiled text when
--   it translated, or the ORIGINAL text byte-for-byte when it did not. There is
--   no nil "decline" path here; "decline" means returning the input unchanged.
--
-- Trigger: the dialect marker
--   The only statements this module acts on begin -- after optional leading
--   whitespace -- with the marker comment  --!dialect:<name>  (e.g.
--   --!dialect:tsql). A statement without the marker is returned byte-for-byte
--   unchanged and the UDF is never called. Always-transpile was rejected: it is
--   slow (~133 ms/statement) and risks corrupting already-valid Exasol SQL.
--
-- Fail-closed contract
--   TRANSLATE is fail-closed: corrupted or wrong-dialect text must never reach
--   the engine masquerading as a successful translation. The UDF returns the
--   sentinel ERR:<message> instead of raising on any parse/transpile failure.
--   When the UDF returns ERR: (or the pquery itself is not-ok / returns no
--   value), this module returns the ORIGINAL statement text unchanged. It never
--   threads the ERR: sentinel forward as statement text.
--
-- No defensive pcall
--   MASTER wraps this call in pcall (fail-open at the rule level). Reading
--   pquery's ok flag is ordinary control flow, not error suppression; a genuine
--   throw is left to propagate so MASTER's pcall can skip the rule visibly.
--
-- gsub two-value trap
--   string.gsub returns (result, count). The marker strip below captures only
--   the result string into a local before use, never forwarding the count.
-- =============================================================================

-- luacheck: globals translate

-- translate(sqltext) -> transpiled Exasol SQL when the statement carries a
-- leading --!dialect:<name> marker and the UDF succeeds; the original text
-- unchanged otherwise (no marker, or a fail-closed UDF/pquery failure).
function translate(sqltext)
    local dialect = string.match(sqltext, "^%s*%-%-!dialect:(%S+)")
    if dialect == nil then
        return sqltext
    end

    local stripped = string.gsub(sqltext, "^%s*%-%-!dialect:%S+[^\n]*\n?", "")

    local ok, result = pquery(
        "SELECT PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1(:d, :t)",
        { d = dialect, t = stripped })

    if not ok or result == nil or result[1] == nil or result[1][1] == null then
        return sqltext
    end

    local transpiled = tostring(result[1][1])
    if string.sub(transpiled, 1, 4) == "ERR:" then
        return sqltext
    end

    return transpiled
end
