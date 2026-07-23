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
--       names a BucketFS location. Its address is resolved via
--       exa.get_connection(name) and mapped to the cluster-local mount
--       (/buckets/<service>/<bucket>/...); the release is read NATIVELY off that
--       mount. BucketFS AUTO-EXTRACTS an uploaded archive and presents it on the
--       UDF mount as a DIRECTORY at the extension-less path (verified live), so
--       the release is normally read as extracted files; a raw tarball file is
--       also supported via tarfile for buckets that do not auto-extract. Either
--       way a BucketFS read performs NO network egress -- the release is
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
--   declares one, because the catalog never needs the body. An index read
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
--   * The artifact's structure is verified after sha256: it must be exactly ONE
--     CREATE OR REPLACE LUA SCRIPT whose target matches the index entry's declared
--     script_name (parity with manifest.verify_artifact_script_name). A mismatch
--     emits an 'artifact_invalid' error row and no usable artifact text.
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
    chunk_index DECIMAL(9,0),
    chunk_count DECIMAL(9,0),
    artifact_chunk VARCHAR(2000000)
) AS
import hashlib
import json
import os
import re
import tarfile
import urllib.request
from urllib.parse import urlparse

INDEX_MEMBER = 'registry/index.json'
CHUNK_CHARS = 2000000
NETWORK_TIMEOUT = 30
LIST_ALL_MODULES = '*'
ERROR_MESSAGE_MAX = 2000

# Structural parity with manifest.verify_artifact_script_name: the canonical
# artifact is exactly one CREATE OR REPLACE LUA SCRIPT statement. Same pattern as
# the CLI's _CREATE_SCRIPT_RE.
_CREATE_SCRIPT_RE = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+LUA\s+SCRIPT\s+([A-Za-z_][A-Za-z0-9_."]*)',
    re.IGNORECASE)


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
    modules = _parse_index(reader(INDEX_MEMBER))
    if module_name == LIST_ALL_MODULES:
        for entry in modules:
            _emit_entry(ctx, entry, 0, 0, None)
        return
    entry = _select_entry(modules, module_name)
    artifact_path = entry.get('artifact_path')
    declared_sha = entry.get('sha256')
    if artifact_path is None and declared_sha is None:
        _emit_entry(ctx, entry, 0, 0, None)
        return
    artifact_bytes = reader(artifact_path)
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    if not declared_sha or digest != declared_sha.lower():
        raise _ShaMismatch(
            'sha256 mismatch for module %r: index declares %s, artifact hashes to %s'
            % (module_name, declared_sha, digest))
    text = artifact_bytes.decode('utf-8')
    _verify_script_name(entry, text)
    _emit_artifact(ctx, entry, text)


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
    if not address:
        raise _SourceUnreadable('connection address is empty')
    if address.startswith('/'):
        return address
    parsed = urlparse(address)
    segments = [segment for segment in parsed.path.split('/') if segment]
    if not segments:
        raise _SourceUnreadable('cannot derive a BucketFS bucket from connection address %r' % address)
    bucket = segments[0]
    subpath = '/'.join(segments[1:])
    base = '/buckets/%s/%s' % (_find_service(bucket), bucket)
    if subpath:
        base = base + '/' + subpath
    return base


def _find_service(bucket):
    root = '/buckets'
    try:
        services = sorted(os.listdir(root))
    except OSError as exc:
        raise _SourceUnreadable('cannot list %s: %s' % (root, exc))
    for service in services:
        if os.path.isdir(os.path.join(root, service, bucket)):
            return service
    raise _SourceUnreadable('no BucketFS service under %s mounts bucket %r' % (root, bucket))


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
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise _SourceUnreadable('registry index is not valid JSON: %s' % exc)
    modules = data.get('modules')
    if not isinstance(modules, list):
        raise _SourceUnreadable("registry index has no 'modules' list")
    return modules


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


def _verify_script_name(entry, text):
    names = _CREATE_SCRIPT_RE.findall(text)
    if len(names) != 1:
        raise _ArtifactInvalid(
            'artifact for module %r must contain exactly one CREATE OR REPLACE '
            'LUA SCRIPT statement; found %d' % (entry.get('name'), len(names)))
    declared = entry.get('script_name')
    if not declared or names[0].upper() != declared.upper():
        raise _ArtifactInvalid(
            'artifact script name %r does not match the declared script_name %r '
            'for module %r' % (names[0], declared, entry.get('name')))


def _entry_metadata(entry):
    """The 11 registry-metadata EMITS columns (name..source_ref) for an entry.

    Single source of truth for the metadata column shape: _emit_entry prepends
    the OK status columns and appends the chunk columns; _emit_error emits an
    all-null metadata block by passing an empty entry.
    """
    scope = entry.get('suggested_scope') or {}
    origin = entry.get('source') or {}
    return (
        entry.get('name'), entry.get('version'), entry.get('phase'),
        entry.get('script_name'), entry.get('function'), entry.get('deploy_mode'),
        entry.get('sha256'), scope.get('type'), scope.get('value'),
        origin.get('repo'), origin.get('ref'))


def _emit_artifact(ctx, entry, text):
    total = max(1, (len(text) + CHUNK_CHARS - 1) // CHUNK_CHARS)
    for index in range(total):
        _emit_entry(ctx, entry, index, total, text[index * CHUNK_CHARS:(index + 1) * CHUNK_CHARS])


def _emit_entry(ctx, entry, chunk_index, chunk_count, chunk):
    ctx.emit('OK', None, None, *_entry_metadata(entry), chunk_index, chunk_count, chunk)


def _emit_error(ctx, kind, message):
    ctx.emit('ERROR', kind, message[:ERROR_MESSAGE_MAX], *_entry_metadata({}), 0, 0, None)
/
