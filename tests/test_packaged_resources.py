"""The sql/ resource tree must survive being packaged into a distribution.

``sql/install|lua|udf|demo`` live at the repository root, outside
``src/preproc/`` — the layout ``specs/install/project-bootstrap`` mandates — so
nothing carries them into a built distribution automatically. Both headline
verbs, ``preproc install`` and ``preproc bundle``, read that tree, and both are
reached through the ``preproc`` console script, which only exists once the
package is installed. A distribution missing those resources therefore installs
cleanly, advertises both verbs, and fails on first use. That is how one shipped:
every consumer ran from a source checkout on ``PYTHONPATH``, where the
repository-relative fallback always resolved, so no test ever read the resources
back out of an installed artifact.

These tests build the real wheel and sdist, install the wheel alone into a
throwaway directory, and resolve the resources from a working directory outside
the repository, so a checkout cannot stand in for the packaged tree.

Marker: ``packaging``, not ``integration``. No database is involved, and CI's
mandatory unit job runs ``-m "not integration"`` — marking these integration
would move the only gate on installability into the informational database job.
They are the slowest offline tests in the suite (a wheel build plus an install),
so they carry their own marker for ``-m "not packaging"`` local loops.

Nothing is downloaded: the install is ``--offline --no-deps`` from the locally
built wheel, and pyexasol is already present in the interpreter running the
tests.
"""

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from preproc import bundle

pytestmark = pytest.mark.packaging

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_SQL_ROOT = _REPO_ROOT / "sql"
_PROBE = Path(__file__).resolve().parent / "_installed_resource_probe.py"

# The install DDL is applied by filename order and every file matters, so the
# packaged set is compared by name: a partially included tree fails the same way
# an empty one does.
_INSTALL_SQL_NAMES = sorted(path.name for path in (_REPO_SQL_ROOT / "install").glob("*.sql"))

# One representative of every other resource directory install.py and bundle.py
# read, so a mapping that covers only sql/install/ is caught here.
_PACKAGED_RESOURCE_SAMPLES = (
    "preproc/sql/lua/master.lua",
    "preproc/sql/lua/module_install.lua",
    "preproc/sql/udf/module_resolver_v1.sql",
    "preproc/sql/udf/demo_sqlglot_transpile_v1.sql",
    "preproc/sql/demo/seed.sql",
)

_BUILD_TIMEOUT = 300


def _run(command, cwd, env=None):
    """Run a build/install/probe command, failing the test with its full output."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, (
        f"{command} failed with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """The wheel built from the working tree, as a Path to the .whl file."""
    out_dir = tmp_path_factory.mktemp("wheel")
    _run(["uv", "build", "--wheel", "--out-dir", str(out_dir)], cwd=_REPO_ROOT)
    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel in {out_dir}, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def installed_site(built_wheel, tmp_path_factory):
    """A throwaway directory holding the built wheel and nothing else.

    ``--target`` rather than a venv: the interpreter running the tests already
    provides pyexasol, so the install needs no dependency resolution and no
    network, and the developer's environment is left untouched.
    """
    site = tmp_path_factory.mktemp("site")
    _run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            sys.executable,
            "--target",
            str(site),
            str(built_wheel),
        ],
        cwd=_REPO_ROOT,
    )
    return site


@pytest.fixture(scope="module")
def probe_report(installed_site, tmp_path_factory):
    """Resolution facts reported by the installed copy, run outside the repository.

    The probe also generates the deploy bundle, which reads every packaged
    resource, so a resource that resolves but cannot be read is reported here too.
    """
    workdir = tmp_path_factory.mktemp("probe")
    env = {**os.environ, "PYTHONPATH": str(installed_site)}
    result = _run(
        [sys.executable, str(_PROBE), str(workdir / "deploy_all.sql")],
        cwd=workdir,
        env=env,
    )
    return json.loads(result.stdout)


def test_wheel_ships_the_sql_resource_tree(built_wheel):
    """The wheel must carry sql/ under preproc/, or nothing installed can read it."""
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())

    expected_install = {f"preproc/sql/install/{name}" for name in _INSTALL_SQL_NAMES}
    assert expected_install <= names, (
        f"wheel is missing install DDL: {sorted(expected_install - names)}"
    )
    assert set(_PACKAGED_RESOURCE_SAMPLES) <= names, (
        f"wheel is missing script resources: {sorted(set(_PACKAGED_RESOURCE_SAMPLES) - names)}"
    )


def test_installed_copy_resolves_the_packaged_resources(probe_report, installed_site):
    """An installed wheel must resolve its resources from inside the package.

    The repository-relative fallback resolves three levels above the package,
    which is ``<prefix>/lib/pythonX.Y/sql`` for an installed copy — a directory
    that does not exist. Asserting the resolved root lies inside the install
    target is what distinguishes a real fix from a checkout masking one.
    """
    package_file = Path(probe_report["package_file"])
    assert package_file.is_relative_to(installed_site), (
        f"the probe imported {package_file}, not the installed copy under "
        f"{installed_site}; the resolution below would prove nothing"
    )

    for key in ("install_dir", "bundle_install_dir"):
        resolved = Path(probe_report[key])
        assert resolved.is_relative_to(installed_site), (
            f"{key} resolved to {resolved}, outside the install target"
        )
        assert not resolved.is_relative_to(_REPO_ROOT), (
            f"{key} resolved to {resolved}, inside the repository checkout"
        )

    assert probe_report["install_sql"] == _INSTALL_SQL_NAMES, (
        f"installed copy resolved {len(probe_report['install_sql'])} install DDL files, "
        f"repository has {len(_INSTALL_SQL_NAMES)}: "
        f"{probe_report['install_dir']}"
    )
    assert probe_report["install_missing"] == [], (
        f"install.py path constants pointing at nothing: {probe_report['install_missing']}"
    )
    assert probe_report["bundle_missing"] == [], (
        f"bundle.py path constants pointing at nothing: {probe_report['bundle_missing']}"
    )
    assert probe_report["bundle_error"] is None, (
        f"a resource resolved but could not be read: {probe_report['bundle_error']}"
    )


def test_installed_console_script_reproduces_the_repository_bundle(installed_site, tmp_path):
    """The ``preproc`` console script must work as distributed, outside the repository.

    ``preproc bundle`` is the one headline verb needing no database, so it is the
    whole install-to-first-use path exercised offline: the entry point pip
    creates, dispatching into code that reads every packaged resource. The
    generator is deterministic, so byte equality with a bundle generated from the
    repository sources proves the packaged tree is the repository's — complete and
    unaltered — without pinning a statement count ``test_cli_bundle`` already owns.
    """
    console_script = installed_site / "bin" / "preproc"
    assert console_script.is_file(), f"no console script at {console_script}"

    from_install = tmp_path / "from_install.sql"
    env = {**os.environ, "PYTHONPATH": str(installed_site)}
    result = _run(
        [sys.executable, str(console_script), "bundle", "--output", str(from_install)],
        cwd=tmp_path,
        env=env,
    )
    assert result.stdout.startswith("Wrote "), result.stdout

    from_repo = tmp_path / "from_repo.sql"
    bundle.run_bundle(from_repo)
    assert from_install.read_bytes() == from_repo.read_bytes()


def test_sdist_carries_the_sql_tree(tmp_path):
    """The sdist must carry sql/ at its root, so a source install can package it.

    ``pip install "preprocessor-framework @ git+https://..."`` builds from
    source, and a wheel built from an sdist can only include what the sdist
    contains. The tree belongs at the sdist root, mirroring the repository: the
    wheel build is what relocates it under ``preproc/``.
    """
    _run(["uv", "build", "--sdist", "--out-dir", str(tmp_path)], cwd=_REPO_ROOT)
    archives = sorted(tmp_path.glob("*.tar.gz"))
    assert len(archives) == 1, f"expected exactly one sdist, got {archives}"

    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()

    root = archives[0].name.removesuffix(".tar.gz")
    prefix = f"{root}/sql/"
    relative = {name.removeprefix(f"{root}/") for name in names if name.startswith(prefix)}

    expected = {f"sql/install/{name}" for name in _INSTALL_SQL_NAMES}
    expected |= {sample.removeprefix("preproc/") for sample in _PACKAGED_RESOURCE_SAMPLES}
    assert expected <= relative, f"sdist is missing: {sorted(expected - relative)}"
