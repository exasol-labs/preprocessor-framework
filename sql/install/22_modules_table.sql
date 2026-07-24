-- Purpose: Create the module provenance table PREPROC.MODULES.
-- CREATE TABLE IF NOT EXISTS makes re-application (fresh install OR upgrade of an
-- older install that predates the module ecosystem) a no-op-safe create: an
-- install that never had MODULES gets the full table here, so no additive column
-- upgrades belong in 25_column_upgrades.sql for the initial schema.
-- PREPROC_ADMIN becomes the implicit owner (the schema is owned by that role and
-- tables inherit schema ownership in Exasol), so this stays in the locked admin
-- schema; operators read provenance through the PREPROC_RT.INSTALLED_MODULES view.
--
-- Natural key: (name, version). One row per deployed module VERSION, never one
-- row per module name — the update flow keeps version N (status 'retired') and
-- version N+1 (status 'deployed') as two distinct coexisting rows until N is
-- reclaimed. PREPROC.MODULE_REGISTER upserts (MERGE) on this (name, version) key
-- and is idempotent; the key is enforced at the admin-surface layer rather than
-- by a DB constraint, matching the CONFIG/PROFILES convention in 20_tables.sql.
--
-- Nullability: source/ref/url/library_version/sha256 are nullable — a
-- self-deployed module carries no library sha256 (contract: "self-deployed
-- manifest without sha256 accepted") and an offline/manual registration may omit
-- source, ref, url, or library_version. retired_at is null until the version is
-- retired. All other columns are required.
--
-- source_url + library_version are the audit anchor for an install resolved
-- through a mutable `latest` source: source_url records the effective source the
-- operator actually fetched from (e.g. a `.../latest/...` raw URL, a pinned
-- `vX.Y.Z` URL, or a `bucketfs:` path), and library_version records the concrete
-- library release the fetched index self-declared — so a `latest` install is
-- auditable back to a fixed release even though the URL's tag keeps moving.

CREATE TABLE IF NOT EXISTS PREPROC.MODULES (
    name            VARCHAR(128)   NOT NULL,
    version         DECIMAL(9, 0)  NOT NULL,
    source_repo     VARCHAR(256),
    source_ref      VARCHAR(128),
    source_url      VARCHAR(2000),
    library_version VARCHAR(64),
    sha256          VARCHAR(64),
    script_name     VARCHAR(128)   NOT NULL,
    function_name   VARCHAR(128)   NOT NULL,
    phase           VARCHAR(32)    NOT NULL,   -- 'TRANSLATE', 'EXPAND', or 'REWRITE'
    deploy_mode     VARCHAR(32)    NOT NULL,   -- e.g. 'library-deployed' or 'self-deployed'
    status          VARCHAR(32)    NOT NULL,   -- 'deployed' or 'retired'
    installed_at    TIMESTAMP      NOT NULL,
    retired_at      TIMESTAMP
);
