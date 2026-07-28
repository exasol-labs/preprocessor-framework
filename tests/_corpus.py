"""Load the shared artifact-split conformance corpus.

``tests/fixtures/artifact_split_corpus.json`` pins the expected statement split
for a set of artifact texts. Both statement splitters — the canonical Python one
in ``preproc.module.artifact`` and the in-database Lua one in
``sql/lua/module_install.lua`` — are tested against it, so a divergence between
the CLI path and the in-database path shows up as a failing conformance case
rather than as an install that silently does the wrong thing.

A case's ``text`` (and each expected statement) is either a literal JSON string
or an array of parts, where a part is a literal string or
``{"repeat_unit": <string>, "count": <integer>}``. The array form exists so the
chunk-boundary case can pin a 2,000,000-character artifact without carrying a
2 MB blob in the repository; ``materialize`` is the whole of the decoding rule.
"""

import json
from pathlib import Path
from typing import NamedTuple

CORPUS_PATH = Path(__file__).parent / "fixtures" / "artifact_split_corpus.json"


class CorpusCase(NamedTuple):
    """One conformance case: an artifact text and the statements it must yield."""

    name: str
    description: str
    text: str
    statements: list[str]


class CorpusErrorCase(NamedTuple):
    """One conformance case whose artifact text both splitters must reject."""

    name: str
    description: str
    text: str


def materialize(value: str | list) -> str:
    """Decode a corpus string-or-parts value into its full text."""
    if isinstance(value, str):
        return value
    return "".join(
        part if isinstance(part, str) else part["repeat_unit"] * part["count"] for part in value
    )


def _load() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def load_split_corpus() -> list[CorpusCase]:
    """Return every conformance case with its text and expected statements materialized."""
    return [
        CorpusCase(
            name=case["name"],
            description=case["description"],
            text=materialize(case["text"]),
            statements=[materialize(statement) for statement in case["statements"]],
        )
        for case in _load()["cases"]
    ]


def load_split_error_cases() -> list[CorpusErrorCase]:
    """Return every conformance case that both splitters must reject."""
    return [
        CorpusErrorCase(
            name=case["name"],
            description=case["description"],
            text=materialize(case["text"]),
        )
        for case in _load()["error_cases"]
    ]
