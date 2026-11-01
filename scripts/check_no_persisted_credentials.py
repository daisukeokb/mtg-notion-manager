"""Phase 3H-R3: fail if any Git credential is persisted or configured.

Run right after actions/checkout (with `python3 -I`). Instead of grepping
individual config files, the check reads the configuration Git actually uses:

* every entry of the effective config, from every scope Git reads (system,
  global, local, worktree, the compiled-in defaults, and `command` for
  GIT_CONFIG_PARAMETERS / GIT_CONFIG_COUNT), with include.path and
  includeIf.*.path followed;
* every remote's effective fetch and push URLs, after url.*.insteadOf and
  url.*.pushInsteadOf rewriting (`git remote get-url --all [--push]`), and
  no legacy remote files ($GIT_DIR/remotes/, $GIT_DIR/branches/), which
  `git remote` does not list;
* the environment: askpass and ssh command overrides, client certificates,
  proxy variables with credentials, and variables that make `git config` or
  Git read a different configuration or repository than the checkout
  (GIT_CONFIG, GIT_DIR, GIT_COMMON_DIR, GIT_WORK_TREE are not allowed);
* a ~/.netrc or ~/_netrc file (libcurl in Git reads it); never opened.

A finding is a credential helper or any credential.* setting, an
http.*.extraheader / cookiefile / sslkey / sslcert / proxysslkey /
proxysslcert, core.askpass, core.sshCommand, a password setting
(sendemail.*.smtpPass, imap.pass), an include (includes are not allowed at all:
their targets and conditions are not reviewed here), a proxy setting with user
info (with or without a scheme), or a URL with credentials anywhere in a config
key or value (any user info for http/https/ftp/ftps, a password for other
schemes). Safe settings such as `url.https://github.com/.insteadOf
git@github.com:` pass.

Only scopes and key names (with the subsection redacted) are printed, never a
value. Fail closed: if Git cannot be run, exits non-zero, writes to stderr, or
prints output that cannot be parsed, the check fails.

Exit codes: 0 no credentials, 1 credentials found, 2 the check could not run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import NamedTuple

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_ERROR = 2

SCOPES = frozenset({"system", "global", "local", "worktree", "command", "unknown"})
ORIGIN_KINDS = frozenset({"file", "command line", "blob", "standard input"})
#: Environment variables that are findings whenever they are set (non-empty).
ENV_FINDINGS = {
    "GIT_ASKPASS": "askpass program",
    "SSH_ASKPASS": "askpass program",
    "GIT_SSH": "ssh command override",
    "GIT_SSH_COMMAND": "ssh command override",
    "GIT_SSL_KEY": "client certificate",
    "GIT_SSL_CERT": "client certificate",
    # `git config` would read another file than the other Git commands, or the
    # check would inspect another repository than the checkout.
    "GIT_CONFIG": "configuration location override (not allowed)",
    "GIT_DIR": "repository location override (not allowed)",
    "GIT_COMMON_DIR": "repository location override (not allowed)",
    "GIT_WORK_TREE": "repository location override (not allowed)",
}
PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
NETRC_FILES = (".netrc", "_netrc")
_SECTION = re.compile(r"[a-z0-9-]+")
_VARIABLE = re.compile(r"[a-z][a-z0-9-]*")
_URL = re.compile(r"([a-z][a-z0-9+.-]*)://([^/?#\s]*)", re.IGNORECASE)
_USERINFO_SCHEMES = frozenset({"http", "https", "ftp", "ftps"})


class CheckError(Exception):
    """The check could not complete; never contains configuration values."""


class Entry(NamedTuple):
    scope: str
    key: str
    value: str | None


class Finding(NamedTuple):
    scope: str
    key: str  # redacted key name, safe to print
    reason: str


def run_git(*args: str) -> str:
    label = "git " + " ".join(a for a in args[:2] if not a.startswith("-"))
    try:
        proc = subprocess.run(["git", *args], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"cannot run {label}: {type(exc).__name__}") from None
    # stderr is never printed: it could quote a configuration value.
    if proc.returncode != 0:
        raise CheckError(f"{label} exited with status {proc.returncode}")
    if proc.stderr.strip():
        raise CheckError(f"{label} wrote unexpected output to stderr")
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise CheckError(f"{label} printed non-UTF-8 output") from None


def parse_config_list(raw: str) -> list[Entry]:
    """Parse `git config --list --show-scope --show-origin --includes -z`."""
    if raw == "":
        return []
    fields = raw.split("\0")
    if fields[-1] != "" or (len(fields) - 1) % 3 != 0:
        raise CheckError("git config output is truncated or malformed")
    entries = []
    for i in range(0, len(fields) - 1, 3):
        scope, origin, key_value = fields[i : i + 3]
        if scope not in SCOPES:
            raise CheckError("git config reported an unknown scope")
        if origin.partition(":")[0] not in ORIGIN_KINDS:
            raise CheckError("git config reported an unknown origin")
        key, has_value, value = key_value.partition("\n")
        split_key(key)  # validates the key format
        entries.append(Entry(scope, key, value if has_value else None))
    return entries


def split_key(key: str) -> tuple[str, str | None, str]:
    """Return (section, subsection, variable); section/variable lower-cased."""
    section, dot, rest = key.partition(".")
    subsection, dot2, variable = rest.rpartition(".")
    section, variable = section.lower(), (variable if dot2 else rest).lower()
    if not dot or not _SECTION.fullmatch(section) or not _VARIABLE.fullmatch(variable):
        raise CheckError("git config reported a malformed key")
    return section, (subsection if dot2 else None), variable


def redact(key: str) -> str:
    section, subsection, variable = split_key(key)
    return f"{section}.<redacted>.{variable}" if subsection is not None else f"{section}.{variable}"


def has_url_credentials(text: str) -> bool:
    for match in _URL.finditer(text):
        scheme, authority = match.group(1).lower(), match.group(2)
        if "@" not in authority:
            continue
        userinfo = authority.rpartition("@")[0]
        if scheme in _USERINFO_SCHEMES or ":" in userinfo:
            return True
    return False


def has_proxy_credentials(text: str) -> bool:
    # Git and libcurl accept a proxy with or without a scheme (user:pw@host:port).
    authority = text.strip().partition("://")[2] or text.strip()
    return "@" in re.split(r"[/?#]", authority, maxsplit=1)[0]


def config_findings(entries: list[Entry]) -> list[Finding]:
    findings = []
    for entry in entries:
        section, subsection, variable = split_key(entry.key)
        reason = None
        if section == "credential":
            reason = "credential setting"
        elif section in ("include", "includeif"):
            reason = "config include (not allowed)"
        elif section == "http" and variable in ("extraheader", "cookiefile", "sslkey", "sslcert",
                                                 "proxysslkey", "proxysslcert"):
            reason = f"http {variable}"
        elif section == "core" and variable == "askpass":
            reason = "askpass program"
        elif section == "core" and variable == "sshcommand":
            reason = "ssh command override"
        elif (section, variable) in (("sendemail", "smtppass"), ("imap", "pass")):
            reason = "password setting"
        elif has_url_credentials(subsection or "") or has_url_credentials(entry.value or ""):
            reason = "credentials in URL"
        elif variable == "proxy" and has_proxy_credentials(entry.value or ""):
            reason = "credentials in proxy URL"
        if reason:
            findings.append(Finding(entry.scope, redact(entry.key), reason))
    return findings


def remote_findings() -> list[Finding]:
    names = run_git("remote").splitlines()
    findings = []
    for name in names:
        if not name:
            raise CheckError("git remote printed an empty remote name")
        shown = name if re.fullmatch(r"[A-Za-z0-9._-]+", name) else "<remote>"
        for direction, extra in (("fetch", ()), ("push", ("--push",))):
            urls = run_git("remote", "get-url", *extra, "--all", "--", name).splitlines()
            if not urls:
                raise CheckError(f"git remote get-url returned no {direction} URL")
            if any(has_url_credentials(url) for url in urls):
                findings.append(
                    Finding("effective", f"remote {shown} {direction} URL", "credentials in URL")
                )
    return findings


def legacy_remote_findings() -> list[Finding]:
    # Remotes defined in $GIT_DIR/remotes/ or $GIT_DIR/branches/ are usable by
    # fetch/push but are not listed by `git remote`; they are not allowed.
    findings = []
    for kind in ("remotes", "branches"):
        path = run_git("rev-parse", "--git-path", kind).strip()
        if not path:
            raise CheckError(f"git rev-parse printed no path for {kind}")
        try:
            entries = os.listdir(path)
        except FileNotFoundError:
            continue
        except OSError:
            raise CheckError(f"cannot read the legacy {kind} location") from None
        if entries:
            findings.append(Finding("repository", f"$GIT_DIR/{kind}/",
                                    "legacy remote definition (not allowed)"))
    return findings


def env_findings(environ: dict[str, str]) -> list[Finding]:
    findings = [Finding("environment", name, reason)
                for name, reason in ENV_FINDINGS.items() if environ.get(name)]
    findings += [Finding("environment", name, "credentials in proxy URL")
                 for name in PROXY_ENV if has_proxy_credentials(environ.get(name, ""))]
    return findings


def netrc_findings() -> list[Finding]:
    home = os.path.expanduser("~")
    if home == "~":
        raise CheckError("cannot determine the home directory")
    findings = []
    for name in NETRC_FILES:
        try:
            os.lstat(os.path.join(home, name))  # existence only; never opened
        except FileNotFoundError:
            continue
        except OSError:
            raise CheckError(f"cannot check for ~/{name}") from None
        findings.append(Finding("home", f"~/{name}", "netrc file (not allowed)"))
    return findings


def main() -> int:
    try:
        if run_git("rev-parse", "--is-inside-work-tree").strip() != "true":
            raise CheckError("not inside a Git work tree")
        raw = run_git("config", "--list", "--show-scope", "--show-origin", "--includes", "-z")
        findings = config_findings(parse_config_list(raw))
        findings += env_findings(dict(os.environ))
        findings += netrc_findings()
        legacy = legacy_remote_findings()
        # With legacy remote files present, newer Git warns on stderr while
        # listing remotes; the result is a finding either way, so stop here.
        findings += legacy or remote_findings()
    except CheckError as exc:
        print(f"CREDENTIAL CHECK ERROR: {exc}; the check could not complete (fail closed)",
              file=sys.stderr)
        return EXIT_ERROR
    if findings:
        print("CREDENTIALS FOUND (key names only, values are never printed):", file=sys.stderr)
        for finding in findings:
            print(f"  [{finding.scope}] {finding.key}: {finding.reason}", file=sys.stderr)
        return EXIT_FOUND
    print("ok: no Git credentials in the effective git config (all scopes, includes, "
          "environment), the effective remote URLs or ~/.netrc")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
