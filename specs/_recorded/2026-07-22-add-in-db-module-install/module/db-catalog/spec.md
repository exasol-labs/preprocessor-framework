# Feature: In-DB Module Catalog (PREPROC.MODULE_CATALOG)

Lets a DBA discover, from SQL, what modules a source offers and which are already
installed — before committing to an install. A read-only
`EXECUTE SCRIPT PREPROC.MODULE_CATALOG(source)` lists each module a source's
registry index advertises with its name, version, phase, and deploy mode, plus an
`installed?` flag joined against the existing `PREPROC_RT.INSTALLED_MODULES`
provenance view. It is the DB-side analogue of `preproc module list --registry`.

## Background

* `PREPROC.MODULE_CATALOG` is an unversioned admin `RETURNS TABLE` script in the
  `PREPROC` schema (the ADR-008 shape). It NEVER mutates `PREPROC.CONFIG`,
  `PREPROC.SETTINGS`, `PREPROC.RESOLUTION`, or `PREPROC.MODULES` — it is a pure
  read surface.
* It drives the same resolver UDF as `MODULE_INSTALL` to read the source's
  `registry/index.json` (BucketFS native, or HTTPS whenever the source scheme is
  `https:`), but performs NO artifact fetch and NO `sha256` verification of bodies
  — it needs only the index metadata. There is no settings toggle: an `https://`
  source is read over the network when supplied, and on a no-egress cluster the
  GET fails with a network error and the catalog returns no rows.
* The `installed?` column is derived by a LEFT JOIN of the advertised entries
  against `PREPROC_RT.INSTALLED_MODULES` on `(name, version)`, reusing the
  existing PUBLIC provenance view (ADR-046). A version whose only provenance row
  is `retired` MUST NOT be reported as installed.
* Admin-only: `MODULE_CATALOG` inherits the same `PREPROC_ADMIN` boundary as
  `MODULE_INSTALL` because the resolver UDF it drives is admin-only.

## Scenarios

### Scenario: Catalogs a BucketFS source's modules with installed state

* *GIVEN* a staged tarball advertising `cast-shorthand` version 1 and `trailing-comma` version 1
* *AND* `cast-shorthand` version 1 already installed
* *WHEN* a caller holding `PREPROC_ADMIN` runs `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz')`
* *THEN* the script MUST return one row per advertised module carrying `name`, `version`, `phase`, and `deploy_mode`
* *AND* the `cast-shorthand` row MUST show `installed?` TRUE
* *AND* the `trailing-comma` row MUST show `installed?` FALSE

### Scenario: The catalog is read-only

* *GIVEN* any resolvable source
* *WHEN* `MODULE_CATALOG` is called
* *THEN* the script MUST NOT modify `PREPROC.CONFIG`, `PREPROC.SETTINGS`, `PREPROC.RESOLUTION`, or `PREPROC.MODULES`

### Scenario: An HTTPS source on a no-egress cluster fails with a network error

* *GIVEN* an `https://` source whose host the cluster cannot reach (no outbound egress)
* *WHEN* `MODULE_CATALOG` is called with that source
* *THEN* the script MUST fail with a network error naming the unreachable source
* *AND* the script MUST NOT return catalog rows

### Scenario: The installed flag reflects deployed, not retired, provenance

* *GIVEN* a source advertising `cast-shorthand` version 1 whose only `PREPROC.MODULES` row is `retired`
* *WHEN* `MODULE_CATALOG` is called for that source
* *THEN* the `cast-shorthand` version 1 row MUST show `installed?` FALSE

### Scenario: A non-admin caller cannot run the catalog

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`
* *WHEN* that user attempts `EXECUTE SCRIPT PREPROC.MODULE_CATALOG(...)`
* *THEN* the database MUST reject the execution with an insufficient-privileges error
