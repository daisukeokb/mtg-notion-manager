"""Phase 3H-R6 (C-2, C-3): where the CLI's settings come from when it is started.

Only synthetic values and a .env written to tmp_path are used; the
repository's .env is never read (Layer A refuses it anyway).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mtg_notion_manager.config import Config, ConfigError

FILE_VALUES = {
    "NOTION_API_KEY": "synthetic-file-key",
    "NOTION_COMMANDER_DATA_SOURCE_ID": "synthetic-file-commander",
    "NOTION_CARD_DATA_SOURCE_ID": "synthetic-file-card",
}


@pytest.fixture
def dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in FILE_VALUES:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "synthetic.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in FILE_VALUES.items()), encoding="utf-8")
    return path


def test_dotenv_values_are_used_when_the_shell_sets_nothing(dotenv: Path) -> None:
    config = Config.load(dotenv_path=dotenv)
    assert (config.notion_api_key, config.commander_data_source_id,
            config.card_data_source_id) == tuple(FILE_VALUES.values())


def test_shell_variables_take_precedence_over_dotenv(
        dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # load_dotenv() does not override: a NOTION_* variable already set in the
    # shell that starts the CLI silently wins over .env (C-2).
    monkeypatch.setenv("NOTION_COMMANDER_DATA_SOURCE_ID", "synthetic-shell-commander")
    config = Config.load(dotenv_path=dotenv)
    assert config.commander_data_source_id == "synthetic-shell-commander"
    assert config.notion_api_key == "synthetic-file-key"


def test_an_empty_shell_variable_hides_the_dotenv_value(
        dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "")
    with pytest.raises(ConfigError, match="NOTION_API_KEY"):
        Config.load(dotenv_path=dotenv)


def _import_probe(tmp_path: Path, isolated: bool) -> int:
    module_dir = tmp_path / "shadow"
    module_dir.mkdir(exist_ok=True)
    (module_dir / "r6_shadow_probe.py").write_text("VALUE = 'shadowed'\n", encoding="utf-8")
    flags = ["-I"] if isolated else []
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(module_dir)}
    return subprocess.run([sys.executable, *flags, "-c", "import r6_shadow_probe"], env=env,
                          capture_output=True, check=False, timeout=60).returncode


def test_pythonpath_reaches_an_interpreter_started_without_isolation(tmp_path: Path) -> None:
    # The console scripts (e.g. .venv-py314-r5/bin/mtg-notion-manager) start the
    # interpreter without -I, so PYTHON* variables of the launching shell apply
    # to the CLI; -I ignores them (C-3).
    assert _import_probe(tmp_path, isolated=False) == 0
    assert _import_probe(tmp_path, isolated=True) != 0
