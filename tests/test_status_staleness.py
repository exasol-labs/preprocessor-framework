"""Integration tests for the PREPROC.STATUS staleness probe.

These tests run against a live Exasol instance via the ``installed`` fixture and
exercise ``PREPROC.STATUS`` as a DB-side script (``EXECUTE SCRIPT``). STATUS is a
read-only drift detector: it recomputes a fingerprint over the user/role-grant
universe and compares it against the value PREPROC.REFRESH stamped into
PREPROC.SETTINGS, returning ``(is_stale, verdict, last_refresh)``.

Shared-state discipline (the connection and SETTINGS row are session-scoped):

* The ``refreshed`` fixture re-runs REFRESH before each test that needs a known
  CURRENT baseline, so leftover staleness from a prior test cannot leak in.
* Tests that mutate the dictionary (create a role / user) or SETTINGS restore the
  pre-test state in ``finally`` — dropping the created object brings the live
  fingerprint back into agreement with the stored one, and ``test_never_refreshed``
  re-runs REFRESH to re-stamp SETTINGS — so the suite is left CURRENT for whatever
  runs next.
"""

import os
import ssl

import pyexasol
import pytest

_CURRENT = "CURRENT"
_STALE = "STALE"
_NEVER_REFRESHED = "NEVER_REFRESHED"

_STATUS = "EXECUTE SCRIPT PREPROC.STATUS"
_REFRESH = "EXECUTE SCRIPT PREPROC.REFRESH"

_SENTINEL_ROLE = "PREPROC_STATUS_TEST_ROLE"
_SENTINEL_USER = "PREPROC_STATUS_TEST_USER"
_NONADMIN_USER = "PREPROC_STATUS_NOADMIN"
_NONADMIN_PASSWORD = "preproc_status_noadmin_pw_1"


def _status(connection):
    """Run PREPROC.STATUS and return (is_stale, verdict, last_refresh)."""
    row = connection.execute(_STATUS).fetchall()[0]
    return row[0], row[1], row[2]


@pytest.fixture
def refreshed(installed):
    """Run REFRESH so each dependent test starts from a known CURRENT state."""
    installed.execute(_REFRESH)
    return installed


@pytest.mark.integration
def test_status_current_after_refresh(refreshed):
    """Immediately after REFRESH, STATUS reports CURRENT and a non-null last_refresh."""
    is_stale, verdict, last_refresh = _status(refreshed)

    assert is_stale is False
    assert verdict == _CURRENT
    assert last_refresh is not None, "CURRENT status must carry the recorded last_refresh"


@pytest.mark.integration
def test_status_stale_after_role_grant(refreshed):
    """Granting a freshly created role changes EXA_DBA_ROLE_PRIVS, so STATUS is STALE.

    The grant edge (role->grantee) enters the fingerprint's role-grant universe.
    Creating the role alone also adds an edge (the role is owned/granted), so the
    role is created and granted inside the window, then dropped in ``finally`` —
    which removes those edges and restores fingerprint agreement with the stored
    value for the next test.
    """
    try:
        refreshed.execute(f"CREATE ROLE {_SENTINEL_ROLE}")
    except Exception as exc:  # noqa: BLE001 - privilege/availability probe
        pytest.skip(f"requires CREATE ROLE privilege: {exc}")

    try:
        refreshed.execute(f"GRANT {_SENTINEL_ROLE} TO PREPROC_ADMIN")

        is_stale, verdict, _ = _status(refreshed)

        assert is_stale is True
        assert verdict == _STALE
    finally:
        refreshed.execute(f"DROP ROLE {_SENTINEL_ROLE}")


@pytest.mark.integration
def test_status_stale_after_new_user(refreshed):
    """Creating a new user changes EXA_DBA_USERS, so STATUS reports STALE.

    Requires CREATE USER (DBA). Skips cleanly when the test connection lacks it
    rather than failing, so the suite stays green on a restricted connection.
    """
    try:
        refreshed.execute(f'CREATE USER {_SENTINEL_USER} IDENTIFIED BY "{_NONADMIN_PASSWORD}"')
    except Exception as exc:  # noqa: BLE001 - privilege/availability probe
        pytest.skip(f"requires DBA for CREATE USER: {exc}")

    try:
        is_stale, verdict, _ = _status(refreshed)

        assert is_stale is True
        assert verdict == _STALE
    finally:
        refreshed.execute(f"DROP USER {_SENTINEL_USER}")


@pytest.mark.integration
def test_status_read_only(refreshed):
    """STATUS must not modify any table: RESOLUTION count and last_refresh are unchanged.

    A read-only probe could regress into a writer (e.g. by stamping SETTINGS or
    rebuilding RESOLUTION); pinning both the row count and the recorded
    last_refresh across the call catches that.
    """
    count_before = refreshed.execute("SELECT COUNT(*) FROM PREPROC.RESOLUTION").fetchval()
    refresh_before = refreshed.execute("SELECT last_refresh FROM PREPROC.SETTINGS").fetchval()

    _status(refreshed)

    count_after = refreshed.execute("SELECT COUNT(*) FROM PREPROC.RESOLUTION").fetchval()
    refresh_after = refreshed.execute("SELECT last_refresh FROM PREPROC.SETTINGS").fetchval()

    assert count_after == count_before, "STATUS must not change PREPROC.RESOLUTION row count"
    assert refresh_after == refresh_before, "STATUS must not change PREPROC.SETTINGS.last_refresh"


@pytest.mark.integration
def test_status_never_refreshed(installed):
    """With last_refresh NULL, STATUS short-circuits to NEVER_REFRESHED, stale, null time.

    SETTINGS is the only state mutated; REFRESH is re-run in ``finally`` to
    re-stamp it so the shared session leaves a clean CURRENT state behind.
    """
    installed.execute("UPDATE PREPROC.SETTINGS SET last_refresh = NULL, state_fingerprint = NULL")
    try:
        is_stale, verdict, last_refresh = _status(installed)

        assert is_stale is True
        assert verdict == _NEVER_REFRESHED
        assert last_refresh is None
    finally:
        installed.execute(_REFRESH)


@pytest.mark.integration
def test_status_requires_admin(installed):
    """A non-admin caller cannot run STATUS: it fails before returning a verdict.

    STATUS reads EXA_DBA_USERS / EXA_DBA_ROLE_PRIVS, which need SELECT ANY
    DICTIONARY (granted to PREPROC_ADMIN, never PUBLIC). A fresh user holding
    none of those privileges must be rejected. Requires CREATE USER to provision
    the unprivileged caller; skips cleanly when unavailable.
    """
    try:
        installed.execute(f'CREATE USER {_NONADMIN_USER} IDENTIFIED BY "{_NONADMIN_PASSWORD}"')
    except Exception as exc:  # noqa: BLE001 - privilege/availability probe
        pytest.skip(f"requires DBA for CREATE USER: {exc}")

    nonadmin = None
    try:
        installed.execute(f"GRANT CREATE SESSION TO {_NONADMIN_USER}")
        nonadmin = _connect_as(_NONADMIN_USER, _NONADMIN_PASSWORD)

        with pytest.raises(Exception):  # noqa: PT011 - any DB-side rejection is acceptable
            nonadmin.execute(_STATUS).fetchall()
    finally:
        if nonadmin is not None:
            nonadmin.close()
        installed.execute(f"DROP USER {_NONADMIN_USER}")


def _connect_as(user, password):
    """Open a second pyexasol connection as ``user`` against the env-configured DSN."""
    kwargs = {
        "dsn": os.environ["EXASOL_DSN"],
        "user": user,
        "password": password,
    }
    tls_verify = os.environ.get("EXASOL_TLS_VERIFY", "").lower()
    if tls_verify in ("0", "false"):
        kwargs["websocket_sslopt"] = {"cert_reqs": ssl.CERT_NONE}
    return pyexasol.connect(**kwargs)
