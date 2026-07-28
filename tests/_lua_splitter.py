"""Drive the in-database Lua statement splitter from the test suite.

``sql/lua/module_install.lua`` carries the Lua half of the shared artifact
grammar; ``src/preproc/module/artifact.py`` carries the canonical Python half.
The Lua half is extracted VERBATIM from between the sentinel comments in that
file, so every probe below runs exactly the source the database runs — the
tested splitter and the deployed splitter cannot drift apart.

Two probes wrap that one source:

``db_probe_body``
    the body of a ``RETURNS TABLE`` Lua script that reads one staged corpus case
    back from ``CORPUS_TABLE`` in chunks, reassembles it, splits it, and returns
    the statements as rows. This is how the conformance scenario reaches the
    in-database splitter, and it reassembles before splitting exactly as
    ``MODULE_INSTALL`` does. Statements are returned chunked because a single
    corpus statement is longer than one ``VARCHAR(2000000)`` column.

``local_probe_program``
    a standalone Lua program that splits the artifact text on its stdin and
    writes a length-framed result to its stdout, so cross-language agreement is
    still provable where no Exasol instance is configured.

Both probes report the same three outcomes — a statement list, a statement
count, or the splitter's error message — because the splitter returns ``nil``
plus a message rather than raising.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

SPLITTER_BEGIN = "-- BEGIN ARTIFACT SPLITTER"
SPLITTER_END = "-- END ARTIFACT SPLITTER"

CORPUS_TABLE = "PREPROC_RT.LUA_SPLITTER_CORPUS"
PROBE_SCRIPT = "PREPROC.LUA_SPLITTER_PROBE"

PROBE_CHUNK_CHARS = 1000000

_MODULE_INSTALL_LUA = Path(__file__).resolve().parent.parent / "sql" / "lua" / "module_install.lua"

_LUA_CANDIDATES = ("lua", "lua5.1", "luajit")


class ProbeResult(NamedTuple):
    """One probe run: the statements the Lua splitter yielded, or its error message."""

    statements: list[str] | None
    error: str | None


def splitter_source() -> str:
    """Return the Lua splitter extracted verbatim from ``module_install.lua``."""
    text = _MODULE_INSTALL_LUA.read_text(encoding="utf-8")
    if SPLITTER_BEGIN not in text or SPLITTER_END not in text:
        raise AssertionError(
            f"{_MODULE_INSTALL_LUA} carries no splitter delimited by "
            f"{SPLITTER_BEGIN!r} ... {SPLITTER_END!r}"
        )
    return text.split(SPLITTER_BEGIN, 1)[1].split(SPLITTER_END, 1)[0]


def db_probe_body() -> str:
    """Return the Lua body of the in-database conformance probe script."""
    return splitter_source() + _DB_PROBE_DRIVER.format(
        corpus_table=CORPUS_TABLE, chunk_chars=PROBE_CHUNK_CHARS
    )


def local_probe_program() -> str:
    """Return a standalone Lua program that splits stdin and frames the result."""
    return splitter_source() + _LOCAL_PROBE_DRIVER


def lua_interpreter() -> str | None:
    """Return a usable Lua interpreter, preferring ``PREPROC_TEST_LUA``, or None."""
    configured = os.environ.get("PREPROC_TEST_LUA")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    for candidate in _LUA_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def write_local_probe(directory: Path) -> Path:
    """Write the standalone probe program into ``directory`` and return its path."""
    path = directory / "lua_splitter_probe.lua"
    path.write_text(local_probe_program(), encoding="utf-8")
    return path


def run_local_probe(interpreter: str, program: Path, text: str) -> ProbeResult:
    """Split ``text`` with the local Lua interpreter and decode the framed result."""
    completed = subprocess.run(
        [interpreter, str(program)],
        input=text.encode("utf-8"),
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"the Lua probe exited {completed.returncode}: "
            + completed.stderr.decode("utf-8", "replace")
        )
    return _decode_framed(completed.stdout)


def decode_probe_rows(rows) -> ProbeResult:
    """Decode the in-database probe's ``(kind, statement_index, chunk_index, payload)`` rows."""
    error = None
    count = None
    chunks: dict[int, dict[int, str]] = {}
    for kind, statement_index, chunk_index, payload in rows:
        if kind == "ERROR":
            error = payload
        elif kind == "COUNT":
            count = int(payload)
        else:
            chunks.setdefault(int(statement_index), {})[int(chunk_index)] = payload
    if error is not None:
        return ProbeResult(None, error)
    if count is None:
        raise AssertionError("the in-database probe returned neither a COUNT nor an ERROR row")
    statements = [
        "".join(part for _, part in sorted(chunks.get(index, {}).items())) for index in range(count)
    ]
    return ProbeResult(statements, None)


def _decode_framed(data: bytes) -> ProbeResult:
    header, _, rest = data.partition(b"\n")
    payloads = []
    while rest:
        length_line, _, rest = rest.partition(b"\n")
        length = int(length_line.split(b" ", 1)[1])
        payloads.append(rest[:length].decode("utf-8"))
        rest = rest[length:]
    if header == b"ERROR":
        return ProbeResult(None, payloads[0])
    count = int(header.split(b" ", 1)[1])
    if count != len(payloads):
        raise AssertionError(f"the Lua probe framed {len(payloads)} statements but counted {count}")
    return ProbeResult(payloads, None)


# The probe drivers. Each is appended to the extracted splitter source, so both
# run `split_statements` from `module_install.lua` itself.

_DB_PROBE_DRIVER = """
-- Conformance probe (generated by tests/_lua_splitter.py): reassemble the staged
-- corpus case named by the `case_name` parameter, split it, and return one COUNT
-- row plus the statements chunked across rows — or one ERROR row carrying the
-- splitter's message.
local PROBE_CHUNK_CHARS = {chunk_chars}

local staged_ok, staged = pquery(
    "SELECT chunk_text FROM {corpus_table} WHERE case_name = :n ORDER BY chunk_index",
    {{n = case_name}})
if not staged_ok then
    error("reading the staged corpus case failed: " .. tostring(staged.error_message))
end
local parts = {{}}
for index, row in ipairs(staged) do
    parts[index] = row[1]
end

local statements, split_error = split_statements(table.concat(parts))
local rows = {{}}
if statements == nil then
    rows[1] = {{"ERROR", 0, 0, split_error}}
else
    rows[1] = {{"COUNT", 0, 0, tostring(#statements)}}
    for index, statement in ipairs(statements) do
        local offset = 1
        local chunk_index = 0
        repeat
            rows[#rows + 1] = {{"STATEMENT", index - 1, chunk_index,
                string.sub(statement, offset, offset + PROBE_CHUNK_CHARS - 1)}}
            offset = offset + PROBE_CHUNK_CHARS
            chunk_index = chunk_index + 1
        until offset > #statement
    end
end
exit(rows, "kind VARCHAR(16), statement_index DECIMAL(9,0), "
    .. "chunk_index DECIMAL(9,0), payload VARCHAR(2000000)")
"""

_LOCAL_PROBE_DRIVER = """
-- Conformance probe (generated by tests/_lua_splitter.py): split the artifact
-- text on stdin and write `COUNT <n>` or `ERROR`, then one `LEN <bytes>` header
-- plus payload per statement (or the error message). Length framing keeps
-- statements carrying newlines unambiguous.
local text = io.read("*a") or ""
local statements, split_error = split_statements(text)
local out = {}
if statements == nil then
    out[1] = "ERROR\\n"
    out[2] = "LEN " .. tostring(#split_error) .. "\\n"
    out[3] = split_error
else
    out[1] = "COUNT " .. tostring(#statements) .. "\\n"
    for _, statement in ipairs(statements) do
        out[#out + 1] = "LEN " .. tostring(#statement) .. "\\n"
        out[#out + 1] = statement
    end
end
io.write(table.concat(out))
"""
