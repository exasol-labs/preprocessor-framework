-- =============================================================================
-- MODULE_RESOLVER_V1  (PREPROC.MODULE_RESOLVER_V1)
-- =============================================================================
-- The DB-side analogue of the CLI's registry read + artifact fetch + sha256
-- verify (registry.read_registry / fetch_artifact_bytes / verify_entry_artifact,
-- deploy.fetch_and_verify_artifact). It reads and integrity-verifies a module
-- artifact from a source entirely inside the database, so a SQL-only DBA can
-- install a module with no operator CLI and no external Python/uv toolchain.
--
-- Placement / privilege  (ADR-052)
--   Lives in the LOCKED admin schema PREPROC, owned by PREPROC_ADMIN, executable
--   by PREPROC_ADMIN ONLY -- it is NEVER granted to PUBLIC. This is a deliberate
--   departure from the PUBLIC placement of PREPROC_RT.DEMO_SQLGLOT_TRANSPILE_V1,
--   because the resolver reaches BucketFS / the network and must never be
--   invokable by an ordinary user. Admin-only is enforced structurally: the
--   PREPROC schema carries no PUBLIC EXECUTE grant.
--
-- Sources
--   bucketfs:<CONNECTION>/<path>/preproc-lib-<ver>.tar.gz
--       An operator-created Exasol CONNECTION (the framework never creates it)
--       names a BucketFS bucket. The CONNECTION's address MUST be the native
--       Exasol BucketFS form 'bucketfs:<service>/<bucket>' (the same string the
--       Exasol docs use, e.g. CREATE CONNECTION ... TO 'bucketfs:bfsdefault/b1'
--       IDENTIFIED BY '<readpw>'): NO host, NO port. exa.get_connection(name)
--       returns that address and it maps 1:1 to the cluster-local UDF mount
--       (/buckets/<service>/<bucket>/...); the release is read NATIVELY off that
--       mount. Read access is governed by Exasol's own BucketFS access rights --
--       the granted CONNECTION and its read password -- not by this script.
--       BucketFS AUTO-EXTRACTS an uploaded archive and presents it on the UDF
--       mount as a DIRECTORY at the extension-less path (verified live), so the
--       release is normally read as extracted files; a raw tarball file is also
--       supported via tarfile for buckets that do not auto-extract. Either way a
--       BucketFS read performs NO network egress -- the release is
--       cluster-replicated and read locally (ADR-050).
--   https://.../registry/index.json
--       A pinned raw source. registry/index.json and the artifact are fetched
--       with urllib. The https path runs whenever the scheme is https: -- there
--       is NO settings toggle (ADR-051). A no-egress cluster simply fails the GET
--       and the failure is signalled, never raised.
--
-- Emit shape  (SET ... EMITS)  -- the never-raise, structured-result contract
--   The resolver is a SET script, not SCALAR: one resolve request maps to a
--   variable-length result set -- a metadata row plus zero-or-more ordered
--   artifact chunks, or a single structured-error row -- and a SET script over a
--   single-row driving input forms exactly one group, so run() fires once and
--   owns the whole result set. (A SCALAR EMITS would tie the invocation count to
--   the driving-row count.) It is called as:
--       SELECT PREPROC.MODULE_RESOLVER_V1(<source>, <module_name>)
--   Every emitted row has the columns, in order:
--       status                 'OK' | 'ERROR'
--       error_kind             null | bad_source | source_unreadable | not_found
--                              | sha256_mismatch | artifact_invalid | network_error
--                              | error
--       error_message          null | human-readable message (names module/source)
--       name, version, phase, script_name, function_name, deploy_mode, sha256,
--       suggested_scope_type, suggested_scope_value, source_repo, source_ref
--       chunk_index            0-based order of this artifact chunk
--       chunk_count            total artifact chunks (0 = no artifact body)
--       artifact_chunk         VARCHAR(2000000) artifact text chunk, or null
--       objects                VARCHAR(200000) newline-separated 'TYPE|NAME' lines
--                              (the same wire format as registry.encode_objects /
--                              PREPROC.MODULES.objects), or null. For a resolved
--                              single module it is the DERIVED inventory (file
--                              order, already verified against the entry); for a
--                              list-all ('*') row it is the entry's DECLARED
--                              inventory (schema_version-1 fallback applied, never
--                              parsed from an artifact); null for a self-deployed
--                              / no-artifact row, matching the registry's own
--                              null-for-self-deployed convention.
--   The caller: if any row is 'ERROR', abort BEFORE any DB write (error_kind is
--   the ERR-sentinel analogue of DEMO_SQLGLOT_TRANSPILE_V1). Otherwise take
--   metadata from any row; chunk_count 0 means a self-deployed entry with a null
--   artifact body (register rule + provenance, no CREATE); chunk_count >= 1 means
--   reassemble artifact_chunk in chunk_index order to recover the artifact text.
--
-- List-all mode (module_name = '*')  -- additive; the single-module contract above
-- is unchanged for any other module_name.
--   PREPROC.MODULE_CATALOG needs index METADATA ONLY for every module a source
--   advertises, not one named module's artifact. Passing the sentinel
--   module_name '*' emits one 'OK' row per entry in registry/index.json --
--   name/version/phase/script_name/function_name/deploy_mode/sha256/scope/source
--   -- with chunk_index=0, chunk_count=0, artifact_chunk=null for every row: NO
--   artifact member is read and NO sha256 is verified, even for an entry that
--   declares one, because the catalog never needs the body. `objects` carries
--   the entry's DECLARED inventory (the schema_version-1 script_name fallback
--   applied where the entry carries no `objects` key) -- never derived from an
--   artifact, since list-all mode reads no artifact. An index read
--   failure (bad_source / source_unreadable / network_error / ...) still emits
--   the usual single ERROR row -- list-all mode only changes entry SELECTION
--   (all entries instead of one matched by name), not index acquisition or
--   error signalling. An index with zero entries emits zero rows (not an
--   error).
--
-- Guarantees
--   * sha256 is verified over the raw artifact bytes vs the index entry's sha256
--     BEFORE any artifact row is emitted; a mismatch emits an error row and no
--     usable artifact text (parity with deploy.fetch_and_verify_artifact).
--   * The artifact's structure is verified after sha256: it is split into its
--     statements, the object each statement creates is derived, and the derived
--     inventory is compared against the index entry's declared `objects` list
--     (parity with manifest.verify_artifact_inventory) in BOTH directions -- an
--     object the artifact creates but the entry does not declare, or an entry-
--     declared object no statement creates, are both hard failures. The artifact
--     must still contain exactly one CREATE OR REPLACE LUA SCRIPT whose target
--     matches the entry's declared script_name -- that script is the module's
--     entry point. A schema_version-1 entry (no `objects` key at all) is verified
--     as a single-statement artifact whose one object is the entry script named
--     by script_name. Any disagreement emits an 'artifact_invalid' error row and
--     no usable artifact text.
--   * Three further rules the CLI settles at manifest parse time are enforced
--     against the INDEX ENTRY here, because for the in-database path this script
--     is the only gate and a parity break would let MODULE_INSTALL accept bytes
--     `preproc module add` rejects. All three emit 'artifact_invalid':
--       - EVERY declared object carries the module's own _V<N> suffix. This is
--         what makes module_reclaim.lua's inventory-wide ownership boundary safe
--         (ADR-056): an unsuffixed name is shared across generations, so an
--         index declaring one for both v1 and v2 would let a v1 reclaim drop an
--         object live v2 depends on -- and the both-directions comparison cannot
--         see it, since both sides agree. Checked for a RESOLVED module, not in
--         list-all mode: the catalog neither installs nor writes provenance, and
--         failing the whole listing over one malformed entry would blind
--         MODULE_CATALOG for every other module the library advertises.
--       - No object is created twice, and none is declared twice. Set comparison
--         is blind to multiplicity, so a single declaration plus two creating
--         statements would otherwise pass and be recorded twice.
--       - The encoded inventory fits OBJECTS_MAX_CHARS (see that constant).
--         Checked on EVERY path that emits `objects`, list-all included, since
--         the column is the same width there.
--   * A self-deployed entry (null artifact_path/sha256) is metadata-only: no
--     artifact member is read and no sha256 is verified.
--   * The module name is matched CASE-INSENSITIVELY (the sugar upper-cases an
--     unquoted name; registry keys are lower_snake_case), so a library must not
--     carry two names differing only by case.
--   * The latest version is selected when several are listed.
--   * The resolver NEVER raises: every failure path returns a structured error
--     row so a failure cannot fail the caller mid-write.
--
-- Versioning / prefix discipline
--   _V1 suffix: author a _V2 to change behaviour rather than replacing a live
--   body. Stdlib only: tarfile, json, hashlib, re, urllib, os.
-- =============================================================================
CREATE OR REPLACE PYTHON3 SET SCRIPT PREPROC.MODULE_RESOLVER_V1(src VARCHAR(2000), module_name VARCHAR(256))
EMITS (
    status VARCHAR(16),
    error_kind VARCHAR(64),
    error_message VARCHAR(2000),
    name VARCHAR(256),
    version DECIMAL(9,0),
    phase VARCHAR(32),
    script_name VARCHAR(256),
    function_name VARCHAR(256),
    deploy_mode VARCHAR(32),
    sha256 VARCHAR(64),
    suggested_scope_type VARCHAR(16),
    suggested_scope_value VARCHAR(256),
    source_repo VARCHAR(256),
    source_ref VARCHAR(256),
    library_version VARCHAR(64),
    chunk_index DECIMAL(9,0),
    chunk_count DECIMAL(9,0),
    artifact_chunk VARCHAR(2000000),
    objects VARCHAR(200000)
) AS
import hashlib
import json
import os
import tarfile
import urllib.request

INDEX_MEMBER = 'registry/index.json'
CHUNK_CHARS = 2000000
NETWORK_TIMEOUT = 30
LIST_ALL_MODULES = '*'
ERROR_MESSAGE_MAX = 2000
# The width of this script's own `objects` column, of MODULE_REGISTER's CAST, and
# of PREPROC.MODULES.objects (sql/install/22_modules_table.sql) -- the same value
# by construction, since this column exists to carry that one. A PINNED copy of
# preproc.module.registry.OBJECTS_MAX_CHARS, which a UDF body cannot import;
# tests/test_module_registry.py asserts every pinned copy still equals the
# canonical constant, because each one gates a refusal and the narrowest copy
# would silently become the real limit. An inventory that will not fit must be
# refused HERE, because the provenance row is written LAST (after every CREATE and
# after RULE_ENSURE): discovering the width at MODULE_REGISTER would leave the
# module live and dispatching with no provenance row, hence neither retirable nor
# reclaimable.
OBJECTS_MAX_CHARS = 200000

# tests/test_module_resolver_udf.py extracts everything between the two
# sentinels below VERBATIM and asserts it is byte-identical to
# src/preproc/module/artifact.py, so the tested source and the deployed
# source are the same bytes -- Python-vs-Python divergence is structurally
# impossible.
# BEGIN ARTIFACT SPLITTER
"""Canonical module-artifact statement splitter and object derivation.

A module's canonical artifact is ONE self-contained ``.sql`` file carrying one
or more statements (ADR-054). This module is the single authority on how that
file is cut into statements and on which database object each statement
creates.

The grammar (ADR-058) is the EXAplus block-marker convention the framework's
own ``release.sql`` already emits:

* A statement whose body contains semicolons is wrapped between a line
  consisting solely of ``--/`` and a line consisting solely of ``/``. The body
  between the markers is preserved VERBATIM — it is never scanned for
  terminators — so a statement whose own syntax needs a trailing semicolon
  (``CREATE FUNCTION … END;``) survives intact. Neither marker line appears in
  the yielded statement.
* Any other statement is terminated by the first ``;`` that falls outside a
  string literal, a quoted identifier, a ``--`` line comment, and a ``/* */``
  block comment. The terminating ``;`` is not part of the yielded statement.
* A lone ``/`` line outside a block terminates a pending statement, mirroring
  EXAplus; with nothing pending it is discarded. This is what keeps the
  pre-composite single-statement artifacts valid unchanged.

Marker recognition applies to a COMPLETE physical line only (ignoring
surrounding whitespace). ``SELECT 1; --/`` therefore ends one statement and
leaves a trailing comment; it does not open a block.

Embedding constraint
--------------------
This file is embedded VERBATIM into the ``PREPROC.MODULE_RESOLVER_V1`` Python
UDF (``sql/udf/module_resolver_v1.sql``) and CI asserts the two copies are
byte-identical. It must therefore stay self-contained and paste-safe:

* standard library only, and nothing imported from the ``preproc`` package
* NO ``from __future__`` import — it is illegal anywhere but the top of a file,
  and the embedded copy sits mid-body
* NO type annotations — they are evaluated at definition time, and the UDF's
  Python runtime is not guaranteed to be new enough for PEP 585/604 forms
* no ``__file__``, no argparse, no module-level side effects
* no line consisting solely of ``/``: ``install.py`` strips every such line from
  the resolver ``.sql`` before sending it, which would silently corrupt the
  embedded copy while the byte-identity check still passed

Public API
----------
``split_statements(artifact_text)``
    Cut an artifact into its statements, in file order.
``derive_object(statement)``
    The ``{"type", "name"}`` object one statement creates, or ``None``.
``derive_objects(statements)``
    The derived object inventory for a statement list, in file order.
``qualified_parts(name)`` / ``normalize_object_name(name)``
    Fold a possibly-quoted, possibly-qualified object name to its canonical
    comparable parts, or to the joined canonical form.
"""

import re

BLOCK_OPEN_MARKER = "--/"
BLOCK_CLOSE_MARKER = "/"

UNQUALIFIED_TYPES = ("SCHEMA", "VIRTUAL SCHEMA", "CONNECTION")

_STRIP_CHARS = " \t\n\v\f\r"
_LINE_WHITESPACE = " \t\v\f\r"

_CLEAN = 0
_SINGLE_QUOTE = 1
_DOUBLE_QUOTE = 2
_BLOCK_COMMENT = 3

_IDENT = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*)'
_QUALIFIED = _IDENT + r"(?:\." + _IDENT + r")*"

_IDENT_RE = re.compile(_IDENT)
_QUALIFIED_RE = re.compile(_QUALIFIED + r"\Z")
_CREATE_RE = re.compile(r"CREATE\b", re.IGNORECASE)
# CREATE [OR REPLACE] [<language>] [SCALAR|SET] SCRIPT <name>. The language is
# any identifier, not a fixed set, because an Exasol script-language alias is
# installation-defined (a custom SLC may be registered as e.g. PYTHON3_TE).
_SCRIPT_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:(?!(?:SCALAR|SET|SCRIPT)\b)(?P<language>[A-Za-z_][A-Za-z0-9_]*)\s+)?"
    r"(?:(?P<mode>SCALAR|SET)\s+)?"
    r"SCRIPT\s+(?P<name>" + _QUALIFIED + r")",
    re.IGNORECASE,
)
_OBJECT_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?"
    r"(?P<kind>VIRTUAL\s+SCHEMA|SCHEMA|TABLE|VIEW|CONNECTION|FUNCTION)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>" + _QUALIFIED + r")",
    re.IGNORECASE,
)

_HEAD_EXCERPT_CHARS = 120


class ArtifactSyntaxError(Exception):
    """Raised when an artifact's statement structure or object DDL cannot be parsed."""


def split_statements(artifact_text):
    """Cut an artifact into its statements, in file order.

    Line endings are normalised to ``\\n`` first, so a CRLF artifact splits
    identically to an LF one. Each yielded statement is stripped of surrounding
    whitespace and carries neither its ``;`` terminator nor any marker line.

    Raises:
        ArtifactSyntaxError: if a ``--/`` block is never closed, or a ``--/``
            opens while a semicolon-terminated statement is still pending.
    """
    statements = []
    pending = []
    pending_has_code = False
    state = _CLEAN
    in_block = False

    for line in _normalize_newlines(artifact_text).split("\n"):
        if in_block:
            if _is_marker_line(line, BLOCK_CLOSE_MARKER):
                in_block = False
                _collect(statements, pending)
                pending = []
            else:
                pending.append(line)
            continue

        if state == _CLEAN and _is_marker_line(line, BLOCK_OPEN_MARKER):
            if pending_has_code:
                raise ArtifactSyntaxError(
                    "a '--/' block opened while the preceding statement is still "
                    "unterminated; terminate it with ';' or wrap it in '--/' ... '/'"
                )
            pending = []
            in_block = True
            continue

        if state == _CLEAN and _is_marker_line(line, BLOCK_CLOSE_MARKER):
            if pending_has_code:
                _collect(statements, pending)
            pending = []
            pending_has_code = False
            continue

        remainder = line
        while True:
            state, has_code, cut = _scan_line(remainder, state)
            if has_code:
                pending_has_code = True
            if cut is None:
                pending.append(remainder)
                break
            pending.append(remainder[:cut])
            _collect(statements, pending)
            pending = []
            pending_has_code = False
            remainder = remainder[cut + 1 :]

    if in_block:
        raise ArtifactSyntaxError(
            "unterminated '--/' block: the artifact ends with no closing lone '/' line"
        )
    if pending_has_code:
        _collect(statements, pending)
    return statements


def derive_object(statement):
    """Return the ``{"type", "name"}`` object a statement creates, or None.

    A statement that creates nothing (``GRANT``, ``INSERT``, ``COMMIT``, …)
    yields None. A statement that does begin with ``CREATE`` must be a form the
    contract admits — anything else raises, so an unrecognised DDL form can
    never slip past validation by contributing no inventory entry.

    Raises:
        ArtifactSyntaxError: if the statement is a ``CREATE`` the contract does
            not admit, or its object name is not qualified as the object's type
            requires.
    """
    head = _strip_leading_comments(statement)
    if not _CREATE_RE.match(head):
        return None

    match = _SCRIPT_CREATE_RE.match(head)
    if match is not None:
        words = (match.group("language"), match.group("mode"), "SCRIPT")
        object_type = " ".join(word.upper() for word in words if word)
    else:
        match = _OBJECT_CREATE_RE.match(head)
        if match is None:
            excerpt = _head_excerpt(head)
            raise ArtifactSyntaxError(
                f"statement is a CREATE the module contract does not admit: {excerpt}"
            )
        object_type = " ".join(match.group("kind").upper().split())

    raw_name = match.group("name")
    parts = qualified_parts(raw_name)
    if object_type in UNQUALIFIED_TYPES:
        if len(parts) != 1:
            raise ArtifactSyntaxError(
                f"a {object_type} name must be a single unqualified identifier; got {raw_name!r}"
            )
    elif len(parts) != 2:
        raise ArtifactSyntaxError(
            f"object name {raw_name!r} must be fully qualified as <schema>.<object> so the "
            "inventory never depends on the session's open schema"
        )
    return {"type": object_type, "name": ".".join(parts)}


def derive_objects(statements):
    """Return the derived object inventory for a statement list, in file order."""
    objects = []
    for statement in statements:
        derived = derive_object(statement)
        if derived is not None:
            objects.append(derived)
    return objects


def qualified_parts(name):
    """Split an object name into its canonical, comparable identifier parts.

    An unquoted part folds to upper case; a quoted part keeps its case with
    ``""`` unescaped to ``"``. The part count is how a caller tells a
    schema-qualified name from a bare one, which a plain ``.``-split cannot do
    once a quoted part is allowed to contain a dot.

    Raises:
        ArtifactSyntaxError: if ``name`` is not a valid SQL identifier.
    """
    text = name.strip(_STRIP_CHARS)
    if not _QUALIFIED_RE.match(text):
        raise ArtifactSyntaxError(f"object name {name!r} is not a valid SQL identifier")
    parts = []
    for part in _IDENT_RE.findall(text):
        if part.startswith('"'):
            parts.append(part[1:-1].replace('""', '"'))
        else:
            parts.append(part.upper())
    return parts


def normalize_object_name(name):
    """Fold an object name to its canonical comparable form, parts rejoined with ``.``.

    Raises:
        ArtifactSyntaxError: if ``name`` is not a valid SQL identifier.
    """
    return ".".join(qualified_parts(name))


def _normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_marker_line(line, marker):
    return line.strip(_STRIP_CHARS) == marker


def _collect(statements, lines):
    text = "\n".join(lines).strip(_STRIP_CHARS)
    if text:
        statements.append(text)


def _scan_line(line, state):
    """Scan one line for a terminating ``;``; return (state, has_code, cut).

    ``state`` carries string and block-comment context across lines. ``cut`` is
    the index of the terminating ``;``, or None when the line does not end the
    statement. ``has_code`` reports whether the line contributed any
    non-whitespace outside a comment, which is what distinguishes an
    unterminated statement from inter-statement filler.
    """
    index = 0
    length = len(line)
    has_code = False
    while index < length:
        char = line[index]
        if state == _CLEAN:
            if char == "'":
                state = _SINGLE_QUOTE
                has_code = True
            elif char == '"':
                state = _DOUBLE_QUOTE
                has_code = True
            elif char == "-" and line[index + 1 : index + 2] == "-":
                return state, has_code, None
            elif char == "/" and line[index + 1 : index + 2] == "*":
                state = _BLOCK_COMMENT
                index += 1
            elif char == ";":
                return _CLEAN, has_code, index
            elif char not in _LINE_WHITESPACE:
                has_code = True
        elif state == _SINGLE_QUOTE:
            if char == "'":
                state = _CLEAN
        elif state == _DOUBLE_QUOTE:
            if char == '"':
                state = _CLEAN
        elif state == _BLOCK_COMMENT:
            if char == "*" and line[index + 1 : index + 2] == "/":
                state = _CLEAN
                index += 1
        index += 1
    return state, has_code, None


def _strip_leading_comments(statement):
    text = statement.lstrip(_STRIP_CHARS)
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline < 0:
                return ""
            text = text[newline + 1 :].lstrip(_STRIP_CHARS)
        elif text.startswith("/*"):
            close = text.find("*/", 2)
            if close < 0:
                return ""
            text = text[close + 2 :].lstrip(_STRIP_CHARS)
        else:
            return text


def _head_excerpt(head):
    return head.split("\n", 1)[0].strip(_STRIP_CHARS)[:_HEAD_EXCERPT_CHARS]
# END ARTIFACT SPLITTER


class _ResolveError(Exception):
    kind = 'error'


class _BadSource(_ResolveError):
    kind = 'bad_source'


class _SourceUnreadable(_ResolveError):
    kind = 'source_unreadable'


class _NotFound(_ResolveError):
    kind = 'not_found'


class _NetworkError(_ResolveError):
    kind = 'network_error'


class _ShaMismatch(_ResolveError):
    kind = 'sha256_mismatch'


class _ArtifactInvalid(_ResolveError):
    kind = 'artifact_invalid'


def run(ctx):
    try:
        _resolve(ctx, ctx.src, ctx.module_name)
    except _ResolveError as exc:
        _emit_error(ctx, exc.kind, str(exc))
    except Exception as exc:
        _emit_error(ctx, 'error', '%s: %s' % (type(exc).__name__, exc))


def _resolve(ctx, source, module_name):
    if not source or not module_name:
        raise _BadSource('source and module_name are both required')
    reader = _make_reader(source)
    modules, library_version = _parse_index(reader(INDEX_MEMBER))
    if module_name == LIST_ALL_MODULES:
        # Encode EVERY entry before emitting ANY row: an inventory too wide for
        # the objects column must surface as the usual single ERROR row, not as
        # OK rows for whichever entries happened to sort ahead of the offender.
        encoded = [_encode_objects(_declared_objects(entry), entry.get('name'))
                   for entry in modules]
        for entry, objects in zip(modules, encoded):
            _emit_entry(ctx, entry, library_version, 0, 0, None, objects)
        return
    entry = _select_entry(modules, module_name)
    artifact_path = entry.get('artifact_path')
    declared_sha = entry.get('sha256')
    if artifact_path is None and declared_sha is None:
        _emit_entry(ctx, entry, library_version, 0, 0, None, None)
        return
    artifact_bytes = reader(artifact_path)
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    if not declared_sha or digest != declared_sha.lower():
        raise _ShaMismatch(
            'sha256 mismatch for module %r: index declares %s, artifact hashes to %s'
            % (module_name, declared_sha, digest))
    text = artifact_bytes.decode('utf-8')
    objects = _verify_inventory(entry, text)
    _emit_artifact(ctx, entry, library_version, text, objects)


def _make_reader(source):
    if source.startswith('bucketfs:'):
        return _bucketfs_reader(source)
    if source.startswith('https:'):
        return _https_reader(source)
    raise _BadSource('unsupported source scheme (expected bucketfs: or https:): %r' % source)


_ARCHIVE_EXTENSIONS = ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tar', '.zip')


def _bucketfs_reader(source):
    rest = source[len('bucketfs:'):]
    conn_name, sep, member_root = rest.partition('/')
    if not conn_name or not sep or not member_root:
        raise _BadSource('malformed bucketfs source (expected bucketfs:CONN/path): %r' % source)
    try:
        connection = exa.get_connection(conn_name)
    except Exception as exc:
        raise _SourceUnreadable('connection %r is not accessible: %s' % (conn_name, exc))
    mount_base = _mount_base(connection.address).rstrip('/')
    archive_path = mount_base + '/' + member_root
    extracted_dir = mount_base + '/' + _strip_archive_extension(member_root)
    # BucketFS auto-extracts an uploaded archive and presents it on the UDF mount
    # as a DIRECTORY at the extension-less path (verified on the live instance),
    # so the extracted-directory case is the common one; a raw tarball file is
    # also supported for buckets that do not auto-extract.
    if os.path.isfile(archive_path):
        return _tarball_reader(archive_path, member_root)
    if os.path.isdir(extracted_dir):
        return _directory_reader(extracted_dir)
    raise _SourceUnreadable(
        'no BucketFS release at %s (tarball) nor %s (auto-extracted)'
        % (archive_path, extracted_dir))


def _strip_archive_extension(name):
    for extension in _ARCHIVE_EXTENSIONS:
        if name.endswith(extension):
            return name[:-len(extension)]
    return name


def _tarball_reader(archive_path, member_root):
    def read(member):
        try:
            with tarfile.open(archive_path, 'r:*') as tar:
                handle = tar.extractfile(member)
                if handle is None:
                    raise _SourceUnreadable('member %r not found in %s' % (member, member_root))
                return handle.read()
        except _ResolveError:
            raise
        except Exception as exc:
            raise _SourceUnreadable('cannot read %r from %s: %s' % (member, member_root, exc))

    return read


def _directory_reader(extracted_dir):
    root = os.path.abspath(extracted_dir)

    def read(member):
        member_path = os.path.abspath(os.path.join(root, *member.split('/')))
        if member_path != root and not member_path.startswith(root + os.sep):
            raise _SourceUnreadable('member %r escapes the release directory' % member)
        try:
            with open(member_path, 'rb') as handle:
                return handle.read()
        except Exception as exc:
            raise _SourceUnreadable('cannot read %r from %s: %s' % (member, extracted_dir, exc))

    return read


def _mount_base(address):
    """Map a BucketFS CONNECTION address to its cluster-local UDF mount path.

    The CONNECTION's address is the native Exasol BucketFS form
    'bucketfs:<service>/<bucket>[/subpath]' (no host, no port -- the same string
    the Exasol docs use), which maps 1:1 to the UDF mount
    '/buckets/<service>/<bucket>[/subpath]'. An address that is already an
    absolute '/buckets/...' mount path is accepted verbatim; anything else
    (an https:// URL, a bare host) is rejected -- the database reaches BucketFS
    natively and never needs a host or port.
    """
    if not address:
        raise _SourceUnreadable('connection address is empty')
    if address.startswith('/'):
        return address.rstrip('/')
    if address.startswith('bucketfs:'):
        rest = address[len('bucketfs:'):].lstrip('/')
        segments = [segment for segment in rest.split('/') if segment]
        if len(segments) < 2:
            raise _SourceUnreadable(
                "malformed BucketFS connection address %r "
                "(expected 'bucketfs:<service>/<bucket>')" % address)
        return '/buckets/' + '/'.join(segments)
    raise _SourceUnreadable(
        "unsupported BucketFS connection address %r "
        "(expected the native 'bucketfs:<service>/<bucket>' form, no host/port)"
        % address)


def _https_reader(source):
    trimmed = source.rstrip('/')
    index_url = source if trimmed.endswith('index.json') else trimmed + '/' + INDEX_MEMBER

    def read(member):
        url = index_url if member == INDEX_MEMBER else _artifact_url(index_url, member)
        try:
            with urllib.request.urlopen(url, timeout=NETWORK_TIMEOUT) as response:
                return response.read()
        except Exception as exc:
            raise _NetworkError('cannot fetch %r: %s' % (url, exc))

    return read


def _artifact_url(index_url, artifact_path):
    base = index_url
    for suffix in (INDEX_MEMBER, 'index.json'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base.rstrip('/') + '/' + artifact_path.lstrip('/')


def _parse_index(raw):
    """Return (modules, library_version) from the index JSON.

    library_version is the optional top-level release stamp (absent in older
    indexes -> None); it is echoed in every emitted row so a caller resolving
    through a mutable `latest` source can record the concrete release.
    """
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise _SourceUnreadable('registry index is not valid JSON: %s' % exc)
    modules = data.get('modules')
    if not isinstance(modules, list):
        raise _SourceUnreadable("registry index has no 'modules' list")
    return modules, data.get('library_version')


def _select_entry(modules, module_name):
    # Module names are matched CASE-INSENSITIVELY: the caller's sugar folds an
    # unquoted name to upper case (Exasol identifier semantics), while registry
    # keys are conventionally lower_snake_case, so an exact match would miss. A
    # module library is expected to hold no two names that differ only by case.
    wanted = module_name.upper()
    matches = [module for module in modules if (module.get('name') or '').upper() == wanted]
    if not matches:
        raise _NotFound('module %r is not listed in the source index' % module_name)
    return max(matches, key=lambda module: module.get('version', 0))


ENTRY_OBJECT_TYPE = 'LUA SCRIPT'
_VERSION_SUFFIX_RE = re.compile(r'_V(\d+)$', re.IGNORECASE)


def _declared_object_errors(module_name, version, declared):
    """Every generational-discipline violation in an entry's declared inventory.

    The rules are manifest._validate_objects's, applied to an index entry
    instead of a module.toml: each declared object names both a type and a name,
    EVERY object carries the module's own _V<N> suffix, and no object is
    declared twice. The CLI settles all three at manifest parse time, so an
    index reaching here with a violation was hand-edited, forked, or produced by
    a publisher that does not use generate_index -- and this is the ONLY gate in
    front of it, because the resolver is the whole of the in-database read path.

    The suffix is not cosmetic: it is what makes module_reclaim.lua's
    inventory-wide ownership boundary safe (ADR-056). An unsuffixed name is
    shared across generations, so an index declaring it for both v1 and v2 lets
    a v1 reclaim drop an object live v2 depends on -- and the declared/derived
    comparison cannot see that, because both sides agree.

    Returns the messages, so a caller collects them with its other findings and
    reports every problem in one error row (matching manifest.py's
    collect-then-raise style).
    """
    expected_suffix = '_V%d' % version if isinstance(version, int) else '_V<N>'
    errors = []
    seen = set()
    for index, obj in enumerate(declared):
        name = obj['name']
        # Checked before anything reads the name, because a hand-edited entry may
        # simply omit it and the checks below would then report a Python
        # AttributeError as the operator-facing diagnostic.
        if not obj['type'] or not name:
            errors.append(
                "index entry for module %r: objects[%d] must declare both 'type' and 'name'"
                % (module_name, index))
            continue
        try:
            parts = qualified_parts(name)
        except ArtifactSyntaxError as exc:
            errors.append(
                'index entry for module %r: objects[%d]: %s' % (module_name, index, exc))
            continue
        suffix = _VERSION_SUFFIX_RE.search(parts[-1])
        if suffix is None or (isinstance(version, int) and int(suffix.group(1)) != version):
            errors.append(
                "index entry for module %r: object %r must carry the module's %s version "
                'suffix; EVERY object an artifact creates carries it, not only the entry '
                'script, so a v1 and a v2 generation coexist without collisions'
                % (module_name, name, expected_suffix))
            continue
        if name in seen:
            errors.append(
                'index entry for module %r: object %r is declared more than once in objects'
                % (module_name, name))
            continue
        seen.add(name)
    return errors


def _duplicate_derived_names(derived):
    """The names the artifact creates more than once, in first-duplicate order.

    Parity with manifest._duplicate_object_names, and compares by NAME alone
    for the same reason: two statements creating one name are a conflict
    whatever types they claim. The declared/derived comparison is blind to this
    -- it compares SETS, and set equality has no multiplicity -- so an artifact
    creating one declared object twice would otherwise pass and be recorded
    twice in the provenance inventory.
    """
    seen = set()
    duplicates = []
    for obj in derived:
        if obj['name'] in seen and obj['name'] not in duplicates:
            duplicates.append(obj['name'])
        seen.add(obj['name'])
    return duplicates


def _declared_objects(entry):
    """The declared object inventory for an index entry, or None.

    An entry carrying its own 'objects' list (schema_version 2) is taken as-is.
    An entry with none -- a schema_version 1 index, whose entries carry no
    'objects' key at all -- falls back to the single ENTRY_OBJECT_TYPE object
    named by the entry's script_name, mirroring
    registry.RegistryIndex.from_dict's schema_version-1 handling exactly, so an
    already-published version-1 index keeps resolving unchanged.
    """
    raw = entry.get('objects')
    if raw is not None:
        return [{'type': obj.get('type'), 'name': obj.get('name')} for obj in raw]
    script_name = entry.get('script_name')
    if not script_name:
        return None
    return [{'type': ENTRY_OBJECT_TYPE, 'name': normalize_object_name(script_name)}]


def _verify_inventory(entry, text):
    """Split the artifact, derive its object inventory, and verify it against the entry.

    Structural parity with manifest.verify_artifact_inventory: the artifact
    must still create exactly one ENTRY_OBJECT_TYPE object named by the entry's
    declared script_name -- that object is the module's entry point -- and the
    derived inventory must agree with the entry's declared objects (the
    schema_version-1 single-object fallback when the entry declares none) in
    BOTH directions. Disagreement in either direction is a hard failure.

    Set agreement alone is not the whole contract, so the rules the CLI settles
    elsewhere are enforced here too, or MODULE_INSTALL would accept bytes
    `preproc module add` rejects: no object is created twice
    (_duplicate_derived_names -- set comparison cannot see multiplicity), and the
    declared inventory is well formed and generational
    (_declared_object_errors -- manifest parsing settles that for the CLI).

    Returns:
        The derived inventory, in file order -- the order an installer creates
        the objects in, and the reverse of the order reclaim drops them in.
    """
    module_name = entry.get('name')
    script_name = entry.get('script_name')
    try:
        statements = split_statements(text)
        derived = derive_objects(statements)
    except ArtifactSyntaxError as exc:
        raise _ArtifactInvalid(
            'cannot parse artifact for module %r: %s' % (module_name, exc))
    if not statements:
        raise _ArtifactInvalid('artifact for module %r carries no statements' % module_name)

    duplicates = _duplicate_derived_names(derived)
    if duplicates:
        raise _ArtifactInvalid(
            'artifact for module %r creates the same object more than once: %s'
            % (module_name, ', '.join(duplicates)))

    entry_name = normalize_object_name(script_name) if script_name else None
    entry_matches = [obj for obj in derived if entry_name and obj['name'] == entry_name]
    errors = []
    if not entry_matches:
        errors.append(
            'artifact for module %r creates no %s named %s for the declared '
            'script_name %r' % (module_name, ENTRY_OBJECT_TYPE, entry_name, script_name))
    elif entry_matches[0]['type'] != ENTRY_OBJECT_TYPE:
        errors.append(
            'artifact object %s for module %r is a %s, but the entry script named '
            'by script_name must be a %s'
            % (entry_name, module_name, entry_matches[0]['type'], ENTRY_OBJECT_TYPE))

    declared = _declared_objects(entry) or []
    errors.extend(_declared_object_errors(module_name, entry.get('version'), declared))
    declared_keys = set((obj['type'], obj['name']) for obj in declared)
    derived_keys = set((obj['type'], obj['name']) for obj in derived)
    errors.extend(
        'artifact for module %r creates %s %s but the index entry does not declare it'
        % (module_name, obj['type'], obj['name'])
        for obj in derived if (obj['type'], obj['name']) not in declared_keys)
    errors.extend(
        'index entry for module %r declares %s %s but the artifact does not create it'
        % (module_name, obj['type'], obj['name'])
        for obj in declared if (obj['type'], obj['name']) not in derived_keys)
    if errors:
        raise _ArtifactInvalid('; '.join(errors))
    return derived


def _encode_objects(objects, module_name):
    """Encode an object inventory in registry.encode_objects's wire format.

    Newline-separated 'TYPE|NAME' lines, one per object, in file order -- the
    exact PREPROC.MODULES.objects VARCHAR form module_reclaim.lua and
    PREPROC_RT.INSTALLED_MODULES read. No escaping is needed: a declared type
    or name containing '\\n', '\\r', or '|' is rejected at manifest parse time,
    so those characters can never occur here (parity with registry.py's
    encode_objects). Returns None for an empty/absent inventory, which the
    caller binds as SQL NULL -- the same nullability the recorded inventory
    already has.

    The width is checked here, at the single point every emitted inventory
    passes through, rather than at each call site. Nothing in the module
    contract caps the object count, so a large subsystem module reaches an
    encoding no column can hold, and this value is bound for an
    OBJECTS_MAX_CHARS column on every path: refusing it here converts what
    would be an opaque data exception inside the UDF -- breaking the
    never-raise contract -- into the ordinary structured error row.

    Raises:
        _ArtifactInvalid: if the encoding exceeds OBJECTS_MAX_CHARS.
    """
    if not objects:
        return None
    encoded = '\n'.join('%s|%s' % (obj['type'], obj['name']) for obj in objects)
    if len(encoded) > OBJECTS_MAX_CHARS:
        raise _ArtifactInvalid(
            'object inventory for module %r encodes to %d characters, over the %d-character '
            'limit of PREPROC.MODULES.objects; a module whose inventory cannot be recorded '
            'could never be retired or reclaimed, so it must not be installed -- split it '
            'into smaller modules or shorten its object names'
            % (module_name, len(encoded), OBJECTS_MAX_CHARS))
    return encoded


def _entry_metadata(entry, library_version):
    """The 12 registry-metadata EMITS columns (name..library_version) for an entry.

    Single source of truth for the metadata column shape: _emit_entry prepends
    the OK status columns and appends the chunk columns; _emit_error emits an
    all-null metadata block by passing an empty entry and no library_version.
    library_version is index-level (not per-entry), so it is threaded in rather
    than read off the entry.
    """
    scope = entry.get('suggested_scope') or {}
    origin = entry.get('source') or {}
    return (
        entry.get('name'), entry.get('version'), entry.get('phase'),
        entry.get('script_name'), entry.get('function'), entry.get('deploy_mode'),
        entry.get('sha256'), scope.get('type'), scope.get('value'),
        origin.get('repo'), origin.get('ref'), library_version)


def _emit_artifact(ctx, entry, library_version, text, objects):
    # Encoded BEFORE the first chunk row, so an over-wide inventory emits the
    # error row alone rather than after a usable artifact body.
    encoded = _encode_objects(objects, entry.get('name'))
    total = max(1, (len(text) + CHUNK_CHARS - 1) // CHUNK_CHARS)
    for index in range(total):
        _emit_entry(ctx, entry, library_version, index, total,
                    text[index * CHUNK_CHARS:(index + 1) * CHUNK_CHARS], encoded)


def _emit_entry(ctx, entry, library_version, chunk_index, chunk_count, chunk, objects=None):
    ctx.emit('OK', None, None, *_entry_metadata(entry, library_version),
             chunk_index, chunk_count, chunk, objects)


def _emit_error(ctx, kind, message):
    ctx.emit('ERROR', kind, message[:ERROR_MESSAGE_MAX], *_entry_metadata({}, None),
             0, 0, None, None)
/
