-- Purpose: Create the four admin configuration tables inside the PREPROC schema.
-- All statements use CREATE TABLE IF NOT EXISTS so re-application is a no-op.
-- PREPROC_ADMIN becomes the implicit owner because the schema is owned by that role
-- and tables inherit schema ownership in Exasol.

CREATE TABLE IF NOT EXISTS PREPROC.CONFIG (
    rule_id          DECIMAL(18, 0)  NOT NULL,
    scope_type       VARCHAR(4)      NOT NULL,   -- 'ROLE' or 'USER'
    scope_value      VARCHAR(128)    NOT NULL,
    client_pattern   VARCHAR(256),
    phase            VARCHAR(32)     NOT NULL,
    script_name      VARCHAR(128)    NOT NULL,
    function_name    VARCHAR(128)    NOT NULL,
    enabled          BOOLEAN         NOT NULL,
    version          DECIMAL(9, 0)   NOT NULL
);

CREATE TABLE IF NOT EXISTS PREPROC.RESOLUTION (
    user_name        VARCHAR(128)    NOT NULL,
    client_pattern   VARCHAR(256),
    phase            VARCHAR(32)     NOT NULL,
    script_name      VARCHAR(128)    NOT NULL,
    function_name    VARCHAR(128)    NOT NULL,
    ordinal          DECIMAL(9, 0),
    profile_hash     VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS PREPROC.SETTINGS (
    mode                 VARCHAR(32)  NOT NULL,
    self_disable_allowed BOOLEAN      NOT NULL,
    last_refresh         TIMESTAMP,
    state_fingerprint    VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS PREPROC.PROFILES (
    profile_hash  VARCHAR(64)   NOT NULL,
    manifest      VARCHAR(4000) NOT NULL,
    created_at    TIMESTAMP     NOT NULL,
    orphaned_at   TIMESTAMP
);
