--@tolerate-exists
ALTER TABLE PREPROC.RESOLUTION ADD COLUMN ordinal DECIMAL(9, 0);

--@tolerate-exists
ALTER TABLE PREPROC.SETTINGS ADD COLUMN state_fingerprint VARCHAR(64);

--@tolerate-exists
ALTER TABLE PREPROC.RESOLUTION ADD COLUMN profile_hash VARCHAR(64);

--@tolerate-exists
ALTER TABLE PREPROC.PROFILES ADD COLUMN orphaned_at TIMESTAMP;

--@tolerate-exists
ALTER TABLE PREPROC.SETTINGS ADD COLUMN group_sys_with_dba BOOLEAN;

-- Module provenance audit anchor for installs resolved through a mutable `latest`
-- source: the effective source URL fetched and the concrete library release the
-- index self-declared (see 22_modules_table.sql). Additive on an install that
-- predates them; a fresh install already has them from 22_modules_table.sql.
--@tolerate-exists
ALTER TABLE PREPROC.MODULES ADD COLUMN source_url VARCHAR(2000);

--@tolerate-exists
ALTER TABLE PREPROC.MODULES ADD COLUMN library_version VARCHAR(64);

-- Deployed object inventory for the composite module artifact contract (see
-- 22_modules_table.sql). Additive on an install that predates the composite
-- contract; a fresh install already has it from 22_modules_table.sql.
--@tolerate-exists
ALTER TABLE PREPROC.MODULES ADD COLUMN objects VARCHAR(200000);

-- Widen `objects` on an install that already got the column at its original
-- 4,000 characters -- about 39 objects at realistic ~100-character qualified
-- names, too tight for a subsystem module that owns its own schema (ADR-057).
-- The width, and why it is what it is, is documented in 22_modules_table.sql.
--
-- A WIDENING is not an ADDITION, so this is MODIFY and not ADD, and it takes no
-- --@tolerate-exists marker: the marker would not help, because re-running this
-- raises no "already exists" error to tolerate. Idempotency comes from MODIFY
-- being DECLARATIVE -- it states the target type absolutely, so on an install
-- that is already wide it converts the column to the type it already has and
-- converges with no error, while ADD COLUMN is additive and must be guarded.
-- Statements in this file are applied in file order (see install.py), so by the
-- time this runs the ADD above has guaranteed the column exists; this statement
-- must therefore stay AFTER it.
--
-- Rows are preserved: Exasol converts the stored values in place (the contract
-- is only that the content be convertible, and every value that fitted the old
-- width fits the new one), so the column is never dropped and recreated and no
-- recorded inventory is lost. Specifying no constraint leaves the existing
-- nullability untouched, and `objects` carries no DEFAULT and no IDENTITY to
-- lose. The release bundle skips this file (it is clean-install only, where
-- 22_modules_table.sql already declares the final width), so this path is
-- reached exclusively by `preproc install` over an existing install.
--
-- PREPROC_RT.INSTALLED_MODULES selects this column and does NOT block the
-- change: Exasol validates views lazily, so the view is merely marked INVALID
-- and recompiled on next read. 40_views.sql sorts after this file and issues
-- CREATE OR REPLACE VIEW anyway, so the same install run rebuilds it against the
-- widened column before returning.
ALTER TABLE PREPROC.MODULES MODIFY (objects VARCHAR(200000));
