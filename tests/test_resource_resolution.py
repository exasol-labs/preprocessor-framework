"""Resolution of the sql/ resource root, and what it says when it fails.

Offline unit tests over ``preproc.resources``. The installed-artifact end of this
contract — that a built wheel really carries the tree and really resolves it — is
``tests/test_packaged_resources.py``; these cover the precedence rule and the
diagnostics, which no artifact build is needed to pin.
"""

import pytest

from preproc import resources


def test_packaged_root_wins_when_both_candidates_exist(monkeypatch, tmp_path):
    """An installed copy must read its own resources, never a neighbouring tree.

    The checkout candidate sits three levels above the module, which for an
    installed copy is ``<prefix>/lib/pythonX.Y`` — a directory owned by no
    package, where an unrelated ``sql/`` must not win.
    """
    packaged = tmp_path / "site-packages" / "preproc" / "sql"
    checkout = tmp_path / "checkout" / "sql"
    packaged.mkdir(parents=True)
    checkout.mkdir(parents=True)
    monkeypatch.setattr(resources, "_SQL_ROOT_CANDIDATES", (packaged, checkout))

    assert resources._resolve_sql_root() == packaged


def test_checkout_root_is_used_when_nothing_is_packaged(monkeypatch, tmp_path):
    """A source checkout has no packaged copy, so the repository tree must win."""
    packaged = tmp_path / "site-packages" / "preproc" / "sql"
    checkout = tmp_path / "checkout" / "sql"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(resources, "_SQL_ROOT_CANDIDATES", (packaged, checkout))

    assert resources._resolve_sql_root() == checkout


def test_resolution_never_raises_when_neither_candidate_exists(monkeypatch, tmp_path):
    """Resolution runs at import, so a packaging gap must not break importing.

    ``preproc.cli`` imports the install and bundle modules to build its parser,
    so raising here would take down every verb — including those that read no SQL
    at all — and any consumer importing the package for its pure logic.
    """
    packaged = tmp_path / "site-packages" / "preproc" / "sql"
    checkout = tmp_path / "checkout" / "sql"
    monkeypatch.setattr(resources, "_SQL_ROOT_CANDIDATES", (packaged, checkout))

    assert resources._resolve_sql_root() == packaged


def test_missing_install_ddl_names_both_candidate_roots(monkeypatch, tmp_path):
    """An unresolved root must be reported as one, naming where it looked.

    A bare "No .sql files found in <prefix>/lib/python3.12/sql/install" reads as
    a missing file; it is the whole distribution's resources that are absent, and
    the message has to say so or the next reader repeats the diagnosis.
    """
    monkeypatch.setattr(resources, "SQL_ROOT", tmp_path / "nowhere")
    empty_install_dir = tmp_path / "nowhere" / "install"

    with pytest.raises(FileNotFoundError) as exc:
        resources.require_install_ddl(empty_install_dir)

    message = str(exc.value)
    assert str(empty_install_dir) in message
    assert str(resources._PACKAGE_SQL_ROOT) in message
    assert str(resources._REPO_SQL_ROOT) in message
    assert "reinstall" in message


def test_missing_single_resource_reports_the_resolved_root(tmp_path):
    """One absent file inside a resolved tree is not a packaging failure.

    The packaging remedy would be wrong advice here, so it is withheld: the tree
    was found, this one source was not.
    """
    absent = tmp_path / "master.lua"

    with pytest.raises(FileNotFoundError) as exc:
        resources.require_resource(absent, "master.lua")

    message = str(exc.value)
    assert "master.lua not found at" in message
    assert str(resources.SQL_ROOT) in message
    assert "reinstall" not in message


def test_install_ddl_is_returned_in_ascending_filename_order(tmp_path):
    """Filename order is the apply order, so discovery must sort, not glob-order."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    for name in ("70_last.sql", "10_first.sql", "20_second.sql"):
        (install_dir / name).write_text("SELECT 1;\n", encoding="utf-8")
    (install_dir / "notes.txt").write_text("ignored\n", encoding="utf-8")

    found = resources.require_install_ddl(install_dir)

    assert [path.name for path in found] == ["10_first.sql", "20_second.sql", "70_last.sql"]
