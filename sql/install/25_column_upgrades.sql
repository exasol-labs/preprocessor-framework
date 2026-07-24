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
