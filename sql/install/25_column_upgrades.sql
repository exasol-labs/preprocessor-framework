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
