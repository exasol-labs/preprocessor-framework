"""Assert ergonomics has been cleanly migrated out of the framework.

cast_shorthand and trailing_comma used to live here as
examples/ergonomics_cast.sql / examples/ergonomics_trailing_comma.sql (plus
their sql/lua/ canonical-source copies and tests/test_ergonomics_*.py files).
They have moved to the community module library,
exasol-labs/preprocessor-library, as the first two modules proving out the
add-module-ecosystem plan (see specs/_plans/add-module-ecosystem/plan.md,
"Migration"). Ergonomics was never part of the core install bundle
(examples-only), so this removal changes no install/bundle statement counts —
these are pure absence checks, static and offline.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ergonomics_cast_removed_from_framework():
    """cast_shorthand's old framework-side files are gone, with no dangling references."""
    assert not (_REPO_ROOT / "examples" / "ergonomics_cast.sql").exists()
    assert not (_REPO_ROOT / "sql" / "lua" / "ergonomics_cast_v1.lua").exists()
    assert not (_REPO_ROOT / "tests" / "test_ergonomics_cast.py").exists()

    install_py = (_REPO_ROOT / "src" / "preproc" / "install.py").read_text(encoding="utf-8")
    assert "ERGONOMICS_CAST_V1" not in install_py

    bundle_py = (_REPO_ROOT / "src" / "preproc" / "bundle.py").read_text(encoding="utf-8")
    assert "ERGONOMICS_CAST_V1" not in bundle_py
    assert "ergonomics_cast" not in bundle_py

    for sql_file in (_REPO_ROOT / "sql" / "install").glob("*.sql"):
        content = sql_file.read_text(encoding="utf-8")
        assert "ERGONOMICS_CAST_V1" not in content, (
            f"ERGONOMICS_CAST_V1 found in core install file {sql_file.name} — "
            "it was migrated to exasol-labs/preprocessor-library and must not "
            "reappear in the core install bundle"
        )

    readme = (_REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    assert "| `ergonomics_cast.sql` |" not in readme, (
        "examples/README.md's Files table must no longer list ergonomics_cast.sql"
    )
    assert "preprocessor-library" in readme


def test_ergonomics_trailing_comma_removed_from_framework():
    """trailing_comma's old framework-side files are gone, with no dangling references."""
    assert not (_REPO_ROOT / "examples" / "ergonomics_trailing_comma.sql").exists()
    assert not (_REPO_ROOT / "sql" / "lua" / "ergonomics_trailing_comma_v1.lua").exists()
    assert not (_REPO_ROOT / "tests" / "test_ergonomics_trailing_comma.py").exists()

    install_py = (_REPO_ROOT / "src" / "preproc" / "install.py").read_text(encoding="utf-8")
    assert "ERGONOMICS_TRAILING_COMMA_V1" not in install_py

    bundle_py = (_REPO_ROOT / "src" / "preproc" / "bundle.py").read_text(encoding="utf-8")
    assert "ERGONOMICS_TRAILING_COMMA_V1" not in bundle_py
    assert "ergonomics_trailing_comma" not in bundle_py

    for sql_file in (_REPO_ROOT / "sql" / "install").glob("*.sql"):
        content = sql_file.read_text(encoding="utf-8")
        assert "ERGONOMICS_TRAILING_COMMA_V1" not in content, (
            f"ERGONOMICS_TRAILING_COMMA_V1 found in core install file {sql_file.name} — "
            "it was migrated to exasol-labs/preprocessor-library and must not "
            "reappear in the core install bundle"
        )

    readme = (_REPO_ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    assert "| `ergonomics_trailing_comma.sql` |" not in readme, (
        "examples/README.md's Files table must no longer list ergonomics_trailing_comma.sql"
    )
    assert "preprocessor-library" in readme
