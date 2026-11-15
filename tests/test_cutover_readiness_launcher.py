"""Phase 3H-R7 C-2/C-3: the dry-run production launcher plan of scripts/cutover_readiness.py.

Synthetic secrets only; no Notion endpoint, no production .env, no real runtime."""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_cutover_readiness_bootstrap import CLEAN_ENV, SCRIPT, Fixture, cr

SECRET = "synthetic-secret-4b1d-not-real"
SOURCE_ID = "0123456789abcdef0123456789abcdef"


def _config(tmp_path: Path, text: str | None = None, mode: int = 0o600) -> Path:
    path = tmp_path / "launcher.conf"
    path.write_text(text if text is not None else
                    f"# synthetic\nNOTION_API_KEY={SECRET}\n"
                    f"NOTION_COMMANDER_DATA_SOURCE_ID={SOURCE_ID}\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _plan(fx: Fixture, config: Path | None, environ: dict[str, str] | None = None,
          args: list[str] | None = None, python: str | None = None) -> Any:
    bootstrap = fx.verify()
    assert bootstrap.status == cr.PASS, bootstrap.as_dict()
    return cr.plan_launch(bootstrap, python or fx.python, config,
                          CLEAN_ENV if environ is None else environ,
                          ["doctor"] if args is None else args)


def _no_secret(*values: Any) -> None:
    for value in values:
        assert SECRET not in str(value) and SECRET not in repr(value)


def test_plan_for_clean_inputs(fx: Fixture, tmp_path: Path) -> None:
    plan = _plan(fx, _config(tmp_path))
    assert plan.status == cr.PASS, plan.as_dict()
    assert plan.argv == [fx.python, "-I", "-B", "-m", "mtg_notion_manager.cli", "doctor"]
    assert plan.executable is False and cr.EXECUTION_ENABLED is False
    assert set(plan.environment) == {"HOME", "USER", "LANG", "PATH", "PYTHON_DOTENV_DISABLED",
                                     "NOTION_API_KEY", "NOTION_COMMANDER_DATA_SOURCE_ID"}
    assert plan.environment["PATH"] == "/usr/bin:/bin"
    assert plan.environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert plan.environment["NOTION_API_KEY"].reveal() == SECRET
    assert plan.may_mutate is False
    rendered = plan.as_dict()
    assert rendered["credentials"] == "<redacted>" and "DISABLED" in rendered["execution"]
    _no_secret(rendered, json.dumps(rendered), plan, plan.environment)


@pytest.mark.parametrize("name", ["NOTION_TOKEN", "NOTION_API_KEY", "PYTHONPATH", "PYTHONHOME",
                                  "PYTHONSTARTUP", "DYLD_INSERT_LIBRARIES", "PIP_CONFIG_FILE"])
def test_inherited_variable_is_refused(fx: Fixture, tmp_path: Path, name: str) -> None:
    plan = _plan(fx, _config(tmp_path), environ={**CLEAN_ENV, name: SECRET})
    assert plan.status == cr.FAIL and plan.argv == [] and plan.environment == {}
    assert plan.checks[-1].name == "inherited_environment" and name in plan.checks[-1].detail
    _no_secret(plan.as_dict(), plan)


def test_wrong_executable_path(fx: Fixture, tmp_path: Path) -> None:
    plan = _plan(fx, _config(tmp_path), python="/usr/bin/python3")
    assert plan.status == cr.FAIL and plan.checks[-1].name == "interpreter_explicit"


def test_missing_config(fx: Fixture) -> None:
    plan = _plan(fx, None)
    assert plan.status == cr.FAIL and "missing config" in plan.checks[-1].detail


def test_config_file_that_does_not_exist(fx: Fixture, tmp_path: Path) -> None:
    plan = _plan(fx, tmp_path / "absent.conf")
    assert plan.status == cr.FAIL and plan.checks[-1].name == "config"


@pytest.mark.parametrize("text, reason", [
    (f"NOTION_API_KEY={SECRET}\n", "missing ['NOTION_COMMANDER_DATA_SOURCE_ID']"),
    (f"NOTION_API_KEY={SECRET}\nNOTION_COMMANDER_DATA_SOURCE_ID={SOURCE_ID}\nEXTRA=1\n",
     "unknown key EXTRA"),
    (f"NOTION_API_KEY={SECRET}\nNOTION_API_KEY={SECRET}\n", "duplicate key NOTION_API_KEY"),
    (f"NOTION_API_KEY={SECRET}\nNOTION_COMMANDER_DATA_SOURCE_ID={SECRET}\n",
     "NOTION_COMMANDER_DATA_SOURCE_ID is not a data source id"),
    (f"NOTION_API_KEY={SECRET} trailing\nNOTION_COMMANDER_DATA_SOURCE_ID={SOURCE_ID}\n",
     "invalid value for NOTION_API_KEY"),
    (f"NOTION_API_KEY=\"{SECRET}\"\nNOTION_COMMANDER_DATA_SOURCE_ID={SOURCE_ID}\n",
     "invalid value for NOTION_API_KEY"),
    (f"export NOTION_API_KEY={SECRET}\n", "expected KEY=VALUE"),
    (f"NOTION_API_KEY=\nNOTION_COMMANDER_DATA_SOURCE_ID={SOURCE_ID}\n", "invalid value"),
])
def test_invalid_config_schema_never_echoes_values(fx: Fixture, tmp_path: Path, text: str,
                                                   reason: str) -> None:
    plan = _plan(fx, _config(tmp_path, text))
    assert plan.status == cr.FAIL and plan.checks[-1].name == "config"
    assert reason in plan.checks[-1].detail
    _no_secret(plan.as_dict(), plan)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o666])
def test_config_readable_by_others(fx: Fixture, tmp_path: Path, mode: int) -> None:
    plan = _plan(fx, _config(tmp_path, mode=mode))
    assert plan.status == cr.FAIL and "group or others" in plan.checks[-1].detail


def test_config_symlink_is_refused(fx: Fixture, tmp_path: Path) -> None:
    real = _config(tmp_path)
    link = tmp_path / "link.conf"
    link.symlink_to(real)
    plan = _plan(fx, link)
    assert plan.status == cr.FAIL and "symlink" in plan.checks[-1].detail


def test_secret_never_in_exception(tmp_path: Path) -> None:
    path = _config(tmp_path,
                   f"NOTION_API_KEY={SECRET}\nNOTION_COMMANDER_DATA_SOURCE_ID={SECRET}x\n")
    with pytest.raises(cr.Rejected) as raised:
        cr.load_config(path)
    _no_secret(raised.value, raised.value.args, raised.getrepr())


def test_secret_repr() -> None:
    secret = cr.Secret(SECRET)
    _no_secret(secret, [secret], {"k": secret}, f"{secret} {secret!r}", str((secret,)))
    assert secret.reveal() == SECRET


def test_secret_never_in_output_or_logs(fx: Fixture, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str],
                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    config = _config(tmp_path)
    status = cr.main(["launch-plan", "--manifest", str(fx.manifest_path), "--manifest-sha256",
                      fx.manifest_sha, "--python", fx.python, "--config", str(config), "--",
                      "import-cards", "https://example.invalid/deck", "--apply"],
                     environ=CLEAN_ENV)
    out, err = capsys.readouterr()
    assert status == 0, out
    report = json.loads(out)
    assert report["may_mutate_notion"] is True and report["executable"] is False
    assert report["argv"][-3:] == ["import-cards", "https://example.invalid/deck", "--apply"]
    _no_secret(out, err, caplog.text)


def test_failure_output_has_no_secret(fx: Fixture, tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    status = cr.main(["launch-plan", "--manifest", str(fx.manifest_path), "--manifest-sha256",
                      fx.manifest_sha, "--python", fx.python, "--config", str(_config(tmp_path)),
                      "doctor"], environ={**CLEAN_ENV, "NOTION_TOKEN": SECRET})
    out, err = capsys.readouterr()
    assert status == 1 and json.loads(out)["status"] == cr.FAIL
    _no_secret(out, err)


def test_unknown_command(fx: Fixture, tmp_path: Path) -> None:
    plan = _plan(fx, _config(tmp_path), args=["drop-database"])
    assert plan.status == cr.FAIL and plan.checks[-1].name == "command"
    assert plan.may_mutate is True


def test_bootstrap_not_passed_means_no_plan(fx: Fixture, tmp_path: Path) -> None:
    bootstrap = fx.verify(expected=None)
    plan = cr.plan_launch(bootstrap, fx.python, _config(tmp_path), CLEAN_ENV, ["doctor"])
    assert plan.status == cr.UNVERIFIED and plan.argv == [] and plan.environment == {}


def test_incomplete_preflight(fx: Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cr, "REQUIRED_PREFLIGHT", (*cr.REQUIRED_PREFLIGHT, "network_policy"))
    plan = _plan(fx, _config(tmp_path))
    assert plan.status == cr.FAIL and plan.checks[-1].detail == "incomplete preflight"
    assert cr.complete([cr.Check("bootstrap", cr.PASS)]) is False


def test_dotenv_without_disable_support(fx: Fixture, tmp_path: Path) -> None:
    (fx.site / "dotenv" / "main.py").write_bytes(b"def load_dotenv(): pass\n")
    plan = _plan(fx, _config(tmp_path))
    assert plan.status == cr.FAIL and plan.checks[-1].name == "dotenv_disabled"


def test_dotenv_unknown_version(fx: Fixture, tmp_path: Path) -> None:
    (fx.site / "python_dotenv-1.2.1.dist-info").rename(fx.site / "python_dotenv-1.0.0.dist-info")
    plan = _plan(fx, _config(tmp_path))
    assert plan.status == cr.FAIL and "1.0.0" in plan.checks[-1].detail


def test_home_must_be_absolute(fx: Fixture, tmp_path: Path) -> None:
    plan = _plan(fx, _config(tmp_path), environ={**CLEAN_ENV, "HOME": "relative"})
    assert plan.status == cr.FAIL and "HOME" in plan.checks[-1].detail


@pytest.mark.parametrize("argv, mutates", [
    (["doctor"], False),
    (["verify-import", "u"], False),
    (["audit-duplicates"], False),
    (["import", "u", "--dry-run"], False),
    (["import", "u"], True),
    (["import-cards", "u", "--deck-name", "d"], False),
    (["import-cards", "u", "--apply"], True),
    (["dedupe-cards", "--apply-schema"], True),
    (["dedupe-cards", "--apply-all", "--yes"], True),
    (["apply-single-title-update", "--apply"], True),
    (["unknown"], True),
    ([], True),
])
def test_command_may_mutate(argv: list[str], mutates: bool) -> None:
    assert cr.command_may_mutate(argv) is mutates


# --- the component cannot launch anything or touch the network ---------------------------


def test_module_has_no_execution_or_network_code() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "argparse", "hashlib", "json", "os", "re", "stat", "sys",
                        "dataclasses", "pathlib", "typing"}, imported
    forbidden = {"system", "popen", "posix_spawn", "posix_spawnp", "fork", "forkpty",
                 "startfile", "kill", "killpg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "os":
            assert node.attr not in forbidden and not node.attr.startswith(("exec", "spawn")), \
                node.attr


AUDIT_RUNNER = r"""
import importlib.util, json, sys
events = []
def hook(event, args):
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn",
                         "os.system", "os.fork", "os.kill")):
        events.append(event)
sys.addaudithook(hook)
spec = importlib.util.spec_from_file_location("cr", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["cr"] = module
spec.loader.exec_module(module)
status = module.main(sys.argv[2:], environ=json.loads(sys.stdin.read()))
print("EVENTS=" + json.dumps(events), file=sys.stderr)
sys.exit(status)
"""


def test_no_network_or_process_at_runtime(fx: Fixture, tmp_path: Path) -> None:
    """In a fresh interpreter with an audit hook (the conftest guard does not apply there),
    the whole launch-plan run starts no process and opens no socket."""
    config = _config(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", AUDIT_RUNNER, str(SCRIPT), "launch-plan",
         "--manifest", str(fx.manifest_path), "--manifest-sha256", fx.manifest_sha,
         "--python", fx.python, "--config", str(config), "--", "doctor"],
        input=json.dumps(CLEAN_ENV), capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=tmp_path, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EVENTS=[]" in result.stderr, result.stderr
    assert SECRET not in result.stdout + result.stderr


# --- python-dotenv's discovery is really disabled by the planned environment -------------


DOTENV_PROBE = ("import os, dotenv; dotenv.load_dotenv(); "
                "print(os.environ.get('NOTION_API_KEY', '<unset>'))")


@pytest.mark.parametrize("disabled", [True, False])
def test_dotenv_auto_discovery_is_disabled(tmp_path: Path, disabled: bool) -> None:
    """A planted .env in the working directory is found by load_dotenv() (control) and not
    found once PYTHON_DOTENV_DISABLED=1 is set, as the launcher plan does."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("NOTION_API_KEY=planted-synthetic-dotenv\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    if disabled:
        env["PYTHON_DOTENV_DISABLED"] = "1"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", DOTENV_PROBE], cwd=work, env=env,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("<unset>" if disabled else "planted-synthetic-dotenv")


def test_launch_environment_has_only_allowlisted_names() -> None:
    environ = {**CLEAN_ENV, "TERM": "x", "SSH_AUTH_SOCK": "/tmp/x", "OLDPWD": "/"}
    env = cr.launch_environment(environ, {"NOTION_API_KEY": cr.Secret(SECRET)})
    assert set(env) == {"HOME", "USER", "LANG", "PATH", "PYTHON_DOTENV_DISABLED",
                        "NOTION_API_KEY"}
    assert os.pathsep.join(cr.LAUNCH_PATH) == env["PATH"]
