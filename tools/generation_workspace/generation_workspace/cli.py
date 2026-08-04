"""Production Migration CLI.

This is the only sanctioned entry point for running a Generation Workspace
mutation against a real Workspace. It never bypasses the function-level
Guard in bootstrap.py: `--apply` still calls
`bootstrap_generation_workspace(...)`, which re-runs Phase A / Lock /
Phase B internally. The CLI's own Preflight (default mode) is a read-only
convenience that never creates any filesystem artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import mutation_guard as guard
from .bootstrap import bootstrap_generation_workspace
from .digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTHORIZATION_REJECTED = 3
EXIT_ROOT_POLICY_REJECTED = 4
EXIT_BASELINE_MISMATCH = 5
EXIT_LOCK_CONFLICT = 6
EXIT_MUTATION_FAILED = 7
EXIT_FAILED_REQUIRES_RECOVERY = 8
EXIT_POST_VERIFICATION_FAILED = 9

_ERROR_CODE_EXIT = {
    guard.MUTATION_AUTHORIZATION_REQUIRED: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_SCHEMA_UNSUPPORTED: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_SCHEMA_INVALID: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_FIELD_MISSING: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_UNKNOWN_FIELD: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_APPLY_REQUIRED: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_OPERATION_MISMATCH: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_TRANSACTION_MISMATCH: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_WORKSPACE_MISMATCH: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_DIGEST_MISMATCH: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_UNSAFE_PATH: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_DUPLICATE_PATH: EXIT_AUTHORIZATION_REJECTED,
    guard.AUTHORIZATION_FILE_REJECTED: EXIT_AUTHORIZATION_REJECTED,
    guard.SOURCE_GENERATION_MISMATCH: EXIT_AUTHORIZATION_REJECTED,
    guard.ROOT_POLICY_REJECTED: EXIT_ROOT_POLICY_REJECTED,
    guard.ROOT_SYMLINK_REJECTED: EXIT_ROOT_POLICY_REJECTED,
    guard.AUTHORIZATION_BASELINE_COUNT_MISMATCH: EXIT_BASELINE_MISMATCH,
    guard.AUTHORIZATION_BASELINE_INVENTORY_MISMATCH: EXIT_BASELINE_MISMATCH,
    guard.AUTHORIZATION_BASELINE_SHA_MISMATCH: EXIT_BASELINE_MISMATCH,
    guard.LOCK_ALREADY_EXISTS: EXIT_LOCK_CONFLICT,
    guard.LOCK_OWNERSHIP_MISMATCH: EXIT_LOCK_CONFLICT,
    guard.TARGET_GENERATION_ALREADY_EXISTS: EXIT_MUTATION_FAILED,
    guard.FAILED_REQUIRES_RECOVERY: EXIT_FAILED_REQUIRES_RECOVERY,
}


def _exit_code_for(error_code: str | None, reason: str) -> int:
    if error_code is None:
        return EXIT_MUTATION_FAILED
    if error_code == guard.FAILED_REQUIRES_RECOVERY and "post-bootstrap verification failed" in (
        reason or ""
    ):
        return EXIT_POST_VERIFICATION_FAILED
    return _ERROR_CODE_EXIT.get(error_code, EXIT_MUTATION_FAILED)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generation_workspace",
        description="Production Migration entry point for the Generation Workspace.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap_p = sub.add_parser(
        "bootstrap", help="Bootstrap a flat workspace into gen-0000000001."
    )
    bootstrap_p.add_argument("--workspace-root", required=True)
    bootstrap_p.add_argument("--authorization-file", required=True)
    bootstrap_p.add_argument("--apply", action="store_true")
    bootstrap_p.add_argument("--json", action="store_true")
    bootstrap_p.add_argument("--report-file", default=None)

    return parser


def _print_output(output: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=False))
        return
    for key, value in output.items():
        print(f"{key}: {value}")


def _write_report(report_path: Path, output: dict, workspace_root: Path) -> None:
    resolved_workspace = workspace_root.resolve()
    if _is_under(report_path.resolve(), resolved_workspace):
        raise ValueError("report-file must not be inside the workspace root")
    report_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def _is_under(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
    except ValueError:
        return False
    return True


def main(argv: list | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    if args.command == "bootstrap":
        return _run_bootstrap(args)
    return EXIT_USAGE


def _run_bootstrap(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root)
    auth_path = Path(args.authorization_file)
    report_path = Path(args.report_file) if args.report_file else None

    output: dict[str, Any] = {
        "mode": "apply" if args.apply else "preflight",
        "operation": guard.OPERATION_SCOPE_BOOTSTRAP,
        "authorization_id": None,
        "authorization_digest": None,
        "transaction_id": None,
        "workspace_root_verified": False,
        "baseline_file_count": None,
        "baseline_digest_status": "UNKNOWN",
        "planned_generation_id": None,
        "preflight_status": "NOT_RUN",
        "apply_requested": bool(args.apply),
        "mutation_started": False,
        "result": "FAILURE",
        "error_code": None,
        "filesystem_writes": 0,
    }

    def finish(code: int) -> int:
        _print_output(output, args.json)
        if report_path is not None:
            try:
                _write_report(report_path, output, workspace_root)
            except ValueError:
                # Reporting the rejection itself must not be skipped, but a
                # report path inside the workspace is never written to.
                pass
        return code

    if report_path is not None and _is_under(report_path.resolve(), workspace_root.resolve()):
        output["error_code"] = "USAGE_ERROR"
        return finish(EXIT_USAGE)

    repo_root = guard.find_repository_root(Path(__file__))
    try:
        authorization = guard.load_authorization_file(
            auth_path, repo_root=repo_root, workspace_root=workspace_root
        )
    except guard.GuardRejection as rej:
        output["error_code"] = rej.error_code
        return finish(_exit_code_for(rej.error_code, rej.message))

    output["authorization_id"] = authorization.authorization_id
    output["authorization_digest"] = authorization.digest()
    output["transaction_id"] = authorization.transaction_id
    output["planned_generation_id"] = authorization.target_generation_id

    try:
        real_root = guard.validate_phase_a(
            authorization,
            expected_operation_scope=guard.OPERATION_SCOPE_BOOTSTRAP,
            workspace_root_arg=str(workspace_root),
            transaction_id_arg=authorization.transaction_id,
        )
    except guard.GuardRejection as rej:
        output["error_code"] = rej.error_code
        return finish(_exit_code_for(rej.error_code, rej.message))

    output["workspace_root_verified"] = True
    output["baseline_file_count"] = authorization.expected_stable_file_count

    try:
        guard.validate_phase_b_bootstrap(real_root, authorization)
    except guard.GuardRejection as rej:
        output["baseline_digest_status"] = "MISMATCH"
        output["error_code"] = rej.error_code
        return finish(_exit_code_for(rej.error_code, rej.message))

    output["baseline_digest_status"] = "MATCH"
    output["preflight_status"] = "PASS"

    if not args.apply:
        output["result"] = "PREFLIGHT_PASS"
        output["error_code"] = None
        return finish(EXIT_OK)

    if not authorization.apply:
        output["error_code"] = guard.AUTHORIZATION_APPLY_REQUIRED
        return finish(_exit_code_for(guard.AUTHORIZATION_APPLY_REQUIRED, ""))

    digest_schema_version = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))
    output["mutation_started"] = True
    result = bootstrap_generation_workspace(
        real_root, authorization.transaction_id, digest_schema_version, authorization
    )
    output["filesystem_writes"] = result.filesystem_writes
    if result.ok:
        output["result"] = "APPLY_COMPLETE"
        output["error_code"] = None
        return finish(EXIT_OK)

    output["result"] = "FAILURE"
    output["error_code"] = result.error_code
    return finish(_exit_code_for(result.error_code, result.reason))


if __name__ == "__main__":
    sys.exit(main())
