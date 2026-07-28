# Feature: In-DB Module Catalog (PREPROC.MODULE_CATALOG)

Lets a DBA discover, from SQL, what modules a source offers and which are already
installed — before committing to an install.

This delta makes the catalog the pre-install review surface for the object
inventory. Because a composite artifact executes arbitrary DDL under the
`PREPROC_ADMIN` boundary and MAY create objects in any schema the installing
admin can write (ADR-057), an operator MUST be able to see exactly which objects
an install would create before running it.

## Background

* `MODULE_CATALOG` reads only the source's `registry/index.json` — no artifact
  fetch, no `sha256` verification of bodies. The object inventory it reports is
  the entry's declared `objects` list, which is why the registry index carries
  the inventory rather than leaving it to be derived from an artifact the
  catalog never reads.
* The catalog reports the inventory as an object count plus the list of
  fully-qualified object names and their types, so an operator can see at a
  glance that a module reaches outside `PREPROC_RT`. This is the review gate
  that makes the unrestricted namespace of ADR-057 safe in practice.
* An entry from a `schema_version = 1` index carries no `objects` list; the
  catalog reports its inventory as the single object named by `script_name`.
* The catalog remains a pure read surface: it NEVER mutates `PREPROC.CONFIG`,
  `PREPROC.SETTINGS`, `PREPROC.RESOLUTION`, or `PREPROC.MODULES`.

## Scenarios

<!-- DELTA:NEW -->
### Scenario: The catalog reports each module's declared object inventory

* *GIVEN* a staged tarball advertising a composite module whose index entry declares four objects and a single-statement module whose entry declares one
* *WHEN* a caller holding `PREPROC_ADMIN` runs `PREPROC.MODULE_CATALOG` for that source
* *THEN* the composite module's row MUST report an object count of four
* *AND* the result MUST expose each declared object's type and fully-qualified name
* *AND* the single-statement module's row MUST report an object count of one
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: The catalog reveals objects a module would create outside PREPROC_RT

* *GIVEN* a staged tarball advertising a module whose declared inventory includes an object in a schema other than `PREPROC_RT` or `PREPROC`
* *WHEN* a caller holding `PREPROC_ADMIN` runs `PREPROC.MODULE_CATALOG` for that source
* *THEN* the reported inventory MUST name that object with its schema
* *AND* the operator MUST be able to see the out-of-schema object WITHOUT having installed the module
* *AND* the catalog MUST NOT create any object it reports
<!-- /DELTA:NEW -->

<!-- DELTA:NEW -->
### Scenario: A schema-version-1 index entry reports a single-object inventory

* *GIVEN* a staged tarball whose `registry/index.json` declares `schema_version` 1 and whose entries carry no `objects` list
* *WHEN* `MODULE_CATALOG` is called for that source
* *THEN* each row MUST report an object count of one
* *AND* the reported object MUST be the entry's `script_name`
<!-- /DELTA:NEW -->
