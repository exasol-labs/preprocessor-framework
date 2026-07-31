# Feature: In-DB Module Catalog (PREPROC.MODULE_CATALOG)

Lets a DBA discover, from SQL, what modules a source offers and which are already
installed — before committing to an install. A read-only
`EXECUTE SCRIPT PREPROC.MODULE_CATALOG(source)` lists each module a source's
registry index advertises with its name, version, phase, and deploy mode, plus an
`installed?` flag joined against the existing `PREPROC_RT.INSTALLED_MODULES`
provenance view. It is the DB-side analogue of `preproc module list --registry`.

The catalog is also the pre-install review surface for the object inventory.
Because a composite artifact executes arbitrary DDL under the `PREPROC_ADMIN`
boundary and MAY create objects in any schema the installing admin can write
(ADR-057), an operator MUST be able to see exactly which objects an install
would create before running it.

This delta changes the `source` argument's BucketFS grammar to the plain
location `bucketfs:<service>/<bucket>/<path>`, which never names a CONNECTION
object. The catalog's read-only contract and its inventory reporting are
unchanged.

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
* A `bucketfs:` source is the BucketFS LOCATION
  `bucketfs:<service>/<bucket>/<path>` and NEVER names a CONNECTION object. The
  catalog passes the source to the resolver verbatim and neither parses nor
  rewrites it, so `bad_source` and `source_unreadable` reach the operator with
  the resolver's wording — including the credential hint naming the
  `CONNECTION TO 'bucketfs:<service>/<bucket>'` grant prerequisite. The catalog
  is therefore the cheapest place for an operator to discover that a bucket is
  not mounted for them, because it writes nothing whatever the outcome.
* The `installed?` column is derived by a LEFT JOIN of the advertised entries
  against `PREPROC_RT.INSTALLED_MODULES` on `(name, version)`, reusing the
  existing PUBLIC provenance view (ADR-046). A version whose only provenance row
  is `retired` MUST NOT be reported as installed.
* Admin-only: `MODULE_CATALOG` inherits the same `PREPROC_ADMIN` boundary as
  `MODULE_INSTALL` because the resolver UDF it drives is admin-only.
* The object inventory `MODULE_CATALOG` reports is the entry's declared `objects`
  list, which is why the registry index carries the inventory rather than
  leaving it to be derived from an artifact the catalog never reads.
* The catalog reports the inventory as an object count plus the list of
  fully-qualified object names and their types, so an operator can see at a
  glance that a module reaches outside `PREPROC_RT`. This is the review gate
  that makes the unrestricted namespace of ADR-057 safe in practice.
* An entry from a `schema_version = 1` index carries no `objects` list; the
  catalog reports its inventory as the single object named by `script_name`.

## Scenarios

### Scenario: Catalogs a BucketFS source's modules with installed state

* *GIVEN* a release staged at `bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz` advertising `cast_shorthand` version 1 and `trailing_comma` version 1
* *AND* `cast_shorthand` version 1 already installed
* *WHEN* a caller holding `PREPROC_ADMIN` runs `EXECUTE SCRIPT PREPROC.MODULE_CATALOG('bucketfs:bfsdefault/default/preproc-lib-0.1.0.tar.gz')`
* *THEN* the script MUST return one row per advertised module carrying `name`, `version`, `phase`, and `deploy_mode`
* *AND* the `cast_shorthand` row MUST show `installed?` TRUE
* *AND* the `trailing_comma` row MUST show `installed?` FALSE

### Scenario: A malformed BucketFS source is reported without a write

* *GIVEN* an installed framework
* *WHEN* a caller holding `PREPROC_ADMIN` runs `MODULE_CATALOG` with the pre-delta two-segment source `bucketfs:PREPROC_BFS/preproc-lib-0.1.0.tar.gz`
* *THEN* the script MUST fail with the resolver's `bad_source` error naming the expected `bucketfs:<service>/<bucket>/<path>` shape
* *AND* the script MUST NOT return catalog rows
* *AND* the script MUST NOT modify `PREPROC.CONFIG`, `PREPROC.SETTINGS`, `PREPROC.RESOLUTION`, or `PREPROC.MODULES`

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

* *GIVEN* a source advertising `cast_shorthand` version 1 whose only `PREPROC.MODULES` row is `retired`
* *WHEN* `MODULE_CATALOG` is called for that source
* *THEN* the `cast_shorthand` version 1 row MUST show `installed?` FALSE

### Scenario: A non-admin caller cannot run the catalog

* *GIVEN* a database user who does not hold `PREPROC_ADMIN`
* *WHEN* that user attempts `EXECUTE SCRIPT PREPROC.MODULE_CATALOG(...)`
* *THEN* the database MUST reject the execution with an insufficient-privileges error

### Scenario: The catalog reports each module's declared object inventory

* *GIVEN* a staged tarball advertising a composite module whose index entry declares four objects and a single-statement module whose entry declares one
* *WHEN* a caller holding `PREPROC_ADMIN` runs `PREPROC.MODULE_CATALOG` for that source
* *THEN* the composite module's row MUST report an object count of four
* *AND* the result MUST expose each declared object's type and fully-qualified name
* *AND* the single-statement module's row MUST report an object count of one

### Scenario: The catalog reveals objects a module would create outside PREPROC_RT

* *GIVEN* a staged tarball advertising a module whose declared inventory includes an object in a schema other than `PREPROC_RT` or `PREPROC`
* *WHEN* a caller holding `PREPROC_ADMIN` runs `PREPROC.MODULE_CATALOG` for that source
* *THEN* the reported inventory MUST name that object with its schema
* *AND* the operator MUST be able to see the out-of-schema object WITHOUT having installed the module
* *AND* the catalog MUST NOT create any object it reports

### Scenario: A schema-version-1 index entry reports a single-object inventory

* *GIVEN* a staged tarball whose `registry/index.json` declares `schema_version` 1 and whose entries carry no `objects` list
* *WHEN* `MODULE_CATALOG` is called for that source
* *THEN* each row MUST report an object count of one
* *AND* the reported object MUST be the entry's `script_name`
