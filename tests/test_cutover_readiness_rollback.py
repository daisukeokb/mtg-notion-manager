"""Phase 3H-R7: the rollback model of scripts/cutover_readiness.py on an isolated fixture, and
the consistency of the component with the repository it describes."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_cutover_readiness_bootstrap import REPO_ROOT, cr

GATE_SCRIPT = REPO_ROOT / "scripts" / "verify_cutover_gate.py"
BASELINE = {"interpreter_sha256": "a" * 64, "version": "Python 3.9.6", "fingerprint": "c" * 64}
CONFIG_FP = "f" * 64


def _inputs(**changes: Any) -> Any:
    values: dict[str, Any] = {"legacy_baseline": dict(BASELINE),
                              "legacy_observed": dict(BASELINE),
                              "config_fingerprint_at_cutover": CONFIG_FP,
                              "config_fingerprint_now": CONFIG_FP, "journal": []}
    values.update(changes)
    return cr.RollbackInputs(**values)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "mtg-notion-manager").write_bytes(b"#!legacy launcher\n")
    (tmp_path / "new").mkdir()
    (tmp_path / cr.SELECTOR).write_text("legacy\n", encoding="ascii")
    return tmp_path


def _tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "legacy").rglob("*")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def healthy(root: Path, runtime: str) -> bool:
    return True


def new_fails(root: Path, runtime: str) -> bool:
    return runtime != "new"


def test_cutover_with_healthy_runtime(root: Path) -> None:
    result = cr.simulate_cutover(root, _inputs(), healthy)
    assert (result.status, result.active) == (cr.PASS, "new")
    assert cr.read_selector(root) == "new"


def test_new_runtime_start_failure_rolls_back_to_the_pre_cutover_state(root: Path) -> None:
    before = _tree(root)
    result = cr.simulate_cutover(root, _inputs(), new_fails)
    assert (result.status, result.active) == ("ROLLED_BACK", "legacy"), result.steps
    assert cr.read_selector(root) == "legacy"
    assert not (root / "new.running").exists()
    assert _tree(root) == before
    assert sorted(p.name for p in root.iterdir()) == [cr.SELECTOR, "legacy", "new"]


def _after_cutover(root: Path) -> None:
    assert cr.simulate_cutover(root, _inputs(), healthy).status == cr.PASS


def test_rollback_after_cutover(root: Path) -> None:
    _after_cutover(root)
    result = cr.simulate_rollback(root, _inputs(), healthy)
    assert (result.status, result.active) == (cr.PASS, "legacy")


def test_configuration_mismatch_stops_rollback(root: Path) -> None:
    _after_cutover(root)
    result = cr.simulate_rollback(root, _inputs(config_fingerprint_now="0" * 64), healthy)
    assert result.status == cr.FAIL and result.active == "new"
    assert "stop: preconditions" in result.steps


def test_missing_config_is_a_mismatch(root: Path) -> None:
    assessment = cr.assess_rollback(_inputs(config_fingerprint_now=None))
    assert assessment.local == cr.FAIL and not assessment.unconditional


def test_missing_artifact_stops_rollback(root: Path) -> None:
    _after_cutover(root)
    result = cr.simulate_rollback(root, _inputs(legacy_observed=None), healthy)
    assert result.status == cr.FAIL and cr.read_selector(root) == "new"
    assert "missing artifact" in cr.assess_rollback(_inputs(legacy_observed=None)).reasons[0]


def test_changed_legacy_runtime_stops_rollback() -> None:
    observed = {**BASELINE, "fingerprint": "d" * 64}
    assessment = cr.assess_rollback(_inputs(legacy_observed=observed))
    assert assessment.local == cr.FAIL and "['fingerprint']" in assessment.reasons[0]


def test_failure_during_rollback_leaves_a_consistent_selector(root: Path) -> None:
    _after_cutover(root)
    result = cr.simulate_rollback(root, _inputs(), healthy, fail_at="switch")
    assert result.status == cr.FAIL and result.active == "new"
    assert cr.read_selector(root) == "new"  # not half-written
    assert not (root / f".{cr.SELECTOR}.tmp").exists()
    assert "no automatic repair" in result.steps[-1]
    # A later, separately approved retry works from the consistent state.
    assert cr.simulate_rollback(root, _inputs(), healthy).status == cr.PASS


def test_health_check_failure_after_rollback(root: Path) -> None:
    _after_cutover(root)
    result = cr.simulate_rollback(root, _inputs(), lambda r, runtime: False)
    assert result.status == cr.FAIL
    assert "post-rollback health check failed" in result.steps[-1]


def test_new_runtime_failure_and_legacy_failure(root: Path) -> None:
    result = cr.simulate_cutover(root, _inputs(), lambda r, runtime: False)
    assert result.status == cr.FAIL and "health check failed" in result.steps[-1]


def test_cutover_only_from_legacy(root: Path) -> None:
    _after_cutover(root)
    assert cr.simulate_cutover(root, _inputs(), healthy).status == cr.FAIL


def test_unknown_selector_value(root: Path) -> None:
    (root / cr.SELECTOR).write_text("other\n", encoding="ascii")
    with pytest.raises(cr.Rejected):
        cr.read_selector(root)


@pytest.mark.parametrize("journal", [
    [["import-cards", "u", "--deck-name", "d", "--apply"]],
    [["doctor"], ["import", "u"]],
    [["apply-price-link-dedupe", "--scope", "canary", "--apply"]],
    [["something-new"]],
    # Click gives "--dry-run" to --deck-name as its value: this import writes
    [["import", "u", "--deck-name", "--dry-run"]],
])
def test_irreversible_mutation_makes_rollback_conditional(root: Path,
                                                          journal: list[list[str]]) -> None:
    assessment = cr.assess_rollback(_inputs(journal=journal))
    assert assessment.local == cr.PASS
    assert assessment.external == "MUTATIONS_REQUIRE_REVIEW"
    assert assessment.unconditional is False
    _after_cutover(root)
    result = cr.simulate_rollback(root, _inputs(journal=journal), healthy)
    assert result.status == cr.PASS  # the local rollback still works ...
    assert "external state needs a separate human review" in " ".join(result.steps)  # ... only


def test_read_only_journal_is_unconditional() -> None:
    journal = [["doctor"], ["verify-import", "u"], ["import", "u", "--dry-run"]]
    assessment = cr.assess_rollback(_inputs(journal=journal))
    assert (assessment.external, assessment.unconditional) == ("NO_MUTATION_RECORDED", True)


# --- consistency with the rest of the repository -----------------------------------------


def _gate() -> Any:
    spec = importlib.util.spec_from_file_location("verify_cutover_gate_r7_check", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_constants_match_the_cutover_gate() -> None:
    gate = _gate()
    assert cr.ENVIRONMENT_KEEP == gate.PYTHON_ENVIRONMENT_KEEP
    assert cr.VERIFIED_ARCHIVE_SHA256 == gate.ARCHIVE_SHA256
    assert cr.KNOWN_VERSIONS == (gate.PYTHON_VERSION,)
    assert (cr.PASS, cr.FAIL, cr.BLOCKED) == (gate.PASS, gate.FAIL, gate.BLOCKED)


def test_cli_commands_match_the_cli() -> None:
    import typer

    from mtg_notion_manager.cli import app

    group = typer.main.get_command(app)
    assert sorted(getattr(group, "commands", {})) == sorted(cr.CLI_COMMANDS)
    assert set(cr.READ_ONLY_COMMANDS) < set(cr.CLI_COMMANDS)


def test_option_tables_match_the_click_commands() -> None:
    """VALUE_OPTIONS / FLAG_OPTIONS are exactly the options of each Click command (the help
    option excepted, which the tables deliberately leave out: it is refused)."""
    import click
    import typer

    from mtg_notion_manager.cli import app

    commands = getattr(typer.main.get_command(app), "commands", {})
    for name, command in commands.items():
        options = [p for p in command.params if isinstance(p, click.Option)]
        values = {o for p in options if not p.is_flag and not p.count
                  for o in [*p.opts, *p.secondary_opts]}
        flags = {o for p in options if p.is_flag for o in [*p.opts, *p.secondary_opts]}
        assert values == cr.VALUE_OPTIONS[name], name
        assert flags == cr.FLAG_OPTIONS[name], name
        assert all(o.startswith("--") for o in values | flags), name
        # parse_invocation assumes each value option takes exactly one token, and counting
        # options are in neither table
        assert not any(getattr(option, "count", False) for option in options), name
        for option in options:
            if not option.is_flag:
                assert option.nargs == 1 and not getattr(option, "_flag_needs_value", False), \
                    (name, option.opts)


def test_click_gives_the_next_token_to_a_value_option() -> None:
    """The parsing rule command_may_mutate relies on, observed on the real import command."""
    import typer

    from mtg_notion_manager.cli import app

    command = getattr(typer.main.get_command(app), "commands", {})["import"]
    context = command.make_context("import", ["u", "--deck-name", "--dry-run"],
                                   resilient_parsing=True)
    assert context.params["deck_name"] == "--dry-run" and context.params["dry_run"] is False


def test_read_only_commands_have_no_write_option() -> None:
    import typer

    from mtg_notion_manager.cli import app

    group = typer.main.get_command(app)
    commands = getattr(group, "commands", {})
    for name in cr.READ_ONLY_COMMANDS:
        options = {opt for param in commands[name].params for opt in getattr(param, "opts", [])}
        assert not any(opt.startswith("--apply") for opt in options), name


def test_dotenv_version_matches_the_lock() -> None:
    lock = (REPO_ROOT / "requirements" / "ci-lock.txt").read_text(encoding="utf-8")
    match = re.search(r"^python-dotenv==(\S+)", lock, re.MULTILINE)
    assert match and match.group(1) in cr.DOTENV_DISABLE_VERSIONS


def test_config_keys_match_the_cli_config() -> None:
    source = (REPO_ROOT / "src" / "mtg_notion_manager" / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    assert read == set(cr.CONFIG_REQUIRED + cr.CONFIG_OPTIONAL)


def test_launch_module_is_runnable_with_dash_m() -> None:
    source = (REPO_ROOT / "src" / "mtg_notion_manager" / "cli.py").read_text(encoding="utf-8")
    assert source.rstrip().endswith('if __name__ == "__main__":\n    app()')
    assert cr.LAUNCH_ARGS[-1] == "mtg_notion_manager.cli"
