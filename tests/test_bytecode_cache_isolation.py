"""Phase 3H-R6 F01: a child pytest must not write caches beside the sources it imports.

A child pytest started without -B writes the bytecode caches of what it imports and
pytest's assertion-rewrite caches (`*.cpython-XY-pytest-N.pyc`) of the plugins and
test modules it rewrites; with the virtual environment's plugins that meant writing
into the virtual environment. Every child here runs on a synthetic plugin and test,
and always with PYTHONPYCACHEPREFIX in tmp_path, so even the control case writes
nothing next to any real source.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PLUGIN = "def pytest_configure(config):\n    assert config is not None\n"
TEST = "def test_sum():\n    assert 1 + 1 == 2\n"


def _child(tmp_path: Path, **extra: str) -> tuple[Path, Path]:
    site, work, prefix = tmp_path / "site", tmp_path / "work", tmp_path / "prefix"
    (site / "r6plugin").mkdir(parents=True)
    (site / "r6plugin" / "__init__.py").write_text(PLUGIN, encoding="utf-8")
    work.mkdir()
    (work / "test_sum.py").write_text(TEST, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "PYTHONPATH": str(site), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "PYTHONPYCACHEPREFIX": str(prefix), **extra}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "r6plugin", "-p", "no:cacheprovider",
         "test_sum.py"], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return site, prefix


def _rewrite_caches(root: Path) -> list[Path]:
    return sorted(root.rglob("*-pytest-*.pyc")) if root.exists() else []


def test_control_rewrite_caches_are_written_but_only_under_the_prefix(tmp_path: Path) -> None:
    site, prefix = _child(tmp_path)
    assert _rewrite_caches(prefix), "the assertion-rewrite cache is active in the control"
    assert not list(site.rglob("__pycache__"))
    assert not list((tmp_path / "work").rglob("__pycache__"))


def test_dont_write_bytecode_stops_every_cache(tmp_path: Path) -> None:
    site, prefix = _child(tmp_path, PYTHONDONTWRITEBYTECODE="1")
    assert _rewrite_caches(prefix) == []
    assert not (prefix.exists() and list(prefix.rglob("*.pyc")))
    assert not list(site.rglob("__pycache__"))
    assert not list((tmp_path / "work").rglob("__pycache__"))
