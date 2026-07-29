"""Resolution of the SQL, Lua, and UDF sources the framework deploys.

Single source of truth for where those sources live. ``install.py`` and
``bundle.py`` each used to carry their own copy of the resolution plus ~25 path
constants; both copies were wrong in the same way, which is how the defect below
survived.

Resolution
----------
The sources live at the repository root under ``sql/install|lua|udf|demo`` — the
layout ``specs/install/project-bootstrap`` mandates — which is outside the
importable package. Two locations are therefore searched, in this order:

1. ``<package>/sql`` — where the wheel build relocates the tree
   (``[tool.hatch.build.targets.wheel.force-include]``). Present in every
   installed copy.
2. ``<repo>/sql`` — three levels above this module in a source checkout, which
   also covers an editable install.

The packaged location is preferred rather than merely tried, because three levels
above an installed module is ``<prefix>/lib/pythonX.Y``: a directory owned by no
package, where an unrelated ``sql/`` would otherwise be picked up. In a source
checkout the packaged candidate never exists, so the repository wins there.

Missing resources
-----------------
Resolution never raises at import — a packaging gap must not make the whole CLI,
including verbs that touch no SQL, unimportable. Instead every read goes through
``require_resource`` or ``require_install_ddl``, which name both candidates when
neither exists. Before this, a distribution shipping no ``sql/`` tree produced an
empty glob and a bare "No .sql files found in
<prefix>/lib/python3.12/sql/install", which read as a missing file rather than as
the packaging defect it was.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_SQL_ROOT = Path(__file__).parent / "sql"
_REPO_SQL_ROOT = Path(__file__).parent.parent.parent / "sql"
_SQL_ROOT_CANDIDATES = (_PACKAGE_SQL_ROOT, _REPO_SQL_ROOT)

_MISSING_ROOT_HINT = (
    "The framework's sql/ resource tree was found in neither candidate "
    "location: {package} (where an installed distribution carries it) nor "
    "{repo} (a source checkout). An installed copy without preproc/sql/ was "
    "built without its resources — reinstall from a distribution that ships "
    "them."
)


def _resolve_sql_root() -> Path:
    """Return the first candidate root that exists, else the first candidate.

    Falling back to a non-existent path rather than raising keeps the failure at
    the point of use, where ``require_resource`` and ``require_install_ddl`` can
    explain it.
    """
    for candidate in _SQL_ROOT_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return _SQL_ROOT_CANDIDATES[0]


SQL_ROOT = _resolve_sql_root()

SQL_INSTALL_DIR = SQL_ROOT / "install"
_LUA_DIR = SQL_ROOT / "lua"
_UDF_DIR = SQL_ROOT / "udf"

MASTER_LUA_PATH = _LUA_DIR / "master.lua"
DEMO_PING_LUA_PATH = _LUA_DIR / "demo_ping_v1.lua"
DEMO_TRANSLATE_LUA_PATH = _LUA_DIR / "demo_translate_v1.lua"
DEMO_AUDIT_LUA_PATH = _LUA_DIR / "demo_audit_v1.lua"
DEMO_SQLGLOT_UDF_PATH = _UDF_DIR / "demo_sqlglot_transpile_v1.sql"
REFRESH_LUA_PATH = _LUA_DIR / "refresh.lua"
STATUS_LUA_PATH = _LUA_DIR / "status.lua"
REFRESH_CORE_LUA_PATH = _LUA_DIR / "refresh_core.lua"
RULE_ADD_LUA_PATH = _LUA_DIR / "rule_add.lua"
RULE_DROP_LUA_PATH = _LUA_DIR / "rule_drop.lua"
RULE_ENABLE_LUA_PATH = _LUA_DIR / "rule_enable.lua"
RULE_DISABLE_LUA_PATH = _LUA_DIR / "rule_disable.lua"
RULE_LIST_LUA_PATH = _LUA_DIR / "rule_list.lua"
SET_MODE_LUA_PATH = _LUA_DIR / "set_mode.lua"
SET_SELF_DISABLE_LUA_PATH = _LUA_DIR / "set_self_disable.lua"
SET_SYS_GROUPING_LUA_PATH = _LUA_DIR / "set_sys_grouping.lua"
CONTROL_SUGAR_LUA_PATH = _LUA_DIR / "control_sugar_v1.lua"
RULE_ENSURE_LUA_PATH = _LUA_DIR / "rule_ensure.lua"
MODULE_REGISTER_LUA_PATH = _LUA_DIR / "module_register.lua"
MODULE_RETIRE_LUA_PATH = _LUA_DIR / "module_retire.lua"
MODULE_RECLAIM_LUA_PATH = _LUA_DIR / "module_reclaim.lua"
MODULE_RESOLVER_UDF_PATH = _UDF_DIR / "module_resolver_v1.sql"
MODULE_INSTALL_LUA_PATH = _LUA_DIR / "module_install.lua"
MODULE_CATALOG_LUA_PATH = _LUA_DIR / "module_catalog.lua"


def _resolution_hint() -> str:
    """Explain where the resource tree resolved to, or that it resolved nowhere."""
    if SQL_ROOT.is_dir():
        return f" The resource tree resolved to {SQL_ROOT}."
    return " " + _MISSING_ROOT_HINT.format(package=_PACKAGE_SQL_ROOT, repo=_REPO_SQL_ROOT)


def require_resource(path: Path, description: str) -> Path:
    """Return path, raising FileNotFoundError if that resource does not exist.

    Used for pre-flight checks as well as reads, so a deploy that needs several
    sources can verify them all before it sends the first statement.
    """
    if path.exists():
        return path
    raise FileNotFoundError(f"{description} not found at {path}.{_resolution_hint()}")


def require_install_ddl(install_dir: Path) -> list[Path]:
    """Return the install DDL files in ascending filename order.

    Raises FileNotFoundError if the directory holds no ``.sql`` file. Ordering is
    part of the contract: the numbered filenames are the apply order.
    """
    sql_files = sorted(install_dir.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"No .sql files found in {install_dir}.{_resolution_hint()}")
    return sql_files
