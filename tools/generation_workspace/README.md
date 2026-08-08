# Generation Workspace

## Purpose

An atomic, multi-file commit mechanism for the WP-CLAIM-EXIT workspace
integrity effort. It lets a set of files be replaced as a single unit — all
of it, or none of it — even if the process crashes mid-write.

## Scope Boundary

Generation Workspace is **not** a Notion / MTG Notion Manager product
feature. It is an independent, self-contained Python package that happens
to live inside this repository at `tools/generation_workspace/`. It has no
import dependency on `mtg_notion_manager`.

Running this tool against a real (production) workspace requires an
explicit, separate User Authorization — distinct from, and not granted by,
this package's presence in the repository. See "Production Authorization
Boundary" below.

## Architecture Summary

Design: **Generation Directory + Single Atomic Active Generation Switch.**

Every write goes into a brand-new `generations/gen-<10-digit-id>/`
directory. The workspace only ever recognizes one directory as authoritative
at a time — the one named by the Active Generation Pointer file
(`active_generation`). Switching which generation is authoritative is a
single atomic filesystem rename of that pointer file; nothing else changes.

## Generation Directory Model

```text
<workspace_root>/
  active_generation                  # Canonical JSON pointer (see below)
  generations/
    gen-0000000001/                  # Immutable once published
    gen-0000000002/
    ...
```

Generation content, once published (renamed into `generations/`), is never
modified again. A new generation is always built in a separate staging
directory first and only renamed into place after it is fully written and
verified.

## Active Generation Pointer

`active_generation` is a small canonical JSON file naming the current
authoritative generation (`generation_id`), the digest schema version, and
the expected content digest for that generation. It is never a symlink.
Resolution is fail-closed: if the pointer is missing, empty, malformed,
carries an unsupported schema version, names an invalid generation id, or
points at a generation directory that does not exist, resolution refuses to
guess — see "Case F Behavior" below.

## Generation Digest

`compute_generation_digest(...)` is a pure function of a generation
directory's own content and its `generation_id` — it does not depend on
`transaction_id` or on any in-flight transaction file. This means a
generation's digest can always be independently recomputed from what is on
disk, even after all transaction bookkeeping has been cleaned up.

## Installation

```bash
python -m pip install -e tools/generation_workspace
```

This is a standalone, PEP 517 package (`wp-claim-exit-generation-workspace`,
importable as `generation_workspace`). It has no third-party runtime
dependencies — the implementation is standard library only.

## Official Console CLI

```bash
generation-workspace --help
```

## python -m CLI

```bash
python -m generation_workspace --help
```

These are the **only** sanctioned ways to run the CLI. Do not rely on
`PYTHONPATH=tools/generation_workspace ...`, `cd`-ing into the package
directory, ad-hoc wrapper scripts, or shell aliases — none of those are a
supported execution contract.

## Default: Preflight

Running the `bootstrap` subcommand without `--apply` performs a read-only
Preflight: it validates the authorization and the workspace baseline, and
creates **zero** filesystem artifacts.

## Explicit `--apply`

```bash
generation-workspace bootstrap \
  --workspace-root <absolute-path> \
  --authorization-file <external-json-path> \
  --apply
```

Mutation only happens when **both** `--apply` is passed **and** the
supplied authorization file itself declares `"apply": true`. The CLI never
writes to the filesystem directly — `--apply` still goes through the same
function-level Production Mutation Guard (Phase A validation → exclusive
lock → Phase B re-validation) as any other caller of
`bootstrap_generation_workspace(...)`.

## External Authorization

Authorization is supplied as an external JSON file, never embedded in code
or committed to the repository. It binds an operation to a specific
transaction id, workspace root, source/target generation ids, and an
expected file inventory (name + SHA-256 per file). Any mismatch between the
authorization and what Preflight/Phase A actually observes is rejected —
the workspace is never coerced to match an authorization that no longer
describes it.

## Production Mutation Guard

Every mutating entry point (`bootstrap_generation_workspace`,
`begin_generation_transaction`, `commit_generation_transaction`) rejects
outright, with zero filesystem writes, if no `mutation_authorization` is
supplied. There is no environment-variable bypass. The sequence is always:
Phase A (read-only validation) → exclusive lock acquisition
(`os.O_EXCL`-based) → Phase B (re-validation under the lock).

## Recovery Behavior

`recover_generation_transaction(workspace_root)` inspects only the pointer,
the generation directories, and (if present) the in-flight control
transaction record, and returns exactly one deterministic Case (A–F). It
never automatically selects a generation, never automatically rolls back,
and never guesses by directory scanning. Any Case ending in
`FAILED_REQUIRES_RECOVERY` requires manual disposition.

## Case F Behavior

Case F (`CASE_F_POINTER_INVALID`) covers every way pointer resolution
itself can fail: the pointer file is missing, empty, or malformed; it
declares an unsupported pointer or digest schema version; it names an
invalid generation id; or it names a generation directory that does not
exist. In all of these, the result is `FAILED_REQUIRES_RECOVERY` with zero
automatic generation selection, zero automatic rollback, and zero
filesystem mutation — identical in strictness to Cases D and E, just
independently identifiable via `RecoveryResult.case == "F"` /
`RecoveryResult.classification == "CASE_F_POINTER_INVALID"`.

## Test Commands

```bash
python -m pytest tools/generation_workspace/tests -v
python -m pytest tools/generation_workspace/tests --collect-only -q
ruff check tools/generation_workspace
mypy tools/generation_workspace/generation_workspace --ignore-missing-imports
```

## CI Coverage

A dedicated `generation-workspace` job in `.github/workflows/ci.yml` runs
the editable install, the test suite (including `--collect-only`), `ruff`,
`mypy`, and both CLI entry points (`generation-workspace --help` and
`python -m generation_workspace --help`). It never touches a production
workspace, never runs `--apply`, and writes only inside pytest's own
temporary directories.

## Flat Baseline Governance

The Flat Baseline is the set of files that sat directly under
`workspace_root` before Bootstrap ran — the same files
`bootstrap_generation_workspace` copies into `generations/gen-0000000001/`.
Once a Generation exists, Governance Decision `UD-OGR-01` fixes the Flat
Baseline's role as **`FROZEN_EVIDENCE`**, and only that role:

- **Operational Read: NO** — once a Generation exists, the Flat Baseline is
  not an authorized operational read source.
- **Fallback: NO** — the Flat Baseline is not an authorized fallback when
  Generation read, verification, or resolution fails.
- **Recovery Source: NO** — the Flat Baseline must not be selected, restored
  from, or otherwise treated as a recovery source.
- **Canonical Read Source: NO** — the Flat Baseline is not an authorized
  canonical source for operational reads. Establishing the canonical
  operational reader is a separate concern and is not defined by this
  governance role.

The Flat Baseline's only sanctioned purpose is as evidence: a historical
comparison point and audit record of what Bootstrap materialized into
`gen-0000000001`. It exists so that a human, or a future audit, can
independently check the Bootstrap Generation against its origin. It is not
authorized for operational reads, fallback, recovery, or canonical reads,
and no such role is established by this governance decision.

## Production Authorization Boundary

Being importable, installable, or covered by CI in this repository does
**not** by itself authorize running this tool against any real workspace.
Production execution — Preflight or Apply — requires a separate, explicit
User Authorization on top of Repository Integration. Nothing in this
package, its tests, or its CI job references a production workspace path,
a production candidate/manifest/state digest, or any authorization content.
