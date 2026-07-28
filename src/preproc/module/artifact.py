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
