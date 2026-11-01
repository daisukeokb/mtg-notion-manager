"""Phase 3H-R3: structural static guard for the CI security invariants.

Used by tests/test_ci_static_guard.py (no network, no Docker). The guard does
not try to understand all of GitHub Actions, YAML or shell. It accepts a
deliberately small, canonical subset and reports everything else as a
violation (fail closed), so an invariant cannot be hidden behind a YAML anchor,
a duplicate key, a quoted key, `no` instead of `false`, an extra job, a
job-level setting, a Unicode line separator or an unusual shell spelling.

Invariants checked:

* workflows: ci.yml is the only workflow, printable ASCII only, and has exactly
  the reviewed jobs, keys and triggers; read-only permissions; no secrets or
  dynamic expressions outside `concurrency`; every action is an allowlisted
  repository at its reviewed commit SHA; every job starts with a checkout that
  sets `persist-credentials: false` and is immediately followed by the
  credential check; every setup-python is immediately followed by the
  hash-verified pip bootstrap; every line of every `run` is one of the
  reviewed command lines (REVIEWED_RUN_LINES, deny by default);
* the files that run on the runner or define Layer B (REVIEWED_FILES) have
  their reviewed content (sha256), and scripts/*.sh is exactly the reviewed
  set. This pin is the authority for those files: shell is too expressive to
  be verified completely by static analysis;
* in addition, as a second line of defence with precise reasons: the
  Dockerfile has a single FROM pinned to the reviewed digest, no parser
  directives, no instruction flags (--from, --mount, ...), no exec-form RUN, no
  ADD/ARG, only reviewed ENV names, and ends as `USER 10001:10001`; the Layer B
  runner has exactly one `docker run` with the isolation flags present exactly
  once and no other docker commands than `info` and a reviewed `build`; every
  pip invocation in the workflow, the Dockerfile and scripts/*.sh is either
  hash-locked or offline/local with only reviewed options, and dynamic shell
  (eval, `sh -c`, `python -c`, a command name built from `$`) is a violation.

Changing any of these on purpose requires updating this guard in the same
change, which makes the change visible in review.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from pathlib import Path
from typing import Any, NamedTuple

# --- reviewed values -----------------------------------------------------------

#: Allowed external actions: repository -> (reviewed commit SHA, release tag).
ALLOWED_ACTIONS = {
    "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4.4.0"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0"),
}
EXPECTED_WORKFLOWS = ("ci.yml",)
EXPECTED_JOBS = ("test", "lint", "type-check", "generation-workspace")
TOP_LEVEL_KEYS = {"name", "on", "permissions", "concurrency", "jobs"}
TRIGGERS = {"push", "pull_request", "workflow_dispatch"}
JOB_KEYS = {"runs-on", "timeout-minutes", "steps"}
STEP_KEYS = {"name", "uses", "with", "run"}
WITH_KEYS = {
    "actions/checkout": {"persist-credentials"},
    "actions/setup-python": {"python-version", "cache", "cache-dependency-path"},
}
CREDENTIAL_CHECK_STEP = ("Verify no persisted Git credentials",
                         "python3 -I scripts/check_no_persisted_credentials.py")
PIP_BOOTSTRAP_STEP = ("Install hash-verified pip", "bash scripts/bootstrap_locked_pip.sh")
BASE_IMAGE = ("python:3.9-slim-bookworm@sha256:"
              "03dee9ce00747bb5390e7b4ad00307b4dbc7790f7da77cc3581f93cfa99a6363")
DOCKERFILE_INSTRUCTIONS = {"FROM", "ENV", "RUN", "WORKDIR", "COPY", "USER", "CMD"}
DOCKERFILE_ENV = {"PYTHONDONTWRITEBYTECODE", "PIP_NO_CACHE_DIR", "PIP_DISABLE_PIP_VERSION_CHECK"}
DOCKERFILE_USER = "10001:10001"
HASHED_LOCKS = {"requirements/ci-lock.txt", "requirements/ci-pip-lock.txt"}

#: `docker run` options that must appear exactly as given (Layer B).
REQUIRED_RUN_OPTIONS = (
    ("--network", "none"),
    ("--read-only",),
    ("--tmpfs", "/tmp:rw,nosuid,nodev,size=1g"),
    ("--tmpfs", "/work:rw,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=0700"),
    ("--cap-drop", "ALL"),
    ("--security-opt", "no-new-privileges:true"),
    ("--user", "10001:10001"),
    ("--pids-limit", "512"),
)
#: All `docker run` options the runner may use (anything else is a violation).
ALLOWED_RUN_OPTIONS = {"--rm", "-e"} | {opt[0] for opt in REQUIRED_RUN_OPTIONS}
#: `docker build` options of the runner: option -> required value (None: any).
BUILD_OPTIONS = {"--quiet": None, "--platform": "linux/amd64", "-f": "", "-t": "$IMAGE"}

#: Every line of every workflow `run` must be one of these (deny by default).
REVIEWED_RUN_LINES = frozenset({
    CREDENTIAL_CHECK_STEP[1],
    PIP_BOOTSTRAP_STEP[1],
    "bash scripts/run_isolated_tests.sh",
    "bash scripts/check_isolation_red.sh",
    "python -m pip --version",
    "python -m pip install --require-hashes --no-deps --only-binary=:all: "
    "-r requirements/ci-lock.txt",
    "python -m pip install --no-deps --no-build-isolation --no-index -e .",
    "python -m pip install -e tools/generation_workspace --no-deps --no-build-isolation "
    "--no-index",
    "python -m pip check",
    "ruff check .",
    "mypy src",
    "bash scripts/run_isolated_tests.sh pytest tools/generation_workspace/tests",
    "bash scripts/run_isolated_tests.sh pytest tools/generation_workspace/tests --collect-only -q",
    "ruff check tools/generation_workspace",
    "mypy tools/generation_workspace/generation_workspace",
    "bash scripts/run_isolated_tests.sh generation-workspace --help",
    "bash scripts/run_isolated_tests.sh python -m generation_workspace --help",
})

#: Reviewed content (sha256) of the files that run on the runner or define the
#: Layer B image and runner. After a reviewed change, update the digest here
#: (the violation prints the new one).
REVIEWED_FILES = {
    "ci/isolated-tests/Dockerfile":
        "b632c45ef358237584adc49599b1d0899dc6d7a0412612e4644cdac2ed792089",
    "scripts/bootstrap_locked_pip.sh":
        "2f2cdbd0174464ec977f8e68f3e6e75bddd6ea8505f377492dd5439fbc6c37e1",
    "scripts/check_isolation_red.sh":
        "6c3c70d7ead534400629b25649111733e3d0b7d7d4633935dcb2b9dff3eedffb",
    "scripts/check_no_persisted_credentials.py":
        "d4686ea630df77dceee4ad4c755dd572a112b0b92e6c1d1e235d422d8ef71ffb",
    "scripts/run_isolated_tests.sh":
        "6f89fd3f354b648e317d8cf6de37f61e71a4d5e4b2b01d1ef289bcd26f81f9d5",
}
REVIEWED_SCRIPTS = ("bootstrap_locked_pip.sh", "check_isolation_red.sh", "run_isolated_tests.sh")


class Violation(NamedTuple):
    code: str
    where: str
    detail: str


class GuardError(Exception):
    """Input outside the supported subset; reported as a violation."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- strict YAML subset -----------------------------------------------------------


class Scalar(NamedTuple):
    text: str
    style: str  # "plain", "double", "literal" or "empty"
    comment: str
    line: int


_KEY = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_-]*):(?: (.*))?$")
# Plain scalars may not start with a YAML indicator (anchors, aliases, tags,
# flow collections, quotes, block scalars, directives, complex keys, ...).
_PLAIN_START_FORBIDDEN = set("&*!|>'\"%@`{}[],?#:-<=~")


# Characters that some parser could read as a line break or that hide text:
# C0/C1 controls other than LF, DEL, NEL, the Unicode line/paragraph
# separators, bidirectional controls and the BOM.
_HIDDEN_CHARACTER = re.compile(
    "[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e"
    "\u2066-\u2069\ufeff]"
)


def check_characters(text: str, ascii_only: bool) -> None:
    """Raise GuardError for hidden characters (or any non-ASCII if ascii_only)."""
    match = _HIDDEN_CHARACTER.search(text)
    if ascii_only and match is None:
        match = re.search(r"[^\n\x20-\x7e]", text)
    if match is not None:
        line = text.count("\n", 0, match.start()) + 1
        raise GuardError("UNSUPPORTED_CHARACTER",
                         f"line {line}: character U+{ord(match.group()):04X} is not supported")


class _Yaml:
    """Block mappings, block sequences, plain/double-quoted scalars and `|`."""

    def __init__(self, text: str) -> None:
        if "\t" in text:
            raise GuardError("YAML_TAB", "tab characters are not supported")
        if "\r" in text or text.startswith("﻿"):
            raise GuardError("YAML_ENCODING", "CR line endings or a BOM are not supported")
        self.lines = text.split("\n")
        self.i = 0
        for number, line in enumerate(self.lines, 1):
            if line.rstrip() in ("---", "...") or line.startswith("%"):
                raise GuardError("YAML_DOCUMENT", f"line {number}: document markers/directives")

    def parse(self) -> Any:
        self._skip()
        if self._eof():
            raise GuardError("YAML_EMPTY", "empty document")
        indent, content = self._current()
        if indent != 0 or content.startswith("-"):
            raise GuardError("YAML_STRUCTURE", "the document must be a top-level mapping")
        document = self._mapping(0)
        self._skip()
        if not self._eof():
            raise GuardError("YAML_STRUCTURE", f"line {self.i + 1}: unexpected content")
        return document

    def _eof(self) -> bool:
        return self.i >= len(self.lines)

    def _current(self) -> tuple[int, str]:
        line = self.lines[self.i]
        stripped = line.lstrip(" ")
        return len(line) - len(stripped), stripped.rstrip(" ")

    def _skip(self) -> None:
        while not self._eof():
            content = self.lines[self.i].strip(" ")
            if content and not content.startswith("#"):
                return
            self.i += 1

    def _node(self, indent: int) -> Any:
        _, content = self._current()
        if content == "-" or content.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            self._skip()
            if self._eof():
                return result
            current, content = self._current()
            if current < indent:
                return result
            line = self.i + 1
            if current > indent:
                raise GuardError("YAML_INDENT", f"line {line}: unexpected indentation")
            match = _KEY.match(content)
            if not match:
                raise GuardError("YAML_UNSUPPORTED_KEY", f"line {line}: unsupported key syntax")
            key, rest = match.group(1), match.group(2)
            if key in result:
                raise GuardError("YAML_DUPLICATE_KEY", f"line {line}: duplicate key {key!r}")
            self.i += 1
            result[key] = self._value(rest, indent, line)

    def _value(self, rest: str | None, indent: int, line: int) -> Any:
        rest = (rest or "").strip(" ")
        if rest == "" or rest.startswith("#"):
            self._skip()
            if not self._eof():
                child, content = self._current()
                if child > indent:
                    return self._node(child)
                if child == indent and (content == "-" or content.startswith("- ")):
                    raise GuardError("YAML_INDENT", f"line {self.i + 1}: unindented sequence")
            return Scalar("", "empty", rest[1:].strip(), line)
        if rest == "|":
            return self._literal(indent, line)
        return _scalar(rest, line)

    def _literal(self, indent: int, line: int) -> Scalar:
        content: list[str] = []
        block_indent = None
        while not self._eof():
            raw = self.lines[self.i]
            if raw.strip(" ") == "":
                content.append("")
                self.i += 1
                continue
            current = len(raw) - len(raw.lstrip(" "))
            if current <= indent:
                break
            if block_indent is None:
                block_indent = current
            if current < block_indent:
                raise GuardError("YAML_INDENT", f"line {self.i + 1}: literal block indentation")
            content.append(raw[block_indent:])
            self.i += 1
        while content and content[-1] == "":
            content.pop()
        if not content:
            raise GuardError("YAML_EMPTY", f"line {line}: empty literal block")
        return Scalar("\n".join(content) + "\n", "literal", "", line)

    def _sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            self._skip()
            if self._eof():
                return items
            current, content = self._current()
            if current < indent:
                return items
            line = self.i + 1
            if current > indent or not (content == "-" or content.startswith("- ")):
                raise GuardError("YAML_STRUCTURE", f"line {line}: expected a sequence item")
            if content == "-":
                raise GuardError("YAML_STRUCTURE", f"line {line}: empty sequence item")
            body = content[2:]
            column = indent + 2 + len(body) - len(body.lstrip(" "))
            body = body.lstrip(" ")
            if _KEY.match(body):
                # A mapping item: re-read the line as a mapping key at its column.
                self.lines[self.i] = " " * column + body
                items.append(self._mapping(column))
            elif body.startswith("-"):
                raise GuardError("YAML_STRUCTURE", f"line {line}: nested inline sequence")
            else:
                self.i += 1
                items.append(_scalar(body, line))


def _scalar(text: str, line: int) -> Scalar:
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1 or "\\" in text[1:end]:
            raise GuardError("YAML_UNSUPPORTED_SCALAR", f"line {line}: unsupported quoted string")
        tail = text[end + 1 :]
        if tail and not re.match(r" +#", tail):
            raise GuardError("YAML_UNSUPPORTED_SCALAR", f"line {line}: text after quoted string")
        return Scalar(text[1:end], "double", tail.strip(" ")[1:].strip(), line)
    if text[0] in _PLAIN_START_FORBIDDEN:
        raise GuardError("YAML_UNSUPPORTED_SCALAR", f"line {line}: unsupported scalar indicator")
    value, _, comment = text.partition(" #")
    value = value.rstrip(" ")
    if ": " in value or value.endswith(":"):
        raise GuardError("YAML_UNSUPPORTED_SCALAR", f"line {line}: ambiguous plain scalar")
    return Scalar(value, "plain", comment.strip(), line)


def parse_yaml(text: str) -> Any:
    return _Yaml(text).parse()


# --- workflow ---------------------------------------------------------------------


def _scalars(node: Any, path: str) -> list[tuple[str, Scalar]]:
    if isinstance(node, Scalar):
        return [(path, node)]
    if isinstance(node, dict):
        return [item for key, value in node.items() for item in _scalars(value, f"{path}.{key}")]
    return [item for index, value in enumerate(node)
            for item in _scalars(value, f"{path}[{index}]")]


def _is(node: Any, text: str, style: str = "plain") -> bool:
    return isinstance(node, Scalar) and node.text == text and node.style == style


def check_workflow(
    text: str, where: str = "ci.yml", image_python: str | None = None
) -> list[Violation]:
    try:
        # YAML 1.1 parsers treat NEL, U+2028 and U+2029 as line breaks; the
        # subset parser does not, so they must not appear at all.
        check_characters(text, ascii_only=True)
        doc = parse_yaml(text)
    except GuardError as exc:
        return [Violation(exc.code, where, exc.detail)]
    found: list[Violation] = []

    def bad(code: str, path: str, detail: str) -> None:
        found.append(Violation(code, f"{where}:{path}", detail))

    if set(doc) != TOP_LEVEL_KEYS:
        bad("WORKFLOW_KEYS", "/", f"top-level keys {sorted(doc)} != {sorted(TOP_LEVEL_KEYS)}")
    triggers = doc.get("on")
    if not isinstance(triggers, dict) or not set(triggers) <= TRIGGERS:
        bad("WORKFLOW_TRIGGERS", "on", "only push, pull_request and workflow_dispatch are allowed")
    permissions = doc.get("permissions")
    if not (isinstance(permissions, dict) and list(permissions) == ["contents"]
            and _is(permissions["contents"], "read")):
        bad("PERMISSIONS_NOT_READ_ONLY", "permissions", "must be exactly `contents: read`")

    for path, scalar in _scalars(doc, ""):
        if re.search(r"secrets|github\.token|GITHUB_TOKEN", scalar.text, re.IGNORECASE):
            bad("SECRET_REFERENCE", path, "secrets and tokens must not be referenced")
        if "${{" in scalar.text and not path.startswith(".concurrency."):
            bad("DYNAMIC_EXPRESSION", path, "expressions are only allowed in concurrency")

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or tuple(jobs) != EXPECTED_JOBS:
        bad("WORKFLOW_JOBS", "jobs", f"jobs must be exactly {list(EXPECTED_JOBS)}")
        jobs = jobs if isinstance(jobs, dict) else {}
    for job_name, job in jobs.items():
        if not isinstance(job, dict) or set(job) != JOB_KEYS:
            bad("JOB_KEYS", f"jobs.{job_name}", f"job keys must be exactly {sorted(JOB_KEYS)}")
            continue
        if not _is(job["runs-on"], "ubuntu-latest"):
            bad("RUNNER", f"jobs.{job_name}.runs-on", "only ubuntu-latest is reviewed")
        steps = job["steps"]
        if not isinstance(steps, list) or not steps:
            bad("STEPS", f"jobs.{job_name}.steps", "steps must be a non-empty list")
            continue
        first = steps[0]
        if not (isinstance(first, dict) and isinstance(first.get("uses"), Scalar)
                and first["uses"].text.partition("@")[0] == "actions/checkout"):
            # Nothing may run (or change $GITHUB_ENV / $GITHUB_PATH) before the
            # checkout and its credential check.
            bad("CHECKOUT_NOT_FIRST", f"jobs.{job_name}.steps[0]", "the first step must be "
                "actions/checkout")
        found += _check_steps(steps, f"{where}:jobs.{job_name}", image_python)
    return found


def _check_steps(steps: list[Any], where: str, image_python: str | None) -> list[Violation]:
    found: list[Violation] = []
    bootstrapped = False
    for index, step in enumerate(steps):
        path = f"{where}.steps[{index}]"
        if not isinstance(step, dict) or not set(step) <= STEP_KEYS or (
            ("uses" in step) == ("run" in step)
        ):
            found.append(Violation("STEP_KEYS", path, "a step has name + (uses [+ with] | run)"))
            continue
        following = steps[index + 1] if index + 1 < len(steps) else {}
        if "run" in step:
            run = step["run"]
            if not isinstance(run, Scalar) or run.style == "empty":
                found.append(Violation("STEP_KEYS", path, "run must be a string"))
                continue
            for line in run.text.split("\n"):
                if line and line not in REVIEWED_RUN_LINES:
                    found.append(Violation("RUN_NOT_REVIEWED", path,
                                           f"unreviewed command line {line[:60]!r}"))
            if _is(run, PIP_BOOTSTRAP_STEP[1]):
                bootstrapped = True
            elif not bootstrapped and re.search(r"pip", run.text, re.IGNORECASE):
                found.append(Violation("PIP_BEFORE_BOOTSTRAP", path, "pip runs before the "
                                       "hash-verified pip bootstrap"))
            found += check_shell(run.text, path)
            continue
        found += _check_action(step, following, path, image_python)
    return found


def _check_action(
    step: dict, following: Any, path: str, image_python: str | None
) -> list[Violation]:
    found: list[Violation] = []
    uses = step["uses"]
    if not isinstance(uses, Scalar) or uses.style != "plain":
        return [Violation("ACTION_REFERENCE", path, "uses must be a plain scalar")]
    repo, _, sha = uses.text.partition("@")
    if repo not in ALLOWED_ACTIONS:
        return [Violation("ACTION_NOT_ALLOWED", path, f"unreviewed action {repo!r}")]
    pinned_sha, version = ALLOWED_ACTIONS[repo]
    if sha != pinned_sha:
        found.append(Violation("ACTION_NOT_PINNED", path, f"{repo} must be pinned to {pinned_sha}"))
    if uses.comment != version:
        found.append(Violation("ACTION_VERSION_COMMENT", path, f"expected `# {version}`"))
    with_ = step.get("with", {})
    if not isinstance(with_, dict) or not set(with_) <= WITH_KEYS[repo]:
        found.append(Violation("ACTION_INPUTS", path,
                               f"inputs must be within {sorted(WITH_KEYS[repo])}"))
        return found
    if repo == "actions/checkout":
        if not _is(with_.get("persist-credentials"), "false"):
            found.append(Violation("CHECKOUT_PERSISTS_CREDENTIALS", path,
                                   "persist-credentials must be the plain scalar `false`"))
        if not _is_step(following, CREDENTIAL_CHECK_STEP):
            found.append(Violation("CHECKOUT_NOT_VERIFIED", path,
                                   "the next step must be the credential check"))
    else:
        python = with_.get("python-version")
        if not isinstance(python, Scalar) or python.style != "double" or (
            image_python is not None and python.text != image_python
        ):
            found.append(Violation("RUNNER_PYTHON", path,
                                   "python-version must be the image Python"))
        if not _is_step(following, PIP_BOOTSTRAP_STEP):
            found.append(Violation("PIP_NOT_BOOTSTRAPPED", path,
                                   "the next step must be the hash-verified pip bootstrap"))
    return found


def _is_step(step: Any, expected: tuple[str, str]) -> bool:
    return (isinstance(step, dict) and set(step) == {"name", "run"}
            and _is(step["name"], expected[0]) and _is(step["run"], expected[1]))


# --- shell: pip invocations ---------------------------------------------------------

_OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
_PIP = re.compile(r"pip(\d+(\.\d+)?)?")
_PYTHON = re.compile(r"python(\d+(\.\d+)?)?")
_FORBIDDEN_INSTALLERS = {"pipx", "easy_install", "uv", "poetry", "pdm", "conda", "mamba",
                         "micromamba", "pipenv", "ensurepip"}
_PREFIXES = {"sudo", "env", "exec", "command", "nice", "time", "nohup",
             # reserved words that may precede a command
             "{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
_DYNAMIC_COMMANDS = {"eval", "source", ".", "alias", "function"}
_PIP_READ_ONLY = {"check", "list", "freeze", "show"}
_PIP_GLOBAL_OPTIONS = {"-q", "--quiet", "--disable-pip-version-check"}
_INSTALL_FLAGS = {"--require-hashes", "--no-deps", "--only-binary=:all:", "--no-build-isolation",
                  "--no-index", "--force-reinstall", "-q", "--quiet"}
_INSTALL_VALUE_OPTIONS = {"-r", "-e", "--find-links"}
_LOCAL_PATH = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*")


def _strip_comment(line: str) -> str:
    """Cut a shell comment: `#` outside quotes at the start of a word only."""
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            quote = None if char == "'" else quote
        elif char == "\\":
            index += 1
        elif quote == '"':
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " ;&|()<>"):
            return line[:index]
        index += 1
    return line


def _lines(text: str) -> list[list[list[str]]]:
    """Split shell text into logical lines of simple commands (lists of words)."""
    joined = text.replace("\\\n", " ")
    lines: list[list[list[str]]] = []
    for line in joined.split("\n"):
        # shlex would also start a comment in the middle of a word (`a#b`),
        # which the shell does not; comments are cut here instead.
        # A file descriptor number written before a redirection (`2>&1`) is
        # not an argument.
        line = re.sub(r"(^|(?<=[\s;&|()]))\d+(?=[<>])", "", _strip_comment(line))
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise GuardError("SHELL_UNPARSABLE", f"cannot tokenize shell line: {exc}") from None
        segments: list[list[str]] = []
        current: list[str] = []
        redirect_target = False
        for token in tokens:
            if redirect_target:
                redirect_target = False  # the file of a redirection, not a command
                continue
            if set(token) <= set("<>&") and set(token) & set("<>"):
                redirect_target = True
            elif token == ")" and not segments and current:
                current = []  # a `case` pattern: `pattern) command ;;`
            elif token in _OPERATORS or set(token) <= set("&|;()<>"):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
        lines.append(segments)
    return lines


def _segments(text: str) -> list[list[str]]:
    return [segment for line in _lines(text) for segment in line]


def _command_words(words: list[str]) -> list[str]:
    index = 0
    while index < len(words) and (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index]) or words[index] in _PREFIXES
    ):
        index += 1
    return words[index:]


def _pip_arguments(words: list[str]) -> list[str] | None:
    """Return the pip arguments if `words` runs pip, else None."""
    if not words:
        return None
    program = os.path.basename(words[0])
    if _PIP.fullmatch(program):
        return words[1:]
    if _PYTHON.fullmatch(program):
        rest = words[1:]
        while rest and rest[0] in ("-I", "-E", "-s", "-B", "-u"):
            rest = rest[1:]
        if rest[:2] == ["-m", "pip"] or rest[:1] == ["-mpip"]:
            return rest[2:] if rest[0] == "-m" else rest[1:]
        if rest and _PIP.fullmatch(os.path.basename(rest[0])):
            return rest[1:]  # a pip entry point or wheel run as a script
    return None


def _mentions_pip(words: list[str]) -> bool:
    return any(re.search(r"pip", word, re.IGNORECASE) for word in words)


#: Non-Python package managers whose `install` is out of this guard's scope.
_OTHER_INSTALLERS = {"apt-get"}


def _is_dynamic(text: str) -> bool:
    return "$" in text or "`" in text


def _runs_inline_code(words: list[str]) -> bool:
    """`sh -c ...`, `bash -ec ...`, `python -c ...`, `python -Ic ...`."""
    for word in words[1:]:
        if not word.startswith("-") or word.startswith("--"):
            return False
        for char in word[1:]:
            if char == "c":
                return True
            if char in "mXWo":  # the rest of the word is the option's value
                break
    return False


def _substitutions(text: str) -> list[str]:
    """Bodies of the `$(...)` and backtick command substitutions in shell text."""
    bodies = re.findall(r"`([^`]*)`", text)
    for match in re.finditer(r"\$\((?!\()", text):
        depth, index = 1, match.end()
        while index < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            index += 1
        bodies.append(text[match.end() : index - 1 if depth == 0 else index])
    return bodies


def check_shell(text: str, where: str, _depth: int = 0) -> list[Violation]:
    if _depth > 3:
        return [Violation("SHELL_DYNAMIC", where, "nested command substitution")]
    try:
        check_characters(text, ascii_only=False)
        lines = _lines(text)
    except GuardError as exc:
        return [Violation(exc.code, where, exc.detail)]
    found: list[Violation] = []
    for body in _substitutions(text):
        found += check_shell(body, where, _depth + 1)
    for segments in lines:
        line_mentions_pip = any(_mentions_pip(words) for words in segments)
        for raw_words in segments:
            words = _command_words(raw_words)
            if not words:
                continue
            program = os.path.basename(words[0])
            # "$@" runs a function's own arguments, which are checked where it is called.
            if (program in _DYNAMIC_COMMANDS or (_is_dynamic(words[0]) and words[0] != "$@")
                    or ((program in _SHELLS or _PYTHON.fullmatch(program))
                        and _runs_inline_code(words))):
                found.append(Violation("SHELL_DYNAMIC", where,
                                       f"{program[:20]}: code the guard cannot inspect"))
                continue
            if program == "trap" and len(words) > 1:
                found += check_shell(words[1], where, _depth + 1)
                continue
            module = None
            if _PYTHON.fullmatch(program):
                rest = [word for word in words[1:] if word not in ("-I", "-E", "-s", "-B", "-u")]
                if rest[:1] == ["-m"]:
                    module = rest[1] if len(rest) > 1 else None
                elif rest[:1] and rest[0].startswith("-m"):
                    module = rest[0][2:]
                target = module if module is not None else (rest[0] if rest else "")
                if _is_dynamic(target) and os.path.basename(target) != "pip":
                    found.append(Violation("SHELL_DYNAMIC", where,
                                           "python target built from an expansion"))
                    continue
            if program in _FORBIDDEN_INSTALLERS or module in _FORBIDDEN_INSTALLERS:
                found.append(Violation("FORBIDDEN_INSTALLER", where,
                                       f"{module or program} is not allowed"))
                continue
            arguments = _pip_arguments(words)
            if arguments is not None:
                found += _check_pip(arguments, where)
            elif "install" in words and program not in _OTHER_INSTALLERS and (
                line_mentions_pip or words[0].startswith("$")
            ):
                # e.g. `$(which pip) install x`, `$PIP install x`, `env -i pip install x`
                found.append(Violation("UNRECOGNIZED_PIP_INVOCATION", where,
                                       "install in a form the guard cannot verify"))
    return found


def _check_pip(arguments: list[str], where: str) -> list[Violation]:
    index = 0
    while index < len(arguments) and arguments[index] in _PIP_GLOBAL_OPTIONS:
        index += 1
    if index == len(arguments):
        return [Violation("PIP_NO_COMMAND", where, "pip without a subcommand")]
    command, options = arguments[index], arguments[index + 1 :]
    if command == "--version" and not options:
        return []
    if command in _PIP_READ_ONLY and not options:
        return []
    if command != "install":
        return [Violation("PIP_FORBIDDEN_COMMAND", where, f"pip {command} is not allowed")]
    flags: set[str] = set()
    values: list[tuple[str, str]] = []
    i = 0
    while i < len(options):
        option = options[i]
        if option in _INSTALL_FLAGS:
            flags.add(option)
        elif option in _INSTALL_VALUE_OPTIONS and i + 1 < len(options):
            values.append((option, options[i + 1]))
            i += 1
        elif option.startswith("-"):
            return [Violation("PIP_UNREVIEWED_OPTION", where, f"pip install {option}")]
        else:
            return [Violation("PIP_UNLOCKED_REQUIREMENT", where,
                              f"positional requirement {option}")]
        i += 1
    requirement_files = {value for option, value in values if option == "-r"}
    editables = [value for option, value in values if option == "-e"]
    find_links = [value for option, value in values if option == "--find-links"]
    for option, value in values:
        # -r / -e: literal local paths only (an expansion could name a VCS URL).
        # --find-links may be an expansion, but only next to a hash-locked -r.
        literal = _LOCAL_PATH.fullmatch(value) and "://" not in value and "@" not in value
        dynamic_find_links = (option == "--find-links" and re.fullmatch(r"\$[a-z_]+", value)
                              and requirement_files and not editables)
        if not (literal or dynamic_find_links):
            return [Violation("PIP_REMOTE_SOURCE", where, f"{option} must be a local path")]
    if "--no-deps" not in flags:
        return [Violation("PIP_RESOLVES_DEPENDENCIES", where, "--no-deps is required")]
    if find_links and "--no-index" not in flags:
        return [Violation("PIP_REMOTE_SOURCE", where, "--find-links requires --no-index")]
    if requirement_files:
        if editables or not requirement_files <= HASHED_LOCKS or not {
            "--require-hashes", "--only-binary=:all:"
        } <= flags:
            return [Violation("PIP_NOT_HASH_LOCKED", where, "lock installs need --require-hashes "
                              "--only-binary=:all: and a reviewed lock file")]
        return []
    if editables and not find_links and {"--no-index", "--no-build-isolation"} <= flags:
        return []
    return [Violation("PIP_NOT_HASH_LOCKED", where, "install is neither hash-locked nor offline")]


# --- Dockerfile and Layer B runner -------------------------------------------------


def check_dockerfile(text: str, where: str = "Dockerfile") -> list[Violation]:
    try:
        check_characters(text, ascii_only=False)
    except GuardError as exc:
        return [Violation(exc.code, where, exc.detail)]
    joined = text.replace("\\\n", " ")
    found: list[Violation] = []
    froms = []
    instructions: list[tuple[str, str]] = []
    for number, line in enumerate(joined.split("\n"), 1):
        stripped = line.strip()
        here = f"{where}:{number}"
        if stripped.startswith("#") and re.match(r"#\s*[A-Za-z]+\s*=", stripped):
            # `# syntax=` pulls an unpinned BuildKit frontend; `# escape=` and
            # `# check=` change how the file is read.
            found.append(Violation("DOCKERFILE_DIRECTIVE", here, "parser directives are not "
                                   "allowed"))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        instruction, _, rest = stripped.partition(" ")
        rest = rest.strip()
        if instruction != instruction.upper() or instruction not in DOCKERFILE_INSTRUCTIONS:
            found.append(Violation("DOCKERFILE_INSTRUCTION", here,
                                   f"unreviewed instruction {instruction}"))
            continue
        instructions.append((instruction, rest))
        if rest.startswith("--"):
            # --from (another image), --mount (bind another image), --network,
            # --security, --platform, --chown, ...
            found.append(Violation("DOCKERFILE_FLAG", here, f"{instruction} flags are not "
                                   "allowed"))
            continue
        if instruction == "FROM":
            froms.append(rest)
        elif instruction == "RUN":
            if rest.startswith("["):
                found.append(Violation("DOCKERFILE_EXEC_FORM", here, "exec-form RUN is not "
                                       "analysed"))
            else:
                found += check_shell(rest, here)
        elif instruction == "ENV":
            names = [item.partition("=")[0] for item in rest.split()]
            if not all("=" in item for item in rest.split()) or not set(names) <= DOCKERFILE_ENV:
                found.append(Violation("DOCKERFILE_ENV", here, f"ENV names must be within "
                                       f"{sorted(DOCKERFILE_ENV)}"))
    if froms != [BASE_IMAGE]:
        found.append(Violation("BASE_IMAGE_NOT_PINNED", where,
                               "exactly one FROM pinned to the reviewed digest is required"))
    users = [rest for instruction, rest in instructions if instruction == "USER"]
    last_run = max((i for i, (name, _) in enumerate(instructions) if name == "RUN"), default=-1)
    last_user = max((i for i, (name, _) in enumerate(instructions) if name == "USER"),
                    default=-1)
    if users != [DOCKERFILE_USER] or last_user < last_run:
        found.append(Violation("DOCKERFILE_USER", where, f"exactly one USER {DOCKERFILE_USER} "
                               "after the last RUN is required"))
    return found


def _check_build(words: list[str], where: str) -> list[Violation]:
    options, i = words[2:], 0
    while i < len(options):
        option = options[i]
        if not option.startswith("-"):
            if i != len(options) - 1:
                return [Violation("LAYER_B_BUILD", where, "the build context must be last")]
            return []
        if option not in BUILD_OPTIONS:
            return [Violation("LAYER_B_BUILD", where, f"unreviewed docker build option {option}")]
        expected = BUILD_OPTIONS[option]
        if expected is not None:
            value = options[i + 1] if i + 1 < len(options) else None
            if value is None or (expected and value != expected):
                return [Violation("LAYER_B_BUILD", where, f"docker build {option} must be "
                                  f"{expected or 'given'}")]
            i += 1
        i += 1
    return [Violation("LAYER_B_BUILD", where, "docker build without a context")]


def check_runner(text: str, where: str = "run_isolated_tests.sh") -> list[Violation]:
    try:
        check_characters(text, ascii_only=False)
        segments = [_command_words(words) for words in _segments(text)]
    except GuardError as exc:
        return [Violation(exc.code, where, exc.detail)]
    found: list[Violation] = []
    runs = []
    for words in segments:
        if not words or os.path.basename(words[0]) != "docker":
            continue
        if words[1:2] == ["run"]:
            runs.append(words)
        elif words[1:2] == ["build"]:
            found += _check_build(words, where)
        elif words[1:] != ["info"]:
            found.append(Violation("LAYER_B_DOCKER", where,
                                   f"unreviewed docker command {' '.join(words[1:2])!r}"))
    if len(runs) != 1:
        return found + [Violation("LAYER_B_RUN", where, "exactly one `docker run` is required")]
    words = runs[0][2:]
    image_index = next((i for i, w in enumerate(words) if w == '$IMAGE'), None)
    if image_index is None:
        return found + [Violation("LAYER_B_RUN", where, "the image must be $IMAGE")]
    options = words[:image_index]
    seen: list[tuple[str, ...]] = []
    i = 0
    while i < len(options):
        option = options[i]
        name = option.split("=", 1)[0]
        if name not in ALLOWED_RUN_OPTIONS or "=" in option:
            found.append(Violation("LAYER_B_OPTION", where,
                                   f"unreviewed docker run option {option}"))
            i += 1
            continue
        if option in ("--rm", "--read-only"):
            seen.append((option,))
            i += 1
            continue
        value = options[i + 1] if i + 1 < len(options) else ""
        if option == "-e":
            # NAME=literal only: `-e NAME` would inherit the host value.
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*=[^$`\\]*", value):
                found.append(Violation("LAYER_B_ENV", where, "only literal dummy -e values"))
        else:
            seen.append((option, value))
        i += 2
    for required in REQUIRED_RUN_OPTIONS:
        if required not in seen:
            found.append(Violation("LAYER_B_MISSING", where, " ".join(required)))
    for option in {item[0] for item in seen}:
        values = [item for item in seen if item[0] == option]
        allowed = 2 if option == "--tmpfs" else 1
        if len(values) > allowed:
            found.append(Violation("LAYER_B_DUPLICATE", where, f"{option} given more than once"))
    return found


# --- repository --------------------------------------------------------------------


def check_repository(root: Path) -> list[Violation]:
    found: list[Violation] = []
    workflows_dir = root / ".github" / "workflows"
    workflows = sorted(p.name for p in workflows_dir.iterdir()) if workflows_dir.is_dir() else []
    if tuple(workflows) != EXPECTED_WORKFLOWS:
        found.append(Violation("WORKFLOW_FILES", ".github/workflows",
                               f"expected {list(EXPECTED_WORKFLOWS)}, found {workflows}"))
    if any((root / ".github").rglob("action.y*ml")):
        found.append(Violation("LOCAL_ACTION", ".github", "local actions are not reviewed"))
    dockerfile = root / "ci" / "isolated-tests" / "Dockerfile"
    runner = root / "scripts" / "run_isolated_tests.sh"
    for required in (workflows_dir / "ci.yml", *(root / name for name in REVIEWED_FILES)):
        if not required.is_file():
            found.append(Violation("MISSING_FILE", str(required.relative_to(root)), "missing"))
    if found:
        return found
    scripts = tuple(sorted(p.name for p in (root / "scripts").glob("*.sh")))
    if scripts != REVIEWED_SCRIPTS:
        found.append(Violation("SCRIPT_FILES", "scripts", f"expected {list(REVIEWED_SCRIPTS)}, "
                               f"found {list(scripts)}"))
    for name, digest in REVIEWED_FILES.items():
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != digest:
            found.append(Violation("REVIEWED_FILE_CHANGED", name,
                                   f"sha256 {actual} is not the reviewed content"))
    docker_text = dockerfile.read_text(encoding="utf-8")
    image_python = re.search(r"\(Python (3\.\d+\.\d+)\)", docker_text)
    found += check_workflow((workflows_dir / "ci.yml").read_text(encoding="utf-8"),
                            image_python=image_python.group(1) if image_python else "")
    found += check_dockerfile(docker_text)
    found += check_runner(runner.read_text(encoding="utf-8"))
    for script in sorted((root / "scripts").glob("*.sh")):
        found += check_shell(script.read_text(encoding="utf-8"), f"scripts/{script.name}")
    return found
