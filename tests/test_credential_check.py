"""Phase 3H-R3: negative and positive fixtures for the Git credential check.

Each case builds a throwaway repository with dummy credentials (never a real
one) and runs scripts/check_no_persisted_credentials.py in a fully controlled
environment: nothing is inherited from the caller, the global config is a
temporary file and the system config is disabled unless a case sets it.

A negative case passes only if the check reports the expected finding (exit 1
plus the expected scope, key and reason) or the expected execution error
(exit 2). A crash of the fixture or an unrelated failure does not count, and no
case may print the dummy secret.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_no_persisted_credentials.py"
GIT = shutil.which("git")
SECRET = "dummy-secret-for-r3-tests"  # appears in fixtures, must never be printed

pytestmark = pytest.mark.skipif(GIT is None, reason="git is required for these fixtures")


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("credential_check", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


class Fixture:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.repo = base / "repo"
        self.global_config = base / "global.gitconfig"
        self.global_config.write_text("", encoding="utf-8")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(base / "home"),
            "LANG": "C",
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        (base / "home").mkdir()
        self.git("init", "-q", str(self.repo), cwd=base)
        self.git("remote", "add", "origin", "https://example.invalid/owner/repo.git")

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run([GIT, *args], cwd=cwd or self.repo, env=self.env, check=True,
                       capture_output=True)

    def run(self, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-I", str(CHECKER)], cwd=cwd or self.repo, env=self.env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert SECRET not in result.stdout + result.stderr, "the dummy secret was printed"
        assert "Traceback" not in result.stderr, result.stderr
        return result


@pytest.fixture()
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def assert_found(result: subprocess.CompletedProcess[str], *lines: str) -> None:
    assert result.returncode == checker.EXIT_FOUND, (result.returncode, result.stderr)
    assert "CREDENTIALS FOUND" in result.stderr
    for line in lines:
        assert line in result.stderr, result.stderr


def assert_error(result: subprocess.CompletedProcess[str], reason: str) -> None:
    assert result.returncode == checker.EXIT_ERROR, (result.returncode, result.stderr)
    assert "CREDENTIAL CHECK ERROR" in result.stderr
    assert reason in result.stderr, result.stderr
    assert "CREDENTIALS FOUND" not in result.stderr


# --- positive fixtures ------------------------------------------------------------


def test_clean_repository_passes(fx: Fixture) -> None:
    result = fx.run()
    assert result.returncode == checker.EXIT_OK, result.stderr
    assert result.stdout.startswith("ok: no Git credentials")


@pytest.mark.parametrize(
    "key,value",
    [
        ("url.https://example.invalid/.insteadOf", "git@example.invalid:"),
        ("url.https://example.invalid/.pushInsteadOf", "ssh://git@example.invalid/"),
        ("remote.mirror.url", "ssh://git@example.invalid/owner/repo.git"),
        ("remote.scp.url", "git@example.invalid:owner/repo.git"),
        ("core.autocrlf", "false"),
        ("http.proxy", "http://proxy.invalid:3128"),
        ("http.proxy", "proxy.invalid:3128"),
        ("core.gitProxy", "none"),
    ],
)
def test_safe_settings_pass(fx: Fixture, key: str, value: str) -> None:
    fx.git("config", key, value)
    assert fx.run().returncode == checker.EXIT_OK


# --- NEG-01: credential helper -------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("credential.helper", "[local] credential.helper: credential setting"),
        ("credential.https://example.invalid.helper",
         "[local] credential.<redacted>.helper: credential setting"),
        ("credential.username", "[local] credential.username: credential setting"),
        ("Credential.Helper", "[local] credential.helper: credential setting"),
    ],
)
def test_neg01_credential_helper_fails(fx: Fixture, key: str, expected: str) -> None:
    fx.git("config", key, SECRET)
    assert_found(fx.run(), expected)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("http.https://example.invalid/.extraheader",
         "[local] http.<redacted>.extraheader: http extraheader"),
        ("http.extraHeader", "[local] http.extraheader: http extraheader"),
        ("http.cookieFile", "[local] http.cookiefile: http cookiefile"),
        ("core.askPass", "[local] core.askpass: askpass program"),
    ],
)
def test_neg01_other_credential_material_fails(fx: Fixture, key: str, expected: str) -> None:
    fx.git("config", key, f"AUTHORIZATION: basic {SECRET}")
    assert_found(fx.run(), expected)


def test_neg01_global_scope_fails(fx: Fixture) -> None:
    fx.git("config", "--global", "credential.helper", SECRET)
    assert_found(fx.run(), "[global] credential.helper: credential setting")


def test_neg01_system_scope_fails(fx: Fixture) -> None:
    system = fx.base / "system.gitconfig"
    system.write_text(f"[http]\n\textraheader = {SECRET}\n", encoding="utf-8")
    del fx.env["GIT_CONFIG_NOSYSTEM"]
    fx.env["GIT_CONFIG_SYSTEM"] = str(system)
    assert_found(fx.run(), "[system] http.extraheader: http extraheader")


def test_neg01_worktree_scope_fails(fx: Fixture) -> None:
    fx.git("config", "extensions.worktreeConfig", "true")
    fx.git("config", "--worktree", "credential.helper", SECRET)
    assert_found(fx.run(), "[worktree] credential.helper: credential setting")


# --- NEG-02: credentials in remote URLs (N-1: case, multiple values) ------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://x-access-token:{SECRET}@example.invalid/owner/repo.git",
        f"https://{SECRET}@example.invalid/owner/repo.git",
        f"HTTPS://user:{SECRET}@example.invalid/owner/repo.git",
        f"Http://user:{SECRET}@example.invalid/owner/repo.git",
        f"ssh://git:{SECRET}@example.invalid/owner/repo.git",
    ],
)
def test_neg02_remote_url_with_credentials_fails(fx: Fixture, url: str) -> None:
    fx.git("remote", "set-url", "origin", url)
    assert_found(fx.run(), "[local] remote.<redacted>.url: credentials in URL",
                 "[effective] remote origin fetch URL: credentials in URL")


def test_neg02_credentials_in_an_earlier_value_of_a_multi_valued_url_fail(fx: Fixture) -> None:
    # `git config --get` returns only the last value; every value must be checked.
    fx.git("config", "--replace-all", "remote.origin.url",
           f"https://user:{SECRET}@example.invalid/owner/repo.git")
    fx.git("config", "--add", "remote.origin.url", "https://example.invalid/owner/repo.git")
    assert_found(fx.run(), "[local] remote.<redacted>.url: credentials in URL",
                 "[effective] remote origin fetch URL: credentials in URL")


def test_neg02_push_url_with_credentials_fails(fx: Fixture) -> None:
    fx.git("remote", "set-url", "--push", "origin",
           f"https://user:{SECRET}@example.invalid/owner/repo.git")
    assert_found(fx.run(), "[local] remote.<redacted>.pushurl: credentials in URL",
                 "[effective] remote origin push URL: credentials in URL")


def test_neg02_credentials_in_any_url_valued_setting_fail(fx: Fixture) -> None:
    fx.git("config", "http.proxy", f"http://proxy-user:{SECRET}@proxy.invalid:3128")
    assert_found(fx.run(), "[local] http.proxy: credentials in URL")


@pytest.mark.parametrize(
    "key,value,expected",
    [
        # Audit D-7: Git accepts a proxy without a scheme.
        ("http.proxy", f"proxy-user:{SECRET}@proxy.invalid:3128",
         "[local] http.proxy: credentials in proxy URL"),
        ("remote.origin.proxy", f"proxy-user:{SECRET}@proxy.invalid",
         "[local] remote.<redacted>.proxy: credentials in proxy URL"),
        ("core.sshCommand", f"ssh -i /nonexistent/{SECRET}",
         "[local] core.sshcommand: ssh command override"),
        ("sendemail.smtpPass", SECRET, "[local] sendemail.smtppass: password setting"),
        ("sendemail.work.smtpPass", SECRET,
         "[local] sendemail.<redacted>.smtppass: password setting"),
        ("imap.pass", SECRET, "[local] imap.pass: password setting"),
        ("http.proxySSLKey", f"/nonexistent/{SECRET}",
         "[local] http.proxysslkey: http proxysslkey"),
    ],
)
def test_neg02_other_credential_sources_fail(fx: Fixture, key: str, value: str,
                                             expected: str) -> None:
    fx.git("config", key, value)
    assert_found(fx.run(), expected)


@pytest.mark.parametrize(
    "relative,expected",
    [
        (".netrc", "[home] ~/.netrc: netrc file (not allowed)"),
        ("_netrc", "[home] ~/_netrc: netrc file (not allowed)"),
        (".git-credentials", "[home] ~/.git-credentials: credential store file (not allowed)"),
        (".config/git/credentials",
         "[home] $XDG_CONFIG_HOME/git/credentials: credential store file (not allowed)"),
        (".ssh/id_ed25519", "[home] ~/.ssh/id_*: ssh private key (not allowed)"),
    ],
)
def test_neg02_home_credential_file_fails(fx: Fixture, relative: str, expected: str) -> None:
    path = Path(fx.env["HOME"]) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"dummy {SECRET}\n", encoding="utf-8")
    assert_found(fx.run(), expected)


def test_xdg_credential_store_location_is_followed(fx: Fixture) -> None:
    config_home = fx.base / "xdg"
    (config_home / "git").mkdir(parents=True)
    (config_home / "git" / "credentials").write_text(f"dummy {SECRET}\n", encoding="utf-8")
    fx.env["XDG_CONFIG_HOME"] = str(config_home)
    assert_found(fx.run(), "[home] $XDG_CONFIG_HOME/git/credentials: credential store file")


def test_public_ssh_key_and_known_hosts_pass(fx: Fixture) -> None:
    ssh = Path(fx.env["HOME"]) / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA dummy\n", encoding="utf-8")
    (ssh / "known_hosts").write_text("example.invalid ssh-ed25519 AAAA\n", encoding="utf-8")
    assert fx.run().returncode == checker.EXIT_OK


# --- NEG-03: the check cannot run (fail closed, reported as an error) -----------------


# Failures of one specific git call are simulated in-process (a fake `git` on
# PATH cannot run in Layer B, whose tmpfs mounts are noexec). The clean run
# below proves the fake itself is accepted, so a failure is the injected one.
_CLEAN_GIT = [
    (("rev-parse", "--is-inside-work-tree"), 0, "true\n"),
    (("config", "--list"), 0, "local\0file:.git/config\0core.bare\nfalse\0"),
    (("remote", "get-url", "--push"), 0, "https://example.invalid/r.git\n"),
    (("remote", "get-url"), 0, "https://example.invalid/r.git\n"),
    (("remote",), 0, "origin\n"),
    (("rev-parse", "--git-path"), 0, "/nonexistent/legacy-remote-dir\n"),
]


def _run_main_with_fake_git(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], home: Path,
    failing: tuple[str, ...] | None = None,
) -> tuple[int, str]:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        args = tuple(argv[1:])
        for prefix, returncode, stdout in _CLEAN_GIT:
            if args[: len(prefix)] == prefix:
                if prefix == failing:
                    return subprocess.CompletedProcess(argv, 128, b"", b"fatal: simulated\n")
                return subprocess.CompletedProcess(argv, returncode, stdout.encode(), b"")
        raise AssertionError(f"unexpected git call: {args}")

    fake = type("FakeSubprocess", (), {"run": staticmethod(fake_run),
                                        "TimeoutExpired": subprocess.TimeoutExpired})
    monkeypatch.setattr(checker, "subprocess", fake)
    # main() runs in this process: nothing from the caller's environment or home
    # directory may decide the outcome.
    for name in (*checker.ENV_FINDINGS, *checker.PROXY_ENV, "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    status = checker.main()
    captured = capsys.readouterr()
    return status, captured.out + captured.err


def test_neg03_fake_git_harness_accepts_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    status, output = _run_main_with_fake_git(monkeypatch, capsys, tmp_path)
    assert status == checker.EXIT_OK and output.startswith("ok: "), output


@pytest.mark.parametrize(
    "failing,message",
    [
        # N-1: the old script read remote URLs through a process substitution and a
        # pipeline, so a failing lookup read as "no credentials".
        (("remote", "get-url"), "git remote get-url exited with status 128"),
        (("remote", "get-url", "--push"), "git remote get-url exited with status 128"),
        (("remote",), "git remote exited with status 128"),
        (("config", "--list"), "git config exited with status 128"),
        (("rev-parse", "--git-path"), "git rev-parse exited with status 128"),
    ],
)
def test_neg03_failing_git_call_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
    failing: tuple[str, ...], message: str,
) -> None:
    status, output = _run_main_with_fake_git(monkeypatch, capsys, tmp_path, failing)
    assert status == checker.EXIT_ERROR, output
    assert message in output and "ok:" not in output and "simulated" not in output


def test_neg03_remote_listing_failure_fails_closed(fx: Fixture) -> None:
    # Real git: an invalid boolean in a remote makes `git remote` exit 128 while
    # rev-parse and config --list still succeed.
    fx.git("config", "remote.origin.prune", "not-a-boolean")
    assert_error(fx.run(), "git remote exited with status 128")


def test_neg03_non_utf8_config_output_fails_closed(fx: Fixture) -> None:
    with (fx.repo / ".git" / "config").open("ab") as config:
        config.write(b"[x]\n\ty = \xff\n")
    assert_error(fx.run(), "git config printed non-UTF-8 output")


def test_neg03_corrupt_config_fails_closed(fx: Fixture) -> None:
    with (fx.repo / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write("[broken\n")
    assert_error(fx.run(), "exited with status")


def test_neg03_corrupt_global_config_fails_closed(fx: Fixture) -> None:
    fx.global_config.write_text("[broken\n", encoding="utf-8")
    assert_error(fx.run(), "exited with status")


def test_neg03_outside_a_repository_fails_closed(fx: Fixture) -> None:
    outside = fx.base / "outside"
    outside.mkdir()
    fx.env["GIT_CEILING_DIRECTORIES"] = str(fx.base)
    assert_error(fx.run(cwd=outside), "git rev-parse exited with status")


def test_neg03_missing_git_fails_closed(fx: Fixture) -> None:
    fx.env["PATH"] = str(fx.base / "empty-bin")
    assert_error(fx.run(), "cannot run git rev-parse")


def test_neg03_unexpected_stderr_fails_closed(fx: Fixture) -> None:
    fx.env["GIT_TRACE"] = "1"  # real git writes trace lines to stderr
    assert_error(fx.run(), "wrote unexpected output to stderr")


# --- legacy remote definitions (not listed by `git remote`) ----------------------------


@pytest.mark.parametrize("kind", ["remotes", "branches"])
def test_legacy_remote_file_fails(fx: Fixture, kind: str) -> None:
    legacy = fx.repo / ".git" / kind
    legacy.mkdir(exist_ok=True)
    content = f"URL: https://u:{SECRET}@example.invalid/r.git\n" if kind == "remotes" else \
        f"https://u:{SECRET}@example.invalid/r.git\n"
    (legacy / "legacy").write_text(content, encoding="utf-8")
    assert_found(fx.run(), f"[repository] $GIT_DIR/{kind}/: legacy remote definition")


def test_empty_legacy_directories_pass(fx: Fixture) -> None:
    (fx.repo / ".git" / "remotes").mkdir(exist_ok=True)
    (fx.repo / ".git" / "branches").mkdir(exist_ok=True)
    assert fx.run().returncode == checker.EXIT_OK


# --- NEG-04: URL rewriting (N-2) -------------------------------------------------------


@pytest.mark.parametrize("variable", ["insteadOf", "pushInsteadOf"])
def test_neg04_rewrite_to_url_with_credentials_fails(fx: Fixture, variable: str) -> None:
    # The credential sits in the key name; the remote URL itself is clean.
    fx.git("config", f"url.https://user:{SECRET}@example.invalid/.{variable}",
           "https://example.invalid/")
    direction = "push" if variable == "pushInsteadOf" else "fetch"
    assert_found(fx.run(), f"[local] url.<redacted>.{variable.lower()}: credentials in URL",
                 f"[effective] remote origin {direction} URL: credentials in URL")


# --- NEG-05: includes (N-2) -------------------------------------------------------------


def test_neg05_include_path_with_credential_fails(fx: Fixture) -> None:
    included = fx.base / "included.gitconfig"
    included.write_text(f"[credential]\n\thelper = {SECRET}\n", encoding="utf-8")
    fx.git("config", "include.path", str(included))
    assert_found(fx.run(), "[local] include.path: config include (not allowed)",
                 "[local] credential.helper: credential setting")


def test_neg05_include_if_with_credential_fails(fx: Fixture) -> None:
    included = fx.base / "included.gitconfig"
    included.write_text(f"[http]\n\textraheader = {SECRET}\n", encoding="utf-8")
    fx.git("config", "--global", f"includeIf.gitdir:{fx.repo}/.git.path", str(included))
    assert_found(fx.run(), "[global] includeif.<redacted>.path: config include (not allowed)",
                 "[global] http.extraheader: http extraheader")


def test_neg05_include_is_rejected_even_when_its_condition_does_not_match(fx: Fixture) -> None:
    included = fx.base / "included.gitconfig"
    included.write_text(f"[credential]\n\thelper = {SECRET}\n", encoding="utf-8")
    fx.git("config", "includeIf.onbranch:never-checked-out.path", str(included))
    assert_found(fx.run(), "[local] includeif.<redacted>.path: config include (not allowed)")


# --- NEG-06: configuration from the environment (N-2) ----------------------------------


def test_neg06_git_config_count_fails(fx: Fixture) -> None:
    fx.env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                   "GIT_CONFIG_VALUE_0": SECRET})
    assert_found(fx.run(), "[command] credential.helper: credential setting")


def test_neg06_git_config_parameters_fails(fx: Fixture) -> None:
    fx.env["GIT_CONFIG_PARAMETERS"] = f"'http.extraheader=AUTHORIZATION: basic {SECRET}'"
    assert_found(fx.run(), "[command] http.extraheader: http extraheader")


def test_neg06_redirected_global_config_fails(fx: Fixture) -> None:
    other = fx.base / "other.gitconfig"
    other.write_text(f"[credential]\n\thelper = {SECRET}\n", encoding="utf-8")
    fx.env["GIT_CONFIG_GLOBAL"] = str(other)
    assert_found(fx.run(), "[global] credential.helper: credential setting")


@pytest.mark.parametrize(
    "name,reason",
    [
        ("GIT_ASKPASS", "askpass program"),
        ("SSH_ASKPASS", "askpass program"),
        ("GIT_SSH", "ssh command override"),
        ("GIT_SSH_COMMAND", "ssh command override"),
        ("GIT_SSL_KEY", "client certificate"),
        ("GIT_SSL_CERT", "client certificate"),
        ("GIT_PROXY_SSL_KEY", "client certificate"),
        ("GIT_PROXY_SSL_CERT", "client certificate"),
        ("SSH_AUTH_SOCK", "ssh agent"),
    ],
)
def test_neg06_credential_program_environment_fails(fx: Fixture, name: str, reason: str) -> None:
    fx.env[name] = f"/nonexistent/{SECRET}"
    assert_found(fx.run(), f"[environment] {name}: {reason}")


def test_neg06_git_config_variable_fails(fx: Fixture) -> None:
    # Audit D-6: GIT_CONFIG redirects `git config` only; the other Git commands
    # still use the extraheader in .git/config.
    fx.git("config", "http.extraheader", f"AUTHORIZATION: basic {SECRET}")
    empty = fx.base / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    fx.env["GIT_CONFIG"] = str(empty)
    assert_found(fx.run(), "[environment] GIT_CONFIG: configuration location override")


@pytest.mark.parametrize("name", ["GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"])
def test_neg06_repository_location_override_fails(fx: Fixture, name: str) -> None:
    value = str(fx.repo if name == "GIT_WORK_TREE" else fx.repo / ".git")
    fx.env[name] = value
    assert_found(fx.run(), f"[environment] {name}: repository location override")


@pytest.mark.parametrize("name", ["https_proxy", "HTTPS_PROXY", "http_proxy", "ALL_PROXY"])
def test_neg06_proxy_environment_with_credentials_fails(fx: Fixture, name: str) -> None:
    fx.env[name] = f"http://proxy-user:{SECRET}@proxy.invalid:3128"
    assert_found(fx.run(), f"[environment] {name}: credentials in proxy URL")


def test_proxy_environment_without_credentials_passes(fx: Fixture) -> None:
    fx.env["https_proxy"] = "http://proxy.invalid:3128"
    fx.env["HTTP_PROXY"] = "proxy.invalid:3128"
    assert fx.run().returncode == checker.EXIT_OK


# --- pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://example.invalid/r.git", False),
        ("ssh://git@example.invalid/r.git", False),
        ("git@example.invalid:r.git", False),
        ("https://user@example.invalid/r.git", True),
        ("HTTPS://u:p@example.invalid/", True),
        ("ftp://u@example.invalid/", True),
        ("ssh://u:p@example.invalid/", True),
        ("https://example.invalid/path@not-userinfo", False),
        ("prefix https://u:p@example.invalid/ suffix", True),
    ],
)
def test_url_credential_detection(text: str, expected: bool) -> None:
    assert checker.has_url_credentials(text) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("http://proxy.invalid:3128", False),
        ("proxy.invalid:3128", False),
        ("http://proxy.invalid/path@x", False),
        ("u:p@proxy.invalid:3128", True),
        ("socks5://u@proxy.invalid", True),
        ("", False),
    ],
)
def test_proxy_credential_detection(text: str, expected: bool) -> None:
    assert checker.has_proxy_credentials(text) is expected


@pytest.mark.parametrize(
    "raw",
    [
        "local\0file:.git/config\0core.bare\nfalse",  # missing trailing NUL
        "local\0file:.git/config\0",  # incomplete record
        "nonsense\0file:.git/config\0core.bare\nfalse\0",  # unknown scope
        "local\0weird:.git/config\0core.bare\nfalse\0",  # unknown origin
        "local\0file:.git/config\0nodot\0",  # malformed key
    ],
)
def test_malformed_config_output_is_an_error(raw: str) -> None:
    with pytest.raises(checker.CheckError):
        checker.parse_config_list(raw)
