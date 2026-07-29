"""Report how an installed preprocessor-framework resolves its SQL resources.

Run as a script by ``tests/test_packaged_resources.py`` inside a subprocess whose
``PYTHONPATH`` points at a throwaway install target, with a working directory
outside the repository. Emits one JSON object on stdout describing what the
installed copy resolved, and writes the offline deploy bundle to the path given
as ``argv[1]``.

Every fact is gathered without raising, so a distribution missing its resources
produces a report the caller can assert against rather than a traceback.
"""

import json
import sys
from pathlib import Path

import preproc
from preproc import bundle, install

_RESOURCE_SUFFIXES = ("_LUA_PATH", "_UDF_PATH")


def _resource_paths(module):
    """Every Lua/UDF source path constant the module exposes, by constant name."""
    return {
        name: str(getattr(module, name))
        for name in dir(module)
        if name.endswith(_RESOURCE_SUFFIXES)
    }


def _missing(paths):
    """The names of the path constants that do not point at an existing file."""
    return sorted(name for name, value in paths.items() if not Path(value).exists())


def _report(bundle_output):
    """Collect the resolution facts and attempt a real bundle generation."""
    install_dir = install._SQL_INSTALL_DIR
    report = {
        "package_file": preproc.__file__,
        "install_dir": str(install_dir),
        "install_sql": sorted(path.name for path in install_dir.glob("*.sql")),
        "install_missing": _missing(_resource_paths(install)),
        "bundle_install_dir": str(bundle._SQL_INSTALL_DIR),
        "bundle_missing": _missing(_resource_paths(bundle)),
        "bundle_error": None,
    }
    try:
        bundle.run_bundle(bundle_output)
    except Exception as exc:
        report["bundle_error"] = f"{type(exc).__name__}: {exc}"
    return report


if __name__ == "__main__":
    print(json.dumps(_report(Path(sys.argv[1]))))
